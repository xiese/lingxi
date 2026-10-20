"""compute_graph 契约回归测试（2026-10-08 拆分重构时新增）。

背景：compute_graph 在拆分前没有任何直接测试覆盖（tests/ 里只有 mock 掉的
invalidate_graph_cache 引用和 cmd_graph 命令名）。本文件锁定模块 docstring
冻结的 v1.0.0 对外契约：nodes/links/meta 字段、wiki 边解析（管道别名 /
文件名回退）、语义边与去重、degree 回填、TTL 缓存复用与 invalidate_cache
失效、语义降级不写缓存。

不依赖真实索引：monkeypatch 临时笔记目录 + 假 search_index（paths +
embeddings），搜索面由测试完全控制。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from kb_lib import graph as g  # noqa: E402


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """三篇笔记（a↔b 互链、a 经别名链 c）+ 假语义索引（b/c 同向、a 异向）。"""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text(
        "---\ntitle: A 笔记\ntags: [alpha, beta]\n---\n\n[[b]] 与 [[c|别名]]。\n",
        encoding="utf-8")
    (notes / "b.md").write_text(
        "---\ntags: alpha\n---\n\n指回 [[a]]。\n",
        encoding="utf-8")
    (notes / "c.md").write_text("纯文本无 frontmatter。\n", encoding="utf-8")
    monkeypatch.setattr(g, "NOTES_DIR", notes)
    monkeypatch.setattr(g, "EMBEDDINGS_FILE", tmp_path / "no_such_embeddings.npy")
    monkeypatch.setattr(g, "search_index", types.SimpleNamespace(
        paths=["a.md", "b.md", "c.md"],
        embeddings=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.0]]),
        load=lambda: None,
    ))
    g.invalidate_cache()
    yield notes
    g.invalidate_cache()


def test_compute_graph_结构与边(graph_env):
    result = g.compute_graph()

    # 节点：字段顺序、title 回落 stem、tags、group（根目录 → 第一个标签）
    assert [n["id"] for n in result["nodes"]] == ["a.md", "b.md", "c.md"]
    node_a = result["nodes"][0]
    assert node_a["title"] == "A 笔记"
    assert node_a["tags"] == ["alpha", "beta"]
    assert node_a["dir"] == ""
    assert node_a["group"] == "alpha"
    assert result["nodes"][2]["group"] == "未分类"

    # wiki 边：[[b]] 文件名命中；[[c|别名]] 管道别名 + stem 回退；b→a 反向
    wiki = {(l["source"], l["target"])
            for l in result["links"] if l["type"] == "wiki"}
    assert wiki == {("a.md", "b.md"), ("a.md", "c.md"), ("b.md", "a.md")}
    assert all(l["weight"] == 1.0 for l in result["links"] if l["type"] == "wiki")

    # 语义边：a 与 b/c 异向（低于 min_sim），b/c 同向 1.0 → 去重后仅一条
    sem = [l for l in result["links"] if l["type"] == "semantic"]
    assert len(sem) == 1
    assert (sem[0]["source"], sem[0]["target"]) == ("b.md", "c.md")
    assert sem[0]["weight"] == 1.0

    # degree 回填：a 3 条边、b 3 条、c 2 条
    assert {n["id"]: n["degree"] for n in result["nodes"]} == {
        "a.md": 3, "b.md": 3, "c.md": 2}

    meta = result["meta"]
    assert meta["node_count"] == 3
    assert meta["link_count"] == 4
    assert meta["wiki_count"] == 3
    assert meta["semantic_count"] == 1
    assert meta["min_sim"] == 0.8
    assert meta["max_links"] == 8
    assert "generated_at" in meta


def test_compute_graph_缓存与降级(graph_env):
    first = g.compute_graph()
    assert g.compute_graph() is first        # TTL 内：复用同一结果对象
    g.invalidate_cache()
    fresh = g.compute_graph()
    assert fresh is not first                # 失效后重算
    assert fresh["nodes"] == first["nodes"]

    # 语义面不可用（embeddings=None）→ 降级为仅 wiki 边，且不写缓存
    g.invalidate_cache()
    g.search_index.embeddings = None
    degraded = g.compute_graph()
    assert [l["type"] for l in degraded["links"]] == ["wiki", "wiki", "wiki"]
    assert degraded["meta"]["semantic_count"] == 0
    assert g._cache == {}

    # 非法参数（min_sim="bad"、max_links="bad"）回落默认值
    reset = g.compute_graph(min_sim="bad", max_links="bad")
    assert reset["meta"]["min_sim"] == 0.8
    assert reset["meta"]["max_links"] == 8
