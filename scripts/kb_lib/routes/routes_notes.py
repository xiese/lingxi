"""知识库 API — 笔记路由（CRUD / 标签 / 历史 / 批量）"""

import re, os, subprocess
import logging
from datetime import datetime
import shutil
from flask import request, jsonify, g

from ..routes import (
    bp, json_body_dict,
    bp, is_excluded_path, trigger_index_refresh, _update_indexes, _build_frontmatter,
    _resolve_note, _dedup_path, _merge_tags, _source_identity, _token_defaults, _safe_notes_subdir,
    _find_existing_doc, _yaml_list, _quote_yaml,
)
from ..routes.routes_general import _invalidate_graph_cache
from ..config import (
    NOTES_DIR, TYPE_MAP,
    SCAN_DIRS, get_setting, KB_ROOT,
)
from ..audit import audit_log
from ..services.text_clean import build_dedup_query
from ..services.links import parse_wikilink_target, resolve_wikilink
from ..services.etag import content_etag, check_precondition
from ..services import backlinks
from pathlib import Path
from ..search import search_index
from ..auth import (
    check_auth,
    safe, safe_note, sanitize, sanitize_path_segment, sanitize_title, parse_fm, finfo,
)
from ..services import write_policy
from ..services import tags as tags_svc


# ── 共享辅助 ──

log = logging.getLogger("kb.routes_notes")

def _archive_file(fp):
    """将文件移入 50-档案，自动处理同名冲突"""
    ar = NOTES_DIR / "50-档案"
    ar.mkdir(parents=True, exist_ok=True)
    dest = _dedup_path(ar, fp.stem, fp.suffix)
    shutil.move(str(fp), str(dest))
    return dest

# ── 标签云 ──

@bp.route("/api/tags")
def api_tags():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    tag_counts = _get_tag_counts()
    tags = [{"name": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])]
    return jsonify({"tags": tags, "total": len(tags)})


@bp.route("/api/tags/governance")
def api_tags_governance():
    """标签治理报告：大小写重复、同义候选、长尾统计（只读）。"""
    tk, err, code = check_auth("r")
    if err:
        return err, code
    from ..services.tags import build_governance_report
    tag_counts = _get_tag_counts()
    report = build_governance_report(tag_counts)
    return jsonify(report)



# ── 版本历史 ──

@bp.route("/api/notes/<path:np>/history")
def note_history(np):
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    rel = str(fp.relative_to(NOTES_DIR))
    git_rel = f"笔记/{rel}"  # git 路径需相对于仓库根目录，NOTES_DIR 是 笔记/
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%H|%ai|%s|%an", "--", git_rel],
            capture_output=True, text=True, cwd=str(KB_ROOT), timeout=10,
        )
        # git 失败时 stdout 为空，旧实现会返回 200 + commits=[]，
        # 把「不是 git 仓库 / git 不可用」伪装成「这个文件没有历史」。
        if result.returncode != 0:
            log.warning("git log 失败 rc=%s: %s", result.returncode, result.stderr.strip()[:200])
            return jsonify({
                "error": "Git 历史不可用",
                "detail": result.stderr.strip()[:200] or "git 返回非零状态",
            }), 502
        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append({"hash": parts[0], "time": parts[1], "message": parts[2], "author": parts[3]})
        return jsonify({"path": rel, "commits": commits, "count": len(commits)})
    except Exception as e:
        log.warning("routes_notes.py 异常: note_history", exc_info=True)
        return jsonify({"error": f"Git 历史获取失败: {e}"}), 500


@bp.route("/api/notes/<path:np>/diff")
def note_diff(np):
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    h1 = request.args.get("h1", "HEAD~1")
    h2 = request.args.get("h2", "HEAD")
    # 验证 git hash 参数格式，防止参数注入（只允许 hex 字符、HEAD、~、^）
    if not re.match(r'^[0-9a-fA-FHEAD~^./]+$', h1) or not re.match(r'^[0-9a-fA-FHEAD~^./]+$', h2):
        return jsonify({"error": "非法的 git hash 参数"}), 400
    rel = str(fp.relative_to(NOTES_DIR))
    git_rel = f"笔记/{rel}"  # git 路径需相对于仓库根目录
    try:
        result = subprocess.run(
            ["git", "diff", f"{h1}:{git_rel}", f"{h2}:{git_rel}"],
            capture_output=True, text=True, cwd=str(KB_ROOT), timeout=10,
        )
        if result.returncode != 0:
            # 区分「无差异」与「git 出错」：两者 stdout 都为空。
            err = result.stderr.strip()[:200]
            if "unknown revision" in err or "unknown commit" in err:
                return jsonify({"error": "指定的版本不存在", "detail": err}), 404
            log.warning("git diff 失败 rc=%s: %s", result.returncode, err)
            return jsonify({"error": "Diff 不可用", "detail": err or "git 返回非零状态"}), 502
        return jsonify({"path": rel, "from": h1, "to": h2, "diff": result.stdout[:50000]})
    except Exception as e:
        log.warning("routes_notes.py 异常: note_diff", exc_info=True)
        return jsonify({"error": f"Diff 失败: {e}"}), 500


# ── 批量操作 ──

@bp.route("/api/notes/batch", methods=["POST"])
def batch_notes():
    tk, err, code = check_auth("w")
    if err:
        return err, code
    d = json_body_dict()
    action = d.get("action", "")
    paths = d.get("paths", [])
    if not paths:
        return jsonify({"error": "需要paths"}), 400
    results = []
    needs_refresh = False
    for rel in paths:
        try:
            fp = safe_note(rel)
        except Exception:
            log.debug("routes_notes.py 异常: batch_notes", exc_info=True)
            results.append({"path": rel, "error": "路径非法"})
            continue
        if not fp.exists():
            results.append({"path": rel, "error": "不存在"})
            continue
        if action == "tag":
            tags = d.get("tags", [])
            if not tags:
                results.append({"path": rel, "error": "需要tags"})
                continue
            try:
                content = fp.read_text(encoding="utf-8")
                meta, body = parse_fm(content)
                old_tags = meta.get("tags", [])
                new_tags = _merge_tags(old_tags, tags)
                new_content, changed = tags_svc.rewrite_tags_block(content, new_tags)
                if changed:
                    fp.write_text(new_content, encoding="utf-8")
                    needs_refresh = True
                    results.append({"path": rel, "action": "tagged", "tags": new_tags})
                else:
                    # 没有可替换的 tags 行 ≠ 打标签成功。如实回报，别让用户以为写进去了。
                    results.append({
                        "path": rel, "action": "tag_skipped", "tags": new_tags,
                        "reason": "frontmatter 中没有可替换的 tags 字段",
                    })
            except Exception:
                # 安全加固：error 体不再回显 str(e)——OSError 的文本
                # 含完整绝对路径（实测把安装路径直接返回给调用方）。细节只进日志，
                # 响应统一给固定错误码。
                log.debug("routes_notes.py 异常: batch_notes", exc_info=True)
                results.append({"path": rel, "error": "io_error"})
        elif action == "archive":
            try:
                dest = _archive_file(fp)
                needs_refresh = True
                results.append({"path": rel, "action": "archived", "to": str(dest.relative_to(NOTES_DIR))})
            except Exception:
                log.debug("routes_notes.py 异常: batch_notes", exc_info=True)
                results.append({"path": rel, "error": "io_error"})
        elif action == "delete":
            try:
                mode = d.get("mode", "archive")
                if mode == "permanent":
                    fp.unlink()
                    needs_refresh = True
                    results.append({"path": rel, "action": "permanently_deleted"})
                else:
                    dest = _archive_file(fp)
                    needs_refresh = True
                    results.append({"path": rel, "action": "archived", "to": str(dest.relative_to(NOTES_DIR))})
            except Exception:
                log.debug("routes_notes.py 异常: batch_notes", exc_info=True)
                results.append({"path": rel, "error": "io_error"})
        else:
            results.append({"path": rel, "error": f"未知操作: {action}"})
    if needs_refresh:
        # 方案 B：batch 统一走 on_note_changed（delete/archive force）
        action_map = {
            "tag": "batch_tagged",
            "archive": "batch_archived",
            "delete": "batch_deleted",
        }
        write_policy.on_note_changed(
            action=action_map.get(action, f"batch_{action}"),
            force_search_refresh=action in ("delete", "archive"),
        )
    audit_log(f"batch_{action}", extra=f"count={len(paths)} {action}")
    return jsonify({"action": action, "total": len(paths), "results": results})


