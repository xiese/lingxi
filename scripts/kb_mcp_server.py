#!/usr/bin/env python3
"""Knowledge Base MCP server.

Wraps the Knowledge Base REST API as MCP tools.
"""
import json
import logging
import sys
import base64
import contextlib
import hashlib
import tempfile
import threading
import os
import mimetypes
import urllib.parse
import urllib.request
import urllib.error
import re
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows 无 fcntl：退化为仅进程内互斥
    fcntl = None

from fastmcp import FastMCP

# fastmcp 鉴权基类：模块级导入，供 KBTokenVerifier 继承。
# 导入失败时不在这里抛错（stdio 模式根本用不到鉴权），而是记下原因，
# 由 build_mcp_auth() 在真正需要鉴权时抛 RuntimeError —— 保持
# 「想开鉴权却开不了时绝不裸奔」的既有约束。
try:
    from fastmcp.server.auth.auth import AccessToken as _AccessToken
    from fastmcp.server.auth.auth import TokenVerifier as _TokenVerifier

    _AUTH_IMPORT_ERR = None
except Exception as _auth_exc:  # pragma: no cover - 依赖安装异常时才走
    _AccessToken = None

    class _TokenVerifier:  # type: ignore[no-redef]
        """占位基类：仅在 fastmcp 鉴权组件缺失时存在。"""

        def __init__(self, *args, **kwargs):
            pass

    _AUTH_IMPORT_ERR = _auth_exc

# ── 记忆检索辅助（2026-10-21 内联，原 kb_mcp_memory.py，避免部署漏拷依赖）──


# 外部长期记忆库（SQLite）。未配置 KB_MEMORY_DB 时默认指向用户主目录下的隐藏路径；
# 该路径不存在时相关工具返回空结果，不影响其余功能。
MEMORY_DB = Path(
    os.environ.get("KB_MEMORY_DB")
    or os.environ.get("SCOPE_RECALL_DB")
    or (Path.home() / ".hermes" / "scope-recall" / "memory.sqlite3"))


def _sqlite_rows_to_markdown(query: str, rows) -> str:
    lines = [f"# 长期记忆搜索结果：{query}", "", f"- 命中：{len(rows)}", f"- 数据库：`{MEMORY_DB}`", ""]
    if not rows:
        lines.append("没有命中长期记忆。")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        rid = r.get("id", "")
        summary = (r.get("summary") or "").replace("\n", " ")[:240]
        content = (r.get("content") or "").replace("\n", " ")[:700]
        target = r.get("target") or ""
        source = r.get("source") or ""
        score = r.get("score", 0)
        updated = r.get("updated_at") or ""
        lines.append(f"## {i}. {summary or rid}")
        lines.append(f"- ID：`{rid}`")
        lines.append(f"- 分数：{score}")
        if target or source:
            lines.append(f"- 来源/目标：`{source}` → `{target}`")
        if updated:
            lines.append(f"- 更新时间：{updated}")
        lines.append(f"- 内容：{content}")
        lines.append("")
    return "\n".join(lines)
def get_memory_files():
    env = os.environ.get("KB_MEMORY_FILES", "").strip()
    if env:
        return [Path(x) for x in env.split(":") if x.strip()]
    home = Path.home()
    return [
        home / ".kb-memory" / "MEMORY.md",
        home / ".kb-memory" / "USER.md",
        home / ".kb-memory" / "AG_MEMORY.md",
    ]
def memory_chunks():
    chunks = []
    for fp in get_memory_files():
        if not fp.exists():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            log.warning("读取长期记忆文件失败，跳过: %s", fp, exc_info=True)
            continue
        parts = [p.strip() for p in text.replace("\r\n", "\n").split("§") if p.strip()]
        if not parts:
            parts = [text.strip()] if text.strip() else []
        for idx, part in enumerate(parts, 1):
            chunks.append({"source": str(fp), "index": idx, "text": part})
    return chunks
def score_text(query: str, text: str) -> int:
    q = query.lower().strip()
    t = text.lower()
    if not q:
        return 0
    score = 0
    if q in t:
        score += 20
    raw_tokens = query.replace("，", " " ).replace(",", " " ).replace("/", " " ).split()
    tokens = [x.lower().strip() for x in raw_tokens if len(x.strip()) >= 2]
    for tok in tokens:
        if tok in t:
            score += 5
    chars = [c for c in query if "\u4e00" <= c <= "\u9fff"]
    if chars:
        score += min(10, sum(1 for c in set(chars) if c in text))
    return score
def _memory_db_search_raw(query: str, limit: int = 6):
    """Search the external long-term memory SQLite store.

    优先使用 memories_fts FTS5 词法召回，失败时回退 LIKE。
    这里保持只读，不写入该数据库，避免污染外部记忆。
    """
    if not MEMORY_DB.exists() or MEMORY_DB.stat().st_size <= 0:
        return []
    limit = max(1, min(int(limit), 20))
    terms = [x.strip() for x in re.split(r"[\s,，、/]+", query) if len(x.strip()) >= 2][:8]
    if not terms:
        terms = list(dict.fromkeys([c for c in query if "\u4e00" <= c <= "\u9fff"]))[:8]
    rows = []

    def score_and_add(row):
        d=dict(row)
        text=(d.get('content') or '')+' '+(d.get('summary') or '')+' '+(d.get('target') or '')+' '+(d.get('source') or '')
        d['score']=score_text(query, text)
        # FTS 命中时 bm25_score 越小越相关；转成一点加分
        try:
            bm=float(d.get('bm25_score'))
            d['score'] += max(0, min(10, int(10/(1+abs(bm)))))
        except Exception:
            log.debug("bm25_score 无法转 float，跳过加分: %r", d.get('bm25_score'), exc_info=True)
        if d['score']>0:
            rows.append(d)

    try:
        conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        # 1) FTS5 优先。把多个 term 作为 OR 查询，避免中文/英文混合时过窄。
        if terms:
            fts_query = ' OR '.join('"'+t.replace('"',' ')+'"' for t in terms)
            try:
                sql = """
                SELECT m.id, m.content, m.summary, m.source, m.target, m.updated_at, m.metadata,
                       bm25(memories_fts) AS bm25_score
                FROM memories_fts
                JOIN memories m ON m.id = memories_fts.memory_id
                WHERE memories_fts MATCH ?
                  AND COALESCE(json_extract(m.metadata, '$.lifecycle'), '') NOT IN ('superseded','obsolete','rejected','archived')
                ORDER BY bm25(memories_fts) ASC, m.updated_at DESC
                LIMIT ?
                """
                for row in conn.execute(sql, [fts_query, max(limit*3, limit)]):
                    score_and_add(row)
            except Exception:
                # FTS 查询语法/分词异常时继续 LIKE fallback
                log.warning("memories_fts FTS5 查询失败，回退 LIKE 召回", exc_info=True)
        # 2) LIKE fallback / 补充召回
        if len(rows) < limit:
            clauses=[]; params=[]
            for t in terms:
                clauses.append("(content LIKE ? OR summary LIKE ? OR target LIKE ? OR source LIKE ?)")
                needle=f"%{t}%"
                params.extend([needle, needle, needle, needle])
            where = " OR ".join(clauses) if clauses else "1=1"
            sql = f"""
            SELECT id, content, summary, source, target, updated_at, metadata, NULL as bm25_score
            FROM memories
            WHERE ({where})
              AND COALESCE(json_extract(metadata, '$.lifecycle'), '') NOT IN ('superseded','obsolete','rejected','archived')
            ORDER BY updated_at DESC
            LIMIT ?
            """
            for row in conn.execute(sql, [*params, max(limit*4, limit)]):
                score_and_add(row)
        conn.close()
    except Exception as e:
        log.warning("长期记忆数据库查询失败", exc_info=True)
        return [{"id":"memory-db-error","summary":"长期记忆数据库查询失败","content":str(e),"score":0,"source":"","target":"","updated_at":""}]
    # 去重 + 排序
    dedup={}
    for r in rows:
        rid=r.get('id') or ''
        if rid not in dedup or r.get('score',0) > dedup[rid].get('score',0):
            dedup[rid]=r
    rows=list(dedup.values())
    rows.sort(key=lambda x: (x.get('score',0), x.get('updated_at') or ''), reverse=True)
    return rows[:limit]
def _file_memory_search_raw(query: str, limit: int = 6):
    rows = []
    for ch in memory_chunks():
        sc = score_text(query, ch["text"])
        if sc > 0:
            rows.append({**ch, "score": sc})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:max(1, min(int(limit), 20))]
