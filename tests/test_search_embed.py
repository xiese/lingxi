"""搜索/精排/嵌入 离线测试（不联网）。

覆盖这次修复的点，防止回归：
- embedding_api 按字符预算分批
- rerank_api 使用可配置模型 + 文档截断
- _cached_embedding 缓存命中
- _cached_rerank 缓存命中 + 失败熔断
"""
from __future__ import annotations
import sys, types
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    pytest_stub = types.ModuleType("pytest")
    pytest_stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = pytest_stub

import pytest
import kb_lib.embed as embed_mod
import kb_lib.search as search_mod


def test_embedding_api_batches_by_budget(monkeypatch):
    calls = []
    def fake_api_call(endpoint, body, timeout=60, retries=3):
        calls.append(body)
        n = len(body["input"])
        return {"data": [{"index": i, "embedding": [0.0] * 8} for i in range(n)],
                "usage": {"total_tokens": 1}}
    monkeypatch.setattr(embed_mod, "_api_call", fake_api_call)
    monkeypatch.setattr(embed_mod, "load_settings",
                        lambda: {"embedding_model": "test-model",
                                 "embedding_base": "http://embed.test/v1"})
    out = embed_mod.embedding_api(["x" * 6000, "y" * 6000, "z" * 6000])
    assert out.shape[0] == 3
    assert len(calls) >= 2  # 12000 字符预算 -> 6000*3 必拆批


def test_rerank_uses_configured_model_and_truncates(monkeypatch):
    captured = {}
    def fake_api_call(endpoint, body, timeout=60, retries=3):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return {"results": [{"index": 0, "relevance_score": 0.9}]}
    monkeypatch.setattr(embed_mod, "_api_call", fake_api_call)
    monkeypatch.setattr(embed_mod, "load_settings", lambda: {"rerank_model": "my-model"})
    out = embed_mod.rerank_api("q", ["a" * 5000] * 20, top_n=3)
    assert captured["endpoint"] == "rerank"
    assert captured["body"]["model"] == "my-model"
    assert len(captured["body"]["documents"]) <= 12
    assert all(len(d) <= 1000 for d in captured["body"]["documents"])
    assert out == [{"index": 0, "relevance_score": 0.9}]


def test_cached_embedding_hits_cache(monkeypatch):
    calls = {"n": 0}
    def fake(texts, task=None):
        calls["n"] += 1
        return np.zeros((1, 4), dtype=np.float32)
    monkeypatch.setattr(search_mod, "embedding_api", fake)
    search_mod._embedding_cache.clear()
    search_mod._cached_embedding("regression-query-xyz", index_version=999)
    search_mod._cached_embedding("regression-query-xyz", index_version=999)
    assert calls["n"] == 1


def test_cached_rerank_hits_cache(monkeypatch):
    calls = {"n": 0}
    def fake(q, docs, top_n=5, **kw):
        calls["n"] += 1
        return [{"index": 0, "relevance_score": 0.5}]
    monkeypatch.setattr(search_mod, "rerank_api", fake)
    monkeypatch.setattr(search_mod, "get_setting",
                        lambda k, d=None: True if k == "rerank_enabled" else d)
    monkeypatch.setattr(search_mod, "_rerank_fail_until", 0.0)
    search_mod._rerank_cache.clear()
    search_mod._cached_rerank("q-reg", ["a", "b"], 2)
    search_mod._cached_rerank("q-reg", ["a", "b"], 2)
    assert calls["n"] == 1


def test_cached_rerank_circuit_breaker(monkeypatch):
    calls = {"n": 0}
    def boom(q, docs, top_n=5, **kw):
        calls["n"] += 1
        raise RuntimeError("rerank down")
    monkeypatch.setattr(search_mod, "rerank_api", boom)
    monkeypatch.setattr(search_mod, "get_setting",
                        lambda k, d=None: True if k == "rerank_enabled" else d)
    monkeypatch.setattr(search_mod, "_rerank_fail_until", 0.0)
    search_mod._rerank_cache.clear()
    assert search_mod._cached_rerank("q-cb", ["a"], 1) == []
    assert search_mod._cached_rerank("q-cb", ["a"], 1) == []
    assert calls["n"] == 1  # 熔断后不再调用
