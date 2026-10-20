from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "kb_compile_wiki.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compile_mod = _load_module(SCRIPT, "kb_compile_wiki_test")


def write_note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_compile_wiki_filters_curated_and_reports_skipped(tmp_path):
    notes_dir = tmp_path / "笔记"
    out_path = notes_dir / "00-索引" / "curated_wiki_index.md"

    write_note(
        notes_dir / "20-知识" / "a.md",
        """---\ntitle: \"A\"\ntype: knowledge\nschema_version: 1\ncontent_layer: curated\ntags: [知识库]\naliases: [Alpha]\nrelations: [10-项目/知识库项目.md]\ncompiled_from: [20-知识/raw-a.md]\n---\n\n# A\n""",
    )
    write_note(
        notes_dir / "20-知识" / "b.md",
        """---\ntitle: \"B\"\ntype: knowledge\nschema_version: 1\ncontent_layer: raw\ntags: [原始]\n---\n\n# B\n""",
    )
    write_note(
        notes_dir / "20-知识" / "legacy.md",
        """---\ntitle: \"Legacy\"\ntype: knowledge\ntags: [旧]\n---\n\n# Legacy\n""",
    )

    result = compile_mod.compile_curated_wiki(notes_dir=notes_dir, output_path=out_path)

    assert result["curated_count"] == 1
    assert result["skipped_count"] == 1
    assert out_path.exists()

    output = out_path.read_text(encoding="utf-8")
    assert "# Curated Wiki Index" in output
    assert "20-知识/a.md" in output
    assert "Alpha" in output
    assert "20-知识/raw-a.md" in output
    assert "20-知识/b.md" not in output
    assert "skipped_count: 1" in output
