#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from kb_lib.auth import parse_fm

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NOTES_DIR = ROOT / "笔记"
DEFAULT_OUTPUT = DEFAULT_NOTES_DIR / "00-索引" / "curated_wiki_index.md"


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def compile_curated_wiki(notes_dir: Path = DEFAULT_NOTES_DIR, output_path: Path = DEFAULT_OUTPUT) -> dict[str, int | str]:
    curated: list[dict[str, Any]] = []
    skipped = 0

    for fp in sorted(notes_dir.rglob("*.md")):
        if output_path.resolve() == fp.resolve():
            continue
        rel = str(fp.relative_to(notes_dir))
        meta, _ = parse_fm(fp)
        schema_version = str(meta.get("schema_version", "")).strip()
        content_layer = str(meta.get("content_layer", "")).strip()
        if not schema_version or not content_layer:
            skipped += 1
            continue
        if content_layer != "curated":
            continue
        curated.append(
            {
                "path": rel,
                "title": meta.get("title", fp.stem),
                "type": meta.get("type", ""),
                "tags": _as_list(meta.get("tags", [])),
                "aliases": _as_list(meta.get("aliases", [])),
                "relations": _as_list(meta.get("relations", [])),
                "compiled_from": _as_list(meta.get("compiled_from", [])),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        'title: "Curated Wiki Index"',
        "type: index",
        "schema_version: 1",
        'content_layer: "context"',
        f"generated_at: {now}",
        f"curated_count: {len(curated)}",
        f"skipped_count: {skipped}",
        "---",
        "",
        "# Curated Wiki Index",
        "",
    ]
    if not curated:
        lines.append("_No curated notes found._")
    else:
        for item in curated:
            lines.extend(
                [
                    f"## {item['title']}",
                    f"- path: {item['path']}",
                    f"- type: {item['type']}",
                    f"- tags: {', '.join(item['tags']) if item['tags'] else '-'}",
                    f"- aliases: {', '.join(item['aliases']) if item['aliases'] else '-'}",
                    f"- relations: {', '.join(item['relations']) if item['relations'] else '-'}",
                    f"- compiled_from: {', '.join(item['compiled_from']) if item['compiled_from'] else '-'}",
                    "",
                ]
            )
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "output_path": str(output_path),
        "curated_count": len(curated),
        "skipped_count": skipped,
    }


if __name__ == "__main__":
    result = compile_curated_wiki()
    print(result)
