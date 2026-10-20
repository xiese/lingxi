"""KB_HOST 拆名回归测试（2026-10-07）。

背景：KB_HOST 历史上同名双义——kb_lib/config.py 用它表示「监听绑定地址」，
kb_mcp_server.py 用它表示「上游 REST API 地址」——运维设错就出事（把 URL 设成
绑定地址会直接起不来，把 0.0.0.0 设成上游地址会打不通）。已拆名为 KB_BIND
（绑定）/ KB_API_BASE（上游 URL），旧名 KB_HOST 只保留一个版本的兼容。

覆盖：
  A 只设旧名 KB_HOST 时，两处（config 绑定 + kb_mcp_server 上游）都拿到兼容值，
    且记一次 deprecation warning；
  B 只设新名时用新名，不记弃用告警；
  C 都不设时用默认值；
  D 新旧名同时设置时新名优先。

kb_mcp_server 依赖 fastmcp，只装在 python3.11（kb-mcp-sse.service 同款解释器），
因此上游地址一侧用 python3.11 子进程读取真实模块，不打桩。
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
CONFTEST_LEGACY = "http://127.0.0.1:8899"  # tests/conftest.py 的 setdefault 值
ENV_KEYS = ("KB_BIND", "KB_HOST", "KB_API_BASE")
LEGACY = "http://legacy-host:9999"
NEW_BIND = "192.0.2.1"
NEW_API = "http://new-api:1234"
PY311 = os.environ.get("KB_TEST_PY311", "/usr/bin/python3.11")


def _pick_mcp_python() -> str | None:
    """挑一个「能 import fastmcp」的解释器来跑 kb_mcp_server。

    原写法写死 /usr/bin/python3.11，CI runner 上没有这个路径就整组失败
    （3.10/3.11/3.12 三个 job 全红），属于环境依赖而非代码回归。
    这里按优先级探测：环境变量 → 生产同款 /usr/bin/python3.11 → PATH 上的
    python3.11 → 当前解释器；都导入不了 fastmcp 就跳过，不判失败。
    """
    cands = [PY311, shutil.which("python3.11") or "", sys.executable]
    for exe in cands:
        if not exe or not os.path.exists(exe):
            continue
        try:
            r = subprocess.run(
                [exe, "-c", "import fastmcp"],
                capture_output=True, timeout=120,
            )
        except Exception:
            continue
        if r.returncode == 0:
            return exe
    return None


MCP_PYTHON = _pick_mcp_python()


def _clear_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _reload_config(monkeypatch, env):
    import kb_lib.config as config

    _clear_env(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


def _restore_config(monkeypatch, config):
    """把环境变量与 config 模块恢复成 conftest 口径，避免污染同进程其他用例。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("KB_HOST", CONFTEST_LEGACY)
    importlib.reload(config)


def _mcp_run(env):
    """在 python3.11（MCP 服务同款解释器）里导入 kb_mcp_server，返回 (stdout 末行, stderr)。"""
    if not MCP_PYTHON:
        pytest.skip("本环境没有能 import fastmcp 的解释器，跳过 MCP 侧验证")
    child = dict(os.environ)
    for k in ENV_KEYS:
        child.pop(k, None)
    child.update(env)
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import kb_mcp_server as m; print(m.KB_API_BASE)"
    ) % str(SCRIPTS)
    r = subprocess.run([MCP_PYTHON, "-c", code], env=child, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"{MCP_PYTHON} 子进程导入 kb_mcp_server 失败:\n{r.stderr[-800:]}"
    lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
    assert lines, "子进程没有输出 KB_API_BASE"
    return lines[-1], r.stderr


def test_只设旧名_KB_HOST_时两处都拿到兼容值并记弃用告警(monkeypatch, caplog):
    import kb_lib.config as config

    with caplog.at_level("WARNING"):
        cfg = _reload_config(monkeypatch, {"KB_HOST": LEGACY})
    assert cfg.HOST == LEGACY, "config 绑定地址未取到旧名兼容值"
    assert cfg.KB_BIND == LEGACY, "config KB_BIND 别名未同步"
    assert "KB_BIND" in caplog.text and "KB_HOST" in caplog.text, "旧名路径未记 deprecation warning"

    out, err = _mcp_run({"KB_HOST": LEGACY})
    assert out == LEGACY, f"kb_mcp_server 上游地址未取到旧名兼容值: {out!r}"
    assert "KB_API_BASE" in err, f"kb_mcp_server 旧名路径未记 deprecation warning:\n{err[-400:]}"

    _restore_config(monkeypatch, cfg)


def test_只设新名时用新名且不记弃用告警(monkeypatch, caplog):
    import kb_lib.config as config

    with caplog.at_level("WARNING"):
        cfg = _reload_config(monkeypatch, {"KB_BIND": NEW_BIND, "KB_API_BASE": NEW_API})
    assert cfg.HOST == NEW_BIND, "config 未使用新名 KB_BIND"
    assert cfg.KB_BIND == NEW_BIND
    assert "弃用" not in caplog.text, f"只设新名却记了弃用告警:\n{caplog.text[-400:]}"

    out, err = _mcp_run({"KB_BIND": NEW_BIND, "KB_API_BASE": NEW_API})
    assert out == NEW_API, f"kb_mcp_server 未使用新名 KB_API_BASE: {out!r}"
    assert "弃用" not in err, f"只设新名却记了弃用告警:\n{err[-400:]}"

    _restore_config(monkeypatch, cfg)


def test_都不设时用默认(monkeypatch):
    import kb_lib.config as config

    cfg = _reload_config(monkeypatch, {})
    assert cfg.HOST == "127.0.0.1", "config 默认绑定地址应为 127.0.0.1"
    assert cfg.KB_BIND == "127.0.0.1"

    out, _ = _mcp_run({})
    assert out == "http://127.0.0.1:8899", f"kb_mcp_server 默认上游地址不符: {out!r}"

    _restore_config(monkeypatch, cfg)


def test_新旧名同时设置时新名优先(monkeypatch):
    import kb_lib.config as config

    env = {"KB_BIND": NEW_BIND, "KB_HOST": LEGACY, "KB_API_BASE": NEW_API}
    cfg = _reload_config(monkeypatch, env)
    assert cfg.HOST == NEW_BIND, "新名未优先于旧名"

    out, _ = _mcp_run(env)
    assert out == NEW_API, "新名未优先于旧名"

    _restore_config(monkeypatch, cfg)
