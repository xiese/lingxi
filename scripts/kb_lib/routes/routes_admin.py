"""知识库 API — 管理/设置路由"""

import re, json, time as ttime, urllib.request
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import request, jsonify

from ..routes import bp, trigger_index_refresh, parse_tags_response, _dedup_path, json_body_dict
from ..routes.routes_notes import _invalidate_notes_cache
from ..routes.routes_general import _invalidate_graph_cache
from ..routes.routes_search import _int_arg
from ..config import (
    NOTES_DIR, HAS_JIEBA, SCAN_DIRS, PROTECTED, is_excluded_path,
    DEFAULT_SETTINGS, load_settings, save_settings, get_setting, TYPE_PREFIX,
    TYPE_MAP, DIR_TO_TYPE, KB_ROOT, SETTINGS_SCHEMA, SECRET_SETTING_KEYS,
)
from ..store import is_master
from ..audit import (
    audit_log, query_audit, audit_stats,
    count_categories, count_older_than, export_rows, delete_older_than, delete_all,
)
from ..search import search_index, get_rebuild_status
from ..llm import llm_chat_with_retry
from ..auth import require_master, parse_fm, client_ip, get_token
from ..push_qq import send_qq_message


# ── 设置 API ──

log = logging.getLogger("kb.routes_admin")

@bp.route("/admin/settings")
@require_master
def get_settings_route():
    _SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password")
    s = load_settings()
    safe = {}
    for k, v in s.items():
        if any(k.lower().endswith(sfx) for sfx in _SECRET_SUFFIXES) and v:
            safe[k] = "<REDACTED>"
        elif k == "qq_push_webhook_url" and v:
            safe[k] = "<REDACTED>"
        else:
            safe[k] = v
    return jsonify(safe)


@bp.route("/api/settings/schema")
@require_master
def get_settings_schema_route():
    """设置页结构：分组 / 控件类型 / 选项 / 说明 / 默认值。

    前端据此渲染设置页 —— 加设置项只改后端 `config.SETTINGS_SCHEMA`，不必动前端。
    只回结构，不回任何**当前值**（当前值走 /admin/settings，密钥在那里已做占位）；
    默认值里剔除敏感项，避免把「密钥默认值」这种东西塞进前端。
    """
    defaults = {k: v for k, v in DEFAULT_SETTINGS.items() if k not in SECRET_SETTING_KEYS}
    return jsonify({
        "schema_version": 1,
        "groups": SETTINGS_SCHEMA,
        "defaults": defaults,
    })


@bp.route("/admin/settings", methods=["POST"])
@require_master
def update_settings_route():
    d = json_body_dict()
    s = load_settings()
    bool_keys = {"index_auto_rebuild", "qq_push_enabled", "rerank_enabled"}
    int_keys = {"token_ttl", "max_log_days", "auth_watch_interval_seconds", "decay_tau_days"}
    float_keys = {"dedup_threshold", "hybrid_alpha"}
    for k in [
        "llm_base", "llm_key", "llm_model", "embedding_base",
        "embedding_key", "embedding_model", "embedding_type",
        "token_ttl", "index_auto_rebuild", "max_log_days", "theme",
        "hybrid_alpha", "qq_push_enabled", "qq_push_webhook_url",
        "qq_push_webhook_secret", "qq_push_target", "qq_push_payload_mode",
        "qq_push_events",
        "auth_watch_interval_seconds", "dedup_threshold", "decay_tau_days",
        "rerank_enabled", "rerank_model",
        "other_server_prefixes",
    ]:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, str):
            if k in bool_keys:
                v = v.lower() in ("true", "1", "yes")
            elif k in int_keys:
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    log.debug("routes_admin.py 异常: update_settings_route", exc_info=True)
                    v = DEFAULT_SETTINGS.get(k, 30)
            elif k in float_keys:
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    log.debug("routes_admin.py 异常: update_settings_route", exc_info=True)
                    v = DEFAULT_SETTINGS.get(k, 0.3)
        s[k] = v
    save_settings(s)
    redacted = {
        k: ("<REDACTED>" if any(x in k.lower() for x in ("key", "secret", "token", "password")) and d[k] else d[k])
        for k in d
    }
    audit_log("settings_updated", extra=json.dumps(redacted, ensure_ascii=False))
    # 返回前脱敏：避免在 API 响应中泄露密钥
    _SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password")
    safe = {}
    for k, v in s.items():
        if any(k.lower().endswith(sfx) for sfx in _SECRET_SUFFIXES) and v:
            safe[k] = "<REDACTED>"
        elif k == "qq_push_webhook_url" and v:
            safe[k] = "<REDACTED>"
        else:
            safe[k] = v
    return jsonify({"status": "ok", "settings": safe})


