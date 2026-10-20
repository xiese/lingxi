#!/usr/bin/env python3
"""Knowledge Base MCP server.

Wraps the Knowledge Base REST API as MCP tools.
"""
import json
import logging
import base64
import os
import mimetypes
import urllib.parse
import urllib.request
import urllib.error
import re
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

# 记忆检索辅助已拆到 kb_mcp_memory.py（2026-09-13）；确保 scripts/ 在 sys.path
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_mcp_memory import memory_search_raw, format_memory_markdown

_KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)
TOKEN_FILE = Path(os.environ.get("KB_TOKEN_FILE", _KB_ROOT / ".api_tokens.json"))
USAGE_LOG = Path(os.environ.get("KB_MCP_USAGE_LOG", _KB_ROOT / ".kb_mcp_usage.jsonl"))
CLIENT_NAME = os.environ.get("KB_CLIENT_NAME", "unknown").strip() or "unknown"
READONLY = os.environ.get("KB_MCP_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


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
    # 1) FastMCP access token (StaticTokenVerifier puts raw token string here)
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
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>"
    return text


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
    """Build StaticTokenVerifier token map from KB_TOKEN env + .api_tokens.json."""
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
        for tk, info in (data.get("tokens") or {}).items():
            if not tk:
                continue
            # skip expired
            exp = (info or {}).get("expires_at")
            if exp:
                try:
                    from datetime import datetime as _dt
                    if _dt.fromisoformat(str(exp).replace("Z", "")) < _dt.now():
                        continue
                except Exception:
                    log.warning("kb_mcp_server.py 异常", exc_info=True)
            perm = str((info or {}).get("permission", "r")).lower()
            scopes = ["kb:r", "kb:rw"] if perm in ("rw", "master") else ["kb:r"]
            name = (info or {}).get("name") or (info or {}).get("device") or "kb-token"
            tokens[tk] = {"client_id": str(name)[:64], "scopes": scopes}
    except Exception:
        log.warning("kb_mcp_server.py 异常", exc_info=True)
    return tokens


def build_mcp_auth():
    """SSE 远程鉴权：校验 Bearer 是否为知识库有效 Token。stdio 默认不启用。"""
    flag = os.environ.get("KB_MCP_SSE_AUTH", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None
    try:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    except Exception as e:
        # 安全加固：原先此处 return None，等于「想开鉴权却开不了时
        # 裸奔启动」，且与用户显式设 KB_MCP_SSE_AUTH=0 的返回值无法区分。改为抛错。
        raise RuntimeError(
            f"SSE 鉴权已启用（KB_MCP_SSE_AUTH 默认 1）但 fastmcp 鉴权组件导入失败: {e}"
        ) from e
    tokens = _load_sse_auth_tokens()
    if not tokens:
        # 同上：空令牌表绝不能降级成无鉴权。要求先由 kb-server 生成主令牌，
        # 或由使用者显式设 KB_MCP_SSE_AUTH=0 表示「我知道这是无鉴权」。
        raise RuntimeError(
            "SSE 鉴权已启用（KB_MCP_SSE_AUTH 默认 1）但未加载到任何令牌。"
            "请先启动 kb-server 生成主令牌，或显式设置 KB_MCP_SSE_AUTH=0 关闭鉴权。"
        )
    # require at least read scope
    return StaticTokenVerifier(tokens=tokens, required_scopes=["kb:r"])


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
    """读取知识库中的一篇笔记。"""
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


@mcp.tool()
def kb_create(title: str, content: str, note_type: str = "knowledge", subdir: str = "", tags: str = "", status: str = "draft", quality_check: bool = True) -> str:
    """创建或更新知识库笔记。

    写完项目总结、研究记录、调试复盘、实现方案后必须调用本工具入库。
    默认启用质量检查；project/knowledge 笔记过短或缺少章节会被拒绝。
    质量/卫生门禁由 REST 单源执行；MCP 只转发并透传错误。

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
    log_usage(
        "kb_create",
        ok=ok,
        title=title,
        path=res.get("path"),
        note_type=note_type,
        reason=None if ok else res.get("error"),
    )
    return brief(res, 12000)


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
    """基于知识库进行问答。需要综合多篇笔记时调用。"""
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
    # 传输层选择（2026-10-17）：KB_MCP_TRANSPORT = sse | streamable-http | both | stdio
    #   默认 both —— 两条 HTTP 传输同时开，客户端按能力自选。
    #   鉴权：走 build_mcp_auth()，空令牌表时抛 RuntimeError 并拒绝启动
    #   （由 kb_mcp_sse.py 同款策略保证：绝不带无鉴权继续监听）。
    #   端口：sse 用 KB_MCP_SSE_HOST/KB_MCP_SSE_PORT（默认 127.0.0.1:8898）；
    #         streamable-http 用 KB_MCP_HTTP_HOST/KB_MCP_HTTP_PORT（默认 127.0.0.1:8897）。
    _transport = (os.environ.get("KB_MCP_TRANSPORT", "both") or "both").strip().lower()
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
