"""前端 JS 回归测试的 pytest 桥（让 `pytest tests/` 一条命令覆盖前后端）。

为什么需要桥：`tests/js/*.test.cjs` 是 Node 内置 node:test 的用例，pytest 默认不会
收集 .cjs。没有这个桥，前端测试就只能靠人记得单独跑一遍——而「靠人记得」正是
移动端抽屉缺陷当初能溜进发布的原因之一。

行为：用 node --test 跑前端用例；node 缺失时 skip（不 fail），因为前端测试是
增量保护网，不应让一个没装 node 的环境整体测试变红。

node 路径：优先用 PATH 里的 node；若不在 PATH（例如经 nvm 安装），再探测
nvm 常见目录；两者都没有才 skip。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "tests" / "js"


def _find_node() -> str | None:
    """找可用的 node：PATH 优先，其次 nvm 常见路径。"""
    node = shutil.which("node")
    if node:
        return node
    candidates = [
        Path("/.nvm/versions/node"),
        Path.home() / ".nvm" / "versions" / "node",
    ]
    for base in candidates:
        if base.is_dir():
            for ver in sorted(base.iterdir(), reverse=True):
                cand = ver / "bin" / "node"
                if cand.exists():
                    return str(cand)
    return None


def test_前端_js_回归用例全过():
    """跑 tests/js/*.test.cjs（Node 内置 node:test），要求零失败。"""
    if not JS_DIR.is_dir() or not list(JS_DIR.glob("*.test.cjs")):
        pytest.skip("tests/js 下没有前端用例，跳过")

    node = _find_node()
    if not node:
        pytest.skip("未找到 node，跳过前端 JS 测试（前端测试是增量保护网，不应让无 node 环境变红）")

    # 显式收集文件路径：不经 shell，glob 不会展开，必须自己列出来
    test_files = sorted(str(f.relative_to(ROOT)) for f in JS_DIR.glob("*.test.cjs"))
    proc = subprocess.run(
        [node, "--test", *test_files],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0:
        pytest.fail(
            "前端 JS 用例失败（node --test 退出码 %d）\n%s"
            % (proc.returncode, output[-3000:])
        )

    # 额外校验：确实跑到了用例，而不是静默 0 用例通过
    if "# pass 0" in output or "# tests 0" in output:
        pytest.fail("node --test 报告 0 个用例，测试文件可能未被收集\n" + output[-1500:])

    # 断言没有 fail 行（双保险）
    assert "# fail 0" in output, "node --test 输出里未见到 '# fail 0'\n" + output[-1500:]
