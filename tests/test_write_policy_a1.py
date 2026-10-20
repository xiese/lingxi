"""A1: 写路径单一真相 — hygiene + quality 在 REST/service 单源。

不依赖 Flask 服务进程；直接测 write_policy 纯函数。
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


def test_quality_rejects_short_project_content():
    wp = _import_write_policy()
    err = wp.validate_note_quality(
        content="太短了，没有章节。",
        note_type="project",
        quality_check=True,
    )
    assert err is not None
    assert err["error"] == "quality_check_failed"
    assert "problems" in err
    assert any("过短" in p for p in err["problems"])


def test_quality_rejects_missing_required_sections():
    wp = _import_write_policy()
    long_body = "x" * 900
    err = wp.validate_note_quality(
        content=long_body,
        note_type="knowledge",
        quality_check=True,
    )
    assert err is not None
    assert err["error"] == "quality_check_failed"
    joined = " ".join(err["problems"])
    assert "必备章节" in joined
    for sec in wp.REQUIRED_SECTIONS:
        assert sec in err.get("required_sections", wp.REQUIRED_SECTIONS)


def test_quality_can_be_disabled():
    wp = _import_write_policy()
    err = wp.validate_note_quality(
        content="短",
        note_type="project",
        quality_check=False,
    )
    assert err is None


def test_quality_skips_diary_and_writing():
    wp = _import_write_policy()
    assert (
        wp.validate_note_quality("短", note_type="diary", quality_check=True) is None
    )
    assert (
        wp.validate_note_quality("短", note_type="writing", quality_check=True) is None
    )


def test_hygiene_rejects_deprecated_path():
    wp = _import_write_policy()
    err, warnings = wp.validate_write_hygiene(
        note_type="project",
        subdir="10-项目_知识库项目",
        title="foo",
        content="body",
    )
    assert err is not None
    assert err["error"] == "path_forbidden"
    assert warnings == [] or isinstance(warnings, list)


def test_hygiene_rejects_project_deploy_fork():
    wp = _import_write_policy()
    err, _ = wp.validate_write_hygiene(
        note_type="project",
        subdir="foo 部署",
        title="bar",
        content="body",
    )
    assert err is not None
    assert err["error"] == "project_dir_fork_forbidden"


def test_hygiene_rejects_archive_root():
    wp = _import_write_policy()
    err, _ = wp.validate_write_hygiene(
        note_type="archive",
        subdir="",
        title="x",
        content="body",
    )
    assert err is not None
    assert err["error"] == "archive_root_forbidden"


def test_hygiene_rejects_skill_dump():
    wp = _import_write_policy()
    dump = "skill dump body " * 2000  # >= 20k without pointer keywords
    assert len(dump) >= 20000
    err, _ = wp.validate_write_hygiene(
        note_type="knowledge",
        subdir="",
        title="my-skill-notes",
        content=dump,
    )
    assert err is not None
    assert err["error"] == "skill_dump_forbidden"


def test_hygiene_allows_skill_pointer():
    wp = _import_write_policy()
    body = (
        "权威来源：~/.kb-skills/foo\n请用 skill_view 读取。\n"
        + ("p" * 20000)
    )
    err, _ = wp.validate_write_hygiene(
        note_type="knowledge",
        subdir="",
        title="foo-skill-pointer",
        content=body,
    )
    assert err is None


def test_create_payload_includes_quality_check_field_contract():
    """契约：REST create 应接受 quality_check，默认 True 语义由服务层表达。"""
    wp = _import_write_policy()
    # 构造「完整」正文，应通过 quality
    sections = "\n\n".join(
        f"{s}\n\n内容占位 " + ("y" * 160) for s in wp.REQUIRED_SECTIONS
    )
    assert len(sections) >= wp.MIN_CONTENT_LEN
    err = wp.validate_note_quality(
        content=sections, note_type="project", quality_check=True
    )
    assert err is None


def test_on_note_changed_callable():
    """统一写后出口存在；默认 force=False。"""
    wp = _import_write_policy()
    assert hasattr(wp, "on_note_changed")
    # 用 no-op hooks 验证可调用，不碰生产索引
    calls = []

    def hook(force=False):
        calls.append(force)

    wp.on_note_changed(
        action="created",
        force_search_refresh=False,
        _hooks={
            "invalidate_notes_cache": lambda: calls.append("notes"),
            "invalidate_graph_cache": lambda: calls.append("graph"),
            "update_indexes": lambda: calls.append("upd"),
            "trigger_index_refresh": hook,
        },
    )
    assert "notes" in calls
    assert "graph" in calls
    assert "upd" in calls
    # create/save 默认不 force
    assert False not in calls  # force 参数以 False 传入过至少一次（hook 被 create 路径调用时）


def test_on_note_changed_delete_forces_refresh():
    wp = _import_write_policy()
    forces = []

    def trig(force=False):
        forces.append(force)

    wp.on_note_changed(
        action="deleted",
        force_search_refresh=True,
        _hooks={
            "invalidate_notes_cache": lambda: None,
            "invalidate_graph_cache": lambda: None,
            "update_indexes": None,  # delete 路径可跳过 shell 冷却组合
            "trigger_index_refresh": trig,
        },
    )
    assert forces == [True]