# ── 梦境设置 API（2026-10-24）──
#
# 梦境有 19 个可调键，分两类：
#   ① 8 个单值文件（.llm_base / .llm_model / .llm_max_tokens / .llm_max_tokens_hard /
#      .min_dream_chars / .llm_system_prompt / .llm_key / .kb_token）
#   ② 11 个 dream.py 源码常量（RECENT_DAYS / MAX_NOTES / …）
# 它们都不在 DEFAULT_SETTINGS 里，走 /admin/settings 会被写入白名单静默丢弃，
# 所以单独开一组接口，落盘到 KB_ROOT/dream_settings.json。
#
# 注意：这份文件目前只是「存下来」，dream.py 仍从原位置读值，尚未接线生效。
DREAM_SETTINGS_FILE = KB_ROOT / "dream_settings.json"

# 键 → (类型, 默认值, 是否敏感)。默认值取自 dream.py 的 _secret() 兜底与常量字面量。
DREAM_KEYS = {
    "llm_base":            ("text",     "http://127.0.0.1:8318/v1", False),
    "llm_model":           ("text",     "deepseek-v4-flash",       False),
    "llm_max_tokens":      ("number",   16000,                     False),
    "llm_max_tokens_hard": ("number",   64000,                     False),
    "min_dream_chars":     ("number",   300,                       False),
    "llm_system_prompt":   ("textarea", "你是知识库助手，负责整理笔记之间的关联。", False),
    "llm_key":             ("password", "",                        True),
    "kb_token":            ("password", "",                        True),
    "RECENT_DAYS":         ("number",   7,                         False),
    "MAX_NOTES":           ("number",   36,                        False),
    "MAX_RECENT":          ("number",   14,                        False),
    "CHARS_PER_NOTE":      ("number",   700,                       False),
    "MAX_TOTAL_CHARS":     ("number",   30000,                     False),
    "MAX_ISLANDS":         ("number",   12,                        False),
    "REF_MIN_KEY":         ("number",   8,                         False),
    "REF_MIN_QUOTE":       ("number",   12,                        False),
    "REF_WORKERS":         ("number",   4,                         False),
    "REF_TIMEOUT":         ("number",   60,                        False),
    "SEEN_KEEP_DAYS":      ("number",   60,                        False),
}


