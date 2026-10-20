#!/usr/bin/env python3
"""增量迁移：为缺少 schema_version / content_layer 的旧笔记补齐默认值。

安全设计：
- 只增加字段，不修改原有字段，不改变正文
- 如果在 frontmatter 内看到任意一个 schema 字段已存在则跳过整篇
- 支持 --dry-run 预览变更范围
- 支持 --force 实际写入
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parent.parent / "笔记"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "笔记" / "00-索引" / "curated_wiki_index.md"

# type -> content_layer 映射（与 _default_content_layer 保持一致）
DEFAULT_LAYER = {
    "project": "curated",
    "knowledge": "curated",
    "writing": "raw",
    "diary": "raw",
    "archive": "raw",
    "record": "raw",
    "note": "raw",
    "index": "context",
}

FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL | re.MULTILINE)


def _derive_layer(note_type: str) -> str:
    return DEFAULT_LAYER.get(note_type, "raw")


def _needs_migration(text: str) -> bool:
    """检查 frontmatter 是否缺少 schema_version 或 content_layer"""
    m = FM_PATTERN.search(text)
    if not m:
        return False  # 没有 frontmatter 的跳过（极少数特殊文件）
    fm = m.group(1)
    # 如果已存在任一 schema 字段，认为已迁移过
    if "schema_version:" in fm and "content_layer:" in fm:
        return False
    return True


def _migrate_frontmatter(text: str) -> str:
    """为 frontmatter 补齐 schema_version 和 content_layer"""
    def replace(m: re.Match) -> str:
        fm_body = m.group(1)
        lines = fm_body.split("\n")
        # 从 type 推导 content_layer
        note_type = "knowledge"  # 默认
        for line in lines:
            if line.startswith("type:"):
                nt = line.split(":", 1)[1].strip().strip('"').strip("'")
                note_type = nt
                break
        layer = _derive_layer(note_type)

        # 收集已有字段名
        existing_fields = set()
        for line in lines:
            key = line.split(":", 1)[0].strip() if ":" in line else ""
            if key:
                existing_fields.add(key)

        new_lines = []
        added_schema = False
        added_layer = False
        for line in lines:
            new_lines.append(line)
            key = line.split(":", 1)[0].strip() if ":" in line else ""
            # 在 type 行后面插入 schema_version 和 content_layer
            if key == "type":
                if "schema_version" not in existing_fields:
                    new_lines.append("schema_version: 1")
                    added_schema = True
                if "content_layer" not in existing_fields:
                    new_lines.append(f'content_layer: "{layer}"')
                    added_layer = True

        if not added_schema and "schema_version" not in existing_fields:
            new_lines.append("schema_version: 1")
        if not added_layer and "content_layer" not in existing_fields:
            new_lines.append(f'content_layer: "{layer}"')

        return f"---\n" + "\n".join(new_lines) + "\n---"

    return FM_PATTERN.sub(replace, text)


def scan_all() -> list[tuple[Path, str, str]]:
    """返回 (路径, 变更类型, note_type) 列表"""
    results = []
    for fp in sorted(NOTES_DIR.rglob("*.md")):
        # 跳过输出文件自身和特殊文件
        rel = str(fp.relative_to(NOTES_DIR))
        if "00-索引/curated_wiki_index.md" in rel or "00-索引/最近更新.md" in rel:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        if not text.startswith("---"):
            continue
        if _needs_migration(text):
            # 粗略提取 type
            note_type = "knowledge"
            m = FM_PATTERN.search(text)
            if m:
                for line in m.group(1).split("\n"):
                    if line.startswith("type:"):
                        note_type = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
            results.append((fp, rel, note_type))
    return results


def main():
    dry_run = "--force" not in sys.argv

    candidates = scan_all()
    total = len(candidates)
    if total == 0:
        print("所有笔记已包含 schema_version + content_layer，无需迁移。")
        return

    # 按类型分类统计
    by_type: dict[str, int] = {}
    for _, _, nt in candidates:
        by_type[nt] = by_type.get(nt, 0) + 1

    print(f"发现 {total} 篇笔记需要迁移：")
    for nt, cnt in sorted(by_type.items()):
        print(f"  type={nt}: {cnt} 篇 → content_layer={_derive_layer(nt)}")

    if dry_run:
        print(f"\n[Dry-run] 使用 --force 实际写入。示例变更：")
        for fp, rel, nt in candidates[:3]:
            print(f"  {rel}  type={nt} → layer={_derive_layer(nt)}")
        return

    # 实际写入
    changed = 0
    errors = 0
    for fp, rel, nt in candidates:
        try:
            text = fp.read_text(encoding="utf-8")
            new_text = _migrate_frontmatter(text)
            if new_text != text:
                fp.write_text(new_text, encoding="utf-8")
                changed += 1
        except Exception as e:
            print(f"  ERROR {rel}: {e}", file=sys.stderr)
            errors += 1

    print(f"\n已完成：{changed} 篇迁移，{errors} 篇错误，{total - changed - errors} 篇无需变更。")


if __name__ == "__main__":
    main()