def memory_search_raw(query: str, limit: int = 6):
    rows = _memory_db_search_raw(query, limit)
    if rows:
        return [{"kind":"memory-db", **r} for r in rows]
    return [{"kind":"file", **r} for r in _file_memory_search_raw(query, limit)]
def format_memory_markdown(query: str, rows) -> str:
    if rows and rows[0].get("kind") == "memory-db":
        return _sqlite_rows_to_markdown(query, rows)
    lines = [f"# 文件记忆搜索结果：{query}", "", f"- 命中：{len(rows)}", ""]
    if not rows:
        lines.append("没有命中长期记忆。")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        snippet = r["text"].replace("\n", " " )[:600]
        lines.append(f"## {i}. {Path(r['source']).name} / chunk {r['index']}")
        lines.append(f"- 来源：`{r['source']}`")
        lines.append(f"- 分数：{r['score']}")
        lines.append(f"- 内容：{snippet}")
        lines.append("")
    return "\n".join(lines)

_KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)
TOKEN_FILE = Path(os.environ.get("KB_TOKEN_FILE", _KB_ROOT / ".api_tokens.json"))
USAGE_LOG = Path(os.environ.get("KB_MCP_USAGE_LOG", _KB_ROOT / ".kb_mcp_usage.jsonl"))
CLIENT_NAME = os.environ.get("KB_CLIENT_NAME", "unknown").strip() or "unknown"
READONLY = os.environ.get("KB_MCP_READONLY", "").strip().lower() in ("1", "true", "yes", "on")
# 命令行直呼模式（--idea）：没有 HTTP 请求上下文，取令牌时跳过请求头那两步。
_CLI_MODE = len(sys.argv) > 1 and sys.argv[1] in ("--idea", "-i")


log = logging.getLogger("kb.kb_mcp_server")


def _env_with_legacy(new_name, old_name, default):
    """优先读新名环境变量；仅设置旧名时取旧值并记一次 deprecation warning。

    背景（2026-10-07 KB_HOST 拆名）：KB_HOST 曾同名双义——本文件用它表示
    上游 REST API 地址，kb_lib/config.py 用它表示监听绑定地址，运维设错就
    出事。现已拆为 KB_API_BASE（上游地址，本文件）与 KB_BIND（绑定地址，
    config.py）；旧名 KB_HOST 只保留一个版本的兼容。
    """
    val = os.environ.get(new_name)
    if val:
        return val
    legacy = os.environ.get(old_name)
    if legacy:
        log.warning(
            "配置弃用警告: 环境变量 %s 已更名为 %s（旧名仅保留一个版本的"
            "兼容，之后将只认新名）。当前按旧名取值 %r，请尽快改用 %s。",
            old_name, new_name, legacy, new_name,
        )
        return legacy
    return default


# 上游 REST API 地址（默认 http://127.0.0.1:8899；kb_mcp_sse.py 以旧名 setdefault 提供）
KB_API_BASE = _env_with_legacy("KB_API_BASE", "KB_HOST", "http://127.0.0.1:8899").rstrip("/")
# 旧模块常量名，兼容一版内引用
KB_HOST = KB_API_BASE

# 用量上报开关：默认开；设 0 只写本地文件，不上报知识库
USAGE_REPORT = os.environ.get("KB_MCP_USAGE_REPORT", "1").strip().lower() not in ("0", "false", "no", "off")
USAGE_REPORT_TIMEOUT = 2.0


def _report_usage(row: dict) -> None:
    """把一行用量 POST 到 <上游>/api/mcp-usage/report。

    失败绝不影响工具调用：整体吞异常，只写一行 stderr，不重试、不阻塞（2 秒超时）。
    """
    try:
        token = get_token()
        body = json.dumps(row, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            KB_HOST + "/api/mcp-usage/report",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urllib.request.urlopen(req, timeout=USAGE_REPORT_TIMEOUT) as r:
            r.read(1024)
    except Exception as e:
        try:
            import sys as _sys
            print(f"[kb_mcp_usage_report] 上报失败（已忽略）: {e}", file=_sys.stderr)
        except Exception:
            pass


def log_usage(tool: str, ok: bool = True, **extra):
    row = None
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"time": datetime.now().isoformat(), "client": CLIENT_NAME, "readonly": READONLY, "tool": tool, "ok": ok, **extra}
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("kb_mcp_server.py 异常", exc_info=True)
    if USAGE_REPORT:
        _report_usage({"time": (row or {}).get("time") or datetime.now().isoformat(),
                       "client": CLIENT_NAME, "readonly": READONLY, "tool": tool, "ok": ok,
                       "extra": extra})


