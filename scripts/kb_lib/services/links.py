"""双链（wiki link）解析：图谱与阅读页共用的单一真相。

支持语法
--------
- `[[文件名]]`        —— 按路径 / 文件名解析
- `[[文件名#标题]]`   —— 忽略锚点，解析到文件（标题锚点由前端滚动定位）
- `[[文件名|别名]]`   —— 别名只影响显示文本，不影响解析
- `[[目录/文件.md]]`  —— 显式带扩展名
- `[[目录/]]`         —— 目录链接，不是文件链接，返回 None

解析优先级（与历史行为一致，勿随意调整）
----------------------------------------
1. 原文精确匹配（含 `目录/文件名` 相对 source 所在目录的拼接）
2. 补 `.md` 后再匹配
3. 全库 title 索引（frontmatter 的 title）
4. 全库文件名（stem）索引

设计取舍
--------
- 本模块**不读磁盘、不建索引**：调用方把 `ids` / `title_index` / `stem_index`
  传进来。图谱用全量扫描建一次索引复用；单篇阅读只需解析几个链接，
  可传最小索引（或空 dict，此时退化为「只能解析同目录/显式路径」）。
- `_WIKILINK_RE` 与前端 `m2()` 的正则保持同一语义：`[[` 到最近的 `]]`，
  内容不允许含 `]`。
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "WIKILINK_RE",
    "iter_wikilinks",
    "parse_wikilink_target",
    "resolve_wikilink",
    "build_link_index",
    "extract_links",
]

# `[[...]]`，内容不含 `]`
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def iter_wikilinks(text):
    """产出正文里所有双链的**原始内容**（不含 `[[ ]]`）。"""
    if not text:
        return
    for m in WIKILINK_RE.finditer(text):
        yield m.group(1)


def parse_wikilink_target(raw):
    """把双链原始内容拆成 (target, anchor, alias)。

    - `[[x#标题]]`   -> ("x", "标题", None)
    - `[[x|别名]]`   -> ("x", None, "别名")
    - `[[x#h|a]]`    -> ("x", "h", "a")
    - `[[目录/]]`    -> (None, None, None)  —— 目录链接，不参与文件解析
    """
    if raw is None:
        return None, None, None
    body = str(raw).strip()
    alias = None
    if "|" in body:
        body, alias = body.split("|", 1)
        alias = alias.strip() or None
    anchor = None
    if "#" in body:
        body, anchor = body.split("#", 1)
        anchor = anchor.strip() or None
    target = body.rstrip("\\").strip().replace("\\", "/")
    if not target or target.endswith("/"):
        return None, anchor, alias
    return target, anchor, alias


def resolve_wikilink(source_rel, raw_target, ids, title_index=None, stem_index=None):
    """把双链目标解析成笔记相对路径；解析不到返回 None。

    ids / title_index / stem_index 由调用方提供（见 `build_link_index`）。
    """
    target, _anchor, _alias = parse_wikilink_target(raw_target)
    if not target:
        return None
    title_index = title_index or {}
    stem_index = stem_index or {}
    source_dir = str(Path(source_rel).parent)
    candidates = []
    for base in (target, target if target.endswith(".md") else target + ".md"):
        candidates.append(base)
        if source_dir != ".":
            candidates.append(str(Path(source_dir) / base))
    for cand in candidates:
        if cand in ids:
            return cand
    return title_index.get(target) or stem_index.get(Path(target).stem)


def build_link_index(notes):
    """从 [(rel, title)] 建 (ids, title_index, stem_index)。

    title_index / stem_index 用 setdefault —— 同名时保留先出现的，
    与图谱历史行为一致（保证同一库两次解析结果相同）。
    """
    ids = set()
    title_index, stem_index = {}, {}
    for rel, title in notes:
        ids.add(rel)
        title_index.setdefault(str(title or Path(rel).stem), rel)
        stem_index.setdefault(Path(rel).stem, rel)
    return ids, title_index, stem_index


def extract_links(source_rel, text, ids, title_index=None, stem_index=None):
    """抽取一篇笔记里的所有双链（按出现顺序去重）。

    返回 [{"raw", "target", "anchor", "alias", "resolved"}]，
    resolved=None 表示未解析到（断链）。
    """
    out, seen = [], set()
    for raw in iter_wikilinks(text):
        target, anchor, alias = parse_wikilink_target(raw)
        if not target:
            continue
        key = (target, anchor, alias)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "raw": raw,
            "target": target,
            "anchor": anchor,
            "alias": alias,
            "resolved": resolve_wikilink(
                source_rel, raw, ids, title_index, stem_index),
        })
    return out
