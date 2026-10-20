# -*- coding: utf-8 -*-
"""rerank 超时/失败的显式降级机制测试（2026-10-07 机制级修复）。

覆盖点（对应 scripts/kb_lib/search.py 的 _cached_rerank / _hybrid_search）：
1. 超时（TimeoutError）与失败（Exception）都不再静默塌降：结果元数据带
   degrade=rerank_timeout / rerank_error，且有 warning 日志（含 query/耗时/
   候选数/降级模式）；
2. 降级后闭环：仍走 alpha 融合 + FALLBACK_BM25_FLOOR/SLOTS 保底名额——
   被 5 个高分纯向量候选压出 top 的强 BM25 候选仍被提进 top；
   SLOTS=0 关断时该候选不在 top（反证保底名额确实在起作用）；
3. rerank 正常时不带 degrade、不误标，且降级登记被取走无残留；
4. 熔断窗内（前序失败）显式标记 rerank_circuit_open 且不再打精排 API。

全部离线：BM25 用真实 rank_bm25，向量面用假候选替身，精排用假 rerank_api。
不碰 KB_ROOT 上的真实笔记数据。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest
from rank_bm25 import BM25Okapi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用安全回归套件的 client fixture
from test_security_regressions import client  # noqa: F401  fixture 引入供本文件用例使用

import kb_lib.search as S

# 强 BM25 候选：doc0 比 doc1 短（同 tf 下 BM25 分更高），归一分 display_score=1.0
PATHS = ["强命中.md", "hello-filler.md",
         "vec-a.md", "vec-b.md", "vec-c.md", "vec-d.md", "vec-e.md"]
CONTENTS = [
    "hello 强命中",
    "hello there filler 内容 乙丙丁戊",
    "向量填充 甲",
    "向量填充 乙",
    "向量填充 丙",
    "向量填充 丁",
    "向量填充 戊",
]
QUERY = "hello"


@pytest.fixture()
def si(monkeypatch):
    """离线检索面：真实 BM25 + 假向量候选（5 个 0.95 高分）+ 假设置。"""
    index = S.SearchIndex()
    index.paths = list(PATHS)
    index.contents = list(CONTENTS)
    index.bm25 = BM25Okapi([S._tokenize(c) for c in index.contents])
    index.embeddings = None
    index._token_sets = None
    index.mtimes = {p: 1.0 for p in PATHS}

    monkeypatch.setattr(
        S, "get_setting",
        lambda k, d=None: {
            "rerank_enabled": True,
            "embedding_type": "llm",
            "hybrid_alpha": 0.6,
        }.get(k, d),
    )
    monkeypatch.setattr(S, "_rerank_fail_until", 0.0)
    S._rerank_cache.clear()
    # 假向量面：5 个候选全部 0.95 分，融合后足以把强 BM25 候选压出 top=5
    monkeypatch.setattr(
        index, "_vec_search",
        lambda q, top=20: [
            {"path": "vec-%s.md" % s, "score": 0.95,
             "snippet": "向量 %s" % s, "idx": 2 + i}
            for i, s in enumerate("abcde")
        ],
    )
    return index


def _paths_of(results):
    return [r["path"] for r in results]


def _kb_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "kb.search" and r.levelno >= logging.WARNING]


def test_rerank_timeout_degrades_explicitly_and_keeps_floor(si, monkeypatch, caplog):
    """超时：结果带 degrade=rerank_timeout，日志带四要素，保底名额仍生效。"""
    calls = {"n": 0}

    def slow(q, docs, top_n=5, **kw):
        calls["n"] += 1
        raise TimeoutError("simulated rerank timeout")

    monkeypatch.setattr(S, "rerank_api", slow)
    with caplog.at_level(logging.WARNING, logger="kb.search"):
        results = si._hybrid_search(QUERY, top=5)

    assert calls["n"] == 1, "超时也应真实调用一次精排（不熔断语义）"
    assert results, "降级不应返回空结果"
    # ① 显式降级：每条结果都带 degrade 元数据，且没有 reranker_score
    assert {r.get("degrade") for r in results} == {"rerank_timeout"}
    assert all("reranker_score" not in r for r in results)
    # ② 日志：query / 耗时 / 候选数 / 降级模式都要在
    msgs = _kb_warnings(caplog)
    hit = [m for m in msgs if "rerank_timeout" in m and "精排" in m]
    assert hit, f"缺少带降级模式的 warning: {msgs}"
    assert any("候选数" in m for m in hit), hit
    assert any("耗时" in m for m in hit), hit
    # ③ 保底名额闭环：强 BM25 候选被 5 个 0.95 向量候选压着，仍被提进 top5
    assert "强命中.md" in _paths_of(results[:5]), _paths_of(results)
    # 登记已取走，无跨请求残留
    assert S._rerank_degrade_take() is None

    # 反证：关断保底名额后同一场景强候选不在 top（证明上面是名额机制在起作用）
    monkeypatch.setattr(S, "FALLBACK_BM25_SLOTS", 0)
    results_off = si._hybrid_search(QUERY + "-off", top=5)
    assert "强命中.md" not in _paths_of(results_off[:5]), _paths_of(results_off)


def test_rerank_error_degrades_explicitly_and_keeps_floor(si, monkeypatch, caplog):
    """失败（非超时异常）：degrade=rerank_error，熔断打开，保底名额仍生效。"""
    def boom(q, docs, top_n=5, **kw):
        raise RuntimeError("rerank service down")

    monkeypatch.setattr(S, "rerank_api", boom)
    with caplog.at_level(logging.WARNING, logger="kb.search"):
        results = si._hybrid_search(QUERY, top=5)

    assert results, "降级不应返回空结果"
    assert {r.get("degrade") for r in results} == {"rerank_error"}
    msgs = _kb_warnings(caplog)
    assert any("rerank_error" in m for m in msgs), msgs
    # 失败语义不变：仍按原逻辑熔断 300s
    assert S._rerank_fail_until > time.time(), "失败应触发熔断"
    assert "强命中.md" in _paths_of(results[:5]), _paths_of(results)


def test_rerank_ok_no_degrade_marker(si, monkeypatch, caplog):
    """正常精排：结果不带 degrade、带 reranker_score，不误标。"""
    def ok(q, docs, top_n=5, **kw):
        return [{"index": 0, "relevance_score": 0.9}]

    monkeypatch.setattr(S, "rerank_api", ok)
    with caplog.at_level(logging.WARNING, logger="kb.search"):
        results = si._hybrid_search(QUERY, top=5)

    assert results and all("degrade" not in r for r in results)
    assert all("reranker_score" in r for r in results)
    assert not [m for m in _kb_warnings(caplog) if "降级" in m]
    assert S._rerank_degrade_take() is None


def test_circuit_open_marks_degrade_without_calling_api(si, monkeypatch, caplog):
    """熔断窗内：不打精排 API，但降级仍显式（rerank_circuit_open）。"""
    monkeypatch.setattr(S, "_rerank_fail_until", time.time() + 300)
    calls = {"n": 0}

    def spy(q, docs, top_n=5, **kw):
        calls["n"] += 1
        return [{"index": 0, "relevance_score": 0.5}]

    monkeypatch.setattr(S, "rerank_api", spy)
    with caplog.at_level(logging.WARNING, logger="kb.search"):
        results = si._hybrid_search(QUERY, top=5)

    assert calls["n"] == 0, "熔断中不应再打精排 API"
    assert {r.get("degrade") for r in results} == {"rerank_circuit_open"}
    assert "强命中.md" in _paths_of(results[:5]), _paths_of(results)


def test_semantic_endpoint_passes_degrade(client):
    """路由层：/api/search/semantic 白名单必须带 degrade，否则降级对调用方不可见。

    钉的是 2026-10-08 收口补丁：routes_search.py 构造 enriched 时按白名单键取值，
    不显式携带 degrade 的话 search 层登记的降级模式到不了 API 响应。
    fixture client 复用 tests/test_security_regressions.py 的隔离 Flask 应用。
    """
    import json
    from unittest import mock

    from kb_lib.routes import routes_search as RS

    c, hdr, notes, tmp = client
    fake = [{"path": "命中.md", "score": 0.5, "snippet": "s",
             "degrade": "rerank_timeout"}]
    with mock.patch.object(RS.search_index, "search", lambda *a, **k: (fake, None)), \
         mock.patch.object(RS, "finfo",
                           lambda fp: {"path": "命中.md", "title": "命中"}):
        resp = c.get("/api/search/semantic?q=x", headers=hdr)

    assert resp.status_code == 200, resp.data
    body = json.loads(resp.data)
    assert body["items"][0].get("degrade") == "rerank_timeout", body
