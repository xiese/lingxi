"""知识库 API — 通用路由（Web / 版本 / 健康 / 仪表盘 / 图谱 / 项目 / 404）"""

import re, json, time as _time_mod
import logging
from datetime import datetime
from flask import request, jsonify, send_file, send_from_directory

from ..routes import bp, is_excluded_path, json_body_dict
from ..config import (
    NOTES_DIR, WEB_DIR, HAS_JIEBA, MCP_USAGE_LOG, VERSION, SCAN_DIRS, SKILLS_DIR,
)
from ..store import load_store
from ..auth import (
    check_auth, parse_fm, require_master,
)
from ..search import search_index
from ..graph import compute_graph, invalidate_cache


# ── Web ──

log = logging.getLogger("kb.routes_general")

@bp.route("/web/")
@bp.route("/web/<path:fn>")
def web(fn="index.html"):
    if not WEB_DIR.exists():
        return "missing", 500
    as_attachment = fn.endswith((".tar.gz", ".zip", ".gz"))
    resp = send_from_directory(str(WEB_DIR), fn, as_attachment=as_attachment)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── 标准 API ──

@bp.route("/api/version")
def api_version():
    skill_file = SKILLS_DIR / "personal-knowledge-base" / "SKILL.md"
    skill_exists = skill_file.exists()
    skill_ver = None
    if skill_exists:
        try:
            meta, _ = parse_fm(skill_file.read_text(encoding="utf-8"))
            skill_ver = meta.get("version", None)
        except Exception:
            log.warning("routes_general.py 异常", exc_info=True)
    return jsonify({
        "server": VERSION,
        "skill": skill_ver or VERSION,
        "skill_version": skill_ver,
        "skill_installed": skill_exists,
        "compatible": True,
        "jieba": HAS_JIEBA,
        "features": [
            "极简白UI", "混合搜索(BM25+FAISS+Reranker)", "jieba中文分词",
            "LLM整理", "图谱计算(PageRank/Louvain)", "智能上传",
            "审计日志(SQLite)", "主Token审批", "笔记列表缓存",
        ],
        "web": f"http://{request.host}/web/",
    })


@bp.route("/health")
def health():
    """轻量探活：绝不触发全盘扫描。

    2026-10-18 第 18 项：原实现每次冷启动第一次访问都会 rglob+stat 全库（2058 篇），
    在内存紧张、swap 已用近 2G 的机器上表现为「首次 /health 卡住」。
    现在只读 15s 缓存：命中即真实值；未命中返回 stale=null 并起后台线程预热，
    由下一次探活拿到结果。stale 为 null 表示「未知」，不是「0」。
    """
    if not search_index.bm25:
        # 索引未加载时没有任何可信来源，状态就是「未知」。
        # 旧写法用 0 充数，会让调用方以为「零待索引」；这里与冷启动分支保持一致。
        stale, cached = None, False
    else:
        stale, cached = search_index.peek_stale()
        if not cached:
            try:
                search_index.prime_stale_async()
            except Exception:
                pass
    return jsonify({
        "service": "知识库API",
        "version": VERSION,
        "status": "ok",
        "jieba": HAS_JIEBA,
        "indexed": len(search_index.paths),
        "stale": stale,
        "stale_cached": cached,
        "time": datetime.now().isoformat(),
    })


@bp.route("/")
def index():
    return jsonify({
        "service": "知识库API v5.9",
        "web": f"http://{request.host}/web/",
        "features": [
            "语义搜索", "LLM整理", "审计日志", "管理设置",
            "主Token审批", "智能上传", "jieba中文分词", "混合搜索",
        ],
        "jieba": HAS_JIEBA,
    })


