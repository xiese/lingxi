"""知识库 API — MCP / 技能集成路由（平台无关）"""

import io, json, os, re, zipfile
import logging
from datetime import datetime
from pathlib import Path
from flask import request, jsonify, send_file, Response

from ..routes import bp
from ..config import KB_ROOT, VERSION
from ..auth import check_auth


log = logging.getLogger("kb.routes_integrations")

# SSE 接入地址：默认本机；对外提供服务时设置 KB_MCP_SSE_URL
def _mcp_sse_url():
    return os.environ.get("KB_MCP_SSE_URL", "http://127.0.0.1:8898/sse")

def _kb_mcp_tools_schema():
    """知识库 MCP 工具 Schema（OpenAI Function Calling 格式）"""
    return [
        {"type": "function", "function": {
            "name": "kb_dual_search",
            "description": "记忆 + 知识库双重检索。回答用户问题前优先使用。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 6}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "memory_search",
            "description": "搜索长期记忆库中已记住的用户偏好与事实。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 6}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "kb_search",
            "description": "搜索结构化知识库笔记。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}, "mode": {"type": "string", "enum": ["semantic", "keyword"], "default": "semantic"}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "kb_read",
            "description": "读取知识库中的一篇笔记。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        }},
        {"type": "function", "function": {
            "name": "kb_list",
            "description": "列出知识库笔记，支持目录、limit、offset。",
            "parameters": {"type": "object", "properties": {"directory": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}}
        }},
        {"type": "function", "function": {
            "name": "kb_create",
            "description": "创建/更新知识库笔记；RW 平台使用。",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "note_type": {"type": "string", "default": "knowledge"}, "subdir": {"type": "string"}, "tags": {"type": "string"}, "quality_check": {"type": "boolean", "default": True}}, "required": ["title", "content"]}
        }},
        {"type": "function", "function": {
            "name": "kb_create_project_log",
            "description": "按固定模板创建项目开发记录；RW 平台使用。",
            "parameters": {"type": "object", "properties": {"project_name": {"type": "string"}, "goal": {"type": "string"}, "changed_files": {"type": "string"}, "key_implementation": {"type": "string"}, "verification": {"type": "string"}, "issues": {"type": "string"}, "next_steps": {"type": "string"}, "tags": {"type": "string"}, "status": {"type": "string"}}, "required": ["project_name", "goal", "changed_files", "key_implementation", "verification"]}
        }},
        {"type": "function", "function": {
            "name": "kb_memory_boundary_check",
            "description": "判断任务总结应写入知识库、长期记忆，还是两者都需要。",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "task_type": {"type": "string", "default": "general"}}, "required": ["summary"]}
        }},
        {"type": "function", "function": {
            "name": "kb_ask",
            "description": "基于知识库进行问答。",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}
        }},
        {"type": "function", "function": {
            "name": "kb_index_status",
            "description": "查看知识库索引状态。",
            "parameters": {"type": "object", "properties": {}}
        }},
        {"type": "function", "function": {
            "name": "kb_index_rebuild",
            "description": "重建知识库搜索索引；RW 平台使用。",
            "parameters": {"type": "object", "properties": {}}
        }},
        {"type": "function", "function": {
            "name": "kb_dashboard",
            "description": "查看知识库统计概览。",
            "parameters": {"type": "object", "properties": {}}
        }},
        {"type": "function", "function": {
            "name": "kb_upload",
            "description": "上传文件到知识库；RW 平台使用。优先 content/content_base64+filename（远端 SSE）；file_path 仅限 MCP 主机本地盘。",
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "MCP 主机本地路径（远端客户端勿用）"},
                "content": {"type": "string", "description": "文本内容（与 content_base64 二选一）"},
                "content_base64": {"type": "string", "description": "Base64 二进制内容"},
                "filename": {"type": "string", "description": "目标文件名，content/base64 模式必填建议"},
                "target_dir": {"type": "string", "default": "_assets"},
                "note_type": {"type": "string", "default": ""}
            }, "required": []}
        }},
    ]


