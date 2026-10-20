"""索引状态机（A2）：clean / dirty / rebuilding / failed + refresh/rebuild 语义文案。

SearchIndex 持有运行态；本模块提供 status payload 与对外说明，避免路由硬编码字符串。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

VALID_STATES = frozenset({"clean", "dirty", "rebuilding", "failed"})

REFRESH_MEANS = "force incremental refresh（公开 /api/index/refresh 与 /api/index/rebuild 别名）"
REBUILD_MEANS = "admin 真全量：POST /admin/rebuild-index（master，build(is_rebuild=True)）"


def public_refresh_response_note() -> str:
    """公开 refresh/rebuild 接口返回说明：明确不是 admin 全量。"""
    return (
        "异步增量刷新已触发（公开 rebuild 是 refresh 别名，非 admin 全量；"
        "真全量请 POST /admin/rebuild-index）"
    )


def build_status_payload(
    *,
    indexed: int,
    has_index: bool,
    has_embeddings: bool,
    has_faiss: bool,
    dirty: bool,
    index_consistent: bool,
    consistency_reason: str,
    vector_search_ready: bool,
    paths_count: int,
    embeddings_count: int,
    faiss_count: int,
    state: str = "clean",
    state_reason: str = "ok",
    rebuild_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """组装 /api/index/status 响应体。"""
    st = state if state in VALID_STATES else ("dirty" if dirty else "clean")
    return {
        "indexed": indexed,
        "has_index": has_index,
        "has_embeddings": has_embeddings,
        "has_faiss": has_faiss,
        "dirty": dirty,
        "index_consistent": index_consistent,
        "consistency_reason": consistency_reason,
        "vector_search_ready": vector_search_ready,
        "paths_count": paths_count,
        "embeddings_count": embeddings_count,
        "faiss_count": faiss_count,
        "state": st,
        "state_reason": state_reason or "ok",
        "rebuild_mode": rebuild_mode,
        "refresh_means": REFRESH_MEANS,
        "rebuild_means": REBUILD_MEANS,
    }