# ── 笔记列表缓存（避免每次请求全量扫描目录树）──
import threading as _threading, time as _time_mod
_notes_cache = {"data": [], "ts": 0, "base_mtime": 0}
_NOTES_CACHE_TTL = 30  # 30 秒缓存
_notes_cache_lock = _threading.Lock()
# 目录 mtime 扫描缓存（TTL 5 秒，避免每次 list_notes 都全盘 stat）
_dir_mtime_cache = {"mt": 0.0, "ts": 0.0}
# _scan_notes 的共享扫描缓存（文件列表 + 最新 mtime）
_scan_cache = {"files": None, "mt": 0.0, "ts": 0.0}
_DIR_MTIME_TTL = 5


# 标签计数缓存（TTL 30 秒，避免 /api/tags 每次全盘解析 frontmatter）
_tags_cache = {"data": None, "ts": 0.0}
_TAGS_CACHE_TTL = 30


def _get_tag_counts():
    """统计所有标签出现次数（带 30 秒缓存）。"""
    now = _time_mod.time()
    if _tags_cache["data"] is not None and now - _tags_cache["ts"] < _TAGS_CACHE_TTL:
        return _tags_cache["data"]
    tag_counts = {}
    for sd in SCAN_DIRS:
        base = NOTES_DIR / sd
        if not base.exists():
            continue
        for fp in base.rglob("*.md"):
            try:
                meta, _ = parse_fm(fp)
                for t in meta.get("tags", []):
                    if isinstance(t, str) and t.strip():
                        tag_counts[t.strip()] = tag_counts.get(t.strip(), 0) + 1
            except Exception:
                log.warning("routes_notes.py 标签解析异常", exc_info=True)
    _tags_cache["data"] = tag_counts
    _tags_cache["ts"] = now
    return tag_counts


def _scan_notes():
    """一次遍历同时得到「笔记文件列表」和「最新 mtime」，结果短时缓存。

    原来 _get_notes_dir_mtime() 与 _get_all_notes() 各自 rglob 一遍，
    每次刷新列表要把整个笔记库遍历两次（都是 stat 系统调用）。
    合并成一次遍历，两者共享。
    """
    now = _time_mod.time()
    if _scan_cache["files"] is not None and now - _scan_cache["ts"] < _DIR_MTIME_TTL:
        return _scan_cache["files"], _scan_cache["mt"]
    files, max_mt = [], 0
    for f in NOTES_DIR.rglob("*.md"):
        rel = str(f.relative_to(NOTES_DIR))
        if is_excluded_path(rel):
            continue
        files.append(f)
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt > max_mt:
            max_mt = mt
    files.sort()
    _scan_cache["files"], _scan_cache["mt"], _scan_cache["ts"] = files, max_mt, now
    return files, max_mt


def _get_notes_dir_mtime():
    """获取笔记文件的最高修改时间（逐文件扫描，精确判断是否有变化）。

    仅检查目录 mtime 不可靠（子目录文件修改不更新父目录 mtime），
    改为递归扫描 *.md 文件。
    """
    _now = _time_mod.time()
    if _now - _dir_mtime_cache["ts"] < _DIR_MTIME_TTL:
        return _dir_mtime_cache["mt"]
    try:
        _files, max_mt = _scan_notes()
        _dir_mtime_cache["mt"] = max_mt
        _dir_mtime_cache["ts"] = _now
        return max_mt
    except Exception:
        log.warning("routes_notes.py 异常: _get_notes_dir_mtime", exc_info=True)
        return 0


def _get_all_notes():
    """获取全部笔记列表（带缓存），避免每次 list_notes 都全量扫描"""
    now = _time_mod.time()
    current_mtime = _get_notes_dir_mtime()
    with _notes_cache_lock:
        if _notes_cache["data"] and now - _notes_cache["ts"] < _NOTES_CACHE_TTL:
            # mtime 不变或查询失败（=0）则返回缓存，否则强制重扫
            if current_mtime == _notes_cache["base_mtime"] or current_mtime == 0:
                return _notes_cache["data"]
            # mtime 变化，标记缓存失效
            _notes_cache["data"] = []
    # 重新扫描（复用 _scan_notes 的遍历结果，不再单独 rglob 一遍）
    results = []
    for f in _scan_notes()[0]:
        rel = str(f.relative_to(NOTES_DIR))
        fname = f.name
        if fname.startswith(".") or fname.startswith("{{") or fname == "README.md":
            continue
        info = finfo(f)
        results.append(info)
    with _notes_cache_lock:
        _notes_cache["data"] = results
        _notes_cache["ts"] = now
        _notes_cache["base_mtime"] = current_mtime or _get_notes_dir_mtime()
    return results


def _invalidate_notes_cache():
    """使笔记列表缓存失效（在写入操作后调用）"""
    with _notes_cache_lock:
        _notes_cache["data"] = []
        _notes_cache["ts"] = 0
    _dir_mtime_cache["ts"] = 0.0
    _tags_cache["ts"] = 0.0
    _scan_cache["ts"] = 0.0