_SECRET_KEYS = ("token", "key", "secret", "password", "credential", "authorization", "llm_key", "embedding_key")
_PRIVATE_PATH_MARKERS = (
    ".api_tokens.json", ".kb_settings.json", ".kb_mcp_usage.jsonl",
    ".sqlite3", ".pkl", ".npy", ".index",
)
_WRITE_TOOLS = {"kb_create", "kb_create_project_log", "kb_index_rebuild", "kb_upload"}
_PRIVATE_MEMORY_TOOLS = {"memory_search", "kb_dual_search", "kb_memory_boundary_check"}
_DEFAULT_SHARE_TOOLS = {"kb_search", "kb_read", "kb_list", "kb_ask", "kb_index_status", "kb_dashboard"}


def _redact_secret_value(key, value):
    if not isinstance(key, str) or not isinstance(value, str):
        return value
    lk = key.lower()
    if any(s in lk for s in _SECRET_KEYS) and value:
        if re.fullmatch(r"\$\{[A-Z0-9_]+\}", value):
            return value
        return "<REDACTED>"
    return value


def _redact_private_path(value):
    if not isinstance(value, str):
        return value
    redacted = value.replace(str(KB_ROOT), "${KB_ROOT}")
    redacted = redacted.replace(str(Path.home() / ".kb-skills"), "${SKILLS_HOME}")
    if any(marker in redacted for marker in _PRIVATE_PATH_MARKERS):
        if "${KB_ROOT}" in redacted or "${SKILLS_HOME}" in redacted:
            return redacted
        return "${LOCAL_PRIVATE_PATH}"
    if redacted.startswith(str(Path.home()) + "/"):
        return redacted.replace(str(Path.home()), "${HOME}", 1)
    return redacted


def _redact_obj(obj, key=""):
    obj = _redact_secret_value(key, obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, key) for v in obj]
    return _redact_private_path(obj)


def _share_tool_policy():
    tools = []
    for item in _kb_mcp_tools_schema():
        fn = item.get("function", {})
        name = fn.get("name", "")
        if name in _WRITE_TOOLS:
            mode, risk, share_default = "write", "write_operation", False
        elif name in _PRIVATE_MEMORY_TOOLS:
            mode, risk, share_default = "read", "private_memory", False
        else:
            mode = "read"
            risk = "low" if name in _DEFAULT_SHARE_TOOLS else "review_required"
            share_default = name in _DEFAULT_SHARE_TOOLS
        tools.append({
            "name": name,
            "description": fn.get("description", ""),
            "mode": mode,
            "risk": risk,
            "share_default": share_default,
        })
    return tools


def _build_sanitized_mcp_config(platform="generic", readonly=True):
    platform = re.sub(r"[^a-zA-Z0-9_.-]", "-", (platform or "generic"))[:64] or "generic"
    cfg = {
        "mcpServers": {
            "personal-knowledge-base": {
                "command": "${PYTHON}",
                "args": ["${KB_ROOT}/scripts/kb_mcp_server.py"],
                "env": {
                    "KB_HOST": "${KB_HOST}",
                    "KB_TOKEN": "${KB_READONLY_TOKEN}" if readonly else "${KB_WRITE_TOKEN}",
                    "KB_CLIENT_NAME": platform,
                    "KB_MCP_READONLY": "true" if readonly else "false",
                },
            }
        },
        "share_policy": {
            "readonly": readonly,
            "default_share_artifact": bool(readonly),
            "risk_level": "low" if readonly else "write",
        },
    }
    if not readonly:
        cfg["share_policy"]["message"] = "读写模板不属于默认分享产物，使用前必须确认接收方权限边界。"
    return _redact_obj(cfg)



