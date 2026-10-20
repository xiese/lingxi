"""知识库 MCP — 记忆检索辅助（2026-09-13 从 kb_mcp_server.py 拆出）"""
import os, re, sqlite3
from pathlib import Path
import logging

log = logging.getLogger("kb_mcp_memory")

# 外部长期记忆库（SQLite）。未配置 KB_MEMORY_DB 时默认指向用户主目录下的隐藏路径；
# 该路径不存在时相关工具返回空结果，不影响其余功能。
MEMORY_DB = Path(os.environ.get(
    "KB_MEMORY_DB", Path.home() / ".kb-memory" / "memory.sqlite3"))


def _sqlite_rows_to_markdown(query: str, rows) -> str:
    lines = [f"# 长期记忆搜索结果：{query}", "", f"- 命中：{len(rows)}", f"- 数据库：`{MEMORY_DB}`", ""]
    if not rows:
        lines.append("没有命中长期记忆。")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        rid = r.get("id", "")
        summary = (r.get("summary") or "").replace("\n", " ")[:240]
        content = (r.get("content") or "").replace("\n", " ")[:700]
        target = r.get("target") or ""
        source = r.get("source") or ""
        score = r.get("score", 0)
        updated = r.get("updated_at") or ""
        lines.append(f"## {i}. {summary or rid}")
        lines.append(f"- ID：`{rid}`")
        lines.append(f"- 分数：{score}")
        if target or source:
            lines.append(f"- 来源/目标：`{source}` → `{target}`")
        if updated:
            lines.append(f"- 更新时间：{updated}")
        lines.append(f"- 内容：{content}")
        lines.append("")
    return "\n".join(lines)
def get_memory_files():
    env = os.environ.get("KB_MEMORY_FILES", "").strip()
    if env:
        return [Path(x) for x in env.split(":") if x.strip()]
    home = Path.home()
    return [
        home / ".kb-memory" / "MEMORY.md",
        home / ".kb-memory" / "USER.md",
        home / ".kb-memory" / "AG_MEMORY.md",
    ]
def memory_chunks():
    chunks = []
    for fp in get_memory_files():
        if not fp.exists():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            log.warning("读取长期记忆文件失败，跳过: %s", fp, exc_info=True)
            continue
        parts = [p.strip() for p in text.replace("\r\n", "\n").split("§") if p.strip()]
        if not parts:
            parts = [text.strip()] if text.strip() else []
        for idx, part in enumerate(parts, 1):
            chunks.append({"source": str(fp), "index": idx, "text": part})
    return chunks
def score_text(query: str, text: str) -> int:
    q = query.lower().strip()
    t = text.lower()
    if not q:
        return 0
    score = 0
    if q in t:
        score += 20
    raw_tokens = query.replace("，", " " ).replace(",", " " ).replace("/", " " ).split()
    tokens = [x.lower().strip() for x in raw_tokens if len(x.strip()) >= 2]
    for tok in tokens:
        if tok in t:
            score += 5
    chars = [c for c in query if "\u4e00" <= c <= "\u9fff"]
    if chars:
        score += min(10, sum(1 for c in set(chars) if c in text))
    return score