def _apply_frontmatter_edit_metadata(content, editor, edit_source_type, now_str=None):
    now_str = now_str or datetime.now().strftime("%Y-%m-%d %H:%M")
    if not content.startswith("---"):
        return content
    meta, body = parse_fm(content)
    meta["updated"] = now_str
    meta["last_edited_by"] = editor
    meta["last_edit_source_type"] = edit_source_type

    ordered_keys = []
    for key in [
        "id", "title", "type", "schema_version", "content_layer", "tags", "canonical_tags", "aliases",
        "relations", "compiled_from", "created", "updated", "status", "priority",
        "last_edited_by", "last_edit_source_type", "source_client", "uploaded_by", "token_name",
        "platform", "project_scope", "source_type",
    ]:
        if key in meta:
            ordered_keys.append(key)
    for key in meta.keys():
        if key not in ordered_keys:
            ordered_keys.append(key)

    lines = ["---"]
    for key in ordered_keys:
        value = meta[key]
        if isinstance(value, list):
            lines.append(f"{key}: [{_yaml_list(value)}]")
        elif key in {"title", "content_layer", "last_edited_by", "last_edit_source_type", "source_client", "uploaded_by", "token_name", "platform", "project_scope", "source_type"}:
            lines.append(f'{key}: "{_quote_yaml(value)}"')
        else:
            v = str(value)
            # 对含 YAML 特殊字符的字符串值加引号，避免破坏 frontmatter 解析
            if isinstance(value, str) and any(c in v for c in ':{}[],&*!|>%`@#"\''):
                lines.append(f'{key}: "{_quote_yaml(v)}"')
            elif isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key}: {value}")
            else:
                lines.append(f"{key}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


# ── 笔记 CRUD ──

@bp.route("/notes")
def list_notes():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    dr = request.args.get("dir", "")
    tag = request.args.get("tag", "")
    tf = request.args.get("type", "")
    try:
        lim = min(int(request.args.get("limit", "50")), 200)
        offset = max(0, int(request.args.get("offset", "0")))
    except (ValueError, TypeError):
        log.warning("routes_notes.py 异常: list_notes", exc_info=True)
        return jsonify({"error": "limit/offset 参数必须为整数"}), 400

    # 使用缓存的全量笔记列表进行过滤
    all_notes = _get_all_notes()
    filtered = []
    for info in all_notes:
        rel = info["path"]
        # 目录过滤：按路径段比较。直接 startswith 会把 "10" 也匹配到
        # "100-其他/"，返回用户没要的目录。
        if dr:
            d = dr.rstrip("/")
            if not (rel == d or rel.startswith(d + "/")):
                continue
        # 标签过滤
        if tag and tag not in info.get("tags", []):
            continue
        # 类型过滤
        if tf and info.get("type") != tf:
            continue
        filtered.append(info)

    total = len(filtered)
    results = filtered[offset:offset + lim]
    return jsonify({"count": len(results), "total": total, "offset": offset, "items": results})


@bp.route("/api/notes/tree")
def api_notes_tree():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    # 侧边栏笔记树需要全量笔记；list_notes 的 limit 上限 200 只是传输裁剪，
    # 这里直接复用 _get_all_notes() 的缓存全量列表，不引入任何分页参数。
    items = []
    for info in _get_all_notes():
        items.append(info)
    return jsonify({"count": len(items), "total": len(items), "items": items})


@bp.route("/notes/<path:np>")
def read_note(np):
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    raw = fp.read_text(encoding="utf-8")
    meta, body = parse_fm(raw)
    return jsonify({
        "path": str(fp.relative_to(NOTES_DIR)),
        "meta": meta,
        "content": body.strip(),
        "raw": raw,
        # 写入乐观锁：客户端下次写入时回传（If-Match 头或 base_hash）
        "etag": content_etag(raw),
        "info": finfo(fp, pre_meta=meta),
    })


def _hygiene_path_guards(nt: str, sd: str, title: str, content: str,
                         tags=None, aliases=None, status=None):
    """写时卫生硬拦（跨平台 REST/MCP 共用）。返回 (error_dict|None, warnings).

    实现已迁入 services.write_policy；此包装保留兼容旧调用点。
    2026-10-31：新增 tags/aliases/status 透传（总览四件套检查 + status 默认提示）。
    """
    return write_policy.validate_write_hygiene(
        note_type=nt,
        subdir=sd,
        title=title,
        content=content,
        notes_dir=NOTES_DIR,
        type_map=TYPE_MAP,
        sanitize_fn=sanitize,
        tags=tags,
        aliases=aliases,
        status=status,
    )


@bp.route("/notes", methods=["POST"])
def create_note():
    tk, err, code = check_auth("w")
    if err:
        return err, code
    d = json_body_dict()
    title = d.get("title", "").strip()
    if not title:
        return jsonify({"error": "缺少title"}), 400
    nt = d.get("type", "knowledge")
    sd = d.get("subdir", "")
    content_body = d.get("content", "") or ""
    # quality_check 默认 True；显式 false/0/"false" 可写草稿
    qc_raw = d.get("quality_check", True)
    if isinstance(qc_raw, str):
        quality_check = qc_raw.strip().lower() not in ("0", "false", "no", "off")
    else:
        quality_check = bool(qc_raw)
    tpl = d.get("template")
    # 带模板时，客户端可能把模板原文（含自带 frontmatter、{{timestamp}} 占位）
    # 整篇送上来。落盘前剥掉开头那段 frontmatter 并替换时间占位，保证只保留
    # _build_frontmatter 生成的那一份，否则会写出两个 --- 块、YAML 直接坏掉。
    # 只在带 template 时处理：普通笔记正文以 --- 开头是合法写法，不能误伤。
    if tpl:
        if content_body.lstrip().startswith("---"):
            _fm_m = re.match(r"\s*---\s*\n.*?\n---\s*\n?", content_body, re.S)
            if _fm_m:
                content_body = content_body[_fm_m.end():]
        if "{{timestamp}}" in content_body:
            content_body = content_body.replace(
                "{{timestamp}}", datetime.now().strftime("%Y%m%d%H%M%S")
            )
        # {{title}} 也一并替换：前端会替换，但直接调 API 的客户端不会，
        # 不处理就会把字面量 {{title}} 落进笔记正文。
        if "{{title}}" in content_body:
            content_body = content_body.replace("{{title}}", sanitize_title(title))
    # 指定了模板但模板不存在时直接 400，不能静默回退默认五节——
    # 否则前端选了个模板、后端按别的章节校验，报错信息会驴唇不对马嘴。
    if tpl:
        from ..services.templates import get_template as _get_tpl
        if not _get_tpl(str(tpl)):
            return jsonify({"error": f"模板不存在：{tpl}"}), 400
    qerr = write_policy.validate_note_quality(
        content=content_body, note_type=nt, quality_check=quality_check,
        template=tpl or None,
    )
    if qerr:
        return jsonify(qerr), 400
    guard_err, hygiene_warnings = _hygiene_path_guards(
        nt, sd, title, content_body,
        tags=d.get("tags", []) or [],
        aliases=d.get("aliases", []) or [],
        status=d.get("status", "") or "",
    )
    if guard_err:
        return jsonify(guard_err), 400
    defaults = _token_defaults()
    # ── 写入规范化（2026-10-16）──
    # 落点与文件名统一由 note_normalizer 决定，规则来自迁移工具链导出的
    # 笔记/.kb/rules.yaml，保证存量迁移与增量写入同一套口径。
    # 任何异常都退回原逻辑，绝不阻断写入；KB_NORMALIZE_WRITES=0 可整体停用。
    _norm = None
    try:
        from ..services.note_normalizer import plan_server_write as _plan_norm
        _norm = _plan_norm(
            NOTES_DIR, title=title, content=content_body, note_type=nt,
            subdir=sd, tags=d.get("tags", []) or [],
            status=d.get("status", "") or "",
        )
    except Exception:
        log.warning("routes_notes.py 写入规范化异常，退回原逻辑", exc_info=True)
        _norm = None
    if _norm:
        # 规范化后的落点必须重过一遍写时卫生闸门；被拒则不接管。
        _nt2 = _norm["type"] or nt
        _sd2 = _norm["folder"]
        _tp2 = str(TYPE_MAP.get(_nt2, "") or "")
        if _tp2 and (_sd2 == _tp2 or _sd2.startswith(_tp2 + "/")):
            _sd2 = _sd2[len(_tp2):].lstrip("/")
        _gerr2, _gw2 = _hygiene_path_guards(
            _nt2, _sd2, title, content_body,
            tags=d.get("tags", []) or [],
            aliases=d.get("aliases", []) or [],
            status=d.get("status", "") or "",
        )
        if _gerr2:
            log.warning("规范化落点被写时闸门拒绝，退回原逻辑: %s", _gerr2)
            _norm = None
        else:
            hygiene_warnings = list(hygiene_warnings or []) + list(_gw2 or [])
    if _norm:
        nt = _norm["type"] or nt
        target = NOTES_DIR / _norm["folder"]
    else:
        target = NOTES_DIR / TYPE_MAP.get(nt, "20-知识")
    if not _norm:
        if sd:
            # 支持多级子目录（如 "知识库项目" 或 "子目录/子子目录"），逐段清理
            from pathlib import Path as PPath
            segments = [sanitize_path_segment(s) for s in PPath(sd.strip()).parts]
            # 如果 subdir 以当前 type 目录前缀开头（如 "10-项目/知识库项目"），去掉首段避免重复
            type_prefix = str(TYPE_MAP.get(nt, ""))
            if segments and segments[0] == type_prefix:
                segments = segments[1:]
            target = target.joinpath(*segments)
        elif defaults.get("default_dir"):
            default_target = _safe_notes_subdir(defaults["default_dir"])
            if default_target:
                target = default_target
    # archive 根保护：最终目录不能是 50-档案 本身
    try:
        if target.resolve() == (NOTES_DIR / "50-档案").resolve():
            return jsonify({
                "error": "archive_root_forbidden",
                "detail": "禁止直接写到 50-档案 根目录",
                "hint": "subdir 请带 文档收敛_YYYY-MM-DD/... 子路径",
            }), 400
    except Exception:
        log.warning("routes_notes.py 异常", exc_info=True)
    # 落盘前最后一道围栏（2026-09-18）：目录 + 文件名都必须落在笔记库内
    guard_err2, target = write_policy.ensure_inside(target, NOTES_DIR, "目标目录")
    if guard_err2:
        return jsonify(guard_err2), 400
    # ── 用户私有目录闸门（2026-09-24）──
    from ..config import USER_OWNED_DIRS, USER_OWNED_ACK
    try:
        _rt = target.resolve()
    except Exception:
        _rt = target
    _uo = [d for d in USER_OWNED_DIRS
           if _rt == (NOTES_DIR / d).resolve() or (NOTES_DIR / d).resolve() in _rt.parents]
    if _uo and d.get(USER_OWNED_ACK, "") != "user":
        return jsonify({
            "error": "user_owned_dir_forbidden",
            "detail": "目录「%s」为受保护的用户专属空间，AI 不得自行写入" % _uo[0],
            "hint": "经验教训与修复记录请写 10-项目/<项目名>/；确需写入请带 %s=user" % USER_OWNED_ACK,
        }), 403
    target.mkdir(parents=True, exist_ok=True)
    if nt == "project" and (target / "README.md").exists() and title == "README":
        fp = target / "README.md"
    elif _norm:
        fp = NOTES_DIR / _norm["rel"]
    else:
        fp = target / f"{sanitize_title(title).replace(' ', '_')}.md"
    guard_err3, fp = write_policy.ensure_inside(fp, NOTES_DIR, "目标文件")
    if guard_err3:
        return jsonify(guard_err3), 400
    action = "updated" if fp.exists() else "created"
    merged_tags = _merge_tags(defaults.get("default_tags", []), d.get("tags", []))
    canon = d.get("canonical_tags", []) or list(merged_tags)
    fm = _build_frontmatter(
        title=title, note_type=nt,
        tags=merged_tags,
        relations=d.get("relations", []),
        status=d.get("status", "draft"),
        priority=d.get("priority"),
        source=_source_identity(),
        content_layer=d.get("content_layer"),
        canonical_tags=canon,
        aliases=d.get("aliases", []),
        compiled_from=d.get("compiled_from", []),
        kind=(_norm or {}).get("kind") or None,
    )
    # 2026-09-26：正文自带一级标题时不再重复插入。
    # 旧写法无条件插 `# {title}`，客户端自己写了标题就会变成两个标题，
    # 反复保存越堆越多（全库曾累积到 833 篇双标题）。
    # 2026-10-05：插入的标题用 sanitize_title(title) 而非原始 title ——
    # 原标题里的 \r \n 会随 `# {title}` 进入正文，在文件里留下控制符。
    # 2026-10-18：原来这里重新读一次 d["content"]，把上面针对模板做的
    # frontmatter 剥离和占位符替换全绕过去了 —— 校验用的是干净正文、
    # 落盘的却是原始模板全文，结果文件里出现两份 frontmatter。
    # 统一用清洗后的 content_body。
    _body = content_body
    _first = next((ln for ln in _body.split("\n") if ln.strip()), "")
    if not _first.startswith("# "):
        _body = f"# {sanitize_title(title)}\n\n" + _body
    full_content = fm + "\n\n" + _body
    # ── 内容查重（2026-10-11 新增）──
    # 本入口原先完全没有内容查重：同内容换个标题就写成两份。
    # 只提示、不拦截 —— 实测 LLM 判定误杀率 40%，硬拦会毁掉正常写入。
    # action=updated（同名覆盖）时自比必然命中，跳过以免噪音。
    _dedup = {}
    if action == "created" and get_setting("dedup_on_create", True):
        try:
            _dd_path, _dd_matches, _dd_reason = _find_existing_doc(
                build_dedup_query(title, _body), title, _body)
        except Exception:
            log.warning("routes_notes.py 查重异常", exc_info=True)
            _dd_path, _dd_matches, _dd_reason = None, [], "error"
        if _dd_path:
            _dedup["duplicate_of"] = _dd_path
        if _dd_matches and isinstance(_dd_matches[0], dict) and "score" in _dd_matches[0]:
            _dedup["duplicate_score"] = round(float(_dd_matches[0]["score"]), 4)
        if _dd_matches:
            # 回传候选：调用方据此判断「查重是否真跑过、看到了什么」。
            _dedup["dedup_candidates"] = [
                {"path": m.get("path"),
                 "score": round(float(m.get("display_score") or m.get("score") or 0), 4)}
                for m in _dd_matches[:3] if isinstance(m, dict) and m.get("path")]
        if _dd_reason:
            _dedup["dedup_verdict"] = _dd_reason
            if not _dd_path and _dd_reason != "llm_judged_new":
                _dedup["dedup_degraded"] = _dd_reason
    fp.write_text(full_content, encoding="utf-8")
    meta, _ = parse_fm(full_content)
    # 第 4 条（2026-10-31）：rel 供「总览待刷新」队列用；notes_dir 必须一起传，
    # 否则 mark_overview_pending 拿不到笔记根会安静跳过。
    # ⚠️ rel 的异常**绝不能**吃掉 on_note_changed —— 原来写成
    #    `rel = str(fp.relative_to(...))` 再调用，relative_to 抛 ValueError 时
    #    缓存失效与索引刷新会被整体跳过（2026-10-31 审查轮3 指出的回归）。
    rel = ""
    try:
        rel = str(fp.relative_to(NOTES_DIR))
    except ValueError:
        log.warning("create_note 落盘路径不在 NOTES_DIR 内，跳过队列标记: %s", fp)
    write_policy.on_note_changed(action=action, force_search_refresh=False,
                                 rel_path=rel or None, notes_dir=NOTES_DIR)
    # 反链增量维护：只重算这一篇的出链，不做全库扫描。
    try:
        from ..services import backlinks as _bl
        _bl.update_note(search_index, rel, full_content)
    except Exception:
        log.debug("routes_notes.py 异常: 反链增量更新", exc_info=True)
    audit_log(f"note_{action}", rel, request.method, 201 if action == "created" else 200,
              extra=f"type={nt} title={title[:30]}")
    payload = {"action": action, "path": rel, "info": finfo(fp, pre_meta=meta)}
    # ── 项目主文档自动生成/更新（T12）──
    # 新建项目时按 五段式落一份主文档；已有主文档只刷 updated 与
    # 「最近笔记」。父目录已有同名主文档的存量项目不回补。钩子内部已兜底，
    # 这里再包一层：任何异常只 warning，绝不让 POST /notes 失败。
    # KB_PROJECT_DOC=0 可整体停用。
    try:
        from ..services import project_doc as _pdoc
        _pdoc_rel = _pdoc.on_project_note_written(NOTES_DIR, fp, rel, nt)
        if _pdoc_rel:
            payload["project_doc"] = _pdoc_rel
    except Exception:
        log.warning("routes_notes.py 项目主文档钩子异常，已忽略", exc_info=True)
    if hygiene_warnings:
        payload["hygiene_warnings"] = hygiene_warnings
    if action == "updated":
        # 2026-10-11：同名写入会**直接覆盖**已有笔记且不留备份 —— 这正是
        # 最初被反馈的「静默覆盖」入口。原先响应只有 action=updated，
        # 调用方无法分辨「覆盖了哪一篇、原来有没有内容」。这里显式回传。
        _dedup["overwrote"] = rel
        _dedup["overwrote_hint"] = "同名笔记已存在，本次为覆盖写入（不保留旧版）"
    if _dedup:
        payload.update(_dedup)
    return jsonify(payload), 201 if action == "created" else 200


@bp.route("/notes/<path:np>", methods=["POST"])
def save_note(np):
    tk, err, code = check_auth("w")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    d = json_body_dict()
    # ── 用户私有目录闸门（2026-09-24）──
    from ..config import USER_OWNED_DIRS, USER_OWNED_ACK
    try:
        _rf = fp.resolve()
    except Exception:
        _rf = fp
    _uo = [d for d in USER_OWNED_DIRS
           if _rf == (NOTES_DIR / d).resolve() or (NOTES_DIR / d).resolve() in _rf.parents]
    if _uo and d.get(USER_OWNED_ACK, "") != "user":
        return jsonify({
            "error": "user_owned_dir_forbidden",
            "detail": "目录「%s」为受保护的用户专属空间，AI 不得自行写入" % _uo[0],
            "hint": "经验教训与修复记录请写 10-项目/<项目名>/；确需写入请带 %s=user" % USER_OWNED_ACK,
        }), 403

    content = d.get("content", "")
    if not content:
        return jsonify({"error": "需要content"}), 400
    # ── 写入乐观锁（2026-10-17 批 3）──
    # GET 返回 etag；这里接受 If-Match 头或 body 的 base_hash。
    # 不带版本号暂时兼容（记 warning），便于观察未升级的客户端。
    try:
        _cur_raw = fp.read_text(encoding="utf-8")
    except FileNotFoundError:
        _cur_raw = ""
    _ifm = request.headers.get("If-Match")
    _bh = d.get("base_hash")
    _ok, _exp, _prov = check_precondition(_cur_raw, if_match=_ifm, base_hash=_bh)
    if not _ok:
        return jsonify({
            "error": "version_conflict",
            "detail": "笔记已被他人修改，请重新读取后再提交",
            "expected": _exp,
            "provided": _prov,
        }), 409
    if _prov is None:
        log.warning("save_note 未带版本号（If-Match/base_hash）：%s", np)
    # save 默认不跑 quality，避免编辑器被卡；显式 quality_check=true 才检查
    qc_raw = d.get("quality_check", False)
    if isinstance(qc_raw, str):
        quality_check = qc_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        quality_check = bool(qc_raw)
    if quality_check:
        # 质量检查正文：有 frontmatter 时尽量取 body
        body_for_q = content
        if content.startswith("---"):
            try:
                _, body_for_q = parse_fm(content)
            except Exception:
                log.warning("routes_notes.py 异常: save_note", exc_info=True)
                body_for_q = content
        qerr = write_policy.validate_note_quality(
            content=body_for_q or content,
            note_type=d.get("type") or "knowledge",
            quality_check=True,
        )
        if qerr:
            return jsonify(qerr), 400
    # ── 保留 frontmatter / H1（2026-10-11 新增，默认关闭）──
    # 背景：save_note 是「整体覆盖」语义 —— 编辑器传完整文件没问题，但 AI 只传
    # 正文时会把 frontmatter（id/title/type/tags/created/version…）和 H1 整个丢掉。
    # preserve_frontmatter=true 且本次没带 frontmatter 时：沿用磁盘上的旧 frontmatter
    # 与 H1，只把正文换掉。默认 False，编辑器行为一字不变。
    _pf_raw = d.get("preserve_frontmatter", False)
    if isinstance(_pf_raw, str):
        _preserve_fm = _pf_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        _preserve_fm = bool(_pf_raw)
    if _preserve_fm and not content.lstrip().startswith("---"):
        try:
            _old_full = fp.read_text(encoding="utf-8")
            _om = re.match(r"^(---\n.*?\n---\n)", _old_full, re.DOTALL)
            _fm_part = _om.group(1) if _om else ""
            _rest_old = _old_full[_om.end():] if _om else _old_full
            _h1m = re.match(r"\s*(#\s*[^\n]*)\n", _rest_old)
            _h1 = _h1m.group(1) if _h1m else ""
            _body_new = content
            if _h1 and not re.match(r"\s*#\s", _body_new):
                _body_new = _h1 + "\n\n" + _body_new
            content = (_fm_part + "\n" + _body_new) if _fm_part else _body_new
        except Exception as e:
            log.warning(f"save_note 保留 frontmatter 失败 [{np}]: {e}")
    # 自动更新 frontmatter updated / 最近编辑者字段
    if content.startswith("---"):
        info = getattr(g, "ti", {}) or {}
        editor = info.get("name") or info.get("identity_label") or info.get("device") or "unknown"
        edit_source_type = info.get("source") or ("master" if info.get("permission") == "master" else "token")
        content = _apply_frontmatter_edit_metadata(content, editor=editor, edit_source_type=edit_source_type)
    # ── 旧版归档（2026-10-11 新增，默认关闭）──
    # 背景：AI 走 MCP 只有 kb_create（路径由标题决定），"更新"时常因标题差异
    # 另存一份、旧文照旧留在检索里 → 库内同题两版、状态混乱。
    # 这里给 save_note 加可选的「改前先归档」：archive_old=true 时把改前内容
    # 原样落到 50-档案/_旧版本/<原相对路径>/（该目录在索引构建时被排除），
    # frontmatter 记 version / supersedes 供回溯。
    # 默认 False —— 编辑器自动保存频率高，默认开会把 50-档案 撑爆。
    _archive_info = {}
    _arc_raw = d.get("archive_old", False)
    if isinstance(_arc_raw, str):
        _archive_old = _arc_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        _archive_old = bool(_arc_raw)
    if _archive_old:
        try:
            old_text = fp.read_text(encoding="utf-8")
            _rel_old = fp.relative_to(NOTES_DIR)
            adir = NOTES_DIR / "50-档案" / "_旧版本" / _rel_old.parent
            adir.mkdir(parents=True, exist_ok=True)
            stem = f"{fp.stem}__{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            adest = _dedup_path(adir, stem, fp.suffix)
            shutil.copy2(str(fp), str(adest))
            _archive_info["archived_to"] = str(adest.relative_to(NOTES_DIR))
            # 版本号递增 + supersedes 指针（只动这两个键，其余原样保留）
            try:
                _old_meta, _ = parse_fm(old_text)
                _ver = int(str(_old_meta.get("version") or "1").strip() or "1") + 1
            except Exception:
                _ver = 2
            if content.startswith("---"):
                _m = re.match(r"^---\n(.*?)\n---\n?", content, re.DOTALL)
                if _m:
                    _lines = _m.group(1).split("\n")

                    def _set_kv(key, val):
                        for _i, _ln in enumerate(_lines):
                            if _ln.startswith(key + ":"):
                                _lines[_i] = f"{key}: {val}"
                                return
                        _lines.append(f"{key}: {val}")

                    _set_kv("version", _ver)
                    _set_kv("supersedes", f'"{_archive_info["archived_to"]}"')
                    content = "---\n" + "\n".join(_lines) + "\n---\n" + content[_m.end():]
                    _archive_info["version"] = _ver
        except Exception as e:
            # 归档失败不阻断写入（与智能上传不同：这里用户已明确要保存正文）
            log.warning(f"save_note 旧版归档失败 [{np}]: {e}")
    fp.write_text(content, encoding="utf-8")
    meta, _ = parse_fm(content)
    # 第 4 条（2026-10-31）：同上。rel 的异常不能吃掉写后刷新（见 create_note 处说明）
    rel = ""
    try:
        rel = str(fp.relative_to(NOTES_DIR))
    except ValueError:
        log.warning("save_note 落盘路径不在 NOTES_DIR 内，跳过队列标记: %s", fp)
    write_policy.on_note_changed(action="saved", force_search_refresh=False,
                                 rel_path=rel or None, notes_dir=NOTES_DIR)
    audit_log("note_updated", rel, request.method, 200)
    payload = {"action": "saved", "path": rel, "info": finfo(fp, pre_meta=meta)}
    if _archive_info:
        payload.update(_archive_info)
    return jsonify(payload), 200


@bp.route("/notes/<path:np>", methods=["DELETE"])
def delete_note(np):
    tk, err, code = check_auth("w")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    dest = _archive_file(fp)
    # 统一写后出口：删除强制绕过冷却
    # 第 4 条（2026-10-31）：删除也要标记该项目总览待刷新（总览里的索引项会失效）
    write_policy.on_note_changed(action="deleted", force_search_refresh=True,
                                 rel_path=np, notes_dir=NOTES_DIR)
    from_path = np  # np 来自 URL，即相对路径
    to_path = str(dest.relative_to(NOTES_DIR))
    audit_log("note_archived", to_path, request.method, 200, extra=f"from={from_path}")
    return jsonify({"action": "archived", "to": to_path})


# ── 移动 / 改名（批 3）──

def _move_one(src_rel, to_rel, update_links=False, dry_run=False):
    """移动单篇笔记。返回 (payload, status)。

    复用 auth.safe_note 做路径安全校验（参照系 = 笔记库，防 ../ 逃逸）。
    目标已存在 → 409（不覆盖）。dry_run 只回报将做什么，不落盘。
    """
    from ..auth import safe_note

    try:
        src = safe_note(src_rel)
    except ValueError as e:
        return {"error": "invalid_source", "detail": str(e)}, 400
    if not src.is_file():
        return {"error": "source_not_found", "path": src_rel}, 404
    if not str(to_rel).strip():
        return {"error": "missing_to"}, 400
    try:
        dst = safe_note(to_rel)
    except ValueError as e:
        return {"error": "invalid_target", "detail": str(e)}, 400
    if dst == src:
        return {"error": "same_path", "path": src_rel}, 400
    if dst.exists():
        return {"error": "target_exists", "path": to_rel}, 409
    if dst.is_dir():
        return {"error": "target_is_dir", "path": to_rel}, 409

    payload = {
        "action": "move",
        "from": src_rel,
        "to": to_rel,
        "dry_run": bool(dry_run),
        "update_links": bool(update_links),
        "links_updated": [],
    }
    if dry_run:
        # 预演：不落盘，但把「将改动哪些笔记的引用」一并回报，供前端确认后再真移。
        if update_links:
            try:
                payload["links_updated"] = _rewrite_links_to(
                    src_rel, to_rel, dry_run=True)
            except Exception:
                log.warning("move dry-run 预演链接改写失败: %s -> %s",
                            src_rel, to_rel, exc_info=True)
        return payload, 200

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    # 索引增量：源消失、目标出现（避免全库重建）
    try:
        _text = dst.read_text(encoding="utf-8")
    except Exception:
        _text = None
    backlinks.remove_note(src_rel)
    backlinks.update_note(search_index, to_rel, _text)

    if update_links:
        # 把全库指向旧路径的双链改成新路径（只改能解析到该文件的链接）
        #
        # 2026-10-17：链接改写是「多文件逐篇落盘」，中途抛错（磁盘满 / 权限 /
        # 编码异常）会留下**已搬走但链接没改完**的状态：文件已在新路径，
        # 指向旧路径的双链全部断掉，且没有任何回滚。这里加事务性收尾：
        # 改写失败 → 把已改的文件内容还原、文件搬回原位、索引复原，
        # 对外返回 500，让调用方看到失败而不是「移动成功但断链」。
        undo = []
        try:
            payload["links_updated"] = _rewrite_links_to(src_rel, to_rel, undo=undo)
        except Exception as e:
            log.warning("move 链接改写失败，回滚: %s -> %s (%s)", src_rel, to_rel, e,
                        exc_info=True)
            _failures = _rollback_move(src, dst, src_rel, to_rel, undo, _text)
            return {"error": "link_rewrite_failed", "detail": str(e),
                    "rolled_back": not _failures,
                    "rollback_failures": _failures}, 500

    audit_log("note_moved", to_rel, request.method, 200,
              extra=f"from={src_rel} links={len(payload['links_updated'])}")
    return payload, 200


def _rollback_move(src, dst, src_rel, to_rel, undo, moved_text=None):
    """撤销一次失败的 move：文件搬回 → 还原已改写的链接 → 复原索引。

    返回失败项列表（空 = 完整回滚）。任何一步失败都只记日志、不抛异常，
    避免在回滚里再抛一个异常盖掉原始错误。

    顺序是刻意的（GLM 审查 2026-10-17 指出原顺序的缺陷）：
    先把文件搬回，再改链接。因为「链接指向 to_rel」在文件还在 dst 时是**自洽**的；
    若搬回失败却先还原了链接，就会得到「链接指旧路径、文件在新路径」的双向断裂。
    同理，搬回失败时不再动索引，保持 dst 在索引里可见，避免文件成为索引外孤儿。
    """
    failures = []
    moved_back = False
    try:
        if dst.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
        moved_back = True
    except Exception as e:
        log.warning("回滚文件搬回失败: %s -> %s (%s)", dst, src, e, exc_info=True)
        failures.append("move_back_failed")

    if not moved_back:
        # 文件仍在新路径：链接与索引都保持指向 to_rel 的自洽状态，不动。
        return failures

    for rel, raw in reversed(undo or []):
        try:
            (NOTES_DIR / rel).write_text(raw, encoding="utf-8")
        except Exception as e:
            log.warning("回滚链接失败: %s (%s)", rel, e, exc_info=True)
            failures.append(f"restore_link_failed:{rel}")
            continue
        try:
            backlinks.update_note(search_index, rel, raw)
        except Exception as e:
            log.warning("回滚链接索引失败: %s (%s)", rel, e, exc_info=True)
            failures.append(f"restore_link_index_failed:{rel}")

    # 两个索引操作各自独立 try：remove 失败不能连带跳过 update。
    try:
        backlinks.remove_note(to_rel)
    except Exception as e:
        log.warning("回滚移除目标索引失败: %s (%s)", to_rel, e, exc_info=True)
        failures.append("remove_target_index_failed")
    try:
        backlinks.update_note(search_index, src_rel, moved_text)
    except Exception as e:
        log.warning("回滚索引失败: %s (%s)", src_rel, e, exc_info=True)
        failures.append("restore_source_index_failed")

    return failures


def _link_source_docs():
    """取「参与链接改写的文档集合」：(paths, contents)。

    优先用索引内存表（生产路径，零磁盘 IO）；但索引是**模块级单例**，
    可能残留上一批/上一次运行的路径（测试里换过 KB_ROOT，生产里换过库）。
    因此只采信「确实存在于当前 NOTES_DIR 下」的条目；一条都不剩就退回扫盘，
    避免因为索引没就绪或指向旧根而漏改链接。
    """
    try:
        raw_paths = [str(p) for p in (getattr(search_index, "paths", []) or [])]
        contents = list(getattr(search_index, "contents", []) or [])
    except Exception:
        raw_paths, contents = [], []

    paths, texts = [], []
    for i, p in enumerate(raw_paths):
        try:
            if (NOTES_DIR / p).is_file():
                paths.append(p)
                texts.append(contents[i] if i < len(contents) else "")
        except Exception:
            continue
    if paths:
        return paths, texts

    # 兜底：扫盘
    paths, texts = [], []
    try:
        for f in sorted(NOTES_DIR.rglob("*.md")):
            try:
                rel = str(f.relative_to(NOTES_DIR))
            except Exception:
                continue
            if is_excluded_path(rel):
                continue
            try:
                paths.append(rel)
                texts.append(f.read_text(encoding="utf-8"))
            except Exception:
                continue
    except Exception:
        pass
    return paths, texts


def _rewrite_links_to(old_rel, new_rel, undo=None, dry_run=False):
    """把全库指向 old_rel 的双链改写成 new_rel，返回被改的笔记列表。

    只处理能解析到 old_rel 的链接（复用 links.resolve_wikilink），
    避免把同名但指向别处的链接误改。改写保留 #锚点 与 |别名。

    `undo` 传入一个 list 时，每改一篇就把 (相对路径, 改写前原文) 追加进去，
    供调用方在后续步骤失败时还原（见 `_rollback_move`）。

    `dry_run=True` 只清点「会改哪些笔记」，不落盘、不入 undo，供移动前预演。
    """
    changed = []
    paths, contents = _link_source_docs()
    if not paths:
        return changed
    # 源文件此刻**已经不在磁盘上**（move 先落盘再改链接），所以 ids 里必须
    # 手动把 old_rel 补回来——否则指向它的双链解析不到，等于漏改。
    ids = list(paths)
    if old_rel not in ids:
        ids.append(old_rel)
    title_index, stem_index = {}, {}
    for p in ids:
        stem_index.setdefault(Path(p).stem, p)
    for i, p in enumerate(paths):
        txt = contents[i] if i < len(contents) else ""
        try:
            meta, _ = parse_fm(txt)
        except Exception:
            meta = {}
        t = (meta or {}).get("title") or Path(p).stem
        title_index.setdefault(str(t), p)
    title_index.setdefault(Path(old_rel).stem, old_rel)
    stem_index.setdefault(Path(old_rel).stem, old_rel)

    for i, p in enumerate(paths):
        if p == old_rel:
            continue
        raw = (NOTES_DIR / p).read_text(encoding="utf-8") if (NOTES_DIR / p).exists() else None
        if raw is None:
            continue
        new_raw, n = _sub_links(raw, p, old_rel, new_rel, ids, title_index, stem_index)
        if n:
            if dry_run:
                changed.append({"path": p, "count": n})
                continue
            if undo is not None:
                undo.append((p, raw))
            (NOTES_DIR / p).write_text(new_raw, encoding="utf-8")
            backlinks.update_note(search_index, p, new_raw)
            changed.append({"path": p, "count": n})
    return changed


def _sub_links(raw, source_rel, old_rel, new_rel, ids, title_index, stem_index):
    """替换 raw 中解析到 old_rel 的双链为 new_rel。返回 (新文本, 次数)。"""
    from ..services.links import WIKILINK_RE, parse_wikilink_target
    new_stem = Path(new_rel).stem
    n = 0

    def _rep(m):
        nonlocal n
        target, anchor, alias = parse_wikilink_target(m.group(1))
        if not target:
            return m.group(0)
        hit = resolve_wikilink(source_rel, target, ids, title_index, stem_index)
        if hit != old_rel:
            return m.group(0)
        n += 1
        # anchor / alias 是裸串（不含 # 与 |），拼回时必须补回前缀
        tail = ("#" + anchor if anchor else "") + ("|" + alias if alias else "")
        return "[[%s%s]]" % (new_stem, tail)

    return WIKILINK_RE.sub(_rep, raw), n


@bp.route("/api/notes/<path:np>/move", methods=["POST"])
def move_note(np):
    tk, err, code = check_auth("w")
    if err:
        return err, code
    d = json_body_dict()
    to_rel = d.get("to") or ""
    payload, status = _move_one(
        np, to_rel,
        update_links=bool(d.get("update_links", False)),
        dry_run=bool(d.get("dry_run", False)),
    )
    if status == 200 and not payload.get("dry_run"):
        # 第 4 条（2026-10-31）：移动后**源与目标**都可能落在项目目录里，两边都要标记
        write_policy.on_note_changed(action="moved", force_search_refresh=True,
                                     rel_path=np, notes_dir=NOTES_DIR)
        if to_rel:
            write_policy.mark_overview_pending(to_rel, action="moved", notes_dir=NOTES_DIR)
    return jsonify(payload), status


@bp.route("/api/notes/move-batch", methods=["POST"])
@bp.route("/api/notes/batch-move", methods=["POST"])
def move_notes_batch():
    """批量移动：body = {"items": [{"from":..., "to":...}], "update_links":bool, "dry_run":bool}"""
    tk, err, code = check_auth("w")
    if err:
        return err, code
    d = json_body_dict()
    items = d.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "需要 items 数组"}), 400
    update_links = bool(d.get("update_links", False))
    dry_run = bool(d.get("dry_run", False))
    results, ok = [], 0
    for it in items:
        if not isinstance(it, dict):
            results.append({"error": "invalid_item"})
            continue
        p, st = _move_one(it.get("from") or "", it.get("to") or "",
                          update_links=update_links, dry_run=dry_run)
        p["status"] = st
        if st == 200:
            ok += 1
        results.append(p)
    if not dry_run and ok:
        write_policy.on_note_changed(action="moved", force_search_refresh=True)
        # 第 4 条（2026-10-31）：批量移动 —— 逐条标记队列（源 + 目标）。
        # 这里直接调 mark_overview_pending，不再重复触发索引刷新。
        for _it in items:
            if not isinstance(_it, dict):
                continue
            for _p in (_it.get("from"), _it.get("to")):
                if _p:
                    write_policy.mark_overview_pending(
                        _p, action="moved", notes_dir=NOTES_DIR)
    return jsonify({"dry_run": dry_run, "total": len(items),
                    "succeeded": ok, "results": results}), 200


