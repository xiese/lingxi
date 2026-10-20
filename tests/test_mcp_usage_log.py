"""钉子用例：调用一次 MCP 工具 → usage 日志落一行（一行一 JSON）。

背景（2026-10-08 MCP 用量审计）：排查「usage logger 停更」疑云时确认
logger 链路本身可用——USAGE_LOG 常量（kb_mcp_server.py:27）、log_usage
写入（:63-70）、各工具调用点均正常，生产本机通道的用量日志持续追加中。
本用例把「工具调用→日志追加一行」钉死，防止未来改动让审计链再次无声
断裂；同时钉住追加语义与失败也落行（ok=false），保证拒绝/失败记录不丢。

不触网、不起服务：api() 打桩返回固定响应；USAGE_LOG 指向 tmp_path，
不写仓库工作区。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import kb_mcp_server  # noqa: E402


def test_tool_call_appends_usage_line(tmp_path, monkeypatch):
    """一次 MCP 工具调用 → usage 日志恰好多一行；一行一 JSON，字段齐全；追加不覆盖。"""
    log_file = tmp_path / ".kb_mcp_usage.jsonl"
    monkeypatch.setattr(kb_mcp_server, "USAGE_LOG", log_file)

    def fake_api(method, path, data=None, timeout=30):
        return {"path": path, "content": "钉子内容", "title": "钉子测试"}

    monkeypatch.setattr(kb_mcp_server, "api", fake_api)

    out = kb_mcp_server.kb_read(path="20-知识/钉子测试.md")
    assert "钉子内容" in out, "MCP 工具未走打桩的 api()，钉子失效"

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"一次工具调用应恰好落 1 行，实际 {len(lines)} 行：{lines!r}"
    row = json.loads(lines[0])
    assert row["tool"] == "kb_read"
    assert row["ok"] is True
    assert row["path"] == "20-知识/钉子测试.md"
    assert row["client"] == kb_mcp_server.CLIENT_NAME
    assert row["readonly"] == kb_mcp_server.READONLY
    assert row["time"], "日志行缺少 time 字段，审计时间链断裂"

    # 失败调用同样落行（ok=false），且为追加而非覆盖——审计不能丢失败记录
    monkeypatch.setattr(
        kb_mcp_server,
        "api",
        lambda method, path, data=None, timeout=30: {"error": "boom"},
    )
    kb_mcp_server.kb_read(path="20-知识/失败路径.md")
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"第二次调用应追加而非覆盖，实际 {len(lines)} 行"
    row2 = json.loads(lines[1])
    assert row2["ok"] is False and row2["tool"] == "kb_read", "失败调用未落 ok=false 行"
