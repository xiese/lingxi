"""B7 收口用例：钉住 kb_maintain.check_mcp_usage 的三态判定。

背景：2026-10-08 实测发现「MCP usage 日志停更」曾被误诊为 logger bug，
真实根因是 10-07 重启后无客户端调用。本用例锁死检查项的三态行为：
  ① 文件缺失 → warn；② mtime≤30 天 → ok；③ mtime>30 天 → warn 且指明区分法。
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def km(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    import kb_maintain
    importlib.reload(kb_maintain)
    return kb_maintain


def _usage_row() -> str:
    return '{"time": "2026-08-19T19:15:26", "tool": "kb_read", "ok": true}\n'


def test_usage_文件缺失告警(km):
    r = km.CheckResult()
    km.check_mcp_usage(r)
    assert any("不存在" in m for _, m in r.details)


def test_usage_新鲜写入通过(km, tmp_path):
    usage = tmp_path / ".kb_mcp_usage.jsonl"
    usage.write_text(_usage_row() * 3, encoding="utf-8")
    r = km.CheckResult()
    km.check_mcp_usage(r)
    assert any("3 行" in m for _, m in r.details)
    # 不得出现停更告警
    assert not any("无新写入" in m for _, m in r.details)


def test_usage_超30天停更告警并带区分指引(km, tmp_path):
    usage = tmp_path / ".kb_mcp_usage.jsonl"
    usage.write_text(_usage_row() * 2, encoding="utf-8")
    stale = time.time() - 45 * 86400
    os.utime(usage, (stale, stale))
    r = km.CheckResult()
    km.check_mcp_usage(r)
    warn_msgs = [m for icon, m in r.details if icon.strip() == "⚠️"]
    assert any("无新写入" in m for _, m in r.details)
    assert any("journalctl" in m and "kb_mcp_stats" in m for m in warn_msgs), "停更告警必须带区分法与交叉验证指引"