# ── 相关笔记 ──

@bp.route("/api/notes/<path:np>/related")
def related_notes(np):
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception:
        log.warning("routes_notes.py 异常: related_notes", exc_info=True)
        return jsonify({"error": "无法读取"}), 500
    _, body = parse_fm(text)
    body = body[:3000]
    results, degraded = search_index.search(body, top=5, with_status=True)
    if degraded:
        log.warning("相关笔记检索不可用: %s", degraded)
    related = []
    for r in results:
        if r["path"] == str(fp.relative_to(NOTES_DIR)):
            continue
        info = finfo(NOTES_DIR / r["path"]) if (NOTES_DIR / r["path"]).exists() else {"title": r["path"]}
        info["score"] = r["score"]
        info["display_score"] = r.get("display_score", r["score"])
        related.append(info)
    payload = {"path": str(fp.relative_to(NOTES_DIR)), "related": related}
    if degraded:
        payload["degraded"] = True
        payload["reason"] = degraded
    return jsonify(payload)


# ── 双链解析 / 反链 ──

@bp.route("/api/links/resolve")
def links_resolve():
    """把 [[双链]] 目标解析成笔记路径。

    查询参数：
      - `target`（必填）：双链目标原文，如 `文件名`、`x#标题`、`x|别名`
      - `from`（可选）：来源笔记路径，用于同目录优先解析
    """
    tk, err, code = check_auth("r")
    if err:
        return err, code
    raw = (request.args.get("target") or "").strip()
    if not raw:
        return jsonify({"error": "缺少 target 参数"}), 400
    src = (request.args.get("from") or "").strip()
    try:
        ids, title_index, stem_index = _link_index()
        resolved = resolve_wikilink(src, raw, ids, title_index, stem_index)
    except Exception:
        log.warning("routes_notes.py 异常: links_resolve", exc_info=True)
        return jsonify({"error": "解析失败"}), 500
    target, anchor, alias = parse_wikilink_target(raw)
    return jsonify({
        "target": target,
        "anchor": anchor,
        "alias": alias,
        "resolved": resolved,
        "exists": bool(resolved),
    })


