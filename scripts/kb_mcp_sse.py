#!/usr/bin/env python3
"""知识库 MCP SSE 服务 — 供外部 MCP 客户端远程连接（Bearer Token 鉴权）"""
import sys, os, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)
os.environ.setdefault("KB_HOST", "http://127.0.0.1:8899")
os.environ.setdefault("KB_TOKEN_FILE", str(_KB_ROOT / ".api_tokens.json"))
# SSE 默认启用鉴权；紧急关闭：KB_MCP_SSE_AUTH=0
os.environ.setdefault("KB_MCP_SSE_AUTH", "1")
# 监听地址：默认仅本机；需对外提供时显式设置 KB_MCP_SSE_HOST=0.0.0.0
SSE_HOST = os.environ.get("KB_MCP_SSE_HOST", "127.0.0.1")
SSE_PORT = int(os.environ.get("KB_MCP_SSE_PORT", "8898"))

from kb_mcp_server import mcp, build_mcp_auth

if __name__ == "__main__":
    # 安全加固：build_mcp_auth 在「想开鉴权却开不了」时抛错而不是
    # 返回 None，这里必须让进程退出，不能带着无鉴权继续监听。
    try:
        auth = build_mcp_auth()
    except RuntimeError as e:
        print(f"❌ {e}")
        raise SystemExit(1)
    if auth is not None:
        mcp.auth = auth
        print(f"📚 知识库 MCP SSE → http://{SSE_HOST}:{SSE_PORT}/sse （Bearer 鉴权已启用）")
    else:
        print(f"📚 知识库 MCP SSE → http://{SSE_HOST}:{SSE_PORT}/sse （鉴权关闭或未加载 Token）")
    asyncio.run(mcp.run_http_async(transport="sse", host=SSE_HOST, port=SSE_PORT))
