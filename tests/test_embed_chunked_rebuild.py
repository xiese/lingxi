"""全量嵌入的断点续跑，与「写入路径不再触发全量重建」（1.0.21）。

背景（2026-11-08 排障）：全量嵌入常态 20+ 分钟，而写入路径曾在 self.lock 内调
build_embeddings()、还给它 300s 硬预算 → 每轮超时回滚，向量面永远补不上，
单次写入被拖到 7 分钟。现在：写入路径只「标记 + 调度后台」，全量改成分片推进、
片间落断点、可跨进程续跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import kb_lib.config as config  # noqa: E402
import kb_lib.search as search_mod  # noqa: E402

DIM = 8


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """索引相关文件全部指向 tmp，并桩掉 embedding_api（返回可预期的向量）。"""
    monkeypatch.setattr(config, "INDEX_FILE", tmp_path / ".search_index.json", raising=False)
    monkeypatch.setattr(config, "EMBEDDINGS_FILE", tmp_path / ".embeddings.npy", raising=False)
    monkeypatch.setattr(config, "FAISS_FILE", tmp_path / ".faiss.index", raising=False)
    monkeypatch.setattr(config, "INDEX_MANIFEST_FILE", tmp_path / ".index_manifest.json", raising=False)
    for _n in ("INDEX_FILE", "EMBEDDINGS_FILE", "FAISS_FILE", "INDEX_MANIFEST_FILE"):
        monkeypatch.setattr(search_mod, _n, getattr(config, _n), raising=False)

    calls = {"batches": 0, "texts": []}

    def fake_embed(texts, task=None):
        calls["batches"] += 1
        calls["texts"].extend(texts)
        base = float(len(calls["texts"]))
        return np.asarray([[base + i for i in range(DIM)] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(search_mod, "embedding_api", fake_embed, raising=False)
    monkeypatch.setattr(
        search_mod, "get_setting",
        lambda k, d=None: "llm" if k == "embedding_type" else d, raising=False,
    )
    return {"emb": config.EMBEDDINGS_FILE, "calls": calls}


def _si(paths, contents):
    si = search_mod.SearchIndex()
    si.paths = list(paths)
    si.contents = list(contents)
    si.mtimes = {p: 1.0 for p in paths}
    return si


def test_断点复用只算断点之后的部分(iso):
    si = _si(["a.md", "b.md", "c.md", "d.md"], ["A", "B", "C", "D"])
    si._embed_ckpt_save(["a.md", "b.md"], np.ones((2, DIM), dtype=np.float32))
    iso["calls"]["texts"].clear()

    assert si.build_embeddings() is True
    assert iso["calls"]["texts"] == ["C", "D"], "断点里的 2 行应被复用，不该重算"
    assert si.embeddings.shape == (4, DIM)
    # 对齐落盘后断点使命完成
    assert not si._embed_ckpt_meta_path().exists()
    assert not si._embed_ckpt_vecs_path().exists()


def test_断点顺序变化时只复用公共前缀(iso):
    si = _si(["x.md", "b.md", "c.md"], ["X", "B", "C"])
    # 断点第 1 行是 a.md，与当前第 1 行 x.md 不同 → 一行都别复用
    si._embed_ckpt_save(["a.md", "b.md"], np.ones((2, DIM), dtype=np.float32))
    iso["calls"]["texts"].clear()

    assert si.build_embeddings() is True
    assert iso["calls"]["texts"] == ["X", "B", "C"], "前缀不同就不该复用（防张冠李戴）"


def test_超预算存断点且不再置空回滚(iso):
    # 40 篇 = 3 批（batch_size 16），好观察「一片只推进一批」
    paths = [f"n{i}.md" for i in range(40)]
    si = _si(paths, [f"正文{i}" for i in range(40)])
    assert si.build_embeddings(budget=0.0) is False

    mp = si._embed_ckpt_meta_path()
    assert mp.exists() and si._embed_ckpt_vecs_path().exists(), "超预算必须留断点"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    assert meta["paths"] == paths[:16], "断点只记已算出来的那一段，且与向量逐行对齐"
    assert meta["n"] == 16, "预算判断在算完一批之后，所以这一片至少推进了一批（16 篇）"
    # 关键回归：旧版会置空 + 标 full_embed_budget_exceeded（每轮从头再来）
    assert getattr(si, "consistency_reason", "") != "full_embed_budget_exceeded"


def test_超预算后可续跑并完成(iso):
    paths = [f"n{i}.md" for i in range(40)]
    si = _si(paths, [f"正文{i}" for i in range(40)])
    assert si.build_embeddings(budget=0.0) is False
    n_first = len(iso["calls"]["texts"])
    assert n_first == 16

    assert si.build_embeddings() is True
    assert si.embeddings.shape == (40, DIM)
    assert len(iso["calls"]["texts"]) == 40, "续跑只补剩下的 24 篇，不能重头再来"
    assert not si._embed_ckpt_meta_path().exists()


def test_计算期间paths变化则丢弃本轮结果(iso, monkeypatch):
    si = _si(["a.md", "b.md"], ["A", "B"])
    real = search_mod.embedding_api

    def tamper(texts, task=None):
        si.paths = ["a.md", "b.md", "new.md"]  # 模拟算到一半有人写入
        return real(texts, task)

    monkeypatch.setattr(search_mod, "embedding_api", tamper, raising=False)
    assert si.build_embeddings() is False
    assert not iso["emb"].exists(), "结果与最新 paths 对不上，不该落盘"
    assert si._embed_ckpt_meta_path().exists(), "断点要留着，供下一轮复用前缀"


def test_写入路径不再触发全量重建(iso, monkeypatch):
    si = _si(["a.md", "b.md"], ["A", "B"])
    si.embeddings = None  # 向量面不可用（启动时被判不一致的典型状态）
    full = {"n": 0}
    sched = {"n": 0}
    monkeypatch.setattr(si, "build_embeddings",
                        lambda *a, **k: full.__setitem__("n", full["n"] + 1))
    monkeypatch.setattr(si, "_schedule_embed_rebuild",
                        lambda *a, **k: sched.__setitem__("n", sched["n"] + 1))

    si.build_embeddings_incremental(["a.md"], ["A"], [], old_paths=[])

    assert full["n"] == 0, "写入路径绝不能自己跑全量（那正是卡 7 分钟的原因）"
    assert sched["n"] == 1, "应改为调度后台分片重建"


def test_后台分片循环能推进到对齐(iso, monkeypatch):
    si = _si(["a.md", "b.md", "c.md"], ["A", "B", "C"])
    # 每片预算极小（只推进一批），靠循环多片补齐 —— 且不能空转
    si._run_embed_rebuild_chunks(chunk_budget=0.0)
    assert si.embeddings is not None and si.embeddings.shape[0] == 3
    assert iso["calls"]["texts"] == ["A", "B", "C"]
    assert not si._embed_ckpt_meta_path().exists()