@bp.route("/dashboard")
@bp.route("/api/dashboard")
def dashboard():
    tk, err, code = check_auth("r")
    if err:
        return err, code

    now = _time_mod.time()
    # TTL 有效 + cache 存在 → 直接返回，跳过文件遍历
    if (
        _DASHBOARD_CACHE["data"] is not None
        and now - _DASHBOARD_CACHE["ts"] < _DASHBOARD_CACHE_TTL
    ):
        return jsonify(_DASHBOARD_CACHE["data"])
    # TTL 过期或 cache 不存在 → 检查文件是否变化
    current_mtime = _get_graph_mtime()
    if (
        _DASHBOARD_CACHE["data"] is not None
        and _DASHBOARD_CACHE["mtime"] == current_mtime
    ):
        # 文件未变化，仅刷新 TTL 时间戳
        _DASHBOARD_CACHE["ts"] = now
        return jsonify(_DASHBOARD_CACHE["data"])

    DIR_KEYS = {"10-项目": "projects", "20-知识": "knowledge",
                 "30-写作": "writing", "30-记录": "records",
                 "40-日记": "diaries", "50-档案": "archived"}
    counts = dict.fromkeys(DIR_KEYS, 0)
    dir_counts = dict.fromkeys(DIR_KEYS, 0)
    for d in DIR_KEYS:
        base = NOTES_DIR / d
        if not base.exists():
            continue
        excl_archived = (d != "50-档案")  # 归档目录自身不能被排除，否则 archived 永远为 0
        for f in base.rglob("*.md"):
            rel = str(f.relative_to(NOTES_DIR))
            fname = f.name
            if is_excluded_path(rel, exclude_archived=excl_archived, exclude_assets=True):
                continue
            if fname.startswith(".") or fname == "README.md" or fname.startswith("{{"):
                continue
            counts[d] += 1
        # 主数字口径（2026-10-18 维护者定）：只数该分类目录下的一级子目录，
        # 不递归、不数文件，排除 . 与 _ 开头的目录。
        try:
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                nm = child.name
                if nm.startswith(".") or nm.startswith("_"):
                    continue
                dir_counts[d] += 1
        except OSError:
            log.warning("routes_general.py 异常: dashboard 子目录计数 %s", d, exc_info=True)

    p, k, w, rc, dy, ar = (counts[d] for d in DIR_KEYS)
    dp, dk, dw, drc, ddy, dar = (dir_counts[d] for d in DIR_KEYS)
    result = {
        # 主数字：一级目录个数
        "projects": dp, "knowledge": dk, "writing": dw,
        "records": drc, "diaries": ddy, "archived": dar,
        # 小字：笔记篇数
        "projects_notes": p, "knowledge_notes": k, "writing_notes": w,
        "records_notes": rc, "diaries_notes": dy, "archived_notes": ar,
        "total": p + k + w + rc + dy + ar,
        "total_dirs": dp + dk + dw + drc + ddy + dar,
        # 该分类下没有任何一级子目录时，主数字退化为篇数（前端按此标记注明）
        "dir_fallback": {key: (dir_counts[d] == 0) for d, key in DIR_KEYS.items()},
    }
    _DASHBOARD_CACHE["data"] = result
    _DASHBOARD_CACHE["ts"] = now
    _DASHBOARD_CACHE["mtime"] = current_mtime
    return jsonify(result)


# ── 图谱缓存（TTL + mtime 检测，避免每次请求全量扫描）──

_GRAPH_CACHE = {"data": None, "ts": 0, "mtime": 0}
_GRAPH_CACHE_TTL = 60  # 60 秒缓存

# ── Dashboard 缓存（TTL + mtime，避免每次请求全量 scandir）──

_DASHBOARD_CACHE = {"data": None, "ts": 0, "mtime": 0}
_DASHBOARD_CACHE_TTL = 30  # 30 秒缓存


def _get_graph_mtime():
    """获取所有笔记文件的最新修改时间（粗略判断是否有变化）"""
    try:
        latest = 0
        for f in NOTES_DIR.rglob("*.md"):
            mt = f.stat().st_mtime
            if mt > latest:
                latest = mt
        return latest
    except Exception:
        log.warning("routes_general.py 异常: _get_graph_mtime", exc_info=True)
        return 0


def _invalidate_graph_cache():
    """使图谱缓存失效（在笔记写入操作后调用）"""
    _GRAPH_CACHE["data"] = None
    _GRAPH_CACHE["ts"] = 0
    invalidate_cache()