@bp.route("/api/links/resolve-batch", methods=["POST"])
def links_resolve_batch():
    """批量解析双链目标，供阅读页一次性标出断链。

    请求体：`{"from": "来源笔记相对路径", "targets": ["目标1", "目标2"]}`
    返回：`{"resolved": {"<target>": "<相对路径>" | null}}`

    为什么要批量：一篇笔记里几十个双链，逐个调 `/api/links/resolve`
    会打几十次请求，每次都重建一遍链接索引（要扫全库），手机端直接卡住。
    解析不到的返回 null，前端据此渲染「目标不存在」提示。
    """
    tk, err, code = check_auth("r")
    if err:
        return err, code
    data = request.get_json(silent=True) or {}
    src = (data.get("from") or "").strip()
    targets = data.get("targets") or []
    if not isinstance(targets, list):
        return jsonify({"error": "targets 必须是数组"}), 400
    # 去重 + 限流：单篇笔记双链数量有限，500 足够；防止恶意大数组拖垮索引构建
    uniq = []
    for t in targets[:500]:
        t = str(t or "").strip()
        if t and t not in uniq:
            uniq.append(t)
    if not uniq:
        return jsonify({"resolved": {}, "count": 0})
    try:
        ids, title_index, stem_index = _link_index()
    except Exception:
        log.warning("routes_notes.py 异常: links_resolve_batch", exc_info=True)
        return jsonify({"error": "解析失败"}), 500
    out = {}
    for t in uniq:
        try:
            out[t] = resolve_wikilink(src, t, ids, title_index, stem_index)
        except Exception:
            out[t] = None
    return jsonify({"resolved": out, "count": len(out)})