def _build_sanitized_sse_config(platform="generic", readonly=True):
    platform = re.sub(r"[^a-zA-Z0-9_.-]", "-", (platform or "generic"))[:64] or "generic"
    cfg = {
        "mcpServers": {
            "personal-knowledge-base": {
                "url": _mcp_sse_url(),
                "headers": {
                    "Authorization": "Bearer ${KB_READONLY_TOKEN}" if readonly else "Bearer ${KB_WRITE_TOKEN}",
                },
            }
        },
        "share_policy": {
            "readonly": readonly,
            "transport": "sse",
            "auth": "bearer",
            "default_share_artifact": bool(readonly),
            "risk_level": "low" if readonly else "write",
            "message": "SSE 必须传 Bearer Token；远端上传请用 kb_upload(content/content_base64)，不要传本机 file_path。",
        },
        "platform": platform,
    }
    return _redact_obj(cfg)


def _build_share_manifest():
    return _redact_obj({
        "schema_version": "kb-share-manifest.v1",
        "name": "personal-knowledge-base-integration",
        "version": VERSION,
        "generated_at": datetime.now().isoformat(),
        "privacy": {
            "project_type": "private",
            "default_readonly": True,
            "includes_tokens": False,
            "includes_private_paths": False,
            "includes_settings": False,
            "includes_logs": False,
            "includes_private_databases": False,
            "includes_full_skills": False,
            "license_included": False,
        },
        "protocols": {
            "mcp_stdio": {"enabled": True, "default_readonly": True},
            "mcp_sse": {
                "enabled": True,
                "default_readonly": True,
                "url": _mcp_sse_url(),
                "auth": "Bearer <KB_TOKEN>",
                "note": "v1.0.0 起 SSE 默认启用 StaticTokenVerifier；客户端需在 Authorization 头传知识库 Token",
            },
            "openai_tools_schema": {"enabled": True, "default_readonly": True},
        },
        "tools": _share_tool_policy(),
        "skills": {"included": False, "status": "deferred", "reason": "技能目录默认属于私有内容"},
        "artifacts": {
            "manifest": "/api/integrations/share/manifest",
            "config_preview": "/api/integrations/share/config-preview?platform=claude&readonly=true",
            "sse_config_preview": "/api/integrations/share/config-preview?platform=claude&readonly=true&transport=sse",
            "sensitive_scan": "/api/integrations/share/scan",
            "download": "/api/integrations/share/download?artifact=manifest|config|scan|zip",
        },
    })


def _scan_sensitive_text(label, text):
    blockers, warnings = [], []
    blocker_patterns = [
        (r"(?:/home|/root|/Users|/srv|/opt)/\S+", "private_abs_path", "发现本机绝对路径"),
        (r"\.api_tokens\.json", "token_file", "发现 token 文件路径"),
        (r"\.kb_settings\.json", "settings_file", "发现 settings 文件路径"),
        (r"\.kb_mcp_usage\.jsonl", "usage_log", "发现 MCP 使用日志路径"),
        (r"\bllm_key\b", "llm_key", "发现 LLM 密钥字段"),
        (r"\bembedding_key\b", "embedding_key", "发现 Embedding 密钥字段"),
        (r"Authorization\s*:\s*Bearer\s+[^\s\"']+", "authorization_bearer", "发现 Authorization Bearer 值"),
        (r"\.(sqlite3|pkl|npy|index)\b", "private_data_file", "发现私有数据库或索引文件"),
    ]
    warning_patterns = [
        (r"\$\{KB_READONLY_TOKEN\}|\$\{KB_WRITE_TOKEN\}|\bKB_TOKEN\b", "token_placeholder", "包含 token 占位符，分享前不要填入真实值"),
        (r"\$\{KB_HOST\}|\bKB_HOST\b", "host_placeholder", "包含服务地址占位符"),
        (r"127\.0\.0\.1|localhost|0\.0\.0\.0", "local_host", "包含本地或监听地址"),
        (r"kb_create|kb_upload|kb_index_rebuild|kb_create_project_log", "write_tool", "包含写入工具名称，默认分享配置应保持只读"),
        (r"KB_MEMORY_DB|KB_MEMORY_FILES|KB_TOKEN_FILE", "private_env_name", "包含私有路径相关环境变量名"),
    ]
    for pattern, kind, message in blocker_patterns:
        if re.search(pattern, text, re.I):
            blockers.append({"target": label, "severity": "blocker", "kind": kind, "message": message})
    for pattern, kind, message in warning_patterns:
        if re.search(pattern, text, re.I):
            warnings.append({"target": label, "severity": "warning", "kind": kind, "message": message})
    return blockers, warnings


