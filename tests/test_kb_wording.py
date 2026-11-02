"""agent 侧知识库话术的措辞约束。

只读源码文本，不导入被测模块：不需要真实知识库，也不发网络请求。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _docstring(path: Path, func_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{path} 里找不到函数 {func_name}")


def test_mcp_tool_docstrings_mark_stale() -> None:
    src = SCRIPTS / "kb_mcp_server.py"
    for name in ("kb_dual_search", "kb_search"):
        doc = _docstring(src, name)
        assert "可能过时" in doc, f"{name} 的 docstring 未标注「可能过时」：{doc!r}"


def test_routes_description_marks_stale() -> None:
    text = (SCRIPTS / "kb_lib" / "routes" / "routes_integrations.py").read_text(encoding="utf-8")
    m = re.search(r'"name":\s*"kb_dual_search",\s*"description":\s*"([^"]*)"', text)
    assert m, "routes_integrations.py 里找不到 kb_dual_search 的 description"
    assert "可能过时" in m.group(1), f"kb_dual_search 的 description 未标注「可能过时」：{m.group(1)!r}"


def test_no_authoritative_wording_left() -> None:
    for path in SCRIPTS.rglob("*"):
        if path.suffix not in (".py", ".md", ".json", ".js"):
            continue
        if not path.is_file():
            continue
        assert "回答用户问题前优先使用" not in path.read_text(encoding="utf-8"), \
            f"{path} 仍残留「回答用户问题前优先使用」"


def test_system_prompt_conflict_rule() -> None:
    text = (SCRIPTS / "kb_lib" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "按用户说法回答" in text, "system.md 缺少「按用户说法回答」的冲突处理规则"