def _token_from_request_context() -> str:
    """Prefer the Bearer presented by the current MCP HTTP/SSE client."""
    # 命令行直呼（--idea）没有 HTTP 请求上下文：直接跳过，免得每次调用刷 4 条
    # "No active HTTP request found" 的堆栈到 stderr（QQ 侧会把 stderr 当日志收）。
    if _CLI_MODE:
        return ""
    # 1. FastMCP access token (StaticTokenVerifier puts raw token string here)
    try:
        from fastmcp.server.dependencies import get_access_token

        at = get_access_token()
        if at is not None:
            tok = getattr(at, "token", None) or getattr(at, "access_token", None)
            if tok:
                return str(tok).strip()
    except Exception:
        log.warning("kb_mcp_server.py 异常", exc_info=True)
    # 2) raw Authorization header from Starlette request
    try:
        from fastmcp.server.dependencies import get_http_request

        req = get_http_request()
        auth = (req.headers.get("authorization") or req.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
    except Exception:
        log.warning("kb_mcp_server.py 异常", exc_info=True)
    return ""


def get_token() -> str:
    # SSE: use caller's token so r vs rw is enforced by REST API
    ctx = _token_from_request_context()
    if ctx:
        return ctx
    env = os.environ.get("KB_TOKEN", "").strip()
    if env:
        return env
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return data.get("master", {}).get("token", "")
    except Exception:
        return ""


def api(method: str, path: str, data=None, timeout: int = 30):
    """调用 REST。HTTPError 时尽量解析 JSON body 并透传 error/problems 字段。"""
    token = get_token()
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        KB_API_BASE + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        try:
            parsed = json.loads(detail) if detail else {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and parsed:
            # 透传 REST 结构化错误（quality/hygiene 等），附带 http_status
            out = dict(parsed)
            out.setdefault("error", parsed.get("error") or f"HTTP {e.code}")
            out["http_status"] = e.code
            if "detail" not in out and detail and not parsed.get("problems"):
                out["detail"] = detail[:1000]
            return out
        return {"error": f"HTTP {e.code}", "detail": detail[:1000], "http_status": e.code}
    except Exception as e:
        return {"error": str(e)}


def brief(obj, max_chars: int = 12000) -> str:
    """把结果序列化成 JSON 文本，超长时**仍然返回合法 JSON**。

    2026-10-22 修（T-11）：原来是 text[:max_chars] + "...<truncated>" 硬切，
    切点落在字符串/对象中间就产出非法 JSON，调用方 json.loads 直接抛
    JSONDecodeError（报错里的 12,000 就是这个切点）。改成逐级降级：
    先整条丢掉尾部条目并置 truncated，仍超长再只留前若干条的 path/title，
    最后兜底返回一个带 truncated 的最小对象——任何一级都是合法 JSON。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text

    def dump(o):
        return json.dumps(o, ensure_ascii=False, indent=2)

    if isinstance(obj, dict):
        items = obj.get("items")
        if isinstance(items, list) and items:
            # 一级：二分找能装下的最大条数
            lo, hi, best = 0, len(items), None
            while lo <= hi:
                mid = (lo + hi) // 2
                cand = dict(obj)
                cand["items"] = items[:mid]
                cand["truncated"] = True
                cand["returned"] = mid
                if len(dump(cand)) <= max_chars:
                    best = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best is not None:
                return dump(best)
            # 二级：只留 path/title
            lo, hi, best2 = 0, len(items), None
            while lo <= hi:
                mid = (lo + hi) // 2
                slim = []
                for it in items[:mid]:
                    if isinstance(it, dict):
                        slim.append({k: it[k] for k in ("path", "title") if k in it})
                    else:
                        slim.append(it)
                cand = dict(obj)
                cand["items"] = slim
                cand["truncated"] = True
                cand["returned"] = mid
                cand["slim"] = True
                if len(dump(cand)) <= max_chars:
                    best2 = cand
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best2 is not None:
                return dump(best2)
        # 兜底：最小合法对象
        # 2026-10-24：etag 是写入乐观锁的版本号，正文被截掉也必须留下，
        # 否则长笔记读回来拿不到版本号，调用方只能盲写（等于没有锁）。
        minimal = {"truncated": True, "error": f"结果超过 {max_chars} 字符，已省略正文"}
        for k in ("count", "total", "mode", "directory", "path", "title", "etag"):
            if k in obj:
                minimal[k] = obj[k]
        return dump(minimal)

    return dump({"truncated": True, "error": f"结果超过 {max_chars} 字符，已省略正文"})


def format_search_markdown(res: dict, query: str) -> str:
    if res.get("error"):
        return brief(res)
    items = res.get("items", [])
    lines = [f"# 知识库搜索结果：{query}", ""]
    lines.append(f"- 模式：`{res.get('mode', 'keyword')}`")
    lines.append(f"- 命中：{res.get('count', len(items))}")
    lines.append("")
    if not items:
        lines.append("没有找到相关笔记。可以尝试换关键词，或创建新笔记沉淀这次发现。")
        return "\n".join(lines)
    for i, item in enumerate(items, 1):
        title = item.get("title") or item.get("path") or "未命名"
        path = item.get("path", "")
        score = item.get("score")
        tags = item.get("tags") or []
        snippet = (item.get("snippet") or "").strip().replace("\n", " ")[:450]
        lines.append(f"## {i}. {title}")
        lines.append(f"- 路径：`{path}`")
        if score is not None:
            try:
                lines.append(f"- 分数：{float(score):.4f}")
            except Exception:
                lines.append(f"- 分数：{score}")
        if tags:
            lines.append(f"- 标签：{', '.join(map(str, tags))}")
        if snippet:
            lines.append(f"- 摘要：{snippet}")
        matches = item.get("matches") or []
        if matches:
            lines.append("- 匹配片段：")
            for m in matches[:3]:
                ctx = str(m.get("context", "")).strip().replace("\n", " / ")[:220]
                lines.append(f"  - L{m.get('line', '?')}: {ctx}")
        lines.append("")
    lines.append("下一步建议：如果要引用细节，请调用 `kb_read(path)` 读取对应笔记。")
    lines.append("要改写其中某一篇时，先 `kb_read(path)` 取回 etag，再 `kb_update(path, content, base_hash=etag)`，避免覆盖别人的并发修改。")
    return "\n".join(lines)


def readonly_error(tool: str) -> str:
    res = {
        "error": "当前 MCP server 运行在只读模式，写入操作已禁用",
        "tool": tool,
        "client": CLIENT_NAME,
        "hint": "如需写入，请使用 RW token 并设置 KB_MCP_READONLY=false。",
    }
    log_usage(tool, ok=False, reason="readonly")
    return brief(res)


def _load_sse_auth_tokens() -> dict:
    """明文令牌表：KB_TOKEN / KB_MCP_SSE_TOKENS / master。

    设备令牌不在这里 —— schema=2 的令牌表键是 sha256(pepper+明文)，
    拿它当明文注册等于把哈希当密码用（见 KBTokenVerifier）。
    """
    tokens = {}
    env_tok = os.environ.get("KB_TOKEN", "").strip()
    if env_tok:
        tokens[env_tok] = {
            "client_id": CLIENT_NAME or "kb-env",
            "scopes": ["kb:rw"] if not READONLY else ["kb:r"],
        }
    # explicit allowlist: comma-separated tokens (optional)
    extra = os.environ.get("KB_MCP_SSE_TOKENS", "").strip()
    if extra:
        for t in extra.split(","):
            t = t.strip()
            if t:
                tokens[t] = {"client_id": "kb-allowlist", "scopes": ["kb:rw"]}
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        master = (data.get("master") or {}).get("token", "").strip()
        if master:
            tokens[master] = {"client_id": "kb-master", "scopes": ["kb:rw", "kb:r"]}
    except Exception:
        log.warning("kb_mcp_server.py 异常", exc_info=True)
    return tokens


def _load_sse_hashed_tokens() -> dict:
    """设备令牌表：{存储键: claims}。

    schema=2 时键是 sha256(pepper+明文)，必须由 KBTokenVerifier 先算哈希再查；
    schema<2（未迁移的旧表）时键是明文，直接当明文注册，保持向后兼容。
    """
    hashed = {}
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return hashed
    schema = (data.get("meta") or {}).get("schema")
    for tk, info in (data.get("tokens") or {}).items():
        if not tk:
            continue
        # skip expired
        exp = (info or {}).get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(str(exp).replace("Z", "")) < datetime.now():
                    continue
            except Exception:
                log.warning("kb_mcp_server.py 异常", exc_info=True)
        perm = str((info or {}).get("permission", "r")).lower()
        scopes = ["kb:r", "kb:rw"] if perm in ("rw", "master") else ["kb:r"]
        name = (info or {}).get("name") or (info or {}).get("device") or "kb-token"
        hashed[tk] = {
            "client_id": str(name)[:64],
            "scopes": scopes,
            "is_hash_key": schema == 2,
        }
    return hashed


class KBTokenVerifier(_TokenVerifier):
    """按知识库令牌表校验 Bearer。

    与 StaticTokenVerifier 的区别：设备令牌表在 schema=2 下存的是哈希键，
    必须先 token_hash(出示的明文) 再查表，否则只有 master 能过，而反过来
    把哈希串当 Bearer 却能握手成功（工具调用再 401）—— 等于把哈希当密码。

    返回的 AccessToken.token 一律是**客户端出示的原始明文**，工具内转发给
    REST 8899 时才能通过它自己的鉴权。
    """

    def __init__(self, plain: dict, hashed: dict, required_scopes=None):
        super().__init__(required_scopes=required_scopes)
        self._plain = plain
        self._hashed = hashed
        self._hash_keys = {k for k, v in hashed.items() if v.get("is_hash_key")}
        self._plain_in_table = {k: v for k, v in hashed.items() if not v.get("is_hash_key")}
        self._pepper_warned = False

    def _hash(self, token: str) -> str:
        try:
            from kb_lib.store import token_hash
        except Exception:
            if not self._pepper_warned:
                log.warning("kb_mcp_server.py: 无法导入 kb_lib.store.token_hash，"
                            "哈希化设备令牌将无法通过 SSE 鉴权")
                self._pepper_warned = True
            return ""
        try:
            return token_hash(token)
        except Exception:
            log.warning("kb_mcp_server.py: token_hash 计算失败", exc_info=True)
            return ""

    def _lookup(self, token: str):
        """返回 (claims, expires_at)；未匹配返回 (None, None)。"""
        info = self._plain.get(token) or self._plain_in_table.get(token)
        if info:
            return info, None
        if not self._hash_keys:
            return None, None
        # 出示的是表里的哈希键本身：它是存储键，不是令牌，必须拒绝。
        if token in self._hash_keys:
            log.warning("kb_mcp_server.py: 拒绝以令牌表哈希键作为 Bearer 使用")
            return None, None
        h = self._hash(token)
        if h and h in self._hash_keys:
            return self._hashed[h], self._hashed[h].get("expires_at")
        return None, None

    async def verify_token(self, token: str):
        info, _ = self._lookup(token)
        if not info:
            return None
        scopes = info.get("scopes", [])
        if self.required_scopes:
            if not set(self.required_scopes).issubset(set(scopes)):
                log.debug("Token missing required scopes")
                return None
        if _AccessToken is None:
            return None
        return _AccessToken(
            token=token,
            client_id=info.get("client_id") or "kb-token",
            scopes=scopes,
            expires_at=None,
            subject=info.get("sub"),
            claims=dict(info),
        )


def build_mcp_auth():
    """SSE 远程鉴权：校验 Bearer 是否为知识库有效 Token。stdio 默认不启用。"""
    flag = os.environ.get("KB_MCP_SSE_AUTH", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None
    try:
        if _AUTH_IMPORT_ERR is not None:
            raise _AUTH_IMPORT_ERR
    except Exception as e:
        # 安全加固：原先此处 return None，等于「想开鉴权却开不了时
        # 裸奔启动」，且与用户显式设 KB_MCP_SSE_AUTH=0 的返回值无法区分。改为抛错。
        raise RuntimeError(
            f"SSE 鉴权已启用（KB_MCP_SSE_AUTH 默认 1）但 fastmcp 鉴权组件导入失败: {e}"
        ) from e
    plain = _load_sse_auth_tokens()
    hashed = _load_sse_hashed_tokens()
    if not plain and not hashed:
        # 同上：空令牌表绝不能降级成无鉴权。要求先由 kb-server 生成主令牌，
        # 或由使用者显式设 KB_MCP_SSE_AUTH=0 表示「我知道这是无鉴权」。
        raise RuntimeError(
            "SSE 鉴权已启用（KB_MCP_SSE_AUTH 默认 1）但未加载到任何令牌。"
            "请先启动 kb-server 生成主令牌，或显式设置 KB_MCP_SSE_AUTH=0 关闭鉴权。"
        )
    # require at least read scope
    return KBTokenVerifier(plain=plain, hashed=hashed, required_scopes=["kb:r"])


def _multipart_upload(filename: str, raw: bytes, target_dir: str = "", note_type: str = "") -> dict:
    token = get_token()
    boundary = "----KBMCPBoundary"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts = []

    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8")

    if target_dir:
        parts.append(field("dir", target_dir))
    if note_type:
        parts.append(field("type", note_type))
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(raw)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        KB_API_BASE + "/upload",
        data=b"".join(parts),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", errors="replace")[:1000]}
    except Exception as e:
        return {"error": str(e)}


mcp = FastMCP("personal-knowledge-base")


@mcp.tool()
def memory_search(query: str, limit: int = 6) -> str:
    """搜索长期记忆库中的用户偏好与已记住的事实。适合查询用户习惯、个人偏好。"""
    rows = memory_search_raw(query, limit)
    log_usage("memory_search", ok=True, query=query, count=len(rows))
    return format_memory_markdown(query, rows)


@mcp.tool()
def kb_dual_search(query: str, limit: int = 6) -> str:
    """记忆 + 知识库双重检索。回答用户问题前优先使用：先看长期记忆，再看结构化知识库。"""
    mem_rows = memory_search_raw(query, limit)
    q = urllib.parse.quote(query)
    kb_res = api("GET", f"/api/search/semantic?q={q}&limit={max(1, min(int(limit), 20))}")
    if kb_res.get("count", 0) == 0:
        kb_res = api("GET", f"/search?q={q}&limit={max(1, min(int(limit), 20))}")
    log_usage("kb_dual_search", ok="error" not in kb_res, query=query, memory_count=len(mem_rows), kb_count=kb_res.get("count"))
    parts = ["# 双重检索结果", "", "## A. 长期记忆 / 用户偏好", "", format_memory_markdown(query, mem_rows), "", "## B. 知识库笔记", "", format_search_markdown(kb_res, query), "", "## 使用建议", "- 偏好、习惯、长期事实优先参考“长期记忆”。", "- 项目细节、代码记录、研究资料优先参考“知识库笔记”。", "- 如果知识库命中具体路径，需要细节时继续调用 `kb_read(path)`。"]
    return "\n".join(parts)


@mcp.tool()
def kb_search(query: str, limit: int = 8, mode: str = "semantic") -> str:
    """搜索用户的个人知识库。用户问技术、项目、笔记、历史记录时优先调用。"""
    limit = max(1, min(int(limit), 20))
    q = urllib.parse.quote(query)
    if mode == "keyword":
        res = api("GET", f"/search?q={q}&limit={limit}")
    else:
        res = api("GET", f"/api/search/semantic?q={q}&limit={limit}")
        if res.get("count", 0) == 0:
            res = api("GET", f"/search?q={q}&limit={limit}")
    ok = "error" not in res
    log_usage("kb_search", ok=ok, query=query, count=res.get("count"), mode=mode)
    return format_search_markdown(res, query)


@mcp.tool()
def kb_mentions(needle: str, limit: int = 50, offset: int = 0) -> str:
    """全库精确扫描：哪些笔记提到了某个字符串（含行号）。

    与 kb_search 的分工：
    - kb_search  = 相关性排序，只回 top-N —— 问「哪个最相关」
    - kb_mentions= 确定性覆盖，回全库所有提到的笔记 —— 问「哪些提到了」

    适用：「哪些笔记提到 X」「X 在哪些地方出现过」「X 相关的全部记录」。
    实测 kb_search 的 top5 对全库长尾实体平均只覆盖约 6%，
    本工具可达 ~90% 以上。要「完整清单」时用它，要「最相关的几篇」用 kb_search。
    """
    q = urllib.parse.quote(needle)
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 50
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    res = api("GET", f"/api/search/mentions?q={q}&limit={limit}&offset={offset}", timeout=60)
    log_usage("kb_mentions", ok="error" not in res, needle=needle, total=res.get("total"))
    if res.get("error"):
        return brief(res)
    items = res.get("items", [])
    lines = [f"# 全库提及扫描：{needle}", ""]
    lines.append(f"- 命中笔记：**{res.get('total', 0)}** 篇（共 {res.get('total_lines', 0)} 处）")
    lines.append(f"- 本页：{offset + 1}~{offset + len(items)}（limit={limit}，可用 offset 翻页）")
    lines.append("")
    if not items:
        lines.append("没有笔记提到这个字符串。")
        return "\n".join(lines)
    for i, it in enumerate(items, offset + 1):
        lines.append(f"## {i}. {it.get('path', '')}")
        lines.append(f"- 提及 {it.get('hit_count', 0)} 处，行号：{it.get('hit_lines', [])}")
        for s in it.get("samples", []):
            lines.append(f"  - L{s.get('line')}: {s.get('text', '')}")
        lines.append("")
    lines.append("需要细节时用 `kb_read(path)` 打开对应笔记。")
    return "\n".join(lines)


@mcp.tool()
def kb_read(path: str) -> str:
    """读取知识库中的一篇笔记。

    读回来先看第一行：若写着「本文只做指针」「主文在 …」，说明正文不在这一篇，
    必须顺着路径去读主文，不要把摘要当完整事实回答。

    返回值里的 `etag` 是这篇笔记当前的版本号（内容 sha256 前 16 位）。
    要改写它时把该值传给 `kb_update(..., base_hash=etag)`：期间若被别人改过，
    服务端会返回版本冲突而不是静默覆盖。
    """
    p = urllib.parse.quote(path)
    res = api("GET", f"/notes/{p}")
    log_usage("kb_read", ok="error" not in res, path=path)
    return brief(res, 20000)


@mcp.tool()
def kb_list(directory: str = "", limit: int = 50, offset: int = 0) -> str:
    """列出知识库笔记。"""
    limit = max(1, min(int(limit), 200))
    d = urllib.parse.quote(directory)
    res = api("GET", f"/notes?dir={d}&limit={limit}&offset={int(offset)}")
    log_usage("kb_list", ok="error" not in res, directory=directory, count=res.get("count"))
    return brief(res)


def _dedup_notice(degraded) -> str:
    """把服务端的 dedup_degraded 英文原因，翻成用户看得懂的中文提示。

    2026-10-17 新增。两种降级的事实不同，说法必须分开，否则等于说假话：
    - llm_unavailable / llm_error：检索面正常，候选分数是真实精排分，可信；
      只是最后那步「是不是同一件事」的模型判定没做。
    - index_degraded：检索索引正在刷新，候选退化，分数不可信，只能手动查。
    - 未知原因：给通用提示，不崩、不静默。

    返回空串表示没降级，调用方不要加字段。
    """
    deg = str(degraded or "").strip()
    if not deg:
        return ""
    if deg.startswith("llm_unavailable") or deg.startswith("llm_error"):
        return (
            "本次查重未生效：模型接口不可用（%s）。"
            "下面列出的相似笔记是按内容相似度排的，分数可信，"
            "但「是不是同一件事」这一步没做，请自己看一遍再决定要不要改写。" % deg
        )
    if deg.startswith("index_degraded"):
        return (
            "本次完全没有查重：检索索引正在刷新，相似度分数不可信。"
            "请手动用 kb_search 查一下库中有没有同题笔记，避免写成两份。"
        )
    return "本次查重未完成（%s），相似笔记仅供参考。" % deg


@mcp.tool()
def kb_create(title: str, content: str, note_type: str = "knowledge", subdir: str = "", tags: str = "", status: str = "draft", quality_check: bool = True) -> str:
    """创建或更新知识库笔记。

    写完项目总结、研究记录、调试复盘、实现方案后必须调用本工具入库。
    默认启用质量检查；project/knowledge 笔记过短或缺少章节会被拒绝。
    质量/卫生门禁由 REST 单源执行；MCP 只转发并透传错误。

    ⚠️ 写前三查（2026-10-21 立，不查就别写）：
    1. 先 kb_search 查同名笔记。若 10-项目/ 或 20-知识/ 已有同名主文 →
       本次只写指针：首行 `> 主文：<绝对路径>，本文只做指针`，
       正文只留结论摘要 + 关联索引，**体积不超主文 1/3**。绝不写第二套全文。
    2. 你打算写进知识库的每个事实，项目侧主文里有没有？没有 → 先补主文再写摘要。
       不许把细节只留在知识侧、让项目版当二手摘要。
    3. 同 basename 已存在 → 覆盖同一篇（本工具即覆盖语义），不要用 kb_upload
       另起一个平行副本，那会造出新重复。

    写完自检：知识库那份 / 主文 > 1/3 → 改回指针；有主文里没有的句子 → 回流进主文。

    写时卫生（跨平台硬约束，与 REST 一致）：
    - 禁止废弃路径 10-项目_知识库项目
    - 禁止项目 subdir 以「部署/deploy」结尾导致双目录
    - 禁止 archive 直接落到 50-档案 根
    - 禁止 knowledge 下 skill 整包 dump（超长正文）
    - 同名已存在时响应可能带 hygiene_warnings：项目主文 vs 知识指针，勿双主文
    - 同日项目日更若已是索引版，请用 write/合并，勿反复 kb_create_project_log 盖全文
    """
    if READONLY:
        return readonly_error("kb_create")
    tag_list = [x.strip() for x in tags.replace("，", ",").split(",") if x.strip()]
    data = {
        "title": title,
        "content": content,
        "type": note_type,
        "subdir": subdir,
        "tags": tag_list,
        "status": status,
        "quality_check": bool(quality_check),
    }
    # 2026-10-17：服务端写笔记要跑 LLM 查重，实测 46 秒，30 秒必超时（误报写入失败）。
    res = api("POST", "/notes", data, timeout=120)
    ok = "error" not in res
    # ── 更新建议（2026-10-11 新增，从本机运行版回流）──
    # 本工具落盘路径由标题派生，标题略有差异就会另存一份 → 同题两版。
    # 这里不靠 LLM 判定（实测 LLM 有 40% 误判成 new），直接用查重候选的客观分数。
    try:
        if res.get("action") == "created":
            _cands = res.get("dedup_candidates") or []
            _top = _cands[0] if _cands else None
            if _top and float(_top.get("score") or 0) >= 0.85:
                res["suggested_action"] = "kb_update"
                res["suggested_path"] = _top.get("path")
                res["suggested_reason"] = (
                    "库中已存在高度相似笔记（相似度 %s）：%s。"
                    "若本次意图是「更新」而非新主题，请改用 kb_update(path=..., content=...) "
                    "改写该篇，避免同题两版；本次已按新建写入 %s。"
                    % (_top.get("score"), _top.get("path"), res.get("path"))
                )
    except Exception:
        pass
    # ── 查重降级提示（2026-10-17 新增，从本机运行版回流）──
    _notice = _dedup_notice(res.get("dedup_degraded"))
    if _notice:
        res["dedup_notice"] = _notice
    log_usage(
        "kb_create",
        ok=ok,
        title=title,
        path=res.get("path"),
        note_type=note_type,
        reason=None if ok else res.get("error"),
    )
    return brief(res, 12000)


# ── 同一篇笔记的乐观锁写入串行化（2026-10-24）──
# 后端 save_note 是「读当前内容 → 校验 base_hash → 落盘」，三步之间没有锁。
# 两个请求同时到达时可能都读到旧内容、都通过校验 → 双双 200，后写的静默覆盖先写的。
# 实测（临时库、两个线程同时 kb_update、同 base_hash、内容互不相同）：20 轮里
# 12 轮出现 (200, 200)，即乐观锁在最需要它的场景下漏掉 60%。
# 这里在客户端按「笔记路径」串行：进程内 threading.Lock + 跨进程 flock，
# 让第二个请求在第一个落盘之后才做校验，从而稳定拿到 409。
# 只对带 base_hash 的调用生效：不带版本号的旧行为（直接覆盖）一字不动。
_UPDATE_LOCKS = {}
_UPDATE_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _serialize_note_write(path: str):
    """同一篇笔记的乐观锁写入串行：先进程内互斥，再跨进程 flock。

    flock 文件放在系统临时目录，按「上游地址 + 笔记路径」哈希命名；进程退出时
    内核自动释放，不会留下死锁。没有 fcntl 的平台退化为仅进程内互斥。
    """
    with _UPDATE_LOCKS_GUARD:
        lk = _UPDATE_LOCKS.setdefault(path, threading.Lock())
    with lk:
        lock_fp = None
        try:
            if fcntl is not None:
                key = hashlib.sha256((KB_API_BASE + "\n" + path).encode("utf-8")).hexdigest()[:16]
                ldir = Path(tempfile.gettempdir()) / "kb-mcp-write-locks"
                ldir.mkdir(parents=True, exist_ok=True)
                lock_fp = open(ldir / (key + ".lock"), "a+")
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock_fp is not None:
                try:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_fp.close()


def _version_conflict(res: dict, path: str) -> dict:
    """把后端 409 version_conflict 翻成调用方看得懂、能照做的冲突信息。

    字段口径与 REST 一致（expected = 服务端当前版本，provided = 你提交的版本），
    额外补中文 message / hint，避免调用方只看到一句 error。
    """
    current = res.get("expected") or res.get("current_etag") or ""
    provided = res.get("provided") or res.get("base_hash") or ""
    out = dict(res)
    out.update({
        "error": "version_conflict",
        "http_status": 409,
        "message": "版本冲突：这篇笔记在你读取之后已被他人修改，本次写入未落盘。",
        "path": path,
        "current_version": current,
        "your_version": provided,
        "hint": (
            "请先 kb_read(path=%r) 取最新 etag 与正文，确认改动仍然适用后，"
            "再用 kb_update(path=%r, content=..., base_hash=<最新 etag>) 重试；"
            "不要不带 base_hash 重写，那会覆盖别人的修改。" % (path, path)
        ),
    })
    return out


@mcp.tool()
def kb_update(path: str, content: str, archive_old: bool = True, quality_check: bool = False, base_hash: str = "") -> str:
    """更新**已存在**的知识库笔记（按路径精确改写同一篇，不会另存新文件）。

    何时用：当你要「更新知识库」而目标笔记已存在时，用本工具，**不要**用 kb_create。
    kb_create 的落盘路径由标题决定，标题略有差异就会另存一份、旧文留在库里，
    导致同题两版、旧状态被检索到。

    参数：
    - path：笔记相对路径（如 20-知识/运维/xxx.md），可从 kb_search / kb_list 取
    - content：新的**正文**（不必带 frontmatter，服务端会沿用旧 frontmatter 与 H1，
      只换正文；若要改标题/标签，请传带 frontmatter 的完整内容）
    - archive_old：默认 True —— 改前内容归档到 50-档案/_旧版本/（不进检索索引），
      并在 frontmatter 记 version / supersedes，可回溯
    - quality_check：默认 False（更新不卡质量门禁；需强校验时传 True）
    - base_hash：可选，写入基线版本号（就是 kb_read 返回的 `etag`）。传了服务端
      会做乐观锁校验：若这期间别人已改过这篇，返回 409 版本冲突并给出当前版本，
      **不落盘**；不传则保持旧行为（直接覆盖，兼容老调用方）。
      多 AG / 多会话同时改同一篇时必须传。
    """
    if READONLY:
        return readonly_error("kb_update")
    p = (path or "").strip().lstrip("/")
    if not p:
        return brief({"error": "需要 path（笔记相对路径，如 20-知识/运维/xxx.md）"})
    data = {
        "content": content,
        "archive_old": bool(archive_old),
        "preserve_frontmatter": True,
        "quality_check": bool(quality_check),
    }
    # 乐观锁：只有调用方显式给了版本号才带 base_hash 字段，
    # 不带时请求体与旧版完全一致（向后兼容，行为一字不改）。
    _bh = str(base_hash or "").strip()
    if _bh:
        data["base_hash"] = _bh
    # 2026-10-17：同上，更新也走查重，30 秒不够。
    if _bh:
        with _serialize_note_write(p):
            res = api("POST", f"/notes/{urllib.parse.quote(p)}", data, timeout=120)
    else:
        res = api("POST", f"/notes/{urllib.parse.quote(p)}", data, timeout=120)
    ok = "error" not in res
    # 版本冲突：后端 409 时把「当前版本 / 你提交的版本 / 下一步怎么做」讲清楚，
    # 不要只回一句 error 让调用方自己猜。
    if res.get("error") == "version_conflict" or res.get("http_status") == 409:
        res = _version_conflict(res, p)
        log_usage("kb_update", ok=False, path=p, reason="version_conflict")
        return brief(res, 12000)
    # 与 kb_create 同一套降级提示（2026-10-17），文案规则集中在 _dedup_notice()。
    _notice = _dedup_notice(res.get("dedup_degraded"))
    if _notice:
        res["dedup_notice"] = _notice
    log_usage("kb_update", ok=ok, path=p, reason=None if ok else res.get("error"))
    return brief(res, 12000)


# ── 想法收集箱（T-104，2026-10-24）──
# 一句话「记个想法 xxx」→ 追加进当天 30-记录/YYYY-MM-DD-待办.md 的「## 想法收集箱」小节。
# 原文照收，不要求先拆成完整任务；不改前端、不建模板。
_IDEA_HEADING = "## 想法收集箱"
_IDEA_TZ = "Asia/Shanghai"


def _idea_now() -> datetime:
    """本机当前时间，固定按 Asia/Shanghai 取（服务器时区可能是 UTC）。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(_IDEA_TZ))
    except Exception:
        log.warning("kb_idea 取 Asia/Shanghai 时区失败，退回本机时区", exc_info=True)
        return datetime.now()


def _idea_page_path(day: str) -> str:
    return f"30-记录/{day}-待办.md"


def _idea_insert(raw: str, entry: str) -> str:
    """把一条 `- HH:MM 原文` 插进「## 想法收集箱」小节末尾；没有该小节就新建。

    小节范围按「下一个一级/二级标题」界定，插在小节最后一个非空行之后，
    并保证小节与后续标题之间留一个空行。原文其余部分逐字保留。
    围栏代码块（``` / ~~~）里的 `##` 不算标题；整篇统一 CRLF 时按 CRLF 回写。
    """
    nl = "\n"
    if raw.count("\n") and raw.count("\r\n") == raw.count("\n"):
        nl = "\r\n"
        raw = raw.replace("\r\n", "\n")
    lines = raw.split("\n")

    def _headings():
        """返回所有「真标题」行号；跳过 frontmatter 与围栏代码块内部。

        围栏按 CommonMark 口径闭合：同种字符且长度 ≥ 开启长度才算闭合，
        否则 4 反引号块里嵌的 3 反引号会把它提前关掉，块内的 `##` 被当成标题。
        """
        out, fence = [], None
        start = 0
        # frontmatter：首行 --- 到下一个 --- 之间不是正文，里面的 `##` 一律不算标题。
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() in ("---", "..."):
                    start = i + 1
                    break
        for i in range(start, len(lines)):
            ln = lines[i]
            s = ln.lstrip()
            m = re.match(r"^(`{3,}|~{3,})", s)
            if m:
                run = m.group(1)
                if fence is None:
                    fence = run
                elif run[0] == fence[0] and len(run) >= len(fence):
                    fence = None
                continue
            if fence is None and re.match(r"^#{1,2}\s", ln):
                out.append(i)
        return out

    heads = _headings()
    idx = None
    for i in heads:
        if lines[i].strip() == _IDEA_HEADING:
            idx = i
            break
    if idx is None:
        # 没有小节：整篇末尾新建。文件末尾的空行先收掉，避免越积越多。
        return (raw.rstrip("\n") + "\n\n" + _IDEA_HEADING + "\n\n" + entry + "\n").replace("\n", nl)
    end = next((j for j in heads if j > idx), len(lines))
    body = lines[idx + 1:end]
    tail = lines[end:]
    while body and not body[-1].strip():
        body.pop()
    # 小节紧跟标题时（空小节）先补一个空行，避免条目贴在小标题下面。
    new_body = (body if body else [""]) + [entry]
    if tail and tail[0].strip():
        new_body.append("")
    return "\n".join(lines[:idx + 1] + new_body + tail).replace("\n", nl)


def _idea_line_no(raw: str, entry: str) -> int:
    """返回该条在全文中的行号（1 起）；同样原文出现多次时取最后一次（刚追加的那条）。

    统一按 LF 切行：CRLF 页面下 \r 会留在行尾，直接比对永远不相等。
    """
    lines = raw.replace("\r\n", "\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip("\r") == entry:
            return i + 1
    return 0


@mcp.tool()
def kb_idea(text: str) -> str:
    """记一个想法：一句话原文追加进当天待办页的「想法收集箱」。

    何时用：用户说「记个想法 xxx」「帮我记一下 xxx」这类**还没想清楚**的碎片，
    不要先追问、不要先改写成任务、不要另开新笔记，原文照收即可。

    落点固定 `30-记录/YYYY-MM-DD-待办.md`（Asia/Shanghai 当天）：
    - 页面不存在 → 自动新建（后端生成 frontmatter，正文为 `# YYYY-MM-DD 待办`）
    - 已有 `## 想法收集箱` → 追加到该小节末尾；没有 → 新建该小节
    - 每条格式固定 `- HH:MM 原文`

    并发：整段「读原文 → 改 → 带 etag 写回」都在 `_serialize_note_write()`
    里串行（进程内互斥 + 跨进程 flock），连说多条不会乱序或互相覆盖；
    写入带 `base_hash=etag`，被外部改动时按版本冲突重读重试，不静默覆盖。
    """
    if READONLY:
        return readonly_error("kb_idea")
    body = re.sub(r"\s*\n\s*", " ", (text or "").strip()).strip()
    if not body:
        return brief({"error": "需要 text（想法原文，一句话即可）"})
    now = _idea_now()
    day = now.strftime("%Y-%m-%d")
    entry = f"- {now.strftime('%H:%M')} {body}"
    p = _idea_page_path(day)
    quoted = urllib.parse.quote(p)

    last = {}
    last_get_err = ""
    # 读-改-写整段串行：连说 3 条时第 2 条必须看到第 1 条已落盘的内容。
    with _serialize_note_write(p):
        for attempt in range(3):
            res = api("GET", f"/notes/{quoted}")
            is_404 = res.get("http_status") == 404 or res.get("error") == "不存在"
            if res.get("error") and not is_404:
                # 超时 / 500 / 非 JSON：绝不能当成「页面不存在」继续往下走。
                # 之前这里会把 raw="" 送进 _idea_insert，再以空 base_hash 覆盖写，
                # 后端「不带版本号即放行」—— 当天页正文会被整段替换成一条想法。
                last_get_err = str(res.get("error"))
                log.warning("kb_idea 读取当天页失败，重试: %s (%s)", p, last_get_err)
                continue
            created = False
            if is_404:
                # 建页只放「标题 + 空小节」，本条统一走下面带 base_hash 的追加。
                # 好处：建页与追加不再各自写一次正文，回读失败重说也不会重复落库。
                page = f"# {day} 待办\n\n{_IDEA_HEADING}\n\n"
                res = api("POST", "/notes", {
                    "title": f"{day}-待办",
                    "content": page,
                    "type": "record",
                    "subdir": "",
                    "tags": ["待办", day],
                    "status": "active",
                    "quality_check": False,
                }, timeout=120)
                if "error" in res:
                    log_usage("kb_idea", ok=False, path=p, reason=res.get("error"))
                    return brief(res, 4000)
                if res.get("action") == "updated":
                    # 404 到 POST 之间别人刚建好当天页，这次建页已把它整页覆盖。
                    # 窗口极窄但不可逆，必须显式报错让人核对，不能只加一句 warning。
                    log_usage("kb_idea", ok=False, path=p, reason="create_race")
                    return brief({
                        "error": "create_race",
                        "path": p,
                        "message": "建页瞬间该页已被别人创建，本次建页覆盖了对方的正文。",
                        "hint": "请先 kb_read(path=%r) 核对当天页是否缺内容，必要时从备份恢复；本条未写入。" % p,
                    }, 4000)
                created = True
                res = api("GET", f"/notes/{quoted}")
                if res.get("error") or not (res.get("raw") or "").strip():
                    log_usage("kb_idea", ok=False, path=p, reason="create_verify_failed")
                    return brief({
                        "error": "create_verify_failed",
                        "path": p,
                        "message": "当天待办页已建好，但回读失败，本条还没写进去。",
                        "hint": "直接重说一次即可：重试会走追加分支，不会重复落库。",
                        "detail": res,
                    }, 4000)
            raw = res.get("raw") or ""
            etag = str(res.get("etag") or "").strip()
            if not raw.strip() or not etag:
                # 没有正文或没有版本号就不许写：空 base_hash 在后端等于「不校验」。
                last_get_err = "empty_body_or_etag"
                log.warning("kb_idea 读到空正文/空 etag，重试: %s", p)
                continue
            new_raw = _idea_insert(raw, entry)
            wr = api("POST", f"/notes/{quoted}", {
                "content": new_raw,
                "archive_old": False,
                "preserve_frontmatter": True,
                "quality_check": False,
                "base_hash": etag,
            }, timeout=120)
            if wr.get("http_status") == 409 or wr.get("error") == "version_conflict":
                # 锁外有人改过这篇（人工编辑 / 另一把锁的进程）：重读后重试。
                log.warning("kb_idea 版本冲突，重读重试: %s (attempt %d)", p, attempt + 1)
                continue
            if "error" in wr:
                log_usage("kb_idea", ok=False, path=p, reason=wr.get("error"))
                return brief(wr, 4000)
            # 写回后读一次：确认本条真的落盘（顺带拿到新 etag 与真实行号）。
            back = api("GET", f"/notes/{quoted}")
            if back.get("error"):
                # 写已经成功，只是回读失败 —— 报「失败」会让人重说一次、多出一条。
                log_usage("kb_idea", ok=True, path=p, line=0, created=created,
                          reason="verify_read_failed")
                return brief({
                    "ok": True,
                    "path": p,
                    "line": 0,
                    "entry": entry,
                    "created": created,
                    "section": _IDEA_HEADING,
                    "message": "已写入，但回读失败，行号未知。",
                    "hint": "不要重说：本条已经落库，用 kb_read(path=%r) 核对即可。" % p,
                    "verify_error": back.get("error"),
                }, 4000)
            line = _idea_line_no(back.get("raw") or "", entry)
            if not line:
                log_usage("kb_idea", ok=False, path=p, reason="verify_failed")
                return brief({
                    "error": "verify_failed",
                    "path": p,
                    "entry": entry,
                    "message": "写入返回成功，但回读没找到这一条。",
                    "hint": "先 kb_read 看当天页末尾再决定是否重说，别盲目重试（可能已落库）。",
                }, 4000)
            last = {"path": p, "line": line, "entry": entry, "created": created,
                    "section": _IDEA_HEADING, "etag": back.get("etag") or ""}
            break
        else:
            if last_get_err:
                log_usage("kb_idea", ok=False, path=p, reason=last_get_err)
                return brief({
                    "error": "read_failed",
                    "path": p,
                    "message": "连续 3 次读不到当天待办页（或读到空正文/空版本号），本条未写入。",
                    "reason": last_get_err,
                    "hint": "先确认知识库服务可用（/health），再重说一次。",
                }, 4000)
            log_usage("kb_idea", ok=False, path=p, reason="version_conflict")
            return brief({
                "error": "version_conflict",
                "path": p,
                "message": "当天待办页连续 3 次被别人改动，本条未落盘。",
                "hint": "稍后重说一次即可；若持续冲突，先 kb_read 看这篇是否有人在同时编辑。",
            }, 4000)
    log_usage("kb_idea", ok=True, path=p, line=last.get("line"), created=last.get("created"))
    last["ok"] = True
    return brief(last, 4000)


@mcp.tool()
def kb_create_project_log(project_name: str, goal: str, changed_files: str, key_implementation: str, verification: str, issues: str = "", next_steps: str = "", tags: str = "项目记录,开发日志", status: str = "active") -> str:
    """按固定模板创建项目开发记录。

    警告：同一天同一项目若已有「索引 + 专篇链接」版日更，禁止再调用本工具覆盖。
    大专题请写 专篇文件（文件名带后缀），日更只追加一行链接；或用 write 合并到现有日更。
    subdir 使用项目名本身，不要用「项目名 部署」。
    """
    if READONLY:
        return readonly_error("kb_create_project_log")
    # 轻量提示：同日文件已存在时仍会走 create 覆盖路径，先查再写
    today = datetime.now().strftime("%Y-%m-%d")
    safe_name = re.sub(r"\s+", "_", project_name.strip())
    guess_paths = [
        f"10-项目/{project_name}/{project_name}_开发记录_{today}.md",
        f"10-项目/{safe_name}/{safe_name}_开发记录_{today}.md",
        f"10-项目/{project_name}/{safe_name}_开发记录_{today}.md",
    ]
    for gp in guess_paths:
        existing = api("GET", f"/notes/{urllib.parse.quote(gp)}")
        if isinstance(existing, dict) and existing.get("content") and "error" not in existing:
            body = existing.get("content") or ""
            if "专篇" in body or "索引" in body or "| 专篇 |" in body or "链接" in body[:800]:
                res = {
                    "error": "same_day_index_daily_exists",
                    "path": gp,
                    "hint": "同日日更已是索引版，禁止 kb_create_project_log 覆盖。请写专篇或 write 合并。见 skill §9 / kb-doc-convergence。",
                }
                log_usage("kb_create_project_log", ok=False, reason="same_day_index", path=gp)
                return brief(res)
    title = f"{project_name} 开发记录 {today}"
    content = f"""## 背景

本记录用于沉淀 `{project_name}` 本次开发、调试或配置变更的过程，避免后续遗忘关键决策和验证方式。

## 关键发现

### 本次目标

{goal.strip()}

### 修改文件 / 相关路径

{changed_files.strip()}

### 关键实现

{key_implementation.strip()}

## 实施步骤

1. 明确本次目标和影响范围。
2. 检查相关文件、配置、服务状态或接口行为。
3. 按最小变更原则修改实现或配置。
4. 执行验证命令，确认功能可用。
5. 将结果写入知识库并重建/检查索引。

## 验证结果

{verification.strip()}

## 遇到的问题

{issues.strip() or "本次未记录明显阻塞问题。"}

## 后续建议

{next_steps.strip() or "继续观察实际使用效果；如果复现率不足，补充更严格的自动化规则和监控。"}
"""
    return kb_create(title=title, content=content, note_type="project", subdir=project_name, tags=tags, status=status, quality_check=True)


@mcp.tool()
def kb_note_hygiene(quiet_ok: bool = False) -> str:
    """扫描笔记卫生问题（只报告不删除）。

    检测：精确双份、同名多路径、近重复、空壳、skill dump、50-档案根散落、
    废弃路径、项目双目录（含部署后缀）、测试渣。
    有问题返回清单；无问题且 quiet_ok=true 时返回 OK。
    处理前须用户确认；默认不归档、不 push。
    """
    import subprocess
    import sys as _sys

    _scripts_dir = Path(__file__).resolve().parent
    script = _scripts_dir / "kb_note_hygiene.py"
    if not script.exists():
        return brief({"error": "hygiene script missing", "path": str(script)})
    cmd = [_sys.executable, str(script)]
    if quiet_ok:
        cmd.append("--quiet-ok")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(_scripts_dir))
        out = (p.stdout or "").strip() or (p.stderr or "").strip() or ("OK" if p.returncode == 0 else "scan_failed")
        log_usage("kb_note_hygiene", ok=p.returncode in (0, 1), exit_code=p.returncode)
        if p.returncode == 0 and quiet_ok and not out:
            return "OK — 无卫生问题"
        return out if out else f"exit={p.returncode}"
    except Exception as e:
        log_usage("kb_note_hygiene", ok=False, reason=str(e))
        return brief({"error": str(e)})


@mcp.tool()
def kb_memory_boundary_check(summary: str, task_type: str = "general") -> str:
    """判断一段任务总结应该写入知识库、建议写入长期记忆，还是两者都需要。

    注意：本工具不直接写长期记忆，只给出边界判断和候选记忆，避免污染长期记忆。
    """
    text = (summary or "").strip()
    low = text.lower()
    memory_keywords = ["偏好", "习惯", "以后", "长期", "默认", "喜欢", "不喜欢", "记住", "用户要求", "每次", "优先"]
    kb_keywords = ["实现", "修改", "文件", "接口", "api", "mcp", "配置", "验证", "部署", "脚本", "bug", "项目", "日志"]
    write_memory = any(k in text for k in memory_keywords)
    write_kb = len(text) >= 120 or any(k in low for k in kb_keywords) or task_type in ("project", "development", "research")
    if len(text) < 30:
        write_memory = False
        write_kb = False
    note_type = "project" if task_type in ("project", "development") or any(k in text for k in ["修改", "实现", "验证", "部署", "文件"]) else "knowledge"
    candidate = ""
    if write_memory:
        candidate = text[:280]
        # 候选记忆应该是稳定事实，不要塞大段项目细节
        candidate = candidate.replace("\n", " ")
    res = {
        "write_to_kb": bool(write_kb),
        "suggest_kb_tool": "kb_create_project_log" if note_type == "project" else "kb_create",
        "kb_note_type": note_type,
        "suggest_memory_update": bool(write_memory),
        "memory_candidate": candidate,
        "do_not_write_memory_when": [
            "只是临时调试输出",
            "一次性路径/日志/报错且没有长期价值",
            "包含 token、密码、私钥、cookie 等敏感内容",
            "大段项目文档，应进入知识库而不是长期记忆",
        ],
        "rule": "长期偏好/稳定事实 → 长期记忆候选；项目过程/实现细节/验证结果 → 知识库笔记。",
    }
    log_usage("kb_memory_boundary_check", ok=True, task_type=task_type, write_to_kb=res["write_to_kb"], suggest_memory_update=res["suggest_memory_update"])
    return brief(res, 8000)


@mcp.tool()
def kb_ask(question: str) -> str:
    """基于知识库进行问答。需要综合多篇笔记时调用。

    服务端提示词已约束：只用库内事实、路径必须真实、冲突时标日期取新、
    区分实测与推测、无答案直接说没有、遇到「本文只做指针」要指向主文。

    注意：它只能读索引里的笔记，读不到项目代码仓里的主文（$KB_ROOT/<项目>/docs/）。
    项目实现细节别只靠它，配合 kb_read 或直接读文件。
    """
    res = api("POST", "/api/llm/ask", {"q": question}, timeout=60)
    log_usage("kb_ask", ok="error" not in res, question=question)
    return brief(res, 20000)


@mcp.tool()
def kb_index_status() -> str:
    """查看知识库索引状态。"""
    res = api("GET", "/api/index/status")
    log_usage("kb_index_status", ok="error" not in res)
    return brief(res)


@mcp.tool()
def kb_index_rebuild() -> str:
    """重建知识库搜索索引。创建/批量修改笔记后可调用。"""
    if READONLY:
        return readonly_error("kb_index_rebuild")
    res = api("POST", "/api/index/rebuild", {})
    log_usage("kb_index_rebuild", ok="error" not in res)
    return brief(res)


@mcp.tool()
def kb_dashboard() -> str:
    """查看知识库统计概览。"""
    res = api("GET", "/dashboard")
    log_usage("kb_dashboard", ok="error" not in res)
    return brief(res)


@mcp.tool()
def kb_upload(
    file_path: str = "",
    target_dir: str = "_assets",
    note_type: str = "",
    content: str = "",
    content_base64: str = "",
    filename: str = "",
) -> str:
    """上传文件到知识库。

    三种输入（优先级：content_base64 > content > file_path）：
    1) 本机路径：file_path=MCP 主机本地文件
    2) 文本内容：content + filename（远端 SSE 推荐）
    3) 二进制：content_base64 + filename（远端 SSE 推荐）

    注意：file_path 读的是 MCP 进程所在机器，不是用户电脑路径。

    ⚠️ 写笔记优先用 kb_create（覆盖同路径、带质量与卫生闸门）。
    本工具是「传文件」语义，同 basename 会另存 → **容易造出平行副本变成新的重复**。
    只有下面两种情况用它：① 传 pdf/图片等二进制；② kb_create 被章节检查卡住，
    正文已写好、只想落盘（此时仍要先 kb_search 确认没有同名主文）。
    """
    if READONLY:
        return readonly_error("kb_upload")

    max_bytes = int(os.environ.get("KB_MCP_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024)))
    raw = None
    name = (filename or "").strip()
    mode = ""

    if content_base64 and str(content_base64).strip():
        mode = "base64"
        try:
            raw = base64.b64decode(str(content_base64).strip(), validate=False)
        except Exception as e:
            log_usage("kb_upload", ok=False, reason="bad_base64", error=str(e))
            return brief({"error": f"content_base64 解码失败: {e}"})
        if not name:
            name = "upload.bin"
    elif content is not None and str(content) != "":
        mode = "content"
        raw = str(content).encode("utf-8")
        if not name:
            name = "note.md"
    if raw is None and file_path:
        mode = "path"
        fp = Path(file_path)
        if not fp.exists() or not fp.is_file():
            log_usage("kb_upload", ok=False, file_path=file_path, reason="missing")
            return brief({
                "error": f"文件不存在或不是普通文件: {file_path}",
                "hint": "远端 SSE 请改用 content / content_base64 + filename；file_path 仅限 MCP 主机本地盘",
            })
        raw = fp.read_bytes()
        if not name:
            name = fp.name

    if raw is None:
        log_usage("kb_upload", ok=False, reason="no_input")
        return brief({
            "error": "请提供 file_path，或 content/content_base64 + filename",
            "hint": "远端客户端优先 content_base64/content，不要传用户本机绝对路径",
        })

    # sanitize filename
    name = Path(name).name.replace("\0", "")
    if not name or name in (".", ".."):
        name = "upload.bin"
    if len(raw) > max_bytes:
        log_usage("kb_upload", ok=False, reason="too_large", size=len(raw))
        return brief({"error": f"文件过大: {len(raw)} bytes，上限 {max_bytes}"})

    res = _multipart_upload(name, raw, target_dir=target_dir or "", note_type=note_type or "")
    log_usage(
        "kb_upload",
        ok="error" not in res,
        mode=mode,
        filename=name,
        size=len(raw),
        target_dir=target_dir,
        file_path=file_path or None,
    )
    return brief(res)


if __name__ == "__main__":
    # ── 命令行直呼（T-104，2026-10-24）──
    # 用途：QQ 侧/脚本侧没有 MCP 调用通路时，也能一句话落库：
    #   python3 scripts/kb_mcp_server.py --idea "明天想想追龙门槛"
    # 走的是同一个 kb_idea()，所以串行化、乐观锁、落点规则与 MCP 调用完全一致。
    if len(sys.argv) > 1 and sys.argv[1] in ("--idea", "-i"):
        # 未加引号的多个词（--idea 明天 想想追龙门槛）要整句收下，不能只取第一段。
        _txt = " ".join(sys.argv[2:]).strip()
        if not _txt:
            print("用法: python3 scripts/kb_mcp_server.py --idea \"想法原文\"", file=sys.stderr)
            raise SystemExit(2)
        _out = kb_idea(_txt)
        print(_out)
        # 失败要给脚本一个非零退出码，否则 QQ 侧「发出去了但没落库」会被当成功。
        try:
            _bad = bool(json.loads(_out).get("error"))
        except Exception:
            _bad = False
        raise SystemExit(1 if _bad else 0)

    # 传输层选择（2026-10-17）：KB_MCP_TRANSPORT = sse | streamable-http | both | stdio
    #   默认 stdio —— 绝大多数客户端（Hermes / DSH / Claude Desktop）都是 stdio 拉起。
    #   默认 both 会让进程去监听 8897/8898 而不是走 stdio，客户端直接连不上。
    #   要对外提供 HTTP 必须显式设 KB_MCP_TRANSPORT=sse|streamable-http|both。
    #   鉴权：走 build_mcp_auth()，空令牌表时抛 RuntimeError 并拒绝启动
    #   （由 kb_mcp_sse.py 同款策略保证：绝不带无鉴权继续监听）。
    #   端口：sse 用 KB_MCP_SSE_HOST/KB_MCP_SSE_PORT（默认 127.0.0.1:8898）；
    #         streamable-http 用 KB_MCP_HTTP_HOST/KB_MCP_HTTP_PORT（默认 127.0.0.1:8897）。
    _transport = (os.environ.get("KB_MCP_TRANSPORT", "stdio") or "stdio").strip().lower()
    if _transport in ("stdio",):
        mcp.run(transport="stdio")
        raise SystemExit(0)
    if _transport not in ("sse", "streamable-http", "both"):
        raise SystemExit(
            f"KB_MCP_TRANSPORT 取值非法: {_transport!r}"
            "（可选 sse | streamable-http | both | stdio）")

    # 空令牌表 / 鉴权组件缺失 → 这里就抛错退出，不留裸奔窗口
    _auth = build_mcp_auth()
    if _auth is not None:
        mcp.auth = _auth

    _SSE_HOST = os.environ.get("KB_MCP_SSE_HOST", "127.0.0.1")
    _SSE_PORT = int(os.environ.get("KB_MCP_SSE_PORT", "8898"))
    _HTTP_HOST = os.environ.get("KB_MCP_HTTP_HOST", "127.0.0.1")
    _HTTP_PORT = int(os.environ.get("KB_MCP_HTTP_PORT", "8897"))

    def _serve(tp, host, port):
        """单条 HTTP 传输，独立事件循环（互不阻塞）。"""
        import asyncio
        tag = "Bearer 鉴权已启用" if _auth is not None else "鉴权关闭"
        print(f"📚 知识库 MCP {tp} → http://{host}:{port}/ （{tag}）", flush=True)
        asyncio.run(mcp.run_http_async(transport=tp, host=host, port=port))

    if _transport == "sse":
        _serve("sse", _SSE_HOST, _SSE_PORT)
    elif _transport == "streamable-http":
        _serve("streamable-http", _HTTP_HOST, _HTTP_PORT)
    else:  # both
        import threading
        _jobs = [("sse", _SSE_HOST, _SSE_PORT),
                 ("streamable-http", _HTTP_HOST, _HTTP_PORT)]
        _ths = []
        for _tp, _h, _p in _jobs:
            _t = threading.Thread(target=_serve, args=(_tp, _h, _p), daemon=True)
            _t.start()
            _ths.append(_t)
        for _t in _ths:
            _t.join()