@bp.route("/api/graph")
def api_graph():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        min_sim = float(request.args.get("min_sim", 0.80))
    except (TypeError, ValueError):
        min_sim = 0.80
    min_sim = min(1.0, max(0.0, min_sim))
    try:
        max_links = int(request.args.get("max_links", 8))
    except (TypeError, ValueError):
        max_links = 8
    max_links = min(50, max(1, max_links))

    def _flag(name, default=True):
        raw = request.args.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "t")

    try:
        result = compute_graph(
            min_sim=min_sim,
            max_links=max_links,
            include_semantic=_flag("include_semantic", True),
            include_wiki=_flag("include_wiki", True),
        )
    except Exception:
        log.warning("routes_general.py 异常: api_graph", exc_info=True)
        result = {
            "nodes": [],
            "links": [],
            "meta": {
                "node_count": 0, "link_count": 0, "wiki_count": 0,
                "semantic_count": 0, "min_sim": round(min_sim, 4),
                "max_links": max_links,
                "generated_at": _time_mod.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        }
    return jsonify(result)


# ── MCP 使用统计 ──

@bp.route("/api/mcp-usage")
def api_mcp_usage():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        if not MCP_USAGE_LOG.exists():
            return jsonify({"total": 0, "today": 0, "ok": 0, "tools": {}, "last": None})
        lines = MCP_USAGE_LOG.read_text(encoding="utf-8").strip().split("\n")
        today = datetime.now().date()
        tool_counts = {}
        today_count = 0
        ok_count = 0
        last = None
        for line in reversed(lines):
            if not line.strip():
                continue
            row = json.loads(line)
            if not last:
                last = row
            date_str = row.get("time", "")
            try:
                dt = datetime.fromisoformat(date_str)
                if dt.date() == today:
                    today_count += 1
            except Exception:
                log.debug("routes_general.py 异常: api_mcp_usage", exc_info=True)
                # 兼容旧格式：fallback 到字符串截取
                if date_str[:10] == today.isoformat():
                    today_count += 1
            tool = row.get("tool", "unknown")
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if row.get("ok"):
                ok_count += 1
        return jsonify({
            "total": len(lines),
            "today": today_count,
            "ok": ok_count,
            "tools": tool_counts,
            "last": last,
        })
    except Exception as e:
        log.warning("routes_general.py 异常: api_mcp_usage", exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# 单行上限 4KB：防止客户端灌大对象把用量库撑爆
MCP_USAGE_MAX_LINE = 4096
# 请求体硬上限 64KB：超过直接 400，连解析都不做
MCP_USAGE_MAX_BODY = 64 * 1024
_MCP_USAGE_CORE = ("time", "client", "readonly", "tool", "ok")


@bp.route("/api/mcp-usage/report", methods=["POST"])
def api_mcp_usage_report():
    """MCP 桥上报一次工具调用，追加一行到 MCP_USAGE_LOG。

    口径（D-e）：桥每次调用都上报入库，统计页只读库，不做跨机文件同步。
    本接口只做「收下并落一行」，不做重活；失败由调用方静默吞掉。
    """
    tk, err, code = check_auth("w")
    if err:
        return err, code

    # 预检：有 Content-Length 时先拦，省内存（chunked 时该值为 None）
    clen = request.content_length or 0
    if clen > MCP_USAGE_MAX_BODY:
        return jsonify({"error": "payload_too_large", "limit": MCP_USAGE_MAX_BODY}), 400
    # 兜底：按实际读取到的字节数再判一次，防止 chunked 绕过上限
    raw = request.get_data(cache=True)
    if len(raw) > MCP_USAGE_MAX_BODY:
        return jsonify({"error": "payload_too_large", "limit": MCP_USAGE_MAX_BODY}), 400

    body = json_body_dict()
    tool = body.get("tool")
    client = body.get("client")
    ok = body.get("ok")
    if not isinstance(tool, str) or not tool.strip():
        return jsonify({"error": "缺少或非法字段: tool"}), 400
    if not isinstance(client, str) or not client.strip():
        return jsonify({"error": "缺少或非法字段: client"}), 400
    if not isinstance(ok, bool):
        return jsonify({"error": "缺少或非法字段: ok"}), 400

    raw_time = body.get("time")
    if raw_time in (None, ""):
        ts = datetime.now().isoformat()
    elif isinstance(raw_time, str):
        try:
            datetime.fromisoformat(raw_time)
        except ValueError:
            return jsonify({"error": "非法字段: time 必须是 ISO8601"}), 400
        ts = raw_time
    else:
        return jsonify({"error": "非法字段: time 必须是 ISO8601 字符串"}), 400

    readonly = body.get("readonly", False)
    if not isinstance(readonly, bool):
        return jsonify({"error": "非法字段: readonly 必须是布尔"}), 400

    extra = body.get("extra", {})
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        return jsonify({"error": "非法字段: extra 必须是对象"}), 400

    row = {"time": ts, "client": client.strip()[:128], "readonly": readonly,
           "tool": tool.strip()[:128], "ok": ok}

    # 其他顶层字段原样并入（保留字除外），与 log_usage(**extra) 的输出一致
    merged = {}
    for k, v in body.items():
        if k in _MCP_USAGE_CORE or k == "extra":
            continue
        merged[k] = v
    merged.update({k: v for k, v in extra.items() if k not in _MCP_USAGE_CORE})

    # 逐字段并入，放不下就丢；核心字段本身超限则 400
    base_len = len((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    if base_len > MCP_USAGE_MAX_LINE:
        return jsonify({"error": "line_too_large", "limit": MCP_USAGE_MAX_LINE}), 400
    dropped = []
    for k, v in merged.items():
        if not isinstance(k, str):
            continue
        trial = dict(row)
        trial[k] = v
        size = len((json.dumps(trial, ensure_ascii=False) + "\n").encode("utf-8"))
        if size <= MCP_USAGE_MAX_LINE:
            row = trial
        else:
            dropped.append(k)

    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        MCP_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MCP_USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        log.warning("routes_general.py 异常: api_mcp_usage_report 落盘失败", exc_info=True)
        return jsonify({"error": "internal_error"}), 500

    resp = {"ok": True, "recorded": True}
    if dropped:
        resp["dropped"] = dropped
    return jsonify(resp)


# ── 项目列表 ──

@bp.route("/api/projects")
def api_projects():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    pd = NOTES_DIR / "10-项目"
    projs = []
    if pd.exists():
        for d in sorted(pd.iterdir()):
            if d.is_dir() and d.name != "_assets":
                rm = d / "README.md"
                info = {"name": d.name, "has_readme": rm.exists()}
                if rm.exists():
                    meta, _ = parse_fm(rm)
                    info["status"] = meta.get("status", "")
                    info["priority"] = meta.get("priority", "")
                    ts = re.findall(r"^- \[(.)\] (.+)$", rm.read_text(encoding="utf-8"), re.MULTILINE)
                    info["tasks_done"] = sum(1 for t in ts if t[0] in "xX")
                    info["tasks_total"] = len(ts)
                projs.append(info)
    return jsonify(projs)


# ── 模板列表 ──

@bp.route("/api/templates")
def api_templates():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        from ..services.templates import list_templates
        return jsonify({"templates": list_templates()})
    except Exception as e:
        log.warning("routes_general.py 异常: api_templates", exc_info=True)
        return jsonify({"templates": [], "error": str(e)})


# ── 主题列表 ──

@bp.route("/api/themes")
def api_themes():
    """可用主题列表：themes/*.css 各一条 + 固定的 classic。"""
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        from ..services.themes import list_themes
        return jsonify({"themes": list_themes()})
    except Exception as e:
        log.warning("routes_general.py 异常: api_themes", exc_info=True)
        # 兜底：主题目录坏了也不能让设置页打不开
        return jsonify({"themes": [{"id": "classic", "name": "经典", "desc": "原版界面"}], "error": str(e)})


# ── 功能插件 ──

@bp.route("/api/plugins")
def api_plugins():
    """插件清单。带 enabled 标记（设置页用）。"""
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        from ..services.plugins import list_plugins
        return jsonify({"plugins": list_plugins()})
    except Exception as e:
        log.warning("routes_general.py 异常: api_plugins", exc_info=True)
        # 兜底：插件目录坏了不能让前端启动流程中断
        return jsonify({"plugins": [], "error": str(e)})


@bp.route("/api/plugins/enabled")
def api_plugins_enabled():
    """只返回已启用的插件——前端启动时按这个清单挂载。"""
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        from ..services.plugins import load_enabled
        return jsonify({"plugins": load_enabled()})
    except Exception as e:
        log.warning("routes_general.py 异常: api_plugins_enabled", exc_info=True)
        return jsonify({"plugins": [], "error": str(e)})


@bp.route("/api/plugins/<name>/toggle", methods=["POST"])
@require_master
def api_plugin_toggle(name):
    """启停一个插件。body 可带 {"enabled": true/false}；不带就按当前状态取反。"""
    from ..services.plugins import set_enabled, is_enabled
    try:
        d = json_body_dict()
    except Exception:
        d = {}
    try:
        want = d.get("enabled")
        if not isinstance(want, bool):
            want = not is_enabled(name)
        return jsonify(set_enabled(name, want))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        log.warning("routes_general.py 异常: api_plugin_toggle", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── 404 / favicon ──

@bp.app_errorhandler(404)
def _404(e):
    return jsonify({"error": "接口不存在"}), 404


@bp.route("/favicon.ico")
def favicon():
    svg = WEB_DIR / "favicon.svg"
    if svg.exists():
        return send_file(str(svg))
    return "", 204