def _scan_share_artifacts():
    artifacts = {
        "manifest": _build_share_manifest(),
        "config_preview": _build_sanitized_mcp_config("claude", True),
        "openai_tools_schema": _redact_obj(_kb_mcp_tools_schema()),
    }
    findings = []
    artifact_blockers = {}
    for label, obj in artifacts.items():
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        blockers, warnings = _scan_sensitive_text(label, text)
        findings.extend(blockers + warnings)
        artifact_blockers[label] = len(blockers)

    src = KB_ROOT / "scripts" / "kb_mcp_server.py"
    if src.exists():
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
            blockers, warnings = _scan_sensitive_text("kb_mcp_server.py", text)
            if blockers:
                findings.append({
                    "target": "kb_mcp_server.py",
                    "severity": "blocker",
                    "kind": "source_contains_private_defaults",
                    "message": "MCP server 源文件包含本机默认路径，第一阶段不作为分享资产。",
                })
            else:
                findings.extend(warnings)
        except Exception:
            log.debug("routes_integrations.py 异常: _scan_share_artifacts", exc_info=True)
            findings.append({
                "target": "kb_mcp_server.py",
                "severity": "warning",
                "kind": "source_scan_failed",
                "message": "未能扫描 MCP server 源文件。",
            })

    blocker_count = sum(1 for f in findings if f["severity"] == "blocker")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")
    return {
        "passed": artifact_blockers.get("manifest", 0) == 0 and artifact_blockers.get("config_preview", 0) == 0,
        "summary": {"blockers": blocker_count, "warnings": warning_count},
        "findings": findings,
        "download_allowed": {
            "manifest": artifact_blockers.get("manifest", 0) == 0,
            "config": artifact_blockers.get("config_preview", 0) == 0,
            "scan": True,
            "mcp_server_source": False,
            "bundle": False,
        },
    }


