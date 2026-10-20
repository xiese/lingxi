# -*- coding: utf-8 -*-
"""精排缺失兜底的 BM25 强命中保底名额测试（2026-10-07）。

背景：alpha 融合兜底下，BM25-only 候选上限 0.4，vec-only 上限 0.6。
验证 _apply_bm25_floor 只做「保底名额」不做「加权」：
- 强 BM25 候选被纯向量压没时，会被提进 top；
- 强 BM25 候选已在 top 时，结果不变（不会把双轴候选越抬越高）；
- 无强候选 / 关断开关时，行为与原来完全一致。
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _reload():
    import kb_lib.search as m
    return importlib.reload(m)


def _mk(path, v, b):
    return (path, 0.0, {"path": path, "score": v, "snippet": "s"})


def _cands(n_vec, vec_score):
    return [(_mk("vec-only-%d" % i, vec_score, 0.0), {}) for i in range(n_vec)]


def test_strong_bm25_promoted_when_suppressed_by_vec():
    m = _reload()
    fused = [_mk("vec-%d" % i, 0.60, 0.0) for i in range(5)]
    bm = {"bm-strong": {"display_score": 0.9}}
    fused.append(_mk("bm-strong", 0.0, 0.0))
    fused.sort(key=lambda x: 0.6 * x[2]["score"], reverse=True)
    out = m._apply_bm25_floor(fused, bm, 5)
    names = [p for p, _, _ in out[:5]]
    assert "bm-strong" in names, names


def test_no_change_when_strong_already_in_top():
    m = _reload()
    fused = [_mk("bm-strong", 0.0, 0.0)] + [_mk("vec-%d" % i, 0.6, 0.0) for i in range(5)]
    bm = {"bm-strong": {"display_score": 0.9}}
    before = [p for p, _, _ in fused[:5]]
    out = m._apply_bm25_floor(fused, bm, 5)
    after = [p for p, _, _ in out[:5]]
    assert before == after, (before, after)


def test_no_strong_candidate_unchanged():
    m = _reload()
    fused = [_mk("vec-%d" % i, 0.6, 0.0) for i in range(5)] + [_mk("bm-weak", 0.0, 0.0)]
    bm = {"bm-weak": {"display_score": 0.2}}
    out = m._apply_bm25_floor(fused, bm, 5)
    assert [p for p, _, _ in out[:5]] == ["vec-0", "vec-1", "vec-2", "vec-3", "vec-4"]


def test_switch_off_matches_legacy():
    m = _reload()
    m.FALLBACK_BM25_SLOTS = 0
    fused = [_mk("vec-%d" % i, 0.6, 0.0) for i in range(5)] + [_mk("bm-strong", 0.0, 0.0)]
    bm = {"bm-strong": {"display_score": 0.9}}
    out = m._apply_bm25_floor(fused, bm, 5)
    assert "bm-strong" not in [p for p, _, _ in out[:5]]


def test_promotes_at_most_slots():
    m = _reload()
    fused = [_mk("vec-%d" % i, 0.6, 0.0) for i in range(5)]
    bm = {"bm-a": {"display_score": 0.95}, "bm-b": {"display_score": 0.9}}
    fused += [_mk("bm-a", 0.0, 0.0), _mk("bm-b", 0.0, 0.0)]
    out = m._apply_bm25_floor(fused, bm, 5)
    names = [p for p, _, _ in out[:5]]
    assert names.count("bm-a") + names.count("bm-b") == 1, names