@bp.route("/api/notes/<path:np>/backlinks")
def note_backlinks(np):
    """反链：哪些笔记链到本篇（随索引增量维护，不做全库扫描）。"""
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp, err, code = _resolve_note(np)
    if err:
        return err, code
    rel = str(fp.relative_to(NOTES_DIR))
    try:
        from ..services import backlinks as _bl
        srcs = _bl.backlinks_of(search_index, rel)
        outs = _bl.outlinks_of(search_index, rel)
    except Exception:
        log.warning("routes_notes.py 异常: note_backlinks", exc_info=True)
        return jsonify({"error": "反链计算失败"}), 500
    items = []
    for s in srcs:
        p = NOTES_DIR / s
        info = finfo(p) if p.exists() else {"title": s}
        info["path"] = s
        items.append(info)
    return jsonify({"path": rel, "backlinks": items,
                    "count": len(items), "outlinks": outs})


def _link_index():
    """从当前笔记集构建 (ids, title_index, stem_index)。

    优先用索引内存表；索引为空或指向旧根时退回扫盘（见 `_link_source_docs`），
    这样刚写入 / 刚移动、索引还没刷新的笔记也能立刻被解析到。
    """
    from ..services.links import build_link_index
    paths, _texts = _link_source_docs()
    notes = []
    for p in paths:
        title = Path(p).stem
        try:
            meta, _ = parse_fm((NOTES_DIR / p).read_text(encoding="utf-8"))
            title = (meta or {}).get("title") or title
        except Exception:
            pass
        notes.append((p, title))
    return build_link_index(notes)

