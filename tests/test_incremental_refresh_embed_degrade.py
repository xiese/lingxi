"""增量刷新：嵌入失败只降级向量面，不回滚 paths/BM25（2026-11-07）。

背景：上游嵌入服务（:3097 垫片 → Cloudflare Workers AI）额度耗尽后持续返回
429/502，`_apply_incremental_docs_locked` 因对齐校验失败抛 RuntimeError，被
`incremental_refresh` 的回滚分支连**已经算好并落盘的文本索引**一起回滚，于是
新笔记永远进不了持久化索引。现约定：文本索引（paths/BM25）与向量嵌入解耦——
文本成功即落盘，向量失败只把 `vector_search_ready` 置 false 并记日志。

钉住：
- test_embed_api_异常_paths_bm25_仍更新并落盘
- test_build_embeddings_incremental_抛错_paths_仍落盘（覆盖新 try/except）
- test_嵌入正常_行为不变
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:  # pragma: no cover
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = stub


def _patch_llm(S):
    """只在 embedding_type 上返回 llm，其余设置走默认值。"""
    return mock.patch.object(
        S, "get_setting",
        lambda k, d=None: "llm" if k == "embedding_type" else d,
    )


def _prep(si, embeddings):
    si.paths, si.contents = ["a.md"], ["甲"]
    si.embeddings = embeddings
    si.mtimes, si.bm25, si.faiss_index = {}, None, None
    si.index_consistent = True
    si.consistency_reason = "ok"
    # 本次变更：a.md 不动，新增 b.md
    si._collect_docs = lambda diff_only=True: (["b.md"], ["乙"], [], {})


def test_embed_api_异常_paths_bm25_仍更新并落盘():
    """embedding_api 抛 429：incremental_refresh 不得抛错，文本索引必须落盘。"""
    import kb_lib.search as S

    si = S.search_index
    _prep(si, np.array([[0.0, 0.0, 1.0]]))

    def boom(batch, task=None):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    with _patch_llm(S), mock.patch.object(S, "embedding_api", boom):
        si.incremental_refresh()  # 关键：上游挂掉时刷新本身不得失败

    assert si.paths == ["a.md", "b.md"], f"文本索引被回滚了: {si.paths}"
    assert si.bm25 is not None, "BM25 未按新文本重建"
    assert si.embeddings is None, "向量面应置空降级"
    assert si.is_vector_search_ready() is False, "向量面应标记为不可用"

    payload, _arrays = S._load_index_payload()
    assert payload is not None, "主索引未落盘"
    assert "b.md" in payload["paths"], f"新篇未进持久化索引: {payload['paths']}"


def test_build_embeddings_incremental_抛错_paths_仍落盘():
    """嵌入组装自身抛错（非内部吞掉）：新 try/except 必须兜住并保留文本索引。"""
    import kb_lib.search as S

    si = S.search_index
    _prep(si, np.array([[0.0, 0.0, 1.0]]))

    def boom(*a, **k):
        raise OSError("embedding backend down")

    with _patch_llm(S), mock.patch.object(si, "build_embeddings_incremental", boom):
        si.incremental_refresh()

    assert si.paths == ["a.md", "b.md"], f"文本索引被回滚了: {si.paths}"
    assert si.embeddings is None
    assert si.is_vector_search_ready() is False

    payload, _arrays = S._load_index_payload()
    assert payload is not None and "b.md" in payload["paths"]


def test_嵌入正常_行为不变():
    """嵌入成功时：路径顺序、向量对齐、新增行取值全部与改动前一致。"""
    import kb_lib.search as S

    si = S.search_index
    _prep(si, np.array([[0.0, 0.0, 1.0]]))

    def embed(batch, task=None):
        return np.array([[1.0, 2.0, 3.0] for _ in batch])

    with _patch_llm(S), mock.patch.object(S, "embedding_api", embed):
        si.incremental_refresh()

    assert si.paths == ["a.md", "b.md"], f"paths 顺序不对: {si.paths}"
    assert si.embeddings is not None, "嵌入成功不应降级向量面"
    assert int(si.embeddings.shape[0]) == len(si.paths), \
        f"embeddings 与 paths 行数不一致: {si.embeddings.shape[0]} vs {len(si.paths)}"
    assert np.allclose(si.embeddings[0], [0.0, 0.0, 1.0]), "保留行被改动"
    assert np.allclose(si.embeddings[1], [1.0, 2.0, 3.0]), "新增行嵌入不对"