def _json_attachment(obj, filename):
    obj = _redact_obj(obj)
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    blockers, _ = _scan_sensitive_text(filename, text)
    if blockers:
        return jsonify({"error": "敏感扫描未通过，下载已阻止", "findings": blockers}), 409
    return Response(
        text + "\n",
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/api/integrations")
def integrations_overview():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    return jsonify(_redact_obj({
        "name": "个人知识库 MCP / Skills 分享中心",
        "description": "第一阶段提供 manifest、只读 MCP 配置预览和敏感扫描；不导出完整 skills、私有数据库、日志、settings 或真实 token。",
        "mcp_server": "${KB_ROOT}/scripts/kb_mcp_server.py",
        "download_api": "/api/integrations/download/kb_mcp_server.py",
        "openai_tools_schema_api": "/api/integrations/openai-tools-schema",
        "mcp_config_api": "/api/integrations/mcp-config?platform=claude&readonly=true",
        "share_manifest_api": "/api/integrations/share/manifest",
        "share_config_preview_api": "/api/integrations/share/config-preview?platform=claude&readonly=true",
        "share_scan_api": "/api/integrations/share/scan",
        "share_download_api": "/api/integrations/share/download?artifact=manifest|config|scan|zip",
        "usage_log": "not_included_in_share_artifacts",
        "tools": [x["function"]["name"] for x in _kb_mcp_tools_schema()],
        "tool_policy": _share_tool_policy(),
        "configs": {
            "readonly": _build_sanitized_mcp_config("claude", True),
            "readwrite": _build_sanitized_mcp_config("cursor-local", False),
        },
        "share_phase": "phase_1_preview_only",
    }))


@bp.route("/api/integrations/openai-tools-schema")
def integrations_openai_schema():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    return jsonify(_kb_mcp_tools_schema())


@bp.route("/api/integrations/mcp-config")
def integrations_mcp_config():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    platform = request.args.get("platform", "generic")
    readonly = request.args.get("readonly", "true").lower() not in ("0", "false", "no", "off")
    return jsonify(_build_sanitized_mcp_config(platform, readonly))


@bp.route("/api/integrations/share/manifest")
def integrations_share_manifest():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    return jsonify(_build_share_manifest())


@bp.route("/api/integrations/share/config-preview")
def integrations_share_config_preview():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    platform = request.args.get("platform", "generic")
    readonly = request.args.get("readonly", "true").lower() not in ("0", "false", "no", "off")
    transport = request.args.get("transport", "stdio").lower()
    if transport == "sse":
        return jsonify(_build_sanitized_sse_config(platform, readonly))
    return jsonify(_build_sanitized_mcp_config(platform, readonly))


@bp.route("/api/integrations/share/scan")
def integrations_share_scan():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    return jsonify(_scan_share_artifacts())


@bp.route("/api/integrations/share/download")
def integrations_share_download():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    artifact = request.args.get("artifact", "manifest")
    if artifact == "manifest":
        return _json_attachment(_build_share_manifest(), "kb-share-manifest.json")
    if artifact == "config":
        platform = request.args.get("platform", "claude")
        readonly = request.args.get("readonly", "true").lower() not in ("0", "false", "no", "off")
        transport = request.args.get("transport", "stdio").lower()
        if transport == "sse":
            return _json_attachment(_build_sanitized_sse_config(platform, readonly), "kb-mcp-sse-config-preview.json")
        return _json_attachment(_build_sanitized_mcp_config(platform, readonly), "kb-mcp-config-preview.json")
    if artifact == "scan":
        return _json_attachment(_scan_share_artifacts(), "kb-share-sensitive-scan.json")
    if artifact == "zip":
        return _share_zip_attachment()
    return jsonify({"error": "未知分享产物", "allowed": ["manifest", "config", "scan", "zip"]}), 400


def _share_zip_attachment():
    """打包脱敏分享产物为 zip（不含真实 Token / settings / 日志）。"""
    platform = request.args.get("platform", "claude")
    readonly = request.args.get("readonly", "true").lower() not in ("0", "false", "no", "off")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        files = {
            "manifest.json": _build_share_manifest(),
            "mcp-config-stdio.json": _build_sanitized_mcp_config(platform, readonly),
            "mcp-config-sse.json": _build_sanitized_sse_config(platform, readonly),
            "sensitive-scan.json": _scan_share_artifacts(),
            "README.txt": {
                "title": "个人知识库分享包",
                "version": VERSION,
                "includes": [
                    "manifest.json",
                    "mcp-config-stdio.json",
                    "mcp-config-sse.json",
                    "sensitive-scan.json",
                ],
                "excludes": ["real tokens", "settings", "usage logs", "private notes"],
                "usage": [
                    "1. 用自己的只读/读写 Token 替换 ${KB_READONLY_TOKEN}/${KB_WRITE_TOKEN}",
                    "2. SSE 客户端在 headers.Authorization 填 Bearer <token>",
                    "3. 远端上传用 kb_upload(content/content_base64)，不要传本机 file_path",
                ],
            },
        }
        for name, obj in files.items():
            if isinstance(obj, str):
                zf.writestr(name, obj)
            else:
                zf.writestr(name, json.dumps(obj, ensure_ascii=False, indent=2))
    data = buf.getvalue()
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=kb-share-pack.zip"},
    )


@bp.route("/api/integrations/download/kb_mcp_server.py")
def download_kb_mcp_server():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    fp = KB_ROOT / "scripts" / "kb_mcp_server.py"
    return send_file(str(fp), mimetype="text/x-python", as_attachment=True, download_name="kb_mcp_server.py")