# ─── 第 5 项：正文本地路径探测（不猜，只 stat）───

# 只认绝对路径。维护者 2026-10-18 定的范围：`./docs/x.md` 这种相对写法不识别，
# 理由是这样范围小、不会把正文里的普通句子误判成路径。
_ABS_PATH_RE = re.compile(
    r"(?<![\w/.:-])"          # 左边不能紧贴单词字符，避免 `http://a/b` 里的 `/b` 被单独抓出来
    r"(/[^\s`'\"()\[\]<>|，。；：、！？（）【】「」《》“”‘’…—]{2,})"
)

# 行号 / 章节后缀：`/a/b.md:12`、`/a/b.py#L9`、`/a/b.md 第 2~18 章`
# 这类尾巴不是路径的一部分，必须先剥掉再 stat。不剥的话本机存在的文件
# 会因 stat 整串失败而被误判成「其他服务器」——判断依据就错了。
_PATH_TAIL_RE = re.compile(
    r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?|\s*第\s*\d+\s*[~\-—至]?\s*\d*\s*[章节行条])$"
)


def _strip_path_tail(p):
    """剥掉路径尾部的行号/章节标注，返回 (干净路径, 被剥掉的尾巴)。"""
    s = str(p or "").strip()
    tail = ""
    for _ in range(3):          # 尾巴可能叠加（`:12 第 3 章`），最多剥三层
        m = _PATH_TAIL_RE.search(s)
        if not m:
            break
        tail = s[m.start():] + tail
        s = s[:m.start()].rstrip()
    return s, tail


