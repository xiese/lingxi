"""A2: IndexState 状态机 + refresh/rebuild 语义分离。

不依赖 Flask 进程；直接测 SearchIndex 状态字段与状态转换 helper。
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


def _import_search():
    import kb_lib.search as search_mod

    return search_mod


def _import_index_state():
    from kb_lib.services import index_state

    return index_state


def test_search_index_has_state_field():
    search_mod = _import_search()
    si = search_mod.SearchIndex()
    assert hasattr(si, "state")
    assert si.state in ("clean", "dirty", "rebuilding", "failed")


def test_mark_dirty_sets_state_and_dirty_flag():
    search_mod = _import_search()
    si = search_mod.SearchIndex()
    si.mark_dirty(reason="write")
    assert si.dirty is True
    assert si.state == "dirty"
    assert "write" in (si.state_reason or "")


def test_begin_rebuild_sets_rebuilding():
    search_mod = _import_search()
    si = search_mod.SearchIndex()
    si.mark_dirty(reason="stale")
    si.begin_rebuild(mode="full")
    assert si.state == "rebuilding"
    assert si.rebuild_mode == "full"


def test_end_rebuild_ok_sets_clean():
    search_mod = _import_search()
    si = search_mod.SearchIndex()
    si.begin_rebuild(mode="incremental")
    si.end_rebuild(ok=True)
    assert si.state == "clean"
    assert si.dirty is False
    assert si.state_reason in ("ok", "", None) or "ok" in str(si.state_reason)


def test_end_rebuild_fail_sets_failed():
    search_mod = _import_search()
    si = search_mod.SearchIndex()
    si.begin_rebuild(mode="full")
    si.end_rebuild(ok=False, error="boom")
    assert si.state == "failed"
    assert "boom" in (si.state_reason or "")


def test_status_payload_includes_state_and_semantics():
    """/api/index/status 应暴露 state + refresh/rebuild 语义说明字段。"""
    ist = _import_index_state()
    payload = ist.build_status_payload(
        indexed=10,
        has_index=True,
        has_embeddings=True,
        has_faiss=True,
        dirty=True,
        index_consistent=True,
        consistency_reason="ok",
        vector_search_ready=True,
        paths_count=10,
        embeddings_count=10,
        faiss_count=10,
        state="dirty",
        state_reason="write",
        rebuild_mode=None,
    )
    assert payload["state"] == "dirty"
    assert payload["state_reason"] == "write"
    assert "refresh_means" in payload
    assert "rebuild_means" in payload
    assert "force incremental" in payload["refresh_means"].lower() or "增量" in payload["refresh_means"]
    assert "admin" in payload["rebuild_means"].lower() or "全量" in payload["rebuild_means"]


def test_public_rebuild_is_refresh_alias_contract():
    """公开 rebuild 必须声明为 refresh 别名，不是 admin 全量。"""
    ist = _import_index_state()
    info = ist.public_refresh_response_note()
    assert "refresh" in info.lower() or "增量" in info
    assert "admin" in info.lower() or "全量" in info or "rebuild-index" in info
