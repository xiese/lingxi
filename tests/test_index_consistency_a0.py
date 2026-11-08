"""A0: 索引 paths / embeddings / faiss 一致性校验与原子 manifest。

不依赖 Flask 服务；所有文件写到临时目录，不碰生产索引。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    pytest_stub = types.ModuleType("pytest")
    pytest_stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = pytest_stub

import pytest

import kb_lib.config as config
import kb_lib.search as search_mod


@pytest.fixture()
def iso_index(tmp_path, monkeypatch):
    """把 INDEX/EMBEDDINGS/FAISS/MANIFEST 全部指到 tmp，并强制 embedding_type=llm。"""
    pkl = tmp_path / ".search_index.json"
    emb = tmp_path / ".embeddings.npy"
    faissf = tmp_path / ".faiss.index"
    manifest = tmp_path / ".index_manifest.json"

    for mod in (config, search_mod):
        monkeypatch.setattr(mod, "INDEX_FILE", pkl, raising=False)
        monkeypatch.setattr(mod, "EMBEDDINGS_FILE", emb, raising=False)
        monkeypatch.setattr(mod, "FAISS_FILE", faissf, raising=False)
        if hasattr(mod, "INDEX_MANIFEST_FILE") or True:
            monkeypatch.setattr(mod, "INDEX_MANIFEST_FILE", manifest, raising=False)

    def _get_setting(k, default=None):
        if k == "embedding_type":
            return "llm"
        return default

    monkeypatch.setattr(search_mod, "get_setting", _get_setting, raising=False)
    monkeypatch.setattr(config, "get_setting", _get_setting, raising=False)

    return {
        "tmp": tmp_path,
        "pkl": pkl,
        "emb": emb,
        "faiss": faissf,
        "manifest": manifest,
    }


def _write_index(path: Path, n_paths: int):
    """写新格式主索引（JSON + 空数组文件），不再使用 pickle。"""
    paths = [f"n{i}.md" for i in range(n_paths)]
    contents = [f"body-{i}" for i in range(n_paths)]
    mtimes = {p: float(i + 1) for i, p in enumerate(paths)}
    path.write_text(
        json.dumps(
            {
                "format_version": 2,
                "bm25": None,
                "paths": paths,
                "contents": contents,
                "mtimes": mtimes,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    np.savez(Path(str(path) + ".arrays.npz"), doc_len=np.asarray([], dtype=np.int64))


def test_load_rejects_mismatched_paths_and_embeddings(iso_index):
    """paths=2 / emb=3 时，load 不得把错位 embeddings 当可用向量。"""
    _write_index(iso_index["pkl"], 2)
    np.save(iso_index["emb"], np.random.randn(3, 8).astype(np.float32))

    si = search_mod.SearchIndex()
    loaded = si.load()

    assert loaded is True
    assert len(si.paths) == 2
    # A0 目标：错位时禁用向量面，避免语义搜指错文
    assert si.embeddings is None or getattr(si, "index_consistent", True) is False
    if hasattr(si, "index_consistent"):
        assert si.index_consistent is False
    if si.embeddings is not None:
        # 若保留 embeddings，必须不能用于检索
        assert not si.is_vector_search_ready()
    else:
        assert not si.is_vector_search_ready()


def test_load_accepts_aligned_paths_and_embeddings(iso_index):
    _write_index(iso_index["pkl"], 2)
    np.save(iso_index["emb"], np.random.randn(2, 8).astype(np.float32))

    si = search_mod.SearchIndex()
    loaded = si.load()

    assert loaded is True
    assert len(si.paths) == 2
    assert si.embeddings is not None
    assert si.embeddings.shape[0] == 2
    if hasattr(si, "index_consistent"):
        assert si.index_consistent is True
    assert si.is_vector_search_ready() is True


def test_check_artifact_consistency_helper():
    """纯函数：计数不一致应返回 ok=False 与原因。"""
    ok, reason = search_mod.check_artifact_consistency(
        n_paths=2, n_contents=2, n_embeddings=3, n_faiss=3
    )
    assert ok is False
    assert "paths" in reason or "embedding" in reason.lower() or "不一致" in reason

    ok2, reason2 = search_mod.check_artifact_consistency(
        n_paths=2, n_contents=2, n_embeddings=2, n_faiss=2
    )
    assert ok2 is True


def test_save_writes_manifest_atomically(iso_index, monkeypatch):
    """_save_index / 写 embedding 后应落 manifest，且计数对齐。"""
    si = search_mod.SearchIndex()
    si.paths = ["a.md", "b.md"]
    si.contents = ["A", "B"]
    si.mtimes = {"a.md": 1.0, "b.md": 2.0}
    si.bm25 = None
    si.embeddings = np.random.randn(2, 4).astype(np.float32)

    # 最小：调用公开的一致性发布 helper
    assert hasattr(si, "publish_index_artifacts") or hasattr(
        search_mod, "write_index_manifest"
    )

    if hasattr(si, "publish_index_artifacts"):
        si.publish_index_artifacts()
    else:
        # 退化为直接写 pkl + emb + manifest
        si._save_index()
        np.save(iso_index["emb"], si.embeddings)
        search_mod.write_index_manifest(
            n_paths=2,
            n_contents=2,
            n_embeddings=2,
            n_faiss=0,
            index_version=si._index_version,
            path=iso_index["manifest"],
        )

    assert iso_index["manifest"].exists()
    data = json.loads(iso_index["manifest"].read_text(encoding="utf-8"))
    assert data["paths_count"] == 2
    assert data["embeddings_count"] == 2
    assert data["contents_count"] == 2


def test_vec_search_skips_when_inconsistent(iso_index, monkeypatch):
    """即使内存里硬塞错位数据，_vec_search 也不能返回结果。"""
    si = search_mod.SearchIndex()
    si.paths = ["a.md", "b.md"]
    si.contents = ["A", "B"]
    si.embeddings = np.random.randn(3, 8).astype(np.float32)
    si.index_consistent = False

    def boom(*a, **k):
        raise AssertionError("不应在不一致时调用 embedding")

    monkeypatch.setattr(search_mod, "_cached_embedding", boom)
    out = si._vec_search("hello", top=5)
    assert out == []


def test_bm25_roundtrip_scores_identical(iso_index):
    """新格式（JSON + npz）读写对称：重建的 BM25 打分与写入前逐位一致。"""
    from rank_bm25 import BM25Okapi

    docs = [["知识", "库", "索引"], ["索引", "格式"], ["知识", "图谱"]]
    si = search_mod.SearchIndex()
    si.paths = ["a.md", "b.md", "c.md"]
    si.contents = ["知识 库 索引", "索引 格式", "知识 图谱"]
    si.mtimes = {"a.md": 1.0, "b.md": 2.0, "c.md": 3.0}
    si.bm25 = BM25Okapi(docs)
    before = si.bm25.get_scores(["索引"])

    si._save_index()
    # 主索引是 JSON，不得是 pickle；数值数组走 npz 且 allow_pickle=False
    raw = iso_index["pkl"].read_text(encoding="utf-8")
    assert json.loads(raw)["format_version"] == 2
    arrays = np.load(str(iso_index["pkl"]) + ".arrays.npz", allow_pickle=False)
    assert arrays["doc_len"].tolist() == [3, 2, 2]

    si2 = search_mod.SearchIndex()
    assert si2.load() is True
    after = si2.bm25.get_scores(["索引"])
    assert np.allclose(before, after)
    assert si2.paths == si.paths and si2.contents == si.contents


def test_legacy_pickle_no_longer_read(iso_index):
    """1.0.19 起不再读取旧 pickle：旧文件在场也当主索引不存在，走全量重建。

    判据不是「旧文件还在」，而是「没拿它写出新索引、也没改名」——
    只断言前者的话，迁移逻辑还留着也能过。
    """
    import pickle

    legacy_path = iso_index["pkl"].with_suffix(".pkl")
    legacy_path.write_bytes(
        pickle.dumps(
            {
                "bm25": None,
                "paths": ["old.md"],
                "contents": ["旧内容"],
                "mtimes": {"old.md": 9.0},
            }
        )
    )
    si = search_mod.SearchIndex()
    assert si.load() is False, "旧 pickle 不再是有效的索引来源"
    assert not iso_index["pkl"].exists(), "不得由旧数据写出新格式主索引"
    assert legacy_path.exists(), "旧文件原地保留（清理交给运维）"
    assert not Path(str(legacy_path) + ".legacy").exists(), "不再执行改名迁移"

