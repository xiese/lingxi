"""增量刷新拆分的接线回归（2026-10-08 拆分 build_embeddings_incremental /
incremental_refresh 后新增）。

钉住此前零覆盖的拆分点：
- 「已存在条目被替换 + 同时有删除」时，旧行号必须按 keep 之后的相对行号取
  （_classify_incremental_changes 的 removed 分支）；
- replace 与 new 两批嵌入的拼接顺序（replace 在前、new 在后）
  （_embed_incremental_entries → _assemble_incremental_embeddings）；
- 混合场景下端到端组装结果与 self.paths 新顺序一致。
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


def test_incremental_refresh_replace_remove_and_append_mixed():
    """替换 + 删除 + 新增同时发生：按 paths 新顺序组装，旧行取 keep 后相对行号。"""
    import kb_lib.search as S

    si = S.search_index
    # 旧索引：a / b / c 三行（单位矩阵行向量），本次之前清干净确保确定性
    si.paths, si.contents = ["a.md", "b.md", "c.md"], ["甲", "乙", "丙"]
    si.embeddings = np.eye(3)
    si.mtimes, si.bm25 = {}, None
    si.faiss_index = None
    # 本次变更：b.md 内容被替换、d.md 新增、a.md 被删除
    si._collect_docs = lambda diff_only=True: (
        ["b.md", "d.md"], ["乙新", "丁"], ["a.md"], {},
    )

    def embed(batch, task=None):
        # 替换批与新增批给不同向量，验证拼接顺序：replace 在前、new 在后
        return np.array(
            [[5.0, 0.0, 5.0] if c == "乙新" else [0.0, 6.0, 6.0] for c in batch]
        )

    with mock.patch.object(
        S, "get_setting",
        lambda k, d=None: "llm" if k == "embedding_type" else d,
    ), mock.patch.object(S, "embedding_api", embed):
        si.incremental_refresh()

    # 合并顺序：未变更条目（c.md）在前，changed（b.md, d.md）追加在后
    assert si.paths == ["c.md", "b.md", "d.md"], f"paths 顺序不对: {si.paths}"
    assert len(si.embeddings) == len(si.paths), \
        f"embeddings 与 paths 行数不一致: {len(si.embeddings)} vs {len(si.paths)}"
    assert np.allclose(si.embeddings[0], [0, 0, 1]), \
        f"c.md 应保留旧行（keep 后相对行号 1）: {si.embeddings[0]}"
    assert np.allclose(si.embeddings[1], [5, 0, 5]), \
        f"b.md 应为 replace 批的新嵌入: {si.embeddings[1]}"
    assert np.allclose(si.embeddings[2], [0, 6, 6]), \
        f"d.md 应为 new 批的新嵌入: {si.embeddings[2]}"