def _memory_db_search_raw(query: str, limit: int = 6):
    """Search the external long-term memory SQLite store.

    优先使用 memories_fts FTS5 词法召回，失败时回退 LIKE。
    这里保持只读，不写入该数据库，避免污染外部记忆。
    """
    if not MEMORY_DB.exists() or MEMORY_DB.stat().st_size <= 0:
        return []
    limit = max(1, min(int(limit), 20))
    terms = [x.strip() for x in re.split(r"[\s,，、/]+", query) if len(x.strip()) >= 2][:8]
    if not terms:
        terms = list(dict.fromkeys([c for c in query if "\u4e00" <= c <= "\u9fff"]))[:8]
    rows = []

    def score_and_add(row):
        d=dict(row)
        text=(d.get('content') or '')+' '+(d.get('summary') or '')+' '+(d.get('target') or '')+' '+(d.get('source') or '')
        d['score']=score_text(query, text)
        # FTS 命中时 bm25_score 越小越相关；转成一点加分
        try:
            bm=float(d.get('bm25_score'))
            d['score'] += max(0, min(10, int(10/(1+abs(bm)))))
        except Exception:
            log.debug("bm25_score 无法转 float，跳过加分: %r", d.get('bm25_score'), exc_info=True)
        if d['score']>0:
            rows.append(d)

    try:
        conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        # 1) FTS5 优先。把多个 term 作为 OR 查询，避免中文/英文混合时过窄。
        if terms:
            fts_query = ' OR '.join('"'+t.replace('"',' ')+'"' for t in terms)
            try:
                sql = """
                SELECT m.id, m.content, m.summary, m.source, m.target, m.updated_at, m.metadata,
                       bm25(memories_fts) AS bm25_score
                FROM memories_fts
                JOIN memories m ON m.id = memories_fts.memory_id
                WHERE memories_fts MATCH ?
                  AND COALESCE(json_extract(m.metadata, '$.lifecycle'), '') NOT IN ('superseded','obsolete','rejected','archived')
                ORDER BY bm25(memories_fts) ASC, m.updated_at DESC
                LIMIT ?
                """
                for row in conn.execute(sql, [fts_query, max(limit*3, limit)]):
                    score_and_add(row)
            except Exception:
                # FTS 查询语法/分词异常时继续 LIKE fallback
                log.warning("memories_fts FTS5 查询失败，回退 LIKE 召回", exc_info=True)
        # 2) LIKE fallback / 补充召回
        if len(rows) < limit:
            clauses=[]; params=[]
            for t in terms:
                clauses.append("(content LIKE ? OR summary LIKE ? OR target LIKE ? OR source LIKE ?)")
                needle=f"%{t}%"
                params.extend([needle, needle, needle, needle])
            where = " OR ".join(clauses) if clauses else "1=1"
            sql = f"""
            SELECT id, content, summary, source, target, updated_at, metadata, NULL as bm25_score
            FROM memories
            WHERE ({where})
              AND COALESCE(json_extract(metadata, '$.lifecycle'), '') NOT IN ('superseded','obsolete','rejected','archived')
            ORDER BY updated_at DESC
            LIMIT ?
            """
            for row in conn.execute(sql, [*params, max(limit*4, limit)]):
                score_and_add(row)
        conn.close()
    except Exception as e:
        log.warning("长期记忆数据库查询失败", exc_info=True)
        return [{"id":"memory-db-error","summary":"长期记忆数据库查询失败","content":str(e),"score":0,"source":"","target":"","updated_at":""}]
    # 去重 + 排序
    dedup={}
    for r in rows:
        rid=r.get('id') or ''
        if rid not in dedup or r.get('score',0) > dedup[rid].get('score',0):
            dedup[rid]=r
    rows=list(dedup.values())
    rows.sort(key=lambda x: (x.get('score',0), x.get('updated_at') or ''), reverse=True)
    return rows[:limit]
def _file_memory_search_raw(query: str, limit: int = 6):
    rows = []
    for ch in memory_chunks():
        sc = score_text(query, ch["text"])
        if sc > 0:
            rows.append({**ch, "score": sc})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:max(1, min(int(limit), 20))]
def memory_search_raw(query: str, limit: int = 6):
    rows = _memory_db_search_raw(query, limit)
    if rows:
        return [{"kind":"memory-db", **r} for r in rows]
    return [{"kind":"file", **r} for r in _file_memory_search_raw(query, limit)]
def format_memory_markdown(query: str, rows) -> str:
    if rows and rows[0].get("kind") == "memory-db":
        return _sqlite_rows_to_markdown(query, rows)
    lines = [f"# 文件记忆搜索结果：{query}", "", f"- 命中：{len(rows)}", ""]
    if not rows:
        lines.append("没有命中长期记忆。")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        snippet = r["text"].replace("\n", " " )[:600]
        lines.append(f"## {i}. {Path(r['source']).name} / chunk {r['index']}")
        lines.append(f"- 来源：`{r['source']}`")
        lines.append(f"- 分数：{r['score']}")
        lines.append(f"- 内容：{snippet}")
        lines.append("")
    return "\n".join(lines)