def _is_inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def _has_hidden_part(p):
    """路径里有以 `.` 开头的段 → True（`/srv/notes/.ssh/id_rsa`、`/srv/notes/.kb_settings.json`）。

    为什么要有这道闸：本接口拿 r 权限 token 就能调，若什么路径都 stat，
    等于给了一个「服务器上任意绝对路径存不存在」的探针 —— 审查明确点出
    `/srv/notes/.ssh/id_rsa` 这个例子。密钥、配置、token 文件全是以 `.` 开头的
    隐藏文件，拦掉它们就能堵住最要命的那一类，而 `docs/`、`笔记/` 这些正常
    路径一个字不受影响。
    """
    parts = [x for x in str(p or "").split("/") if x]
    return any(x.startswith(".") for x in parts)


def _probe_one(raw, server_prefixes):
    """对单条路径做本机 stat，返回徽标所需的最小信息。

    返回 dict：
        path    原始路径（前端按原文做 key 回填，不能改）
        clean   剥掉行号/章节后的真实路径
        tail    被剥掉的尾巴（前端原样显示在徽标后面）
        kind    local_note | local_file | remote | invalid
        note    当 kind=local_note 时，库内相对路径（供前端 rn() 跳转）
        server  命中 other_server_prefixes 时的服务器名，否则空
    """
    raw = str(raw or "").strip()
    clean, tail = _strip_path_tail(raw)
    out = {"path": raw, "clean": clean, "tail": tail, "kind": "invalid",
           "note": None, "server": ""}
    if not clean.startswith("/"):
        return out
    # 形状校验：和前端同一条口径（同一套字符集），挡住含空格/引号/反引号的
    # 字符串被当成路径送进来。前端本来就不会发这种，这里是接口自保。
    if not _ABS_PATH_RE.fullmatch(clean):
        return out
    # 隐藏路径不探测（见 _has_hidden_part 注释）。
    if _has_hidden_part(clean):
        return out
    # 先在配置的「其他服务器」前缀里找。注意：这一步只给徽标换名字，
    # 不改变可点性 —— 能不能点永远只看本机 stat 结果。
    for pfx, name in server_prefixes:
        if pfx and clean.startswith(pfx):
            out["server"] = name or "其他服务器"
            break
    try:
        # 用 os.stat 而不是 Path.exists()：exists() 把「权限不足」也吞成 False，
        # 于是有权限问题的本机文件会被标成「其他服务器」——判断依据就错了。
        os.stat(clean)
    except (FileNotFoundError, NotADirectoryError):
        out["kind"] = "remote"
        return out
    except PermissionError:
        out["kind"] = "local_file"      # 本机存在但读不了：不可点
        return out
    except Exception:
        out["kind"] = "remote"
        return out
    if not os.path.isfile(clean):
        out["kind"] = "local_file"      # 目录也算本机存在
        return out
    # 是符号链接的话，按真实指向判断是否在库内 —— 否则库外放个软链指进库内，
    # 或库内放个软链指向 /etc，都会让「可点」的判断失真。
    try:
        real = os.path.realpath(clean)
    except Exception:
        real = clean
    # 是本机文件 —— 再判断是不是「库内笔记」，这决定能不能点。
    if _is_inside(real, NOTES_DIR):
        try:
            rel = str(Path(real).resolve().relative_to(NOTES_DIR.resolve()))
        except Exception:
            rel = ""
        if rel:
            out["kind"] = "local_note"
            out["note"] = rel.replace(os.sep, "/")
            return out
    out["kind"] = "local_file"
    return out


def _server_prefixes():
    """读 other_server_prefixes：一行一个 `前缀|服务器名`，也允许只写前缀。"""
    raw = get_setting("other_server_prefixes") or ""
    if isinstance(raw, (list, tuple)):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw).splitlines()
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "|" in ln:
            pfx, name = ln.split("|", 1)
            out.append((pfx.strip(), name.strip()))
        else:
            out.append((ln, ""))
    return [(p, n) for p, n in out if p]


@bp.route("/api/path-probe", methods=["POST"])
def path_probe():
    """正文里的绝对路径：逐条在本机文件系统上 stat，返回徽标状态。

    请求体：`{"paths": ["/srv/notes/示例/docs/a.md", "/srv/notes/nope.md"]}`
    返回：`{"probed": {"/srv/notes/示例/docs/a.md": {...}, ...}, "count": N}`

    为什么必须真 stat 而不是「按文件名在库里搜」：库里搜到同名笔记
    ≠ 本机真有这个文件。不 stat 就点进去，结果是空的——这是一条已知的踩坑记录
    「万一你搜索过去，笔记里有这个文件，但是你在本机找不到。那不是白弄了吗？」
    所以口径是：可点的一定是本机真实存在且在库内的文件，其余一律不可点。
    """
    tk, err, code = check_auth("r")
    if err:
        return err, code
    data = json_body_dict()
    paths = data.get("paths") or []
    if not isinstance(paths, list):
        return jsonify({"error": "paths 必须是数组"}), 400
    # 去重 + 限流：一篇笔记里的路径数量有限，300 足够；
    # 防恶意大数组把每条都 stat 一遍拖垮磁盘。
    uniq = []
    for p in paths[:300]:
        p = str(p or "").strip()
        if p and p not in uniq:
            uniq.append(p)
    if not uniq:
        return jsonify({"probed": {}, "count": 0})
    prefixes = _server_prefixes()
    out = {}
    for p in uniq:
        try:
            out[p] = _probe_one(p, prefixes)
        except Exception:
            log.warning("routes_notes.py 异常: path_probe", exc_info=True)
            out[p] = {"path": p, "clean": p, "tail": "", "kind": "remote",
                      "note": None, "server": ""}
    return jsonify({"probed": out, "count": len(out)})


# ── 文档模板（T9）：模板集合的增删改查 ──
#
# 与 routes_general.py 里那个老 `GET /api/templates` 的关系说明（别删这段）：
# 老路由在 routes_general.py 里注册，而 routes_general 比本模块**先**导入
# （routes_notes 顶部就 import 了它），Flask 对同路径规则按注册顺序取第一条，
# 所以同路径再注册一个 GET 永远命中老处理函数（实测过：返回的仍是老的
# 「默认模板」一条）。老路由与 routes_general.py 都不在本次改动范围内，
# 因此 GET 走 before_request 做窄口径接管：只认 `GET /api/templates` 这一条，
# 其余请求原样放行。POST / DELETE 是全新路径，正常注册。

@bp.before_request
def _templates_catalog_get():
    """GET /api/templates 返回模板集合（内置 5 套 + _模板/ + 自定义）。"""
    if request.method != "GET" or request.path != "/api/templates":
        return None
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        from ..services.templates import list_catalog
        return jsonify({"templates": list_catalog()})
    except Exception as e:
        log.warning("routes_notes.py 异常: 模板集合列表", exc_info=True)
        return jsonify({"templates": [], "error": str(e)}), 500


@bp.route("/api/templates", methods=["POST"])
def api_template_save():
    """新建或覆盖一条自定义模板。body 必须非空；内置 id 不许覆盖。"""
    tk, err, code = check_auth("w")
    if err:
        return err, code
    from ..services.templates import save_custom, get_catalog_item, BuiltinTemplateError
    d = json_body_dict()
    existed = bool(d.get("id")) and get_catalog_item(str(d.get("id"))) is not None
    try:
        item = save_custom(d)
    except BuiltinTemplateError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        log.warning("routes_notes.py 异常: 保存模板", exc_info=True)
        return jsonify({"error": "保存模板失败"}), 500
    return jsonify({"template": item}), 200 if existed else 201


@bp.route("/api/templates/<tid>", methods=["DELETE"])
def api_template_delete(tid):
    """删一条自定义模板；内置的返回 400，不存在的返回 404。"""
    tk, err, code = check_auth("w")
    if err:
        return err, code
    from ..services.templates import delete_custom, BuiltinTemplateError
    try:
        ok = delete_custom(tid)
    except BuiltinTemplateError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        log.warning("routes_notes.py 异常: 删除模板", exc_info=True)
        return jsonify({"error": "删除模板失败"}), 500
    if not ok:
        return jsonify({"error": f"模板不存在：{tid}"}), 404
    return jsonify({"deleted": tid}), 200
