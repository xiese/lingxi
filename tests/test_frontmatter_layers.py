from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    pytest_stub = types.ModuleType("pytest")
    pytest_stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = pytest_stub

import pytest
from flask import Flask, g

from kb_lib.routes import _build_frontmatter, _source_identity
from kb_lib.routes.routes_notes import _apply_frontmatter_edit_metadata


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


def test_build_frontmatter_includes_layer_schema_and_edit_fields(app):
    with app.app_context():
        g.ti = {
            "name": "tester-token",
            "permission": "rw",
            "platform": "mcp",
            "project_scope": "知识库项目",
            "source": "request",
        }
        source = _source_identity()
        fm = _build_frontmatter(
            title="分层试点",
            note_type="knowledge",
            tags=["分层", "wiki"],
            relations=["10-项目/知识库项目.md"],
            status="draft",
            priority="p0",
            source=source,
            content_layer="curated",
            canonical_tags=["知识库", "分层"],
            aliases=["Layer Pilot"],
            compiled_from=["20-知识/raw-a.md"],
        )

    assert "schema_version: 1" in fm
    assert 'content_layer: "curated"' in fm
    assert "canonical_tags: [知识库, 分层]" in fm
    assert "aliases: [Layer Pilot]" in fm
    assert "compiled_from: [20-知识/raw-a.md]" in fm
    assert 'last_edited_by: "tester-token"' in fm
    assert 'last_edit_source_type: "request"' in fm
    assert 'source_type: "request"' in fm


def test_build_frontmatter_derives_default_layer_by_note_type(app):
    with app.app_context():
        assert 'content_layer: "curated"' in _build_frontmatter(
            title="项目页", note_type="project", tags=[]
        )
        assert 'content_layer: "raw"' in _build_frontmatter(
            title="日记页", note_type="diary", tags=[]
        )


def test_save_note_updates_last_edit_fields():
    content = """---\ntitle: \"旧文档\"\nupdated: 2026-07-08 10:00\nlast_edited_by: \"old-user\"\nlast_edit_source_type: \"manual\"\n---\n\nbody\n"""
    updated = _apply_frontmatter_edit_metadata(
        content,
        editor="new-user",
        edit_source_type="token",
        now_str="2026-07-08 11:22",
    )

    assert 'updated: "2026-07-08 11:22"' in updated
    assert 'last_edited_by: "new-user"' in updated
    assert 'last_edit_source_type: "token"' in updated
    assert 'last_edited_by: "old-user"' not in updated
