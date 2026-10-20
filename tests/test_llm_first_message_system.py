"""LLM 消息数组首条 role=system 回归测试（2026-10-07）。

背景：LLM 网关（本机 CPA 8318）要求 messages 首条必须是 system，否则 HTTP 400，
异常被 except 吞掉后走默认值 —— 查重永远判 new、分类永远落 knowledge，静默失效。
2026-09-29 已按 docs/知识库审查与功能规划_2026-09-29.md §3.8 修掉两处，
本文件把「首条必须是 system」钉成不变量，防未来新增调用点回归。

只校验传给 llm 的消息结构，不复核任何业务逻辑、不发真实 LLM/嵌入请求。
"""
from __future__ import annotations

import ast
from pathlib import Path

from test_security_regressions import client  # noqa: F401  复用隔离 Flask app fixture

ROOT = Path(__file__).resolve().parent.parent
ROUTES_LLM = ROOT / "scripts" / "kb_lib" / "routes" / "routes_llm.py"


def test_smart_upload_传给_llm_的消息首条必须是_system(client, monkeypatch):
    """实跑 /api/smart-upload，捕获每次 llm 调用的 messages，断言首条 role=system。

    llm 调用与 write_policy 全部 monkeypatch 假掉，不触达真实 LLM / 嵌入。
    """
    import kb_lib.routes as ROUTES_PKG
    import kb_lib.routes.routes_llm as RL

    captured = []

    def fake_chat_with_retry(msgs, *a, **k):
        captured.append(list(msgs))
        return "project"

    def fake_chat(msgs, *a, **k):
        captured.append(list(msgs))
        return "知识, 测试"

    for mod in (RL, ROUTES_PKG):
        monkeypatch.setattr(mod, "llm_chat_with_retry", fake_chat_with_retry, raising=False)
        monkeypatch.setattr(mod, "llm_chat", fake_chat, raising=False)
    monkeypatch.setattr(RL.write_policy, "on_note_changed", lambda *a, **k: None, raising=False)

    c, h, _, _ = client
    r = c.post(
        "/api/smart-upload",
        json={"text": "# 回归测试标题\n\n" + "这是用于触发智能上传 LLM 链路的正文内容。" * 30},
        headers=h,
    )
    assert r.status_code in (200, 201), r.get_data(as_text=True)[:300]
    assert captured, "smart-upload 未触发任何 llm 调用，测试失去意义"
    for i, msgs in enumerate(captured):
        head = msgs[0] if msgs else {}
        assert head.get("role") == "system", (
            f"第 {i} 次 llm 调用首条 role={head.get(chr(114) + chr(111) + chr(108) + chr(101))!r}，应为 system"
        )


def test_routes_llm_全部_llm_调用点首条必须是_system():
    """AST 审计 routes_llm.py 所有 llm_chat* 调用点，首条消息 role 必须是 system。"""
    tree = ast.parse(ROUTES_LLM.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("llm_chat", "llm_chat_with_retry"):
                calls.append((node.lineno, node))
    assert len(calls) >= 6, f"审计到 {len(calls)} 个调用点，少于预期 6 个"
    for lineno, node in calls:
        assert node.args, f"routes_llm.py:{lineno} llm 调用缺参数"
        first = node.args[0]
        assert isinstance(first, ast.List), f"routes_llm.py:{lineno} 首参不是 messages 列表字面量"
        assert first.elts, f"routes_llm.py:{lineno} messages 为空"
        head = first.elts[0]
        assert isinstance(head, ast.Dict), f"routes_llm.py:{lineno} messages 首条不是字典字面量"
        keys = [k.value for k in head.keys if isinstance(k, ast.Constant)]
        assert "role" in keys, f"routes_llm.py:{lineno} messages 首条无 role 键"
        role_val = head.values[keys.index("role")]
        assert isinstance(role_val, ast.Constant) and role_val.value == "system", (
            f"routes_llm.py:{lineno} messages 首条 role 不是 system"
        )
