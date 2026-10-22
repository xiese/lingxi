"""知识库 API — 搜索/索引路由"""
import logging

from flask import request, jsonify

from ..routes import bp, trigger_index_refresh
from ..config import (
    NOTES_DIR, SCAN_DIRS, get_setting,
)
from ..search import search_index, _cosine_sim
from ..auth import check_auth, finfo, parse_fm
from ..services import index_state


# ── 搜索 ──

log = logging.getLogger("kb.routes_search")


def _int_arg(name, default, lo, hi):
    """解析整数查询参数，越界裁剪，非法值返回 None（调用方回 400）。

    原实现只做上限裁剪：`?limit=abc` 直接 ValueError→500，
    `?limit=-5` 会一路传到检索层，表现为「搜索无结果」而非参数错误。
    """
    try:
        return max(lo, min(int(request.args.get(name, str(default))), hi))
    except (TypeError, ValueError):
        return None

@bp.route("/api/search/semantic")
def semantic_search():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    q = request.args.get("q", "").strip()
    top = _int_arg("limit", 5, 1, 20)
    if top is None:
        return jsonify({"error": "limit 必须为整数"}), 400
    if not q:
        return jsonify({"error": "需要q"}), 400
    results, degraded = search_index.search(q, top, with_status=True)
    enriched = []
    for r in results:
        fp = NOTES_DIR / r["path"]
        if fp.exists():
            info = finfo(fp)
            info["score"] = r["score"]
            info["display_score"] = r.get("display_score", r["score"])
            info["snippet"] = r["snippet"]
            if "reranker_score" in r:
                info["reranker_score"] = r["reranker_score"]
            if "degrade" in r:
                # 2026-10-08 收口：rerank 降级登记（timeout/error/circuit_open）
                # 由 search 层透出，白名单不帶這個鍵的话调用方看不到降级模式
                info["degrade"] = r["degrade"]
            enriched.append(info)
        else:
            enriched.append(r)
    mode_name = f"semantic({get_setting('embedding_type')})"
    # 检测实际使用的搜索模式
    if get_setting("embedding_type") == "llm" and search_index.faiss_index is None:
        if _cosine_sim is None:
            mode_name = "bm25(fallback: no faiss/sklearn)"
        elif search_index.embeddings is None:
            mode_name = "bm25(fallback: embeddings not loaded)"
    payload = {"query": q, "mode": mode_name, "count": len(enriched), "items": enriched}
    if degraded:
        # 检索本身失败 ≠ 没有匹配结果。带上标记，避免调用方把两者混为一谈。
        payload["degraded"] = True
        payload["reason"] = degraded
    return jsonify(payload)


@bp.route("/api/search/mentions")
def search_mentions():
    """全库精确扫描：哪些笔记提到了某个字符串（含行号）。

    与 /api/search/semantic 的分工：
    - semantic：相关性排序，只回 top-N（limit 上限 20）—— 问「哪个最相关」
    - mentions：确定性覆盖，回全库所有提到的笔记（limit 上限 200 + 分页）—— 问「哪些提到了」

    适用「哪些笔记提到 X」「X 在哪些地方出现过」类问题。
    实测：RAG top5 对全库实体平均覆盖仅 4.6%，本端点可达 ~90%。
    """
    tk, err, code = check_auth("r")
    if err:
        return err, code
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "需要q"}), 400
    limit = _int_arg("limit", 50, 1, 200)
    if limit is None:
        return jsonify({"error": "limit 必须为整数"}), 400
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except Exception:
        offset = 0
    res = search_index.mentions(q, limit=limit, offset=offset)
    return jsonify(res)


@bp.route("/api/index/refresh", methods=["POST"])
@bp.route("/api/index/rebuild", methods=["POST"])
def refresh_index():
    """增量刷新搜索索引（对外统一名称为 refresh，rebuild 保留为兼容别名）。

    注意：这不是 admin 真全量。真全量请 POST /admin/rebuild-index（master）。
    """
    tk, err, code = check_auth("w")
    if err:
        return err, code
    # force=True 绕过 5 分钟冷却，确保外部 MCP 调用实际触发刷新
    trigger_index_refresh(force=True)
    return jsonify({
        "status": "triggered",
        "mode": "incremental_refresh",
        "note": index_state.public_refresh_response_note(),
    })


