# -*- coding: utf-8 -*-
"""批 2：双链解析（services/links.py）+ 反链增量维护 的测试。

覆盖：
- 三种语法 [[文件名]] / [[x#标题]] / [[x|别名]] / 组合
- 目录链接 [[目录/]] 不解析
- 解析优先级：同目录优先 → 补 .md → title 索引 → stem 索引
- extract_links 去重、断链 resolved=None
- backlinks 增量：写一篇 → 反链出现；改掉双链 → 反链消失；删笔记 → 反链清空
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from kb_lib.services.links import (  # noqa: E402
    build_link_index,
    extract_links,
    iter_wikilinks,
    parse_wikilink_target,
    resolve_wikilink,
)
from kb_lib.services import backlinks as bl  # noqa: E402


# ── 解析语法 ──

def test_parse_plain():
    assert parse_wikilink_target("文件名") == ("文件名", None, None)


def test_parse_anchor():
    assert parse_wikilink_target("x#标题") == ("x", "标题", None)


def test_parse_alias():
    assert parse_wikilink_target("x|别名") == ("x", None, "别名")


def test_parse_anchor_and_alias():
    assert parse_wikilink_target("x#h|a") == ("x", "h", "a")


def test_parse_dir_link_returns_none_target():
    assert parse_wikilink_target("目录/") == (None, None, None)


def test_iter_wikilinks():
    text = "看 [[a]] 和 [[b#c|d]]，还有 [[e]]"
    assert list(iter_wikilinks(text)) == ["a", "b#c|d", "e"]


def test_iter_wikilinks_empty():
    assert list(iter_wikilinks("")) == []


# ── 解析优先级 ──

def _idx(notes):
    return build_link_index(notes)


def test_resolve_exact_path():
    ids, ti, si = _idx([("10-项目/a.md", "A"), ("20-知识/b.md", "B")])
    assert resolve_wikilink("x.md", "20-知识/b.md", ids, ti, si) == "20-知识/b.md"


def test_resolve_adds_md():
    ids, ti, si = _idx([("20-知识/b.md", "B")])
    assert resolve_wikilink("x.md", "20-知识/b", ids, ti, si) == "20-知识/b.md"


def test_resolve_relative_same_dir():
    ids, ti, si = _idx([("20-知识/sub/c.md", "C")])
    assert resolve_wikilink("20-知识/other.md", "sub/c", ids, ti, si) == "20-知识/sub/c.md"


def test_resolve_by_title():
    ids, ti, si = _idx([("20-知识/b.md", "标题B")])
    assert resolve_wikilink("x.md", "标题B", ids, ti, si) == "20-知识/b.md"


def test_resolve_by_stem():
    ids, ti, si = _idx([("20-知识/b.md", "标题B")])
    assert resolve_wikilink("x.md", "b", ids, ti, si) == "20-知识/b.md"


def test_resolve_with_alias_and_anchor():
    ids, ti, si = _idx([("20-知识/b.md", "B")])
    assert resolve_wikilink("x.md", "b#标题|别名", ids, ti, si) == "20-知识/b.md"


def test_resolve_missing_returns_none():
    ids, ti, si = _idx([("20-知识/b.md", "B")])
    assert resolve_wikilink("x.md", "不存在", ids, ti, si) is None


def test_resolve_dir_link_returns_none():
    ids, ti, si = _idx([("20-知识/b.md", "B")])
    assert resolve_wikilink("x.md", "20-知识/", ids, ti, si) is None


# ── extract_links ──

def test_extract_links_dedup_and_order():
    ids, ti, si = _idx([("a.md", "A"), ("b.md", "B")])
    text = "[[b]] 然后 [[a]] 再 [[b|别名]]"
    out = extract_links("src.md", text, ids, ti, si)
    # (target, anchor, alias) 三元组去重 → b / a / b|别名 视为不同 key
    assert [o["target"] for o in out] == ["b", "a", "b"]
    assert all(o["resolved"] for o in out)


def test_extract_links_broken_kept():
    ids, ti, si = _idx([("a.md", "A")])
    out = extract_links("src.md", "[[nope]]", ids, ti, si)
    assert len(out) == 1 and out[0]["resolved"] is None


# ── 反链增量 ──

class _FakeIndex:
    def __init__(self, paths, contents, mtimes=None):
        self.paths = list(paths)
        self.contents = list(contents)
        self.mtimes = mtimes or {p: i for i, p in enumerate(paths)}


def setup_function(_fn):
    bl.reset()


def test_backlinks_incremental_add_and_remove():
    idx = _FakeIndex(["a.md", "b.md"], ["正文 [[b]]", "正文"])
    assert bl.backlinks_of(idx, "b.md") == ["a.md"]

    # 改掉 a 的双链 → 反链消失
    bl.update_note(idx, "a.md", "正文，不再引用")
    assert bl.backlinks_of(idx, "b.md") == []

    # 再加回来
    bl.update_note(idx, "a.md", "又 [[b]] 了")
    assert bl.backlinks_of(idx, "b.md") == ["a.md"]


def test_backlinks_outlinks():
    idx = _FakeIndex(["a.md", "b.md"], ["[[b]] [[b]]", ""])
    assert bl.outlinks_of(idx, "a.md") == ["b.md"]


def test_backlinks_deleted_note_cleared():
    idx = _FakeIndex(["a.md", "b.md"], ["[[b]]", ""])
    assert bl.backlinks_of(idx, "b.md") == ["a.md"]
    # a 被删除：paths 里不再有 a
    idx.paths = ["b.md"]
    idx.contents = [""]
    bl.update_note(idx, "a.md")
    assert bl.backlinks_of(idx, "b.md") == []


def test_backlinks_self_link_ignored():
    idx = _FakeIndex(["a.md"], ["[[a]]"])
    assert bl.backlinks_of(idx, "a.md") == []


def test_backlinks_signature_rebuild():
    idx = _FakeIndex(["a.md", "b.md"], ["[[b]]", ""])
    assert bl.backlinks_of(idx, "b.md") == ["a.md"]
    # 索引换代（新增一篇也链 b）
    idx.paths = ["a.md", "b.md", "c.md"]
    idx.contents = ["[[b]]", "", "也 [[b]]"]
    idx.mtimes = {p: i for i, p in enumerate(idx.paths)}
    assert bl.backlinks_of(idx, "b.md") == ["a.md", "c.md"]
