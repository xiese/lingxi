"""增量删除分支的行号错位回归（2026-10-25）。

现象（生产实例 192.0.2.10:8899，2026-10-25 03:21:08）：
    File "search.py", line 1311, in build_embeddings_incremental
        self.embeddings = self.embeddings[keep_indices]
    IndexError: index 1867 is out of bounds for axis 0 with size 1867

根因：
    `keep_indices` 是拿 `ref_paths`（旧 paths，长度 N）算出来的行号，
    但 `self.embeddings` 可能只有 N-1 行（上一次增量/落盘异常留下的错位状态）。
    直接拿 N 的行号去索引 N-1 行的矩阵 -> IndexError。
    该异常被外层 `except` 吞掉后 `return self.build_embeddings()` 做全量重建，
    于是每次刷新都触发一次全量重建；waitress 线程池被占满，
    表现成「服务还活着但所有请求超时」（实测队列堆到 7 层）。

判据（本条测试钉住的契约）：
    1. 只有删除时，`self.embeddings` 与 `self.paths` 必须一起变短，行数始终对齐；
    2. 不允许因为「embeddings 行数与 ref_paths 不一致」抛 IndexError；
    3. 错位状态必须被识别为「不可增量」，降级全量重建，而不是带着错位继续走。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:  # pragma: no cover
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = stub

import pytest


def _fresh(monkeypatch, tmp_path):
    """把索引文件全部指到 tmp，避免碰真实索引。"""
    import kb_lib.search as S

    for name, fn in (
        ("INDEX_FILE", tmp_path / "index.json"),
        ("EMBEDDINGS_FILE", tmp_path / "emb.npy"),
        ("FAISS_FILE", tmp_path / "faiss.index"),
        ("INDEX_MANIFEST_FILE", tmp_path / "manifest.json"),
    ):
        monkeypatch.setattr(S, name, str(fn), raising=False)
    si = S.search_index
    si.faiss_index = None
    si.faiss_ids = {}
    si._faiss_next_id = 1
    si.bm25 = None
    si.mtimes = {}
    si._date_cache = {}
    si._importance_cache = {}
    si._meta_cache = {}
    si.index_consistent = True
    si.consistency_reason = "ok"
    return S, si


def test_纯删除时行号不会越界(monkeypatch, tmp_path, caplog):
    """paths 3 条、embeddings 2 条（错位）时，只删一条不得抛 IndexError。

    真正的判据是**走哪条降级路径**：
      · 修复前：IndexError 被外层 except 吞掉 → 额外记一条「索引保存失败」；
      · 修复后：提前识别错位 → 只记一条「增量嵌入跳过」，不出现「索引保存失败」。
    只断言「build_embeddings 被调用」是假绿——两条路都会调用它。
    """
    import logging

    S, si = _fresh(monkeypatch, tmp_path)
    si.paths = ["a.md", "b.md", "c.md"]
    si.contents = ["甲", "乙", "丙"]
    si.embeddings = np.eye(2)          # 故意比 paths 少一行 = 生产错位形态

    called = {"n": 0}

    def fake_build(*a, **k):
        called["n"] += 1
        return None

    monkeypatch.setattr(si, "build_embeddings", fake_build)
    monkeypatch.setattr(S, "get_setting",
                        lambda k, d=None: "llm" if k == "embedding_type" else d)

    with caplog.at_level(logging.WARNING, logger="kb_lib.search"):
        si.build_embeddings_incremental([], [], ["b.md"], list(si.paths))

    msgs = [r.getMessage() for r in caplog.records]
    joined = " | ".join(msgs)
    assert called["n"] >= 1, "行数错位时必须降级全量重建，不能静默继续"
    assert "增量嵌入跳过" in joined, (
        "错位没有被主动识别（应走前置校验分支）: %s" % joined
    )
    assert "索引保存失败" not in joined, (
        "仍然靠异常被吞掉来降级（生产同款崩溃路径）: %s" % joined
    )


def test_正常纯删除时paths与embeddings一起变短(monkeypatch, tmp_path):
    """对齐状态下只删一条：两边的行数必须一起减一，且顺序不错。"""
    S, si = _fresh(monkeypatch, tmp_path)
    si.paths = ["a.md", "b.md", "c.md"]
    si.contents = ["甲", "乙", "丙"]
    si.embeddings = np.eye(3)          # 对齐

    monkeypatch.setattr(si, "build_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(S, "get_setting",
                        lambda k, d=None: "llm" if k == "embedding_type" else d)

    si.build_embeddings_incremental([], [], ["b.md"], list(si.paths))

    # 注意：本函数只负责「向量」这一半，paths 的更新由调用方
    # （_apply_incremental_docs_locked）在同一把锁内完成。所以这里只断言向量侧
    # 按 keep_indices 剪对了行，不能要求 paths 也变短。
    assert len(si.embeddings) == 2, "删除后向量行数不对: %d" % len(si.embeddings)
    assert np.allclose(si.embeddings[0], [1, 0, 0]), "a.md 的行被换掉了"
    assert np.allclose(si.embeddings[1], [0, 0, 1]), "c.md 的行被换掉了"


def test_错位时不得抛IndexError(monkeypatch, tmp_path):
    """把「不抛 IndexError」本身作为契约（生产就是被这个异常打挂的）。"""
    S, si = _fresh(monkeypatch, tmp_path)
    si.paths = ["a.md", "b.md", "c.md"]
    si.contents = ["甲", "乙", "丙"]
    si.embeddings = np.eye(2)

    monkeypatch.setattr(si, "build_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(S, "get_setting",
                        lambda k, d=None: "llm" if k == "embedding_type" else d)

    try:
        si.build_embeddings_incremental([], [], ["b.md"], list(si.paths))
    except IndexError as e:  # pragma: no cover - 修复前会走到这里
        pytest.fail("仍然抛出 IndexError（生产同款崩溃）: %s" % e)