def _dream_load():
    """读 dream_settings.json；缺失或损坏都回落空表（由调用方补默认值）。"""
    if not DREAM_SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(DREAM_SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        log.warning("dream_settings.json 解析失败，按空配置处理", exc_info=True)
        return {}


def _dream_save(data):
    """原子写入，避免写一半断电留下坏文件。"""
    import tempfile, os as _os
    fd, tmp = tempfile.mkstemp(dir=str(DREAM_SETTINGS_FILE.parent), prefix=".dream.", suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, str(DREAM_SETTINGS_FILE))
    finally:
        if _os.path.exists(tmp):
            try:
                _os.unlink(tmp)
            except OSError:
                log.warning("清理梦境设置临时文件失败", exc_info=True)


def _dream_public(raw):
    """对外视图：敏感键只回布尔「已设置」，绝不回明文。"""
    out = {}
    for k, (typ, default, secret) in DREAM_KEYS.items():
        v = raw.get(k, default)
        if secret:
            out[k] = {"value": "", "set": bool(v), "default": "", "type": typ, "secret": True}
        else:
            out[k] = {"value": v, "set": bool(v), "default": default, "type": typ, "secret": False}
    return out


@bp.route("/admin/dream/settings")
@require_master
def get_dream_settings():
    return jsonify({"settings": _dream_public(_dream_load()), "file": DREAM_SETTINGS_FILE.name})


@bp.route("/admin/dream/settings", methods=["POST"])
@require_master
def update_dream_settings():
    d = json_body_dict()
    raw = _dream_load()
    changed = {}
    for k, v in d.items():
        if k not in DREAM_KEYS:
            continue  # 未知键直接丢弃，不落盘（防止拼错静默生效）
        typ, default, secret = DREAM_KEYS[k]
        if secret:
            # 空串 = 不动（前端不回显明文，留空表示「保持原值」）
            if v in ("", None, "<REDACTED>"):
                continue
            raw[k] = str(v)
            changed[k] = "<REDACTED>"
            continue
        if typ == "number":
            try:
                v = int(v)
            except (ValueError, TypeError):
                return jsonify({"error": f"{k} 必须是整数"}), 400
        else:
            v = str(v)
        raw[k] = v
        changed[k] = v
    _dream_save(raw)
    audit_log("dream_settings_updated", extra=json.dumps(changed, ensure_ascii=False))
    return jsonify({"status": "ok", "settings": _dream_public(raw)})


@bp.route("/admin/push/qq/test", methods=["POST"])
@require_master
def test_qq_push():
    d = json_body_dict()
    message = d.get("message", "知识库 QQ 推送测试")
    dry_run = d.get("dry_run", True)
    res = send_qq_message(message, dry_run=dry_run)
    audit_log("qq_push_test", "/admin/push/qq/test", extra=f"dry_run={bool(dry_run)} sent={res.get('sent')}")
    return jsonify(res)


@bp.route("/admin/models")
@require_master
def list_models():
    s = load_settings()
    result = {"llm_models": [], "embedding_models": []}
    try:
        req = urllib.request.Request(f"{s['llm_base']}/models", headers={"Authorization": f"Bearer {s['llm_key']}"})
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        result["llm_models"] = [m["id"] for m in data.get("data", [])]
    except Exception:
        log.warning("routes_admin.py 异常: list_models", exc_info=True)
        result["llm_models"] = []  # 未配置 LLM 服务时不臆造模型名
    try:
        req = urllib.request.Request(f"{s['embedding_base']}/models", headers={"Authorization": f"Bearer {s['embedding_key']}"})
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        result["embedding_models"] = [m["id"] for m in data.get("data", [])]
    except Exception:
        log.warning("routes_admin.py 异常: list_models", exc_info=True)
        result["embedding_models"] = []  # 未配置嵌入服务时不臆造模型名
    return jsonify(result)


# ── 审计日志 API ──

@bp.route("/admin/audit")
@require_master
def audit_list():
    # 原来直接 int(...)：?page=abc 会 ValueError→500，?limit=-1 会传进查询层。
    # 复用搜索路由里的参数解析助手，非法值统一回 400。
    page = _int_arg("page", 1, 1, 100000)
    limit = _int_arg("limit", 100, 1, 200)
    if page is None or limit is None:
        return jsonify({"error": "page/limit 必须是整数"}), 400
    action = request.args.get("action", "")
    device = request.args.get("device", "")
    category = request.args.get("category", "")
    result = query_audit(page=page, limit=limit, action=action, device=device, category=category)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@bp.route("/admin/audit/stats")
@require_master
def audit_stats_route():
    stats = audit_stats()
    # 第 4 项：分类条数给前端标签页用。分类口径按 method 列分读写，
    # 不按 action 名前缀猜（生产 action 是 notes/search/index，没有 read*/write*）。
    stats["categories"] = count_categories()
    return jsonify(stats)


@bp.route("/admin/audit/cleanup/preview")
@require_master
def audit_cleanup_preview():
    """清理弹窗预览：选 N 天前会删多少条。真删前必须先看这个数。"""
    days = _int_arg("days", 7, 1, 3650)
    if days is None:
        return jsonify({"error": "days 必须是整数"}), 400
    return jsonify({"days": days, "will_delete": count_older_than(days), "total": audit_stats()["total"]})


@bp.route("/admin/audit/cleanup", methods=["POST"])
@require_master
def audit_cleanup_route():
    """手动清理审计日志（真删）。

    两种模式：
      {"mode":"days","days":7,"backup":true}  删 N 天前
      {"mode":"all","confirm":true,"backup":false}  全清

    维护者 2026-10-18 定：备份是**勾选框，由维护者自己决定这次要不要留底**，
    不强制、不默认勾选。勾了就先把待删记录导出成 JSON 存 `_assets/` 再删。
    """
    d = json_body_dict()
    mode = str(d.get("mode") or "").strip()
    backup = bool(d.get("backup"))
    days = 0

    if mode == "all":
        if d.get("confirm") is not True:
            return jsonify({"error": "全清必须显式传 confirm=true"}), 400
        before_iso = None
        will_delete = audit_stats()["total"]
    elif mode == "days":
        days = d.get("days")
        try:
            days = int(days)
        except (TypeError, ValueError):
            return jsonify({"error": "days 必须是整数"}), 400
        if days < 1:
            return jsonify({"error": "days 至少为 1"}), 400
        before_iso = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        will_delete = count_older_than(days)
    else:
        return jsonify({"error": "mode 只能是 days 或 all"}), 400

    backup_path = None
    if backup and will_delete:
        # 备份落在库根 _assets/ 下，文件名带时间戳，不覆盖历史备份。
        try:
            # 不传 limit：备份必须包含全部待删记录。传 limit 截断的话，
            # 少导的那几条就是「删了没留底」（审查点出 200000 上限的隐患）。
            rows = export_rows(before_iso=before_iso)
            # 备份必须覆盖全部待删记录：少导一条，就等于有一条被删了没留底。
            if len(rows) != will_delete:
                return jsonify({"error": "导出备份条数(%d)与待删条数(%d)不一致，未删除任何记录"
                                         % (len(rows), will_delete)}), 500
            adir = Path(NOTES_DIR).parent / "_assets"
            adir.mkdir(parents=True, exist_ok=True)
            fn = "audit-backup-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
            fp = adir / fn
            fp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            backup_path = "_assets/" + fn
        except Exception:
            log.warning("审计日志导出备份失败", exc_info=True)
            return jsonify({"error": "导出备份失败，未删除任何记录"}), 500

    deleted = delete_all() if mode == "all" else delete_older_than(days)
    audit_log("audit_cleanup", path="mode=%s deleted=%s backup=%s" % (mode, deleted, bool(backup_path)))
    return jsonify({"ok": True, "mode": mode, "deleted": deleted,
                    "backup_path": backup_path, "total": audit_stats()["total"]})


# ── 自动整理 / 调试 ──

@bp.route("/admin/debug-auth")
@require_master
def debug_auth():
    # 只返回判定结果，不回显任何凭据片段。master token 由 token_urlsafe(40)
    # 生成（约 54 字符），"Bearer "+tk 约 61 字符 < 原来的 80 截断上限，
    # 因此旧实现的 auth_header 是完整明文，all_headers 同理。
    # 2026-10-23：改走 get_token()，与全站鉴权同一口径（只认 Authorization 头）。
    a = request.headers.get("Authorization", "")
    tk = get_token()
    return jsonify({
        "auth_header_present": bool(a),
        "auth_scheme": a.split(" ", 1)[0] if a else "",
        "token_len": len(tk),
        "is_master": is_master(tk) if tk else False,
        "remote_ip": client_ip(),
        "header_names": sorted(request.headers.keys()),
    })


# ── /admin/scan-new：拆分后的单一职责函数 ──

def _parse_scan_request():
    """解析请求参数，返回 (hours, since 时间点)。"""
    hours = int(request.args.get("hours", "24"))
    since = datetime.now() - timedelta(hours=hours)
    return hours, since


def _iter_recent_notes(since):
    """遍历 SCAN_DIRS 下最近修改且未排除的 .md，产出 (sd, fp, rel)。"""
    for sd in SCAN_DIRS:
        base = NOTES_DIR / sd
        if not base.exists():
            continue
        for fp in base.rglob("*.md"):
            rel = str(fp.relative_to(NOTES_DIR))
            if is_excluded_path(rel):
                continue
            mtime = datetime.fromtimestamp(fp.stat().st_mtime)
            if mtime < since:
                continue
            yield sd, fp, rel


def _fix_missing_title(fp, rel, content, body, file_issues, file_fixed):
    """缺 title：从正文 H1 提取并写入 frontmatter；无法修则记 issue。"""
    m = re.search(r"^# (.+)$", body, re.MULTILINE)
    if m:
        title_line = f'title: "{m.group(1)}"'
        new_content = re.sub(
            r"^(id:.*\n)", f"\\1{title_line}\n", content, count=1, flags=re.MULTILINE
        )
        if new_content == content:
            # 没有 id: 行, 在 opening --- 后插入
            new_content = re.sub(
                r"^(---\n)", f"\\1{title_line}\n", content, count=1
            )
        if new_content != content:
            fp.write_text(new_content, encoding="utf-8")
            file_fixed.append({"path": rel, "issue": "missing_title", "fix": f"已从标题提取: {m.group(1)}"})
        else:
            file_issues.append({"type": "missing_title", "msg": "缺少 title"})
    else:
        file_issues.append({"type": "missing_title", "msg": "缺少 title"})


def _fix_wrong_directory(sd, fp, rel, meta, file_issues, file_fixed):
    """type 与所在目录不符：按 TYPE_MAP 搬文件；返回更新后的 (fp, rel)。"""
    note_type = meta.get("type", "")
    expected_type = DIR_TO_TYPE.get(sd)
    if note_type and expected_type and note_type != expected_type:
        correct_dir_name = TYPE_MAP.get(note_type)
        if correct_dir_name:
            base = NOTES_DIR / sd
            correct_dir = NOTES_DIR / correct_dir_name
            sub_rel = fp.parent.relative_to(base) if str(fp.parent).startswith(str(base)) else Path(".")
            dest_dir = correct_dir / sub_rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _dedup_path(dest_dir, fp.stem, fp.suffix)
            fp.rename(dest)
            file_fixed.append({"path": rel, "issue": "wrong_directory", "fix": f"已从 {sd}/ 移植到 {correct_dir_name}/",
                               "from": rel, "to": str(dest.relative_to(NOTES_DIR))})
            # 更新 fp 和 rel 指向新路径，防止后续写入操作写旧路径
            return dest, str(dest.relative_to(NOTES_DIR))
        else:
            file_issues.append({"type": "wrong_directory", "msg": f"type={note_type} 但放在 {sd}/ 下"})
    return fp, rel


def _fix_missing_fields(sd, fp, rel, content, meta, file_fixed):
    """缺 type/status/created：按目录约定补 frontmatter 字段。"""
    note_type = meta.get("type", "")
    expected_type = DIR_TO_TYPE.get(sd)
    needs_fix = []
    missing_fields = []
    if not note_type and expected_type:
        missing_fields.append("type")
        needs_fix.append(("type", expected_type))
    if not meta.get("status"):
        missing_fields.append("status")
        needs_fix.append(("status", "active"))
    if not meta.get("created") and not meta.get("date"):
        missing_fields.append("created")
        needs_fix.append(("created", datetime.fromtimestamp(fp.stat().st_ctime).strftime("%Y-%m-%d")))
    if missing_fields:
        new_content = content
        for field, value in needs_fix:
            if "title:" in new_content:
                new_content = re.sub(r"(title:.*\n)", f"\\1{field}: {value}\n", new_content, count=1)
            else:
                new_content = re.sub(r"(---\n)", f"\\1{field}: {value}\n", new_content, count=1)
        if new_content != content:
            fp.write_text(new_content, encoding="utf-8")
            file_fixed.append({"path": rel, "issue": "missing_fields", "fix": f"已补全: {', '.join(missing_fields)}"})


def _autofix_note(sd, fp, rel):
    """单文件检查 + 自动修复；返回 (rel, modified, file_issues, file_fixed)，读文件失败返回 None。"""
    mtime = datetime.fromtimestamp(fp.stat().st_mtime)
    try:
        content = fp.read_text(encoding="utf-8")
    except Exception:
        log.debug("routes_admin.py 异常: scan_new", exc_info=True)
        return None
    file_issues = []
    file_fixed = []
    if not content.startswith("---"):
        file_issues.append({"type": "missing_frontmatter", "msg": "缺少 YAML frontmatter"})
        return rel, mtime.isoformat(), file_issues, file_fixed
    meta, body = parse_fm(content)
    if not meta.get("title"):
        _fix_missing_title(fp, rel, content, body, file_issues, file_fixed)
    tags_val = meta.get("tags") or []
    if not isinstance(tags_val, list):
        tags_val = []
    if not tags_val:
        file_issues.append({"type": "missing_tags", "msg": "缺少标签"})
    fp, rel = _fix_wrong_directory(sd, fp, rel, meta, file_issues, file_fixed)
    expected_prefix = TYPE_PREFIX.get(meta.get("type", ""), "kb")
    note_id = meta.get("id", "")
    if note_id and not note_id.startswith(expected_prefix + "-"):
        file_issues.append({"type": "wrong_id_prefix", "msg": f"id应以 {expected_prefix}- 开头: {note_id}"})
    _fix_missing_fields(sd, fp, rel, content, meta, file_fixed)
    return rel, mtime.isoformat(), file_issues, file_fixed


def _scan_and_fix_notes(since):
    """扫描 + frontmatter 自动修复阶段，返回 (issues, fixed)。"""
    issues = []
    fixed = []
    for sd, fp, rel in _iter_recent_notes(since):
        result = _autofix_note(sd, fp, rel)
        if result is None:
            continue
        rel, modified, file_issues, file_fixed = result
        fixed.extend(file_fixed)
        if file_issues:
            issues.append({"path": rel, "modified": modified, "issues": file_issues})
    return issues, fixed


def _write_tags_to_file(fp, rel, new_tags, fixed_list, auto_list):
    """将标签列表写入笔记文件的 tags 字段，自动追加到 fixed/auto 列表"""
    if not new_tags:
        return
    try:
        content = fp.read_text(encoding="utf-8")
        # 优先匹配 tags: []（空数组），其次匹配 tags:（空值/无值）
        new_content, count = re.subn(
            r"tags:\s*\[\s*\]",
            f"tags: [{', '.join(new_tags)}]",
            content,
        )
        if count == 0:
            new_content, count = re.subn(
                r"^tags:\s*$",
                f"tags: [{', '.join(new_tags)}]",
                new_content,
                flags=re.MULTILINE,
            )
        if count > 0:
            fp.write_text(new_content, encoding="utf-8")
            auto_list.append({"path": rel, "added_tags": new_tags})
            fixed_list.append({"path": rel, "issue": "missing_tags", "fix": f"LLM推荐: {new_tags}"})
        else:
            try:
                import logging
                logging.getLogger("kb").warning("auto-tag regex no match: %s", rel)
            except Exception:
                log.warning("routes_admin.py 异常", exc_info=True)
    except Exception:
        log.warning("routes_admin.py 异常", exc_info=True)


def _recommend_single(fp, rel, snippet, fixed_list, auto_list):
    """单文件逐条 LLM 标签推荐"""
    try:
        suggested = llm_chat_with_retry([
            {"role": "system", "content": "为以下内容推荐3-5个中文标签，用逗号分隔。只输出标签。"},
            {"role": "user", "content": snippet},
        ], 80, 0.3)
        new_tags = parse_tags_response(suggested)
        _write_tags_to_file(fp, rel, new_tags, fixed_list, auto_list)
    except Exception:
        log.warning("routes_admin.py 异常", exc_info=True)


def _collect_pending_tag_files(since):
    """收集无标签且正文非空的最近笔记，返回 [(fp, rel, snippet)]。"""
    pending_tag_files = []
    for sd, fp, rel in _iter_recent_notes(since):
        try:
            content = fp.read_text(encoding="utf-8")
            meta, body = parse_fm(content)
            existing_tags = meta.get("tags", [])
            if not existing_tags and body.strip():
                pending_tag_files.append((fp, rel, body[:3000]))
        except Exception:
            log.debug("routes_admin.py 异常: _recommend_single", exc_info=True)
            continue
    return pending_tag_files


def _batch_llm_tagging(pending_tag_files, fixed_list, auto_list):
    """LLM 批量推荐标签：先整批 JSON，解析失败或无结果逐条回退。"""
    if not pending_tag_files:
        return
    batch_prompt = '为以下笔记分别推荐3-5个中文标签，返回JSON: {"path1":["标签1","标签2"],...}\n\n'
    for i, (fp, rel, snippet) in enumerate(pending_tag_files):
        batch_prompt += f"--- 笔记{i+1} 路径:{rel} ---\n{snippet}\n\n"
    try:
        raw = llm_chat_with_retry([
            {"role": "system", "content": "你是标签推荐专家，只返回JSON。"},
            {"role": "user", "content": batch_prompt[:24000]},
        ], 800, 0.3)
        # 方案 B：利用 JSONDecoder.raw_decode 提取第一个 JSON 对象
        json_match = None
        if "{" in raw:
            try:
                decoder = json.JSONDecoder()
                obj, end = decoder.raw_decode(raw[raw.index("{"):])
                json_match = type("Match", (), {"group": lambda s: json.dumps(obj)})()
            except (json.JSONDecodeError, Exception):
                log.debug("routes_admin.py 异常: _recommend_single", exc_info=True)
                json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
        batch_ok = False
        if json_match:
            try:
                tag_map = json.loads(json_match.group())
                for fp, rel, _ in pending_tag_files:
                    _write_tags_to_file(fp, rel, tag_map.get(rel, []), fixed_list, auto_list)
                if auto_list:
                    batch_ok = True
            except Exception:
                log.warning("routes_admin.py 异常", exc_info=True)
        # 批量 JSON 解析失败或无结果 → 回退到单文件逐条推荐
        if not batch_ok:
            for fp, rel, snippet in pending_tag_files:
                _recommend_single(fp, rel, snippet, fixed_list, auto_list)
    except Exception:
        log.debug("routes_admin.py 异常: _recommend_single", exc_info=True)
        # LLM 调用异常 → 逐条降级
        for fp, rel, snippet in pending_tag_files:
            _recommend_single(fp, rel, snippet, fixed_list, auto_list)


def _scan_post_effects(fixed_list, auto_list):
    """扫描收尾：索引刷新 + 缓存失效（保持原有顺序与条件）。"""
    if get_setting("index_auto_rebuild"):
        trigger_index_refresh(force=False)  # 受 5 分钟冷却约束，不强制绕过——scan_new 本身耗时，无需每次强制重建
    _invalidate_notes_cache()
    if fixed_list or auto_list:
        _invalidate_graph_cache()


def _build_scan_response(hours, issues, fixed_list, auto_list):
    """构造 scan_new 的响应体。"""
    return jsonify({
        "scanned_period": f"最近{hours}小时",
        "issues_found": len(issues),
        "issues": issues,
        "auto_fixed": len(fixed_list),
        "fixed": fixed_list,
        "auto_tagged": len(auto_list),
        "safety": "未删除/修改任何受保护文件",
    })


@bp.route("/admin/scan-new", methods=["POST"])
@require_master
def scan_new():
    """扫描新笔记 → 自动修 frontmatter → LLM 批量标签（薄编排层）。"""
    hours, since = _parse_scan_request()
    issues, fixed = _scan_and_fix_notes(since)
    auto_tags = []
    pending_tag_files = _collect_pending_tag_files(since)
    _batch_llm_tagging(pending_tag_files, fixed, auto_tags)
    _scan_post_effects(fixed, auto_tags)
    return _build_scan_response(hours, issues, fixed, auto_tags)


# ── 重建索引 ──

@bp.route("/admin/rebuild-index", methods=["POST"])
@require_master
def rebuild_index():
    start = ttime.time()
    try:
        search_index.build(is_rebuild=True)
        _invalidate_notes_cache()
        elapsed = round(ttime.time() - start, 2)
        return jsonify({
            "status": "ok",
            "indexed": len(search_index.paths),
            "elapsed_seconds": elapsed,
            "jieba": HAS_JIEBA,
            "embedding_type": get_setting("embedding_type"),
        })
    except Exception as e:
        log.warning("routes_admin.py 异常: rebuild_index", exc_info=True)
        return jsonify({"status": "error", "error": "internal_error"}), 500


# ── 全量重建：后台异步 + 进度查询（t3）──

@bp.route("/api/index/rebuild/async", methods=["POST"])
@require_master
def rebuild_index_async():
    """启动后台全量重建，立即返回，不阻塞 HTTP 请求。"""
    try:
        res = search_index.start_rebuild_async()
    except Exception as e:
        log.warning("routes_admin.py 异常: rebuild_index_async", exc_info=True)
        return jsonify({"status": "error", "error": "internal_error"}), 500
    if res.get("status") == "started":
        audit_log("index_rebuild_async_started", "/api/index/rebuild/async",
                  extra="started_at=%s" % res.get("started_at"))
    return jsonify(res)


@bp.route("/api/index/rebuild/status", methods=["GET"])
def rebuild_index_status():
    """查询后台全量重建进度。"""
    return jsonify(get_rebuild_status())