@bp.route("/api/index/status")
def index_status():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    emb_n = 0 if search_index.embeddings is None else int(search_index.embeddings.shape[0])
    faiss_n = 0
    if search_index.faiss_index is not None:
        try:
            faiss_n = int(search_index.faiss_index.ntotal)
        except Exception:
            log.warning("routes_search.py 异常: index_status", exc_info=True)
            faiss_n = 0
    return jsonify(index_state.build_status_payload(
        indexed=len(search_index.paths),
        has_index=search_index.bm25 is not None,
        has_embeddings=search_index.embeddings is not None,
        has_faiss=search_index.faiss_index is not None,
        dirty=search_index.dirty,
        index_consistent=getattr(search_index, "index_consistent", True),
        consistency_reason=getattr(search_index, "consistency_reason", "ok"),
        vector_search_ready=search_index.is_vector_search_ready() if hasattr(search_index, "is_vector_search_ready") else (search_index.embeddings is not None),
        paths_count=len(search_index.paths or []),
        embeddings_count=emb_n,
        faiss_count=faiss_n,
        state=getattr(search_index, "state", "dirty" if search_index.dirty else "clean"),
        state_reason=getattr(search_index, "state_reason", "ok"),
        rebuild_mode=getattr(search_index, "rebuild_mode", None),
    ))


@bp.route("/api/index/stale", methods=["GET"])
def index_stale():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    count = search_index.count_stale()
    total = len(search_index.paths)
    return jsonify({
        "stale_count": count,
        "indexed_count": total,
        "dirty": search_index.dirty,
        "stale_pct": round(count / max(total, 1) * 100, 1) if total > 0 else 0,
    })


# ── 全文搜索 ──

@bp.route("/search")
def search_notes():
    tk, err, code = check_auth("r")
    if err:
        return err, code
    q = request.args.get("q", "")
    tag = request.args.get("tag", "")
    lim = _int_arg("limit", 20, 1, 50)
    if lim is None:
        return jsonify({"error": "limit 必须为整数"}), 400
    if not q and not tag:
        return jsonify({"error": "需要q或tag"}), 400
    results = []
    for sd in SCAN_DIRS:
        if len(results) >= lim:
            break
        base = NOTES_DIR / sd
        if not base.exists():
            continue
        for f in base.rglob("*.md"):
            if len(results) >= lim:
                break
            try:
                rel = str(f.relative_to(NOTES_DIR))
            except ValueError:
                rel = str(f)
            # 2026-10-22：跳过 50-档案/_旧版本 下的历史副本。
            # 背景：kb_update 每改一次就在 50-档案/_旧版本/ 留一份同名副本，
            # 搜「1.0.4」13 条里 7 条是同一个笔记的旧副本，页面搜索框被刷屏。
            # 只跳过 _旧版本 这一层，50-档案 下正常的归档笔记仍可搜到。
            if rel.startswith("50-档案/_旧版本/"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                log.debug("routes_search.py 异常: search_notes", exc_info=True)
                continue
            try:
                meta, _ = parse_fm(content)
            except Exception:
                log.debug("routes_search.py 异常: search_notes", exc_info=True)
                meta = {}
            if tag:
                tags_list = meta.get("tags", [])
                if not isinstance(tags_list, list):
                    tags_list = []
                if tag not in tags_list:
                    continue
            matches = []
            if q and q.lower() in content.lower():
                lines = content.split("\n")
                for i, ln in enumerate(lines):
                    if q.lower() in ln.lower():
                        ctx = "\n".join(lines[max(0, i - 1):min(len(lines), i + 2)]).strip()
                        matches.append({"line": i + 1, "context": ctx})
                        if len(matches) >= 5:
                            break
            if q and not matches:
                continue
            info = finfo(f, pre_meta=meta)
            info["matches"] = matches
            results.append(info)
    return jsonify({"query": q, "tag": tag, "count": len(results), "items": results})