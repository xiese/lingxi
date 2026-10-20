"""方案 B: upload / smart-upload / batch 统一走 on_note_changed。

不依赖 Flask 进程；用 hooks 断言写后出口语义。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    pytest_stub = types.ModuleType("pytest")
    pytest_stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = pytest_stub

import pytest


def _import_write_policy():
    from kb_lib.services import write_policy

    return write_policy


def _hooks(calls):
    def trig(force=False):
        calls.append(("trig", force))

    return {
        "invalidate_notes_cache": lambda: calls.append("notes"),
        "invalidate_graph_cache": lambda: calls.append("graph"),
        "update_indexes": lambda: calls.append("upd"),
        "trigger_index_refresh": trig,
        "mark_index_dirty": lambda: calls.append("dirty"),
    }


def test_on_note_changed_uploaded_uses_update_indexes_not_force():
    """upload(.md) 与 create 一致：冷却增量，不 force。"""
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="uploaded", force_search_refresh=False, _hooks=_hooks(calls))
    assert "notes" in calls
    assert "graph" in calls
    assert "upd" in calls
    assert ("trig", True) not in calls


def test_on_note_changed_smart_uploaded_uses_update_indexes():
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="smart_uploaded", _hooks=_hooks(calls))
    assert "upd" in calls
    assert ("trig", True) not in calls


def test_on_note_changed_batch_tag_uses_update_indexes():
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="batch_tagged", _hooks=_hooks(calls))
    assert "upd" in calls
    assert ("trig", True) not in calls


def test_on_note_changed_batch_delete_forces_refresh():
    """batch delete/archive 必须 force，避免冷却期内搜到已删。"""
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="batch_deleted", _hooks=_hooks(calls))
    assert ("trig", True) in calls
    assert "upd" not in calls


def test_on_note_changed_batch_archive_forces_refresh():
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="batch_archived", _hooks=_hooks(calls))
    assert ("trig", True) in calls


def test_on_note_changed_marks_index_dirty_when_hook_present():
    """写后应 mark dirty，给 A2 IndexState 喂信号。"""
    wp = _import_write_policy()
    calls = []
    wp.on_note_changed(action="uploaded", _hooks=_hooks(calls))
    assert "dirty" in calls


def test_write_side_actions_documented():
    """方案 B 动作集合应可被路由复用，避免字符串漂移。"""
    wp = _import_write_policy()
    assert hasattr(wp, "FORCE_REFRESH_ACTIONS")
    for a in ("deleted", "delete", "archived", "batch_deleted", "batch_archived"):
        assert a in wp.FORCE_REFRESH_ACTIONS
    assert "uploaded" not in wp.FORCE_REFRESH_ACTIONS
    assert "smart_uploaded" not in wp.FORCE_REFRESH_ACTIONS
    assert "batch_tagged" not in wp.FORCE_REFRESH_ACTIONS
