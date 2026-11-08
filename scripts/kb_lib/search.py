"""知识库 API — 搜索索引（BM25 + BGE-M3 向量混合检索）"""

import os, re, threading, time, hashlib, json, logging
from datetime import datetime, timezone
import numpy as np
from pathlib import Path
from rank_bm25 import BM25Okapi

# FAISS 可选导入——不可用时降级到 numpy/sklearn（向量搜索降约 10x，但对几千条笔记影响不大）
try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False

from .config import (
    NOTES_DIR, INDEX_FILE, EMBEDDINGS_FILE, FAISS_FILE, INDEX_MANIFEST_FILE,
    HAS_JIEBA, _jieba_tokenizer, load_settings, get_setting,
    is_excluded_path,
)
from .embed import embedding_api, rerank_api
from .services.text_clean import clean_for_match

log = logging.getLogger("kb.search")


# ── 全量重建进度跟踪（后台异步重建 + 进度查询）──
_progress_lock = threading.Lock()
# 全量嵌入的分片重建线程（全局单例：任何时刻只跑一个，避免几路写入同时起重建）
_embed_rebuild_lock = threading.Lock()
_embed_rebuild_thread = None
_rebuild_running = False          # 模块级互斥标志：后台全量重建进行中
_rebuild_thread = None
_rebuild_progress = {
    "running": False,
    "processed": 0,
    "total": 0,
    "state": "idle",              # idle | running | done | failed
    "error": "",
    "started_at": None,
    "finished_at": None,
}


def _iso_now():
    """当前时间 ISO8601（带本地时区偏移）"""
    try:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def get_rebuild_status():
    """返回全量重建进度快照（线程安全，供 GET /api/index/rebuild/status 使用）"""
    with _progress_lock:
        snap = dict(_rebuild_progress)
    snap["running"] = bool(snap.get("state") == "running")
    return snap


def is_rebuild_running():
    with _progress_lock:
        return bool(_rebuild_progress.get("state") == "running")


def _progress_begin(total=0):
    """标记全量重建开始；若已有重建在跑返回 False。"""
    global _rebuild_running
    with _progress_lock:
        if _rebuild_progress.get("state") == "running":
            return False
        _rebuild_running = True
        _rebuild_progress.update({
            "running": True,
            "processed": 0,
            "total": int(total or 0),
            "state": "running",
            "error": "",
            "started_at": _iso_now(),
            "finished_at": None,
        })
        return True


def _progress_set_total(total):
    with _progress_lock:
        if _rebuild_progress.get("state") == "running":
            _rebuild_progress["total"] = int(total or 0)


def _progress_advance(n):
    with _progress_lock:
        if _rebuild_progress.get("state") == "running":
            _rebuild_progress["processed"] = int(_rebuild_progress.get("processed", 0)) + int(n or 0)


def _progress_finish(ok=True, error=""):
    """结束全量重建：成功 → done；失败 → failed 并记录 error。"""
    global _rebuild_running
    with _progress_lock:
        _rebuild_running = False
        _rebuild_progress["running"] = False
        _rebuild_progress["state"] = "done" if ok else "failed"
        _rebuild_progress["error"] = "" if ok else str(error or "rebuild failed")
        if ok:
            _rebuild_progress["processed"] = int(_rebuild_progress.get("total", 0) or 0)
        _rebuild_progress["finished_at"] = _iso_now()

# 懒加载 sklearn cosine_similarity（仅 FAISS 降级时使用）
try:
    from sklearn.metrics.pairwise import cosine_similarity as _cosine_sim
except ImportError:
    log.warning("search.py 异常: 模块级", exc_info=True)
    _cosine_sim = None


def check_artifact_consistency(n_paths, n_contents, n_embeddings=None, n_faiss=None):
    """检查索引产物计数是否对齐。

    规则：
    - paths 与 contents 必须同长
    - 若提供 embeddings/faiss，则须与 paths 同长（或 faiss=0 表示未建/已禁用）
    返回 (ok: bool, reason: str)
    """
    try:
        n_paths = int(n_paths)
        n_contents = int(n_contents)
    except (TypeError, ValueError):
        log.warning("search.py 异常: check_artifact_consistency", exc_info=True)
        return False, "paths/contents 计数非法"

    if n_paths < 0 or n_contents < 0:
        return False, "paths/contents 计数为负"
    if n_paths != n_contents:
        return False, f"paths({n_paths}) 与 contents({n_contents}) 不一致"

    if n_embeddings is not None:
        try:
            n_embeddings = int(n_embeddings)
        except (TypeError, ValueError):
            log.warning("search.py 异常: check_artifact_consistency", exc_info=True)
            return False, "embeddings 计数非法"
        if n_embeddings != n_paths:
            return False, f"paths({n_paths}) 与 embeddings({n_embeddings}) 不一致"

    if n_faiss is not None:
        try:
            n_faiss = int(n_faiss)
        except (TypeError, ValueError):
            log.warning("search.py 异常: check_artifact_consistency", exc_info=True)
            return False, "faiss 计数非法"
        # n_faiss==0 表示未启用 FAISS / 已降级，不算错位
        if n_faiss > 0 and n_embeddings is not None and n_faiss != n_embeddings:
            return False, f"faiss({n_faiss}) 与 embeddings({n_embeddings}) 不一致"
        if n_faiss > 0 and n_faiss != n_paths:
            return False, f"faiss({n_faiss}) 与 paths({n_paths}) 不一致"

    return True, "ok"


def write_index_manifest(
    n_paths,
    n_contents,
    n_embeddings=0,
    n_faiss=0,
    index_version=0,
    path=None,
    extra=None,
):
    """原子写入 .index_manifest.json（temp + os.replace）。"""
    import os
    from datetime import datetime, timezone

    target = Path(path) if path is not None else INDEX_MANIFEST_FILE
    payload = {
        "schema_version": 1,
        "paths_count": int(n_paths),
        "contents_count": int(n_contents),
        "embeddings_count": int(n_embeddings or 0),
        "faiss_count": int(n_faiss or 0),
        "index_version": int(index_version or 0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return payload


def _atomic_write_bytes(target, data: bytes):
    import os
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


# ── 索引序列化：JSON（结构化）+ .npy（数值）──
#
# 旧格式是整包 pickle（`.search_index.pkl`），加载即执行任意代码：索引文件一旦
# 被替换（下载目录、同步盘、备份还原），等于给攻击者一个 import 期执行点。
# 新格式不执行代码：
#   * 结构化部分（paths / contents / mtimes / BM25 参数与词表）→ JSON
#   * 数值数组（BM25 的 doc_len / average_idf、doc_freqs 里的词频）→ .npz，
#     读取一律 allow_pickle=False
# BM25 模型不再整对象存盘，改为存「构造参数 + 统计量」，加载时用
# _bm25_from_payload 重建对象（行为与 rank_bm25.BM25Okapi 构造后一致）。
INDEX_FORMAT_VERSION = 2


def _bm25_to_payload(bm25):
    """把 BM25Okapi 拆成可 JSON 化的部分（返回 (json 部分, 数组部分)）。

    doc_freqs 是「每篇一个 dict」，体量与语料同阶；这里转成
    (行号, 词表下标, 词频) 三元组存进 npz，JSON 里词表只存一份，
    避免同一个词在每篇里重复出现把文件撑大。
    """
    if bm25 is None:
        return None, None
    vocab = {}
    rows, cols, vals = [], [], []
    for i, df in enumerate(getattr(bm25, "doc_freqs", None) or []):
        for term, freq in df.items():
            j = vocab.setdefault(str(term), len(vocab))
            rows.append(i)
            cols.append(j)
            vals.append(int(freq))
    json_part = {
        "k1": float(getattr(bm25, "k1", 1.5)),
        "b": float(getattr(bm25, "b", 0.75)),
        "epsilon": float(getattr(bm25, "epsilon", 0.25)),
        "corpus_size": int(getattr(bm25, "corpus_size", 0)),
        "avgdl": float(getattr(bm25, "avgdl", 0.0)),
        "average_idf": float(getattr(bm25, "average_idf", 0.0)),
        "idf": {str(k): float(v) for k, v in (getattr(bm25, "idf", None) or {}).items()},
        "vocab": list(vocab.keys()),
    }
    arrays = {
        "doc_len": np.asarray(getattr(bm25, "doc_len", []) or [], dtype=np.int64),
        "df_row": np.asarray(rows, dtype=np.int64),
        "df_col": np.asarray(cols, dtype=np.int64),
        "df_val": np.asarray(vals, dtype=np.int64),
    }
    return json_part, arrays


def _bm25_from_payload(json_part, arrays):
    """由 _bm25_to_payload 的产物重建 BM25Okapi（不读 pickle、不执行代码）。"""
    if not json_part:
        return None
    bm25 = BM25Okapi.__new__(BM25Okapi)
    bm25.k1 = float(json_part.get("k1", 1.5))
    bm25.b = float(json_part.get("b", 0.75))
    bm25.epsilon = float(json_part.get("epsilon", 0.25))
    bm25.corpus_size = int(json_part.get("corpus_size", 0))
    bm25.avgdl = float(json_part.get("avgdl", 0.0))
    bm25.average_idf = float(json_part.get("average_idf", 0.0))
    bm25.idf = {str(k): float(v) for k, v in (json_part.get("idf") or {}).items()}
    bm25.tokenizer = None
    vocab = list(json_part.get("vocab") or [])
    n_docs = int(arrays["doc_len"].shape[0]) if "doc_len" in arrays else 0
    doc_freqs = [dict() for _ in range(n_docs)]
    rows = arrays.get("df_row")
    cols = arrays.get("df_col")
    vals = arrays.get("df_val")
    if rows is not None and cols is not None and vals is not None:
        for r, c, v in zip(rows.tolist(), cols.tolist(), vals.tolist()):
            if 0 <= int(r) < n_docs and 0 <= int(c) < len(vocab):
                doc_freqs[int(r)][vocab[int(c)]] = int(v)
    bm25.doc_freqs = doc_freqs
    bm25.doc_len = arrays["doc_len"].tolist() if "doc_len" in arrays else []
    return bm25


def _index_arrays_path():
    """数值数组文件路径：与 JSON 主索引同名前缀，避免再引一个配置常量。"""
    return Path(str(INDEX_FILE) + ".arrays.npz")


def _dump_index_payload(paths, contents, mtimes, bm25):
    """把主索引写成新格式：`<INDEX_FILE>`（JSON）+ `<INDEX_FILE>.arrays.npz`。

    两个文件都走「临时文件 + fsync + os.replace」，先写数组再写 JSON：
    JSON 是加载方的入口，它出现即代表同批数组已经落盘。
    """
    json_part, arrays = _bm25_to_payload(bm25)
    arrays_path = _index_arrays_path()
    if arrays is None:
        arrays = {"doc_len": np.asarray([], dtype=np.int64)}
    tmp_npz = Path(str(arrays_path) + ".tmp")
    arrays_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_npz, "wb") as f:
        # 压缩存：词频/偏移是稀疏小整数，压缩后体积明显小于未压缩 npz
        np.savez_compressed(f, **arrays)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_npz, arrays_path)
    payload = {
        "format_version": INDEX_FORMAT_VERSION,
        "bm25": json_part,
        "paths": list(paths or []),
        "contents": list(contents or []),
        "mtimes": {str(k): float(v) for k, v in (mtimes or {}).items()},
    }
    # 紧凑分隔符：索引里以路径/词表为主，去掉多余空格能省下可观体积
    _atomic_write_bytes(
        INDEX_FILE,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def _load_index_payload():
    """读新格式主索引，返回 (payload, arrays)。

    数组文件缺失或损坏按读取失败处理（返回 (None, None)），由调用方决定
    是否全量重建；绝不回落到 pickle。
    """
    if not INDEX_FILE.exists():
        return None, None
    payload = json.loads(Path(INDEX_FILE).read_text(encoding="utf-8"))
    arrays = {}
    arrays_path = _index_arrays_path()
    if arrays_path.exists():
        with np.load(arrays_path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
    return payload, arrays


# ── P1: 索引内容级抽检（2026-10-07）──
# 背景：09-29 复发过「health 报 clean 但实际错位」——paths 与 contents 数量
# 一致、内容互相错位；check_artifact_consistency 只比数量，对这种错位完全
# 无感知。这里在 refresh/rebuild 落盘时做轻量内容级抽检：按索引顺序均匀抽
# CONTENT_SAMPLE_SIZE 篇，把「索引进内存内容的 SHA-256」记入 manifest
# （content_samples），再与磁盘文件正文哈希逐一比对；任何一片不一致即把
# index_consistent 置 false 并写 consistency_reason。只读文件 + 哈希，
# 不重新嵌入；CONTENT_SAMPLE_SIZE=0 时整体关掉。
CONTENT_SAMPLE_SIZE = 20

# ── 精排缺失兜底的 BM25 强命中保底（2026-10-07 加）──
# 背景：精排超时/失败走 alpha 融合兜底时，vec-only 候选上限 alpha=0.6，
# BM25-only 强字面命中候选上限 0.4；当查询词极稀有且向量面没覆盖时，
# 字面最优解会被压出 top。这里用「保底名额」而非「加权」：
# 加权实测会把双轴都强的候选越抬越高（见 2026-10-07 检索调研），
# 保底名额只保证至少 1 个强 BM25 候选在位，不动分数排序本体，零回归面。
# 注意：它不解决「精排超时导致整体排序塌降」——那需要机制级修复（另立）。
FALLBACK_BM25_FLOOR = 0.75   # bm25 归一分达到此值算「强命中」
FALLBACK_BM25_SLOTS = 1      # 兜底结果里至少保留几个强 BM25 候选；置 0 完全关断
_FM_HEAD_BYTES = 2048        # T4: 读 frontmatter 只读文件头这么多字节，避免整篇 IO
# T-23-a: 笔记重要度加成系数。frontmatter 写了 `importance: 1.0` 的笔记，
# 最终得分 = base × (1 + 1.0 × 0.5) = base × 1.5，即最高提升 50%。
# 用乘法而非加法：加法会让低分笔记靠 importance 反超高分笔记，等于置顶开关；
# 乘法保持原排序相对次序。没写 importance 的笔记乘 1.0，行为完全不变。
IMPORTANCE_BOOST = 0.5


def _decay_tau():
    """T4: 读「检索时间衰减」参数（天）。0 或非法值 = 关闭。

    设置项可能在运行中被改成非法值（Web 界面/手改 json），这里做兜底，
    绝不让一个坏配置把检索打挂。
    """
    try:
        v = get_setting("decay_tau_days", 0)
        v = float(v)
        if v <= 0 or v != v or v == float("inf"):
            return 0.0
        return min(v, 3650.0)
    except Exception:
        return 0.0



def _even_sample_indices(n, size=CONTENT_SAMPLE_SIZE):
    """按索引顺序均匀取至多 size 个下标（含首尾，去重）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return []
    if size <= 0:
        return []
    size = min(size, n)
    if size == 1:
        return [0]
    step = (n - 1) / float(size - 1)
    return sorted({int(round(i * step)) for i in range(size)})


def _sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()



def _apply_bm25_floor(fused, bm25_map, top):
    """精排缺失兜底：保证前 top 里至少有 FALLBACK_BM25_SLOTS 个 BM25 强命中候选。

    fused: [(path, fused_score, candidate)]，已按 fused_score 降序。
    bm25_map: path -> bm25_pool 行（含 display_score 归一分）。
    top: 本页结果条数。
    返回新列表（不改原顺序语义：强候选插到 top 末位，替换原末位）。
    """
    slots = FALLBACK_BM25_SLOTS
    if slots <= 0 or top <= 0 or len(fused) <= top:
        return fused
    floor = FALLBACK_BM25_FLOOR
    head = fused[:top]
    tail = fused[top:]
    have = sum(
        1 for p, _, _ in head
        if float((bm25_map.get(p) or {}).get("display_score", 0.0)) >= floor
    )
    need = slots - have
    for _ in range(max(0, need)):
        strong = [
            (p, sc, c) for p, sc, c in tail
            if float((bm25_map.get(p) or {}).get("display_score", 0.0)) >= floor
        ]
        if not strong:
            break
        promoted = strong[0]          # tail 已按 fused_score 降序，取首个即最强
        tail.remove(promoted)
        head = head[:-1] + [promoted]  # 插到 top 末位
    return head + tail

def _note_body_from_disk(notes_dir, rel):
    """按 _collect_docs 同款规则从磁盘读笔记正文（统一换行 + 剥 frontmatter）。

    规则必须与 _collect_docs 逐字一致，否则归一化差异会造成误报。
    """
    txt = (Path(notes_dir) / rel).read_text(encoding="utf-8")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"^---\n.*?\n---\n", "", txt, flags=re.DOTALL)


def sample_content_digests(paths, contents, notes_dir, size=CONTENT_SAMPLE_SIZE):
    """对抽样篇目取「索引进内存内容」的 SHA-256，返回 [{path, sha256}]。

    记的是索引实际持有的内容（即参与 BM25/向量的内容），manifest 落这份；
    verify 时与磁盘文件正文比对，两侧即「索引认为的」对「磁盘上真实的」。
    paths/contents 长度不一致或为空时返回 []（调用方按无记录处理）。
    """
    out = []
    paths = list(paths or [])
    contents = list(contents or [])
    if not paths or not contents or len(paths) != len(contents):
        return out
    for i in _even_sample_indices(len(paths), size):
        try:
            out.append({"path": paths[i], "sha256": _sha256_text(contents[i])})
        except Exception:
            log.warning("search.py 异常: sample_content_digests", exc_info=True)
    return out


def verify_content_samples(samples, notes_dir):
    """内容级抽检：manifest 记录的索引内容哈希 vs 磁盘文件正文哈希。

    返回 (ok, reason)。samples 为空 = 无记录可查，不判负。
    读取失败也判不一致（索引声称存在的文件磁盘上读不到）。
    """
    if not samples:
        return True, "ok"
    for s in samples:
        rel = (s or {}).get("path")
        rec = (s or {}).get("sha256")
        try:
            cur = _sha256_text(_note_body_from_disk(notes_dir, rel))
        except Exception:
            log.warning("search.py 异常: verify_content_samples", exc_info=True)
            return False, "内容抽检无法读取: %s" % rel
        if cur != rec:
            return False, "内容抽检不一致: %s" % rel
    return True, "ok"


def _atomic_np_save(target, arr):
    import os
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp.npy")
    np.save(tmp, arr)
    os.replace(tmp, target)


def _tokenize(text):
    """分词：使用 jieba（若可用）否则简单按空白切分"""
    if HAS_JIEBA:
        return list(_jieba_tokenizer(text))
    return text.split()


# ── 嵌入查询缓存（TTL 300秒，key 为 query MD5，线程安全，最大 1000 条防内存泄漏）──

_embedding_cache = {}
_EMB_CACHE_TTL = 300
_EMB_CACHE_MAX_SIZE = 1000
_embedding_cache_lock = threading.Lock()


def _trim_embedding_cache():
    """在锁内调用：缓存超限时移除 30% 最旧条目（基于插入时间）"""
    if len(_embedding_cache) <= _EMB_CACHE_MAX_SIZE:
        return
    sorted_items = sorted(_embedding_cache.items(), key=lambda x: x[1][1])
    remove_count = int(len(sorted_items) * 0.3)
    for k, _ in sorted_items[:remove_count]:
        del _embedding_cache[k]


def _cached_embedding(query, index_version=0):
    """带 TTL 缓存的 embedding 查询，避免相同 query 重复调用 API（key 含 model 名 + index_version 防版本污染）

    Args:
        query: 查询文本
        index_version: 搜索索引版本号，缓存 key 依赖此值避免脏缓存
    """
    s = load_settings()
    model = s.get("embedding_model", "unknown")
    key = hashlib.md5(f"{query}|{model}|{index_version}".encode("utf-8")).hexdigest()
    now = time.time()
    # 无锁读取（Python dict 读是原子的）
    entry = _embedding_cache.get(key)
    if entry is not None and now - entry[1] < _EMB_CACHE_TTL:
        return entry[0]
    with _embedding_cache_lock:
        # double-check
        entry = _embedding_cache.get(key)
        if entry is not None and now - entry[1] < _EMB_CACHE_TTL:
            return entry[0]
        vec = embedding_api(query, task="retrieval.query")
        _embedding_cache[key] = (vec, now)
        _trim_embedding_cache()
        return vec


# ── count_stale 缓存（TTL 15 秒，/health 会被频繁调用，避免每次全盘 stat ~900 文件）──

_stale_cache = {"count": 0, "ts": 0.0}
_STALE_CACHE_TTL = 15
_stale_cache_lock = threading.Lock()
_stale_prime_thread = None


def _invalidate_stale_cache():
    with _stale_cache_lock:
        _stale_cache["ts"] = 0.0


# ── rerank 缓存（TTL 300 秒，相同查询重复搜索不再重复打精排 API）──

_rerank_cache = {}
_RERANK_CACHE_TTL = 300
_RERANK_CACHE_MAX = 500
_rerank_cache_lock = threading.Lock()
_rerank_fail_until = 0.0

# ── rerank 降级显式化（2026-10-07 机制级修复）──
# 背景：精排超时/失败时 _hybrid_search 原先静默走 alpha 融合兜底，调用方与运维
# 都无法从结果看出「本轮没有精排」。现把降级模式登记到线程本地，_hybrid_search
# 读取后三件事闭环：① 打带 query/耗时/候选数/模式的 warning；
# ② 把模式写进结果元数据（degrade=rerank_timeout / rerank_error 等）；
# ③ 兜底仍走既有 alpha 融合 + FALLBACK_BM25_FLOOR/SLOTS 保底名额（逻辑不变）。
# 线程本地而非全局：并发检索互不污染；_cached_rerank 入口清零、
# _hybrid_search 调用后取走，杜绝跨请求残留。
RERANK_DEGRADE_TIMEOUT = "rerank_timeout"           # 单次超时（不熔断）
RERANK_DEGRADE_ERROR = "rerank_error"               # 服务不可用（已熔断 300s）
RERANK_DEGRADE_CIRCUIT_OPEN = "rerank_circuit_open"  # 熔断窗内，本轮未打精排

_rerank_degrade_state = threading.local()


def _rerank_degrade_set(mode, query="", elapsed_s=0.0, n_docs=0):
    """登记本轮 rerank 降级（线程本地）。"""
    try:
        n = int(n_docs or 0)
    except (TypeError, ValueError):
        n = 0
    _rerank_degrade_state.info = {
        "mode": mode,
        "query": query or "",
        "elapsed_s": round(float(elapsed_s or 0.0), 3),
        "n_docs": n,
    }


def _rerank_degrade_take():
    """取走并清空当前线程的降级记录；无登记返回 None。"""
    info = getattr(_rerank_degrade_state, "info", None)
    _rerank_degrade_state.info = None
    return info

# 精排超时（秒）与重试次数。精排是可选增强：超时/失败即返回 []，
# -> search() 走「向量+BM25 融合」兜底，结果不为空。
# 2026-09-23 方案 B：单次超时**不再触发熔断**（只对真正的服务不可用触发），
# 理由见下面 _cached_rerank 里的注释。
# 取值依据（2026-09-23 实测 n=24）：精排 p50 1324ms / p90 2441ms / max 5920ms；
# 取 3s 覆盖约 90% 正常请求，最坏端到端约 6.9s，仍在消费端 8s 超时以内。
# retries=1 必须同时设：_api_call 默认 retries=3 且指数退避（2+4=6s），
# 只加 timeout 会让一次超时拖成 3 次共 12s，比改前更慢。
# 回滚：改回 rerank_api(query, docs, top_n=top_n)（即去掉这两个参数）。
_RERANK_TIMEOUT = 3
_RERANK_RETRIES = 1


# 送精排的单篇文档字符预算与窗口宽度（2026-09-29）。
# rerank_api 会把每篇文档截断到 1000 字符（d[:1000]），所以送「全文」等于只送前 1000 字符。
_HIT_WINDOW_WIDTH = 350


def _hit_window(body, query, width=_HIT_WINDOW_WIDTH):
    """取正文中命中 query 词的最佳窗口，让关键词落进精排的 1000 字符预算内。

    背景（2026-09-29 实测）：原实现送 c["snippet"] + 全文，被 rerank_api 截断到
    前 1000 字符；当关键词位于长文深处（实测最远 15239 字符）时精排永远看不到它，
    候选被压低排不进 top5 —— 8/25 硬用例即因此漏检（目标已在候选池、排名 2~10）。
    改为送「命中词附近窗口」后：25 条硬用例 88% → 100%，回归 0，且因送出的文档
    更短而更快（57.8s → 31.6s）。

    选窗判据（2026-10-04 修正）：原实现 `body.find(t)` 只取每个词的**首次**出现，
    且按 `100000 - pos` 打分 = 「越靠前越好」。长文里查询词往往在第 1 章和第 9 章
    各出现一次，取首次出现就把窗口钉在第 1 章，真正多词共现的第 9 章永远进不了
    预算 —— 多词查询因此经常只覆盖到一个词。现在改为：在所有出现位置里找
    **覆盖不同查询词最多**的窗口，同分时取靠前的（保留原有偏好）。
    """
    if not body:
        return ""
    terms = [
        t for t in re.findall(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{2,}", query)
        if len(t) >= 3
    ]
    if not terms:
        return ""
    # 收集每个词的出现位置（上限防止超长文里的高频词拖慢检索）
    _MAX_OCC = 200
    hits = []  # (pos, term)
    for t in set(terms):
        start = 0
        for _ in range(_MAX_OCC):
            pos = body.find(t, start)
            if pos < 0:
                break
            hits.append((pos, t))
            start = pos + 1
    if not hits:
        return ""
    hits.sort()
    # 滑窗：找覆盖不同查询词最多的区间，窗口宽度为 2*width
    span = width * 2
    best_cnt, best_lo, best_hi = -1, 0, 0
    counts, distinct, lo = {}, 0, 0
    for hi in range(len(hits)):
        t = hits[hi][1]
        counts[t] = counts.get(t, 0) + 1
        if counts[t] == 1:
            distinct += 1
        while hits[hi][0] - hits[lo][0] > span:
            lt = hits[lo][1]
            counts[lt] -= 1
            if counts[lt] == 0:
                distinct -= 1
            lo += 1
        # 同分取更靠前的窗口（hits 已按 pos 升序，先到先得）
        if distinct > best_cnt:
            best_cnt, best_lo, best_hi = distinct, lo, hi
    # 窗口中心 = 命中区中点，保证首尾命中词都落在预算内
    center = (hits[best_lo][0] + hits[best_hi][0]) // 2
    start = max(0, center - width)
    return body[start:start + span]


def _rerank_doc(candidate, contents, query):
    """构造送精排的单篇文档：snippet + 命中词窗口（未命中时退回原文开头）。

    2026-10-17：送精排的正文先过 `clean_for_match` 去模板套话。
    背景：服务端生成的五节模板笔记，前 200–400 字字面完全一致
    （`# 标题` + 「本记录用于沉淀…」+ 五节标题），snippet 恰好只取前 200 字，
    于是套话成了「相似度证据」——无关内容也拿高分。清洗规则集中在
    `services/text_clean.py`，不在本函数里硬编码。
    """
    body = contents[candidate["idx"]] if "idx" in candidate else ""
    body = clean_for_match(body)
    win = _hit_window(body, query)
    if not win:
        win = body[:800]
    return clean_for_match(candidate["snippet"]) + " " + win


def _cached_rerank(query, docs, top_n):
    """带缓存的精排调用。

    熔断（5 分钟）只针对**服务不可用**（网络错误 / 5xx / 解析失败）；
    单次**超时**不熔断，只丢这一次、走融合兜底 —— 见函数内注释。
    """
    global _rerank_fail_until
    _rerank_degrade_state.info = None  # 入口清零：只登记本轮降级，不带入上轮残留
    if not get_setting("rerank_enabled", True):
        return []
    now = time.time()
    if now < _rerank_fail_until:
        # 熔断窗内：本轮精排被前序失败禁用，结果同样缺精排 —— 显式登记，
        # 让 _hybrid_search 的兜底结果带上降级标记（见 _rerank_degrade_set 注释）。
        _rerank_degrade_set(RERANK_DEGRADE_CIRCUIT_OPEN, query=query,
                            elapsed_s=0.0, n_docs=len(docs or []))
        return []
    key = hashlib.md5((query + "||" + str(top_n) + "||" + "|".join(docs)).encode("utf-8")).hexdigest()
    entry = _rerank_cache.get(key)
    if entry is not None and now - entry[1] < _RERANK_CACHE_TTL:
        return entry[0]
    with _rerank_cache_lock:
        entry = _rerank_cache.get(key)
        if entry is not None and now - entry[1] < _RERANK_CACHE_TTL:
            return entry[0]
        t0 = time.time()
        try:
            res = rerank_api(query, docs, top_n=top_n, timeout=_RERANK_TIMEOUT, retries=_RERANK_RETRIES)
        except TimeoutError:
            elapsed = time.time() - t0
            # 单次超时只说明"这一次慢"，**不能证明精排服务挂了** —— 不触发熔断。
            # 2026-09-23 实测：熔断是全局的，一次超时会禁用后续 300s 的全部精排
            # （紧循环 40 次查询只有 42% 用上精排）。超时属单次事件，不该当成服务健康判据。
            # 注意：TimeoutError 是 OSError 子类，必须排在下面的 Exception 之前，否则会被吞掉。
            log.warning(
                "search.py 精排单次超时（不熔断，走融合兜底）query=%r 耗时=%.3fs 候选数=%d 降级=%s",
                query, elapsed, len(docs), RERANK_DEGRADE_TIMEOUT,
            )
            _rerank_degrade_set(RERANK_DEGRADE_TIMEOUT, query=query,
                                elapsed_s=elapsed, n_docs=len(docs or []))
            return []
        except Exception:
            elapsed = time.time() - t0
            # 网络错误 / 5xx / 响应解析失败 = 服务不可用，按原语义熔断 300s。
            log.warning(
                "search.py 异常: _cached_rerank query=%r 耗时=%.3fs 候选数=%d 降级=%s",
                query, elapsed, len(docs), RERANK_DEGRADE_ERROR, exc_info=True,
            )
            _rerank_degrade_set(RERANK_DEGRADE_ERROR, query=query,
                                elapsed_s=elapsed, n_docs=len(docs or []))
            _rerank_fail_until = time.time() + 300
            return []
        _rerank_cache[key] = (res, now)
        if len(_rerank_cache) > _RERANK_CACHE_MAX:
            items = sorted(_rerank_cache.items(), key=lambda x: x[1][1])
            for k, _ in items[: max(1, int(len(items) * 0.3))]:
                _rerank_cache.pop(k, None)
        return res


class SearchUnavailable(RuntimeError):
    """检索原语不可用 —— 与「检索成功但没有匹配」区分开。

    原实现把两者都表达为返回空列表，于是索引损坏 / 分词异常 / 依赖缺失
    在接口层表现为「知识库没有相关内容」：问答接口把失败渲染成
    「无相关笔记」喂给 LLM，查重接口把失败当成「新文档」重复写入。
    零配置默认走 BM25 路径，恰好是最容易静默失败的一条。
    """


class SearchIndex:
    def __init__(self):
        self.bm25 = None
        self.paths = []
        self.contents = []
        self.embeddings = None
        self.faiss_index = None
        # t6: IndexIDMap 稳定 id 映射（path -> 整数 id），支持真增量增删
        self.faiss_ids = {}
        self._faiss_next_id = 1
        self.mtimes = {}
        self._date_cache = {}   # T4: rel -> epoch 秒（-1 = 无日期），惰性填、随索引清空
        self._importance_cache = {}  # T-23-a: rel -> 0.0~1.0，惰性填、随索引清空
        self._meta_cache = {}  # 2026-10-22: rel -> 标题+标签小写文本，惰性填、随索引清空
        self.lock = threading.Lock()
        self.dirty = False
        self._index_version = 0  # 索引版本号，重建/增量更新时 bump，嵌入缓存 key 依赖此值避免脏缓存
        # A0: 向量面是否与 paths 对齐；False 时禁用 _vec_search，避免语义搜指错文
        self.index_consistent = True
        self.consistency_reason = "ok"
        # A2: 索引生命周期状态机
        self.state = "clean"  # clean | dirty | rebuilding | failed
        self.state_reason = "ok"
        self.rebuild_mode = None  # None | "incremental" | "full"

    def mark_dirty(self, reason: str = "write"):
        """标记索引待刷新（写后 / 外部变更）。"""
        self.dirty = True
        if self.state != "rebuilding":
            self.state = "dirty"
        self.state_reason = reason or "write"

    def begin_rebuild(self, mode: str = "incremental"):
        """进入重建中（incremental=公开 refresh，full=admin 全量）。"""
        self.state = "rebuilding"
        self.rebuild_mode = mode if mode in ("incremental", "full") else "incremental"
        self.state_reason = f"rebuilding:{self.rebuild_mode}"

    def end_rebuild(self, ok: bool = True, error: str = ""):
        """结束重建：成功 → clean；失败 → failed。"""
        _invalidate_stale_cache()
        if ok:
            self.dirty = False
            self.state = "clean"
            self.state_reason = "ok"
            self.rebuild_mode = None
        else:
            self.state = "failed"
            self.state_reason = error or "rebuild_failed"
            self.dirty = True

    def is_vector_search_ready(self):
        """向量检索可用：有对齐 embeddings，且一致性标志为 True。"""
        if not self.index_consistent:
            return False
        if self.embeddings is None:
            return False
        try:
            return int(self.embeddings.shape[0]) == len(self.paths) and len(self.paths) > 0
        except Exception:
            log.warning("search.py 异常: is_vector_search_ready", exc_info=True)
            return False

    def _recompute_consistency(self):
        n_paths = len(self.paths or [])
        n_contents = len(self.contents or [])
        n_emb = None if self.embeddings is None else int(self.embeddings.shape[0])
        n_faiss = None
        if self.faiss_index is not None:
            try:
                n_faiss = int(self.faiss_index.ntotal)
            except Exception:
                log.warning("search.py 异常: _recompute_consistency", exc_info=True)
                n_faiss = None
        # 无 embeddings 时只校验 paths/contents
        ok, reason = check_artifact_consistency(
            n_paths,
            n_contents,
            n_embeddings=n_emb,
            n_faiss=n_faiss if n_emb is not None else None,
        )
        self.index_consistent = ok
        self.consistency_reason = reason
        return ok, reason

    def publish_index_artifacts(self, write_embeddings=True, write_faiss=True):
        """将当前内存索引原子落盘：pkl +（可选）npy/faiss + manifest。"""
        n_paths = len(self.paths or [])
        n_contents = len(self.contents or [])
        n_emb = 0 if self.embeddings is None else int(self.embeddings.shape[0])
        n_faiss = 0
        if self.faiss_index is not None:
            try:
                n_faiss = int(self.faiss_index.ntotal)
            except Exception:
                log.warning("search.py 异常: publish_index_artifacts", exc_info=True)
                n_faiss = 0

        ok, reason = check_artifact_consistency(
            n_paths, n_contents, n_embeddings=n_emb if n_emb else None, n_faiss=n_faiss if n_faiss else None
        )
        # 即使仅 BM25 也可写 pkl；向量面不一致时禁用向量并清空向量落盘意图
        self.index_consistent = ok if (n_emb == 0 or n_emb == n_paths) else False
        if n_emb and n_emb != n_paths:
            self.index_consistent = False
            self.consistency_reason = reason
            write_embeddings = False
            write_faiss = False
            self.embeddings = None
            self.faiss_index = None
            n_emb = 0
            n_faiss = 0
        else:
            self.consistency_reason = reason if not ok else "ok"

        # 主索引（JSON + npz，不执行代码）
        _dump_index_payload(self.paths, self.contents, self.mtimes, self.bm25)

        if write_embeddings and self.embeddings is not None and n_emb == n_paths:
            _atomic_np_save(EMBEDDINGS_FILE, self.embeddings)
        if write_faiss and self.faiss_index is not None and n_faiss == n_paths and _HAS_FAISS:
            import os
            tmp = Path(str(FAISS_FILE) + ".tmp")
            faiss.write_index(self.faiss_index, str(tmp))
            os.replace(tmp, FAISS_FILE)

        write_index_manifest(
            n_paths=n_paths,
            n_contents=n_contents,
            n_embeddings=n_emb if (write_embeddings and self.embeddings is not None) else 0,
            n_faiss=n_faiss if (write_faiss and self.faiss_index is not None) else 0,
            index_version=self._index_version,
            path=INDEX_MANIFEST_FILE,
            extra={"consistent": self.index_consistent, "reason": self.consistency_reason},
        )
        return self.index_consistent

    def _collect_docs(self, diff_only=False):
        paths, contents = [], []
        new_mtimes = {}
        for f in NOTES_DIR.rglob("*.md"):
            rel = str(f.relative_to(NOTES_DIR))
            if is_excluded_path(rel, exclude_archived=True):
                continue
            if "{{title}}" in rel or f.name.startswith("{{"):
                continue
            try:
                mtime = f.stat().st_mtime
                new_mtimes[rel] = mtime
                if diff_only and rel in self.mtimes and self.mtimes.get(rel) == mtime:
                    continue
                txt = f.read_text(encoding="utf-8")
                # 统一换行符：CRLF/CR → LF（与 auth.py parse_fm 保持一致）
                txt = txt.replace("\r\n", "\n").replace("\r", "\n")
                body = re.sub(
                    r"^---\n.*?\n---\n", "", txt, flags=re.DOTALL
                )
                paths.append(rel)
                contents.append(body)
            except Exception:
                log.warning("读取笔记失败: %s", rel, exc_info=True)
        removed = [p for p in (self.paths or []) if p not in new_mtimes]
        return paths, contents, removed, new_mtimes

    def _save_index(self):
        try:
            # A0: 原子写 pkl，并同步 manifest（embeddings 计数以内存为准）
            n_emb = 0 if self.embeddings is None else int(self.embeddings.shape[0])
            n_faiss = 0
            if self.faiss_index is not None:
                try:
                    n_faiss = int(self.faiss_index.ntotal)
                except Exception:
                    log.warning("search.py 异常: _save_index", exc_info=True)
                    n_faiss = 0
            _dump_index_payload(self.paths, self.contents, self.mtimes, self.bm25)
            self._recompute_consistency()
            # P1（2026-10-07）：内容级抽检——计数一致不代表不错位（09-29 事故）。
            # 均匀抽 20 篇，把索引内容的 SHA-256 记入 manifest，再与磁盘文件
            # 正文比对；不一致即禁用向量面（BM25 仍可用），并在 manifest 留痕。
            _samples = sample_content_digests(self.paths, self.contents, NOTES_DIR)
            _ok_cs, _reason_cs = verify_content_samples(_samples, NOTES_DIR)
            if not _ok_cs:
                self.index_consistent = False
                self.consistency_reason = _reason_cs
            _extra = {"consistent": self.index_consistent, "reason": self.consistency_reason}
            if _samples:
                _extra["content_samples"] = _samples
            write_index_manifest(
                n_paths=len(self.paths or []),
                n_contents=len(self.contents or []),
                n_embeddings=n_emb if self.index_consistent else 0,
                n_faiss=n_faiss if self.index_consistent else 0,
                index_version=self._index_version,
                path=INDEX_MANIFEST_FILE,
                extra=_extra,
            )
        except Exception:
            log.warning("索引落盘失败", exc_info=True)

    def build(self, is_rebuild=False, _tracked=False):
        mode = "full" if is_rebuild else "incremental"
        self.begin_rebuild(mode=mode)
        # 进度跟踪：_tracked=True 表示调用方（后台异步任务）已 _progress_begin，
        # 进度更新由本函数负责，收尾由调用方负责；否则本函数自行接管并收尾。
        _owns = bool(_tracked)
        _finish = False
        if is_rebuild and not _owns:
            _owns = _progress_begin(len(self.contents or []))
            _finish = _owns
        try:
            paths, contents, removed, mtimes = self._collect_docs(diff_only=False)
            if not contents:
                # 安全加固：原先直接 return，paths/contents/bm25/
                # embeddings/faiss 全部保持旧值 —— 删光笔记后已删内容仍能搜到，
                # 且 mtimes 里还留着已删文件使 count_stale 恒为 0，没有任何力量
                # 纠正；重启后从 pkl 读回来依旧。这里把索引三产物一并清空。
                self._reset_index_empty()
                self.end_rebuild(ok=True)
                if _finish:
                    _progress_finish(ok=True)
                return
            if _owns:
                _progress_set_total(len(contents))
            tokenized = [_tokenize(c) for c in contents]
            with self.lock:
                self.bm25 = BM25Okapi(tokenized)
                self.paths, self.contents = paths, contents
                self.mtimes = mtimes
                self._date_cache = {}   # T4: 全量重建，日期缓存整体失效
                self._importance_cache = {}  # T-23-a: 同上
                self._meta_cache = {}   # 2026-10-22: 标题/标签缓存同步清空
                self._index_version += 1  # Bump: 失效旧嵌入缓存
                self._token_sets = None    # 失效分词集合缓存
                self._save_index()  # 锁内持久化，避免锁外写入时读到不一致状态
            if get_setting("embedding_type") == "llm":
                if is_rebuild:
                    self.build_embeddings()
                else:
                    self._build_embeddings_background()
            self.end_rebuild(ok=True)
            if _finish:
                _progress_finish(ok=True)
        except Exception as e:
            self.end_rebuild(ok=False, error=str(e))
            if _finish:
                _progress_finish(ok=False, error=str(e))
            raise

    @staticmethod
    def _build_faiss_index(embeddings, ids=None):
        """从嵌入矩阵构建 FAISS 索引（L2 归一化 + IndexIDMap(IndexFlatIP) = 余弦相似度）

        ids=None 时使用 0..n-1 顺序 id（等价旧行为）；传 ids 时用 add_with_ids（真增量前提）。
        """
        if not _HAS_FAISS or embeddings is None or embeddings.shape[0] == 0:
            return None
        dim = embeddings.shape[1]
        normed = np.asarray(embeddings, dtype=np.float32).copy()
        faiss.normalize_L2(normed)
        idx = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
        if ids is None:
            ids = np.arange(normed.shape[0], dtype=np.int64)
        ids = np.asarray(ids, dtype=np.int64)
        if ids.shape[0] != normed.shape[0]:
            return None
        idx.add_with_ids(normed, ids)
        return idx

    # ── t6: FAISS 真增量（IndexIDMap + 稳定 id 映射）──

    def _faiss_ids_file(self):
        """稳定 id 映射的旁路落盘文件（与 FAISS_FILE 同目录）。"""
        return Path(str(FAISS_FILE) + ".ids.json")

    def _save_faiss_ids(self):
        try:
            payload = {
                "next_id": int(self._faiss_next_id or 1),
                "ids": dict(self.faiss_ids or {}),
            }
            _atomic_write_bytes(
                self._faiss_ids_file(),
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception:
            log.warning("faiss id 映射落盘失败", exc_info=True)

    def _load_faiss_ids(self):
        try:
            p = self._faiss_ids_file()
            if not p.exists():
                return False
            d = json.loads(p.read_text(encoding="utf-8"))
            ids = d.get("ids") or {}
            self.faiss_ids = {str(k): int(v) for k, v in ids.items()}
            nxt = d.get("next_id")
            if not nxt:
                nxt = (max(self.faiss_ids.values()) + 1) if self.faiss_ids else 1
            self._faiss_next_id = int(nxt)
            return bool(self.faiss_ids)
        except Exception:
            log.warning("faiss id 映射读取失败", exc_info=True)
            return False

    def _ensure_faiss_ids(self):
        """补齐缺失 id，返回与 self.paths 同序的 id 列表；不删除任何既有 id。"""
        changed = False
        for p in (self.paths or []):
            if p not in self.faiss_ids:
                self.faiss_ids[p] = self._faiss_next_id
                self._faiss_next_id += 1
                changed = True
        if changed:
            self._save_faiss_ids()
        return [self.faiss_ids[p] for p in (self.paths or [])]

    def _prune_faiss_ids(self):
        """清掉已不在 self.paths 里的 id（仅影响旁路映射，不动索引）。"""
        valid = set(self.paths or [])
        stale = [p for p in list((self.faiss_ids or {}).keys()) if p not in valid]
        for p in stale:
            self.faiss_ids.pop(p, None)
        if stale:
            self._save_faiss_ids()

    def _faiss_apply_incremental(self, changed_paths, removed):
        """真增量同步 FAISS：删 removed/changed 旧向量，补 changed/新增新向量。

        返回 True=增量成功；False=情形不确定，调用方应退回全量重建。
        任何异常都返回 False —— 最坏情况等价于改动前的整表重建，不会写坏索引。
        """
        if not _HAS_FAISS or self.faiss_index is None or self.embeddings is None:
            return False
        if not isinstance(self.faiss_index, faiss.IndexIDMap):
            return False
        try:
            ids = self._ensure_faiss_ids()
            if len(ids) != int(self.embeddings.shape[0]):
                return False
            pos_of = {p: j for j, p in enumerate(self.paths or [])}
            # 1) 删除 removed 全部 + changed 的旧向量
            del_ids = set()
            for p in list(removed or []):
                _i = self.faiss_ids.get(p)
                if _i is not None:
                    del_ids.add(int(_i))
            for p in list(changed_paths or []):
                _i = self.faiss_ids.get(p)
                if _i is not None:
                    del_ids.add(int(_i))
            if del_ids:
                self.faiss_index.remove_ids(np.asarray(sorted(del_ids), dtype=np.int64))
            # 2) 写入 changed + 当前不在索引中的路径
            try:
                present = set(int(x) for x in faiss.vector_to_array(self.faiss_index.id_map))
            except Exception:
                return False
            add_ids, add_rows = [], []
            for p in list(changed_paths or []):
                j = pos_of.get(p)
                i = self.faiss_ids.get(p)
                if j is None or i is None:
                    continue
                add_ids.append(int(i))
                add_rows.append(int(j))
            for p, i in (self.faiss_ids or {}).items():
                i = int(i)
                if i in present or i in add_ids:
                    continue
                j = pos_of.get(p)
                if j is None:
                    continue
                add_ids.append(i)
                add_rows.append(int(j))
            if add_ids:
                vecs = np.asarray(self.embeddings[add_rows], dtype=np.float32).copy()
                faiss.normalize_L2(vecs)
                self.faiss_index.add_with_ids(vecs, np.asarray(add_ids, dtype=np.int64))
            # 3) 自检：向量数必须等于 paths 数，否则视为失败
            if int(self.faiss_index.ntotal) != len(self.paths or []):
                return False
            return True
        except Exception:
            log.warning("FAISS 真增量失败，退回全量重建", exc_info=True)
            return False

    def _ensure_idmap_loaded(self):
        """确保 faiss_index 是 IndexIDMap 且 id 映射可信；否则重建一次（不丢向量）。"""
        if not _HAS_FAISS or self.faiss_index is None or self.embeddings is None:
            return
        try:
            n_emb = int(self.embeddings.shape[0])
            if n_emb == 0 or n_emb != len(self.paths or []) or int(self.faiss_index.ntotal) != n_emb:
                return
            need_rebuild = not isinstance(self.faiss_index, faiss.IndexIDMap)
            if not need_rebuild:
                if not self.faiss_ids and not self._load_faiss_ids():
                    need_rebuild = True
                else:
                    try:
                        present = set(int(x) for x in faiss.vector_to_array(self.faiss_index.id_map))
                    except Exception:
                        present = None
                    if present is None or present != set(int(v) for v in (self.faiss_ids or {}).values()):
                        need_rebuild = True
            if not need_rebuild:
                return
            self.faiss_ids = {}
            self._faiss_next_id = 1
            _ids = self._ensure_faiss_ids()
            _up = self._build_faiss_index(self.embeddings, _ids)
            if _up is None or int(_up.ntotal) != n_emb:
                return
            self.faiss_index = _up
            self._prune_faiss_ids()
            self._save_faiss_ids()
            import os
            _tmp = Path(str(FAISS_FILE) + ".tmp")
            faiss.write_index(self.faiss_index, str(_tmp))
            os.replace(_tmp, FAISS_FILE)
            logging.getLogger(__name__).info(
                "FAISS 索引已升级为 IndexIDMap（%d 向量）", n_emb
            )
        except Exception:
            log.warning("FAISS IndexIDMap 升级失败，保留原索引", exc_info=True)

    # ── 全量嵌入的断点与分片重建（1.0.21）──
    # 断点文件从 EMBEDDINGS_FILE 派生（测试里 patch 一个常量即可隔离）。
    def _embed_ckpt_meta_path(self):
        return Path(str(EMBEDDINGS_FILE) + ".ckpt.json")

    def _embed_ckpt_vecs_path(self):
        return Path(str(EMBEDDINGS_FILE) + ".ckpt.npy")

    def _content_hash(self, text):
        """正文指纹：断点复用的键不能只有路径（用法见 build_embeddings 的复用分支）。"""
        return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]

    def _embed_ckpt_load(self):
        """读断点 → (paths, vecs, meta)；没有或读坏了返回 (None, None, {})。"""
        mp, vp = self._embed_ckpt_meta_path(), self._embed_ckpt_vecs_path()
        if not mp.exists() or not vp.exists():
            return None, None, {}
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            arr = np.load(str(vp), allow_pickle=False)
            return list(meta.get("paths") or []), arr, meta
        except Exception:
            log.warning("嵌入断点读取失败，忽略", exc_info=True)
            return None, None, {}

    def _embed_ckpt_save(self, paths, vecs):
        try:
            _n = int(vecs.shape[0]) if vecs is not None else 0
            # 只记「已经算出来」的那一段：paths 与 vecs 必须逐行对齐，
            # 否则续跑时长度一比对不上，整份断点会被判为无效而白白重算。
            paths = list(paths)[:_n]
            # 内容指纹：只比路径会张冠李戴 —— 正文改了而路径顺序没变时（末位编辑是常态），
            # 复用旧向量等于把这次改动在向量面永久丢掉（2026-11-08 独立审查实测复现）。
            hashes = [self._content_hash(c) for c in list(self.contents or [])[:_n]]
            _atomic_np_save(self._embed_ckpt_vecs_path(), vecs)
            mp = self._embed_ckpt_meta_path()
            tmp = Path(str(mp) + ".tmp")
            tmp.write_text(json.dumps(
                {"paths": paths, "n": _n, "hashes": hashes,
                 "index_version": self._index_version},
                ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, mp)
        except Exception:
            log.warning("嵌入断点写入失败", exc_info=True)

    def _embed_ckpt_clear(self):
        for f in (self._embed_ckpt_meta_path(), self._embed_ckpt_vecs_path()):
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass

    def _schedule_embed_rebuild(self, chunk_budget=60.0):
        """标记向量面待重建，并（若没在跑）起一个后台分片重建线程。

        写入路径只调它，不再自己跑全量 —— 这是「写入卡 7 分钟」的根治点。
        返回 True = 本次真的起了线程。
        """
        global _embed_rebuild_thread
        with _embed_rebuild_lock:
            if _embed_rebuild_thread is not None and _embed_rebuild_thread.is_alive():
                return False
            t = threading.Thread(target=self._run_embed_rebuild_chunks,
                                 args=(chunk_budget,), daemon=True,
                                 name="kb-embed-chunks")
            _embed_rebuild_thread = t
            t.start()
            return True

    def _run_embed_rebuild_chunks(self, chunk_budget=60.0, max_stall=5):
        """后台按片推进全量嵌入，直到向量面与 paths 对齐；片间让出 CPU。

        `max_stall`：连续多少片「一点没推进」就放弃。否则一旦嵌入服务或断点写盘持续失败，
        这个循环会无限重试、每片白烧一整片的时间（2026-11-08 独立审查实测：把断点写盘
        桩成失败后 3 秒起 6 批、线程永不退出，只能重启停）。
        """
        try:
            stall = 0
            last_done = -1
            while stall < max_stall:
                if self.embeddings is not None and \
                        int(self.embeddings.shape[0]) == len(self.paths or []):
                    return
                if not self.contents:
                    return
                if self.build_embeddings(budget=chunk_budget):
                    log.info("后台分片重建完成：%d 篇向量已对齐", len(self.paths or []))
                    return
                # 「推进了没有」以断点里已算出的行数为准：不变就记一次停滞
                ck_paths, _ck_vecs, ck_meta = self._embed_ckpt_load()
                done = int(ck_meta.get("n") or 0) if ck_paths else 0
                stall = stall + 1 if done <= last_done else 0
                last_done = max(last_done, done)
                time.sleep(0.5)
            log.warning("后台分片重建连续 %d 片没有推进，本轮放弃（保留断点，等下次触发）", max_stall)
        except Exception:
            log.warning("后台分片重建异常", exc_info=True)

    def build_embeddings(self, budget=None, use_checkpoint=True):
        """全量算所有正文的向量并落盘。**支持断点续跑**（1.0.21）。

        budget：本轮时间预算（秒）。None = 不限。超预算**不算失败** ——
                保存断点后返回 False，由调用方稍后接着推进（跟旧版的区别：
                旧版超预算会把 embeddings 置空并回滚，等于每轮从头再来）。
        use_checkpoint：是否复用/写断点文件。

        返回 True = 向量面已与 paths 对齐并落盘；False = 本轮没做完（有断点）或
        paths 在计算期间变了（结果已丢弃，断点保留待续）。

        背景（2026-11-08 排障）：全量嵌入常态要 20+ 分钟，而写入路径曾在 self.lock
        内调本函数、还给它 300s 硬预算 → 每轮都超时回滚、向量面永远补不上，
        写入被拖到 7 分钟。现在写入路径不再调它（见 `_schedule_embed_rebuild`），
        本函数的预算只用来「切分片」，不再用于「放弃」。
        """
        if not self.contents:
            return False
        try:
            batch_size = 16
            _total = len(self.contents)
            _progress_set_total(_total)
            paths = list(self.paths or [])
            # 1) 断点复用：取与当前 paths 的最长公共前缀。
            #    新笔记通常追加在尾部、原地改写不改变顺序，所以前缀命中率高；
            #    顺序一旦变化（删中间的文件）就只复用变化点之前的部分，绝不会张冠李戴。
            all_embeds = []
            start = 0
            if use_checkpoint:
                ck_paths, ck_vecs, ck_meta = self._embed_ckpt_load()
                ck_ok = (bool(ck_paths) and ck_vecs is not None
                         and int(ck_vecs.shape[0]) == len(ck_paths)
                         and int(ck_meta.get("index_version", -1)) == int(self._index_version))
                if ck_ok:
                    ck_hashes = list(ck_meta.get("hashes") or [])
                    contents = list(self.contents or [])
                    n = 0
                    lim = min(len(ck_paths), len(paths))
                    # 逐行要求「路径 + 正文指纹」都对得上：正文改过就不能复用（否则这次改动
                    # 会永久丢在向量面）；断点缺指纹（旧格式）也一律当不可复用，宁可重算。
                    while (n < lim and ck_paths[n] == paths[n]
                           and n < len(ck_hashes)
                           and ck_hashes[n] == self._content_hash(contents[n])):
                        n += 1
                    if n:
                        all_embeds.append(ck_vecs[:n])
                        start = n
                        log.info("嵌入断点复用 %d/%d 行，从第 %d 行继续", n, _total, n)
                elif ck_paths:
                    log.info("嵌入断点不可复用（index_version 不符或格式过旧），本轮全量重算")
            # 2) 从 start 继续往后算
            _t0 = time.monotonic()
            _since_ckpt = 0
            for i in range(start, _total, batch_size):
                batch = self.contents[i : i + batch_size]
                all_embeds.append(embedding_api(batch, task="retrieval.passage"))
                _progress_advance(len(batch))
                _since_ckpt += 1
                # 预算判断放在「算完一批之后」：保证任何预算下每片都至少推进一批 ——
                # 否则后台分片循环会在「片内一批都没算完」时反复空转（死循环）。
                over = budget is not None and (time.monotonic() - _t0) > budget
                if use_checkpoint and (_since_ckpt >= 10 or over):
                    _since_ckpt = 0
                    self._embed_ckpt_save(paths, np.vstack(all_embeds))
                if over:
                    log.info(
                        "全量嵌入本轮推进到 %d/%d（预算 %.0fs 用尽），断点已存，稍后续跑",
                        i + len(batch), _total, budget,
                    )
                    return False
            _new_emb = np.vstack(all_embeds) if all_embeds else np.array([])
            # 计算期间若 paths 变了（有人写入），本轮结果对不上最新状态：
            # 存断点（下次按新 paths 复用前缀）后丢弃，不落盘。
            # ⚠️ 不能先把它赋给 self.embeddings 再撒手：旧值会被冲掉，而新值行数恰好等于最新
            # paths 时会静默通过 is_vector_search_ready()（行数对上、内容错位）——
            # 那比降级 BM25 更糟（2026-11-08 独立审查指出）。校验通过才赋值。
            if list(self.paths or []) != paths:
                log.info("重建期间 paths 已变化（%d → %d 行），丢弃本轮结果并保留断点",
                         len(paths), len(self.paths or []))
                self._embed_ckpt_save(paths, _new_emb)
                return False
            self.embeddings = _new_emb
            # t6: 构建 FAISS 索引（IndexIDMap + 稳定 id）
            _ids = self._ensure_faiss_ids()
            self.faiss_index = self._build_faiss_index(self.embeddings, _ids)
            self._prune_faiss_ids()
            self._save_faiss_ids()
            # A0: 原子落盘 npy/faiss + 更新 manifest
            n_paths = len(self.paths or [])
            n_emb = 0 if self.embeddings is None else int(self.embeddings.shape[0])
            if n_emb != n_paths:
                import logging
                logging.getLogger(__name__).warning(
                    f"build_embeddings 结果与 paths 不对齐: paths={n_paths} emb={n_emb}，禁用向量面"
                )
                self.embeddings = None
                self.faiss_index = None
                self.index_consistent = False
                self.consistency_reason = f"paths({n_paths}) 与 embeddings({n_emb}) 不一致"
                write_index_manifest(
                    n_paths=n_paths,
                    n_contents=len(self.contents or []),
                    n_embeddings=0,
                    n_faiss=0,
                    index_version=self._index_version,
                    path=INDEX_MANIFEST_FILE,
                    extra={"consistent": False, "reason": self.consistency_reason},
                )
                return False
            _atomic_np_save(EMBEDDINGS_FILE, self.embeddings)
            if self.faiss_index is not None:
                import os
                tmp = Path(str(FAISS_FILE) + ".tmp")
                faiss.write_index(self.faiss_index, str(tmp))
                os.replace(tmp, FAISS_FILE)
            self.index_consistent = True
            self.consistency_reason = "ok"
            write_index_manifest(
                n_paths=n_paths,
                n_contents=len(self.contents or []),
                n_embeddings=n_emb,
                n_faiss=int(self.faiss_index.ntotal) if self.faiss_index is not None else 0,
                index_version=self._index_version,
                path=INDEX_MANIFEST_FILE,
                extra={"consistent": True, "reason": "ok"},
            )
            # 对齐落盘成功 → 断点使命完成
            self._embed_ckpt_clear()
            return True
        except Exception:
            log.warning("search.py 索引保存失败", exc_info=True)
            return False

    def _build_embeddings_background(self):
        """后台补建向量：走统一的分片 + 断点路径（不再一次性跑全量）。"""
        self._schedule_embed_rebuild()

    def start_rebuild_async(self):
        """启动后台全量重建（立即返回，不阻塞请求）。

        返回 {"status": "started", "started_at": ...} 或 {"status": "already_running"}。
        """
        global _rebuild_thread
        if not _progress_begin(len(self.contents or [])):
            return {"status": "already_running"}
        with _progress_lock:
            _started_at = _rebuild_progress.get("started_at")

        def _run():
            try:
                self.build(is_rebuild=True, _tracked=True)
                _progress_finish(ok=True)
            except Exception as e:
                log.warning("后台全量重建失败", exc_info=True)
                _progress_finish(ok=False, error=str(e))

        _t = threading.Thread(target=_run, daemon=True, name="kb-full-rebuild")
        _rebuild_thread = _t
        _t.start()
        return {"status": "started", "started_at": _started_at}

    def build_embeddings_incremental(self, changed_paths, changed_contents, removed, old_paths=None):
        if not self.contents or self.embeddings is None or self.embeddings.shape[0] == 0:
            # 向量面当前不可用（启动时被判不一致，或从没建过）：**不在写入路径里跑全量**
            # —— 全量常态 20+ 分钟，会让这次写入一直挂着（2026-11-08 实测 7 分钟）。
            # 改成标记 + 起后台分片重建，本次写入直接按 BM25 返回。
            self._schedule_embed_rebuild()
            return
        if not changed_paths and not removed:
            return
        try:
            # 使用 old_paths（传入的 pre-update 路径列表）构建索引映射，
            # 避免 self.paths 已更新导致 old_emb 与路径索引错位
            ref_paths = old_paths if old_paths is not None else self.paths
            # 行数前置校验（2026-10-25）：keep_indices 是按 ref_paths 的长度算的，
            # 而 self.embeddings 可能比它少行（上一次增量/落盘异常留下的错位状态）。
            # 不先校验就直接 self.embeddings[keep_indices]，会抛
            #   IndexError: index N is out of bounds for axis 0 with size N
            # 该异常被下面的 except 吞掉后走全量重建 —— 于是「每次刷新都全量重建」，
            # waitress 线程被 40 秒级重建占满，表现成服务活着但所有请求超时
            # （生产 2026-10-25 03:21 实测：队列堆到 7 层）。
            # 这里显式识别错位并降级，语义与「增量不可用」一致，不改变正确性。
            if int(self.embeddings.shape[0]) != len(ref_paths):
                log.warning(
                    "增量嵌入跳过：embeddings 行数(%d) 与 ref_paths(%d) 不一致，改走后台分片重建",
                    int(self.embeddings.shape[0]), len(ref_paths),
                )
                self._schedule_embed_rebuild()
                return
            keep_indices = list(range(len(ref_paths)))
            if removed:
                keep_indices = [i for i in keep_indices if ref_paths[i] not in removed]
            replace_info, new_extra = self._classify_incremental_changes(
                ref_paths, keep_indices, removed, changed_paths, changed_contents
            )
            new_embeds = self._embed_incremental_entries(replace_info, new_extra)
            if not new_embeds:
                # 只有删除、没有新增/替换：仍需剪掉被删行，否则 paths 与 embeddings 不一致
                if removed:
                    self.embeddings = self.embeddings[keep_indices]
                else:
                    return
            else:
                self.embeddings = self._assemble_incremental_embeddings(
                    ref_paths, keep_indices, removed, replace_info, new_extra, new_embeds
                )
            n_paths = len(self.paths or [])
            n_emb = int(self.embeddings.shape[0])
            if n_emb != n_paths:
                import logging
                logging.getLogger(__name__).warning(
                    f"incremental embeddings 与 paths 不对齐: paths={n_paths} emb={n_emb}，改走后台分片重建"
                )
                self._schedule_embed_rebuild()
                return
            # t6: 真增量——只增删变化向量，不再整表重建
            if not self._faiss_apply_incremental(changed_paths, removed):
                # 任何不确定情形一律退回全量重建（安全兜底，等价改动前行为）
                self.faiss_index = self._build_faiss_index(
                    self.embeddings, self._ensure_faiss_ids()
                )
            self._prune_faiss_ids()
            self._persist_incremental_index(n_paths, n_emb)
        except Exception:
            # 增量嵌入失败：同样不在写入路径里跑全量，改走后台分片重建
            log.warning("search.py 索引保存失败", exc_info=True)
            self._schedule_embed_rebuild()

    @staticmethod
    def _classify_incremental_changes(ref_paths, keep_indices, removed,
                                      changed_paths, changed_contents):
        """把变更条目按 ref_paths 分为「原地替换」与「追加新增」两类。

        必须同时记录 path：后续要按 self.paths（新顺序）组装向量，只靠位置
        无法区分某条 changed 是「原地替换」还是「追加到末尾」。
        返回 (replace_info, new_extra)：
        - replace_info: (path, 旧行号, 内容)；有 removed 时旧行号为 keep 后的相对行号
        - new_extra:    (path, 内容)
        """
        path_to_idx = {p: i for i, p in enumerate(ref_paths)}
        keep_idx_set = set(keep_indices)
        replace_info = []   # (path, 旧行号, 内容)
        new_extra = []      # (path, 内容)
        for p, c in zip(changed_paths, changed_contents):
            if p in path_to_idx and path_to_idx[p] in keep_idx_set:
                idx_in_old = path_to_idx[p]
                replace_info.append(
                    (p, keep_indices.index(idx_in_old) if removed else idx_in_old, c)
                )
            else:
                new_extra.append((p, c))
        return replace_info, new_extra

    @staticmethod
    def _embed_incremental_entries(replace_info, new_extra):
        """对替换/新增条目做 passage 嵌入，返回向量列表（可能为空）。

        批次顺序固定：替换在前、新增在后，与 _assemble_incremental_embeddings
        的登记顺序一致；无任何变更条目时返回空列表（纯删除场景）。
        """
        new_embeds = []
        if replace_info:
            batch = [c for _, _, c in replace_info]
            new_embeds.append(embedding_api(batch, task="retrieval.passage"))
        if new_extra:
            new_embeds.append(embedding_api([c for _, c in new_extra], task="retrieval.passage"))
        return new_embeds

    def _assemble_incremental_embeddings(self, ref_paths, keep_indices, removed,
                                         replace_info, new_extra, new_embeds):
        """把新编码向量与保留的旧向量按 self.paths（新顺序）严格拼装。

        缺行时抛 RuntimeError（由调用方降级全量重建），杜绝顺序错位。
        """
        all_new = np.vstack(new_embeds)
        # 新编码向量按 path 登记（顺序与上面 new_embeds 拼接一致：replace 在前、new 在后）
        row_of_new = {}
        new_ptr = 0
        for _p, _old_idx, _c in replace_info:
            row_of_new[_p] = all_new[new_ptr]
            new_ptr += 1
        for _p, _c in new_extra:
            row_of_new[_p] = all_new[new_ptr]
            new_ptr += 1
        # 旧行按 path 登记（keep 之后的相对行号）
        kept_old = self.embeddings[keep_indices] if removed else self.embeddings
        row_of_old = {ref_paths[_i]: _j for _j, _i in enumerate(keep_indices)}
        # 严格按 self.paths（新顺序）组装，杜绝顺序错位
        rows, missing = [], []
        for _p in (self.paths or []):
            if _p in row_of_new:
                rows.append(row_of_new[_p])
            elif _p in row_of_old:
                rows.append(kept_old[row_of_old[_p]])
            else:
                missing.append(_p)
        if missing:
            raise RuntimeError(
                f"增量组装缺行 {len(missing)} 条（例: {missing[:3]}），退回全量重建"
            )
        return np.vstack(rows)

    def _persist_incremental_index(self, n_paths, n_emb):
        """落盘增量结果：embeddings / faiss / faiss_ids / manifest 一起写。"""
        _atomic_np_save(EMBEDDINGS_FILE, self.embeddings)
        if self.faiss_index is not None:
            import os
            tmp = Path(str(FAISS_FILE) + ".tmp")
            faiss.write_index(self.faiss_index, str(tmp))
            os.replace(tmp, FAISS_FILE)
            self._save_faiss_ids()
        self.index_consistent = True
        self.consistency_reason = "ok"
        write_index_manifest(
            n_paths=n_paths,
            n_contents=len(self.contents or []),
            n_embeddings=n_emb,
            n_faiss=int(self.faiss_index.ntotal) if self.faiss_index is not None else 0,
            index_version=self._index_version,
            path=INDEX_MANIFEST_FILE,
            extra={"consistent": True, "reason": "ok"},
        )

    def incremental_refresh(self):
        # 与后台全量重建互斥：全量重建进行中时不并发操作索引，避免写坏索引
        if is_rebuild_running():
            log.debug("全量重建进行中，跳过本次增量刷新")
            return
        self.begin_rebuild(mode="incremental")
        _snap = None
        try:
            changed_paths, changed_contents, removed, mtimes = self._collect_docs(
                diff_only=True
            )
            if not changed_paths and not removed:
                self.end_rebuild(ok=True)
                return
            # 预检：本次需要重新嵌入时，先探活嵌入服务；不可用则跳过本次刷新，保持原索引不动
            if get_setting("embedding_type") == "llm" and changed_paths:
                _pf_ok, _pf_err = self._embedding_preflight()
                if not _pf_ok:
                    log.warning("嵌入服务预检失败，跳过本次刷新以保护索引: %s", _pf_err)
                    self.end_rebuild(ok=False, error="embedding preflight failed")
                    return
            n_changed = len(changed_paths) + len(removed)
            n_total = len([p for p in self.paths if p]) if self.paths else 0
            if n_changed > max(n_total * 0.5, 10):
                # build() 自己会 begin/end；此处先不 end，交由 build 接管
                return self.build()
            merged_paths, merged_contents = self._merge_unchanged_docs(
                changed_paths, changed_contents, removed
            )
            if not merged_paths:
                # 同上：没有可保留的条目时把索引清空，而不是留着旧数据。
                self._reset_index_empty()
                self.end_rebuild(ok=True)
                return
            # 保存嵌入增量更新需要的 old_paths（self.paths 即将被更新）
            old_paths_for_embed = list(self.paths)
            # 快照：文本侧异常时回滚，避免半写状态。嵌入失败不再走这里——
            # 2026-11-07 起嵌入只降级向量面、不回滚 paths/BM25。
            _snap = (
                list(self.paths or []), list(self.contents or []), dict(self.mtimes or {}),
                self.bm25, self.embeddings, self.faiss_index, self._index_version,
            )
            tokenized = [_tokenize(c) for c in merged_contents]
            # 嵌入组装必须与 paths 更新留在**同一把锁内**（安全加固）：
            # 原先 build_embeddings_incremental 在锁外执行，并发的另一次刷新会读到
            # 「已更新的 self.paths + 尚未更新的 self.embeddings」，于是按新 paths
            # 去配旧的对齐矩阵，产出**路径与向量静默错位**——且 n_emb == n_paths、
            # manifest 仍写 consistent:true，计数校验完全放行。
            # 读方（_vec_search / search / count_stale）不持本锁，因此持锁做嵌入
            # 网络调用不会阻塞检索，只会让并发的另一次刷新排队。
            with self.lock:
                self._apply_incremental_docs_locked(
                    merged_paths, merged_contents, mtimes, tokenized,
                    changed_paths, changed_contents, removed, old_paths_for_embed,
                )
            self.end_rebuild(ok=True)
        except Exception as e:
            if _snap is not None:
                try:
                    with self.lock:
                        (self.paths, self.contents, self.mtimes, self.bm25,
                         self.embeddings, self.faiss_index, self._index_version) = _snap
                        self._save_index()
                    log.warning("增量刷新失败，已回滚到刷新前索引（保持原状）: %s", e)
                except Exception:
                    log.warning("增量刷新失败且回滚失败", exc_info=True)
            self.end_rebuild(ok=False, error=str(e))
            raise

    @staticmethod
    def _embedding_preflight():
        """嵌入服务探活。返回 (ok, err)；ok=False 时 err 为原始异常（由调用方记日志）。"""
        try:
            import socket as _socket
            from urllib.parse import urlparse as _urlparse
            _u = _urlparse(get_setting("embedding_base") or "")
            if _u.hostname:
                _c = _socket.create_connection((_u.hostname, _u.port or 80), timeout=3)
                _c.close()
            return True, None
        except Exception as _pe:
            return False, _pe

    def _merge_unchanged_docs(self, changed_paths, changed_contents, removed):
        """保留未变更条目并与本次 changed 合并为新的 (paths, contents)。

        顺序：保留的旧条目在前、changed 追加在后；只剩 removed 时返回空列表，
        由调用方走 _reset_index_empty 清空索引。
        """
        path_to_content = {}
        if self.paths and self.contents:
            new_removed_set = set(removed)
            new_changed_set = set(changed_paths)
            for p, c in zip(self.paths, self.contents):
                if p not in new_removed_set and p not in new_changed_set:
                    path_to_content[p] = c
        merged_paths = list(path_to_content.keys()) + changed_paths
        merged_contents = list(path_to_content.values()) + changed_contents
        return merged_paths, merged_contents

    def _apply_incremental_docs_locked(self, merged_paths, merged_contents, mtimes,
                                       tokenized, changed_paths, changed_contents,
                                       removed, old_paths):
        """在**已持有 self.lock** 的前提下换入新索引并做嵌入增量更新。

        调用方负责加锁；本方法不自行获取 self.lock，以保持与原内联实现
        完全相同的锁范围（embedding_api 调用期间锁必须保持持有，理由见
        incremental_refresh 中的注释）。文本索引先落盘；嵌入未对齐只降级
        向量面（embeddings/faiss 置空后落盘），不再抛错回滚文本索引。
        """
        self.bm25 = BM25Okapi(tokenized)
        self.paths = merged_paths
        self.contents = merged_contents
        self.mtimes = mtimes
        # T4: 变更/删除的笔记，日期缓存必须失效（否则改了 frontmatter 日期不生效）
        for _p in list(changed_paths or []) + list(removed or []):
            self._date_cache.pop(_p, None)
            self._importance_cache.pop(_p, None)  # T-23-a: 改了 importance 要立刻生效
            self._meta_cache.pop(_p, None)  # 2026-10-22: 改了标题/标签要立刻生效
        self._index_version += 1  # Bump: 失效旧嵌入缓存
        self._token_sets = None    # 失效分词集合缓存
        self._save_index()  # 在锁内持久化，避免 search 线程读到不一致状态
        if get_setting("embedding_type") == "llm":
            # 文本索引（paths/BM25）与向量嵌入是两件事：文本部分此刻已经落盘
            # 成功，嵌入只是可降级的外围能力。嵌入失败时只禁用向量面，绝不
            # 连文本索引一起回滚（否则上游一挂，新笔记永远进不了索引）。
            try:
                self.build_embeddings_incremental(
                    changed_paths, changed_contents, removed, old_paths
                )
            except Exception:
                log.warning("增量嵌入抛错，仅降级向量面，保留本次文本索引", exc_info=True)
            if not self._incremental_embeddings_aligned():
                # 向量面不可用：置空 embeddings/faiss 并落盘，vector_search_ready
                # 随之变 False（检索退回 BM25），本次 paths/BM25 结果保持不变。
                log.warning(
                    "增量嵌入未成功（embeddings 与 paths 未对齐），仅降级向量面："
                    "paths/BM25 已落盘、不回滚；vector_search_ready=False"
                )
                self.embeddings = None
                self.faiss_index = None
                self._save_index()

    def _incremental_embeddings_aligned(self):
        """embeddings 与 paths 行数是否对齐；访问异常按未对齐处理。"""
        if self.embeddings is None:
            return False
        try:
            return int(self.embeddings.shape[0]) == len(self.paths)
        except Exception:
            return False


    def peek_stale(self):
        """只读缓存，不做任何文件系统扫描（供 /health 这类高频探活接口用）。

        返回 (count, cached)：命中缓存时 cached=True、count 为真实值；
        未命中时 cached=False、count 为 None —— 调用方应显示「未知」而不是 0，
        因为 0 会被误读成「索引完全跟得上磁盘」。
        """
        now = time.time()
        with _stale_cache_lock:
            if now - _stale_cache["ts"] < _STALE_CACHE_TTL:
                return _stale_cache["count"], True
            if _stale_cache["ts"] > 0:
                # 有过历史结果但已过期：返回旧值并标记为过期，避免首次/冷启动阻塞
                return _stale_cache["count"], False
        return None, False

    def prime_stale_async(self):
        """后台线程预热 stale 缓存，不阻塞调用方；已有线程在跑则不重复起。

        线程的创建、登记、start 全在锁内完成：若只在锁内判断、锁外赋值，
        两个并发未命中请求会各自看到「没有线程」而各起一个扫描线程
        （GLM 2026-10-18 审查指出）。start() 本身只是让出 GIL，代价可忽略，
        不会把「不阻塞」这个目标破坏掉。
        """
        global _stale_prime_thread
        with _stale_cache_lock:
            t = _stale_prime_thread
            if t is not None and t.is_alive():
                return False

            def _run():
                try:
                    self.count_stale()
                except Exception:
                    log.warning("stale 预热失败", exc_info=True)

            t = threading.Thread(target=_run, name="stale-prime", daemon=True)
            _stale_prime_thread = t
            t.start()
        return True

    def count_stale(self):
        """扫描文件系统，统计与索引相比过期的文件数（排除_assets和受保护目录和50-档案）
        注意：exclude_assets=True 必须与 _collect_docs 保持一致，否则 _assets/ 下的文件始终显示为 stale"""
        now = time.time()
        with _stale_cache_lock:
            if now - _stale_cache["ts"] < _STALE_CACHE_TTL:
                return _stale_cache["count"]
        count = 0
        for f in NOTES_DIR.rglob("*.md"):
            rel = str(f.relative_to(NOTES_DIR))
            if is_excluded_path(rel, exclude_archived=True, exclude_assets=True):
                continue
            if "{{title}}" in rel or f.name.startswith("{{"):
                continue
            mtime = f.stat().st_mtime
            if rel not in self.mtimes or self.mtimes.get(rel) != mtime:
                count += 1
        with _stale_cache_lock:
            _stale_cache["count"] = count
            _stale_cache["ts"] = time.time()
        return count

    def _load_index_file(self):
        """加载主索引（JSON + npz），含 bm25 重建兜底。

        返回 loaded：新格式索引存在且读取成功。读取异常只记日志、
        保持 loaded=False，由调用方决定走 build 全量重建。旧索引有 contents
        但无 bm25 时，立即分词重建并落盘。
        """
        loaded = False
        if INDEX_FILE.exists():
            try:
                payload, arrays = _load_index_payload()
                if payload is None:
                    raise ValueError("索引为空")
                if payload.get("bm25") and "doc_len" not in arrays:
                    raise ValueError("索引数组文件缺失")
                with self.lock:
                    self.bm25 = _bm25_from_payload(payload.get("bm25"), arrays)
                    self.paths = payload.get("paths", [])
                    self.contents = payload.get("contents", [])
                    self.mtimes = payload.get("mtimes", {})
                    self._token_sets = None  # 索引不存分词集合，置脏按需重建
                loaded = True
            except Exception:
                log.warning("主索引读取失败，将全量重建: %s", INDEX_FILE, exc_info=True)
        # 兼容旧索引：若有 vectorizer 但无 bm25，触发重建
        if loaded and self.bm25 is None and self.contents:
            try:
                tokenized = [_tokenize(c) for c in self.contents]
                self.bm25 = BM25Okapi(tokenized)
                self._save_index()
            except Exception:
                log.warning("search.py 异常", exc_info=True)
        return loaded

    def _check_pkl_consistency(self):
        """A0 第一关：paths/contents 数量自检（方法名沿用旧称，读的是新格式）。

        不一致即置 index_consistent=False 并记 consistency_reason（不等
        向量面裁决）；返回 (ok, reason) 供无向量时的最终裁决复用。
        """
        n_paths = len(self.paths or [])
        n_contents = len(self.contents or [])
        ok_pc, reason_pc = check_artifact_consistency(n_paths, n_contents)
        if not ok_pc:
            import logging
            logging.getLogger(__name__).warning(f"索引 pkl 内部不一致: {reason_pc}")
            self.index_consistent = False
            self.consistency_reason = reason_pc
        return ok_pc, reason_pc

    def _load_embeddings_npy(self):
        """加载 embeddings.npy；仅当文件存在且 embedding_type=llm 时读取。

        读取异常只记日志并返回 None（降级为纯 BM25），不向调用方抛出。
        """
        embeddings = None
        if EMBEDDINGS_FILE.exists() and get_setting("embedding_type") == "llm":
            try:
                embeddings = np.load(EMBEDDINGS_FILE)
            except Exception:
                log.warning("search.py 异常: load", exc_info=True)
                embeddings = None
        return embeddings

    def _load_faiss_for(self, embeddings):
        """加载 faiss 索引并与 embeddings 尺寸核对，不符则降级到 sklearn。

        返回对齐的 faiss index 或 None；读取失败/尺寸不符均只记日志后返回
        None（不抛出）。embeddings 为空或缺 faiss 时同样返回 None。
        """
        faiss_index = None
        if embeddings is not None and _HAS_FAISS and FAISS_FILE.exists():
            try:
                tmp = faiss.read_index(str(FAISS_FILE))
                if tmp.ntotal == embeddings.shape[0]:
                    faiss_index = tmp
                else:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"FAISS 索引尺寸({tmp.ntotal}) 与 embeddings ({embeddings.shape[0]}) 不匹配，降级到 sklearn"
                    )
                    faiss_index = None
            except Exception:
                log.warning("search.py 异常: load", exc_info=True)
                faiss_index = None
        return faiss_index

    def _apply_vector_consistency(self, embeddings, faiss_index, ok_pc, reason_pc):
        """A0 第二关：paths == embeddings 才交向量面，最终裁决写回全局状态。

        - embeddings 非空但计数不齐：禁用向量面（self.embeddings/faiss_index
          置 None，index_consistent=False），保留 BM25；计数对齐才赋值并把
          旧格式裸 IndexFlatIP 升级为 IndexIDMap。
        - embeddings 为空：向量面置空；一致性退化为第一关 ok_pc 的结论。
        """
        if embeddings is not None:
            n_paths = len(self.paths or [])
            n_contents = len(self.contents or [])
            n_emb = int(embeddings.shape[0])
            n_faiss = int(faiss_index.ntotal) if faiss_index is not None else 0
            ok, reason = check_artifact_consistency(
                n_paths, n_contents, n_embeddings=n_emb, n_faiss=n_faiss if n_faiss else None
            )
            if not ok:
                import logging
                logging.getLogger(__name__).warning(
                    f"索引产物不一致，禁用向量检索: {reason}"
                )
                self.embeddings = None
                self.faiss_index = None
                self.index_consistent = False
                self.consistency_reason = reason
            else:
                self.embeddings = embeddings
                self.faiss_index = faiss_index
                self.index_consistent = True
                self.consistency_reason = "ok"
                # t6: 旧格式裸 IndexFlatIP → 自动升级为 IndexIDMap（真增量前提），不丢向量
                self._ensure_idmap_loaded()
        else:
            self.embeddings = None
            self.faiss_index = None
            # 无向量时，paths/contents 对齐即可视为 BM25 一致
            self.index_consistent = ok_pc
            self.consistency_reason = reason_pc if not ok_pc else "ok"

    def load(self):
        """索引加载主入口（薄编排）：pkl 加载 → 一致性自检 → 向量面裁决。

        顺序与降级条件均与拆分前一致：先读主索引（含 bm25 重建兜底），
        再自检 paths/contents，然后按条件读 npy / faiss，最后由
        _apply_vector_consistency 决定向量面去留并写回全局状态。
        返回 loaded：主索引存在且读取成功（调用方据此决定是否 build 全量）。
        """
        loaded = self._load_index_file()
        ok_pc, reason_pc = self._check_pkl_consistency()
        embeddings = self._load_embeddings_npy()
        faiss_index = self._load_faiss_for(embeddings)
        self._apply_vector_consistency(embeddings, faiss_index, ok_pc, reason_pc)
        return loaded

    def _vec_search(self, query, top=20):
        if not self.is_vector_search_ready():
            return []
        if self.embeddings is None or self.embeddings.shape[0] == 0:
            return []
        try:
            q_emb = _cached_embedding(query, index_version=self._index_version)  # shape (1, dim)
            # 使用 FAISS 搜索（若可用）
            if self.faiss_index is not None:
                q_norm = np.asarray(q_emb, dtype=np.float32).copy()
                faiss.normalize_L2(q_norm)
                scores_arr, idx_arr = self.faiss_index.search(q_norm, top)
                if _HAS_FAISS and isinstance(self.faiss_index, faiss.IndexIDMap):
                    # IndexIDMap.search 返回稳定 id（不是 paths 下标）→ 必须反查
                    inv = {int(v): k for k, v in (self.faiss_ids or {}).items()}
                    pos_of = {p: j for j, p in enumerate(self.paths or [])}
                    pairs = []
                    for _id, _sc in zip(idx_arr[0], scores_arr[0]):
                        _id = int(_id)
                        if _id < 0:
                            continue
                        _p = inv.get(_id)
                        if _p is None:
                            continue
                        _j = pos_of.get(_p)
                        if _j is None:
                            continue
                        pairs.append((_j, _sc))
                else:
                    pairs = list(zip(idx_arr[0], scores_arr[0]))
            elif _cosine_sim is not None:
                # 降级到 numpy cosine_similarity
                sims = _cosine_sim(q_emb, self.embeddings)[0]
                idx = np.argsort(sims)[::-1][:top]
                pairs = [(i, sims[i]) for i in idx]
            else:
                # 既无 FAISS 也无 sklearn——向量搜索不可用
                import logging
                logging.getLogger(__name__).warning(
                    "FAISS 和 sklearn 均不可用，向量搜索降级为空。"
                    "请安装 scikit-learn (`pip install scikit-learn`)。"
                )
                return []
            results = []
            for i, score in pairs:
                if i < 0 or i >= len(self.paths):
                    continue
                score = float(score)
                if score < 0.2:
                    continue
                results.append(
                    {
                        "path": self.paths[i],
                        "score": round(score, 4),
                        "display_score": round(max(0.0, min(score, 1.0)), 4),
                        "snippet": self.contents[i][:200].strip(),
                        "idx": int(i),
                    }
                )
            return results
        except Exception as e:
            log.warning("向量检索失败", exc_info=True)
            raise SearchUnavailable(f"向量检索失败: {e}") from e

    def _token_sets_for(self):
        """与 self.contents 同寿命的分词集合缓存。

        安全加固：`_bm25_search` 原先每请求对全库 contents 逐篇
        `_tokenize()`，而 `bm25.get_scores` 刚算过分数 —— 纯重复劳动。
        千级篇目 × 800 字实测 13.7~14.1 s/query，与 top 无关；Flask 8 线程会
        全部耗在分词上，检索整体不可用。

        缓存在 refresh / build / load 改写 contents 时被主动置 None，
        这里只做「长度不一致就重建」的兜底，不承担正确性责任。
        """
        ts = getattr(self, "_token_sets", None)
        if ts is None or len(ts) != len(self.contents):
            ts = [set(_tokenize(c)) for c in self.contents]
            self._token_sets = ts
        return ts

    def _reset_index_empty(self):
        """把内存索引与落盘产物清空（库内已无任何笔记时调用）。

        写路径统一走 store/index 的原子写；调用方负责 begin/end_rebuild 状态机。
        """
        with self.lock:
            self.paths, self.contents, self.mtimes = [], [], {}
            self._date_cache = {}   # T4: 索引清空，日期缓存一并失效
            self._importance_cache = {}  # T-23-a: 同上
            self._meta_cache = {}   # 2026-10-22: 标题/标签缓存一并失效
            self.bm25 = None
            self.embeddings = None
            self.faiss_index = None
            self._token_sets = None
            self._index_version += 1
            # 断点文件也要清：空库/重建后若留着旧断点，续跑会拿它当基线（2026-11-08 独立审查指出）
            self._embed_ckpt_clear()
            try:
                self._prune_faiss_ids()
            except Exception:
                log.warning("search.py 异常: _reset_index_empty", exc_info=True)
            # 删除向量面落盘产物。**不能写 None**：np.save(None) 会存成 0 维
            # 对象数组，之后任何读 .embeddings.npy 的地方对 .shape[0] 都会
            # IndexError。删文件 + embeddings=None 才是「没有向量」的一致表示。
            for _art in (EMBEDDINGS_FILE, FAISS_FILE,
                         Path(str(FAISS_FILE) + ".ids.json")):
                try:
                    if _art.exists():
                        _art.unlink()
                except Exception:
                    log.warning("search.py 异常: _reset_index_empty", exc_info=True)
            self._save_index()

    def _bm25_search(self, query, top=5):
        # 2026-10-11：共享索引状态必须在锁内取快照。
        # 增量刷新在 self.lock 内**分步**换入（先 bm25，再 paths/contents），
        # 无锁读取会撞上中间态：scores 按新 bm25 长度算出，token_sets/paths
        # 还是旧长度 → `token_sets[i]` IndexError，被 except 包成
        # SearchUnavailable("BM25 检索失败: list index out of range")，
        # 整个检索降级不可用（查重随之静默失效）。_vec_search 已有越界保护，
        # 这里补上对称的快照 + 边界检查。
        with self.lock:
            bm25 = self.bm25
            paths = self.paths
            contents = self.contents
            token_sets = self._token_sets_for()
        if bm25 is None:
            return []
        try:
            q_tokens = _tokenize(query)
            scores = bm25.get_scores(q_tokens)
            idx = np.argsort(scores)[::-1]
            # 相关性判据用「是否命中查询词」，不用「分值是否为正」。
            # rank_bm25 的 idf 在词出现在过半文档时会变负，且其 eps 下限取
            # epsilon * average_idf —— average_idf 本身为负时下限也是负的。
            # 小库（新装、笔记很少）里查询词常落在过半文档中，于是「命中」的分
            # 反而比「不命中」（恒为 0）更低；若按分值 >= 0 过滤会把命中全丢掉，
            # 表现为全新部署搜不到任何东西。
            q_set = set(q_tokens)
            # 命中判据用缓存的分词集合。原实现对 self.contents 逐篇
            # _tokenize()，而 bm25.get_scores 上面刚算过分数——每次检索
            # （含 llm 模式下 pool=20 的 _hybrid_search 调用）都把整个语料
            # 重新切一遍：千级篇目×800 字实测 13.7~14.1 s/query，与 top 无关，
            # Flask 8 线程会全部耗在分词上。缓存与 contents 同寿命，
            # 由 _token_sets_for() 在长度变化时重建、由 refresh/build 主动失效。
            n_paths = len(paths)
            n_tok = len(token_sets)
            hits = [
                i for i in idx
                if i < n_paths and i < n_tok and (q_set & token_sets[i])
            ][:top]
            raw_scores = [float(scores[i]) for i in hits]
            if raw_scores:
                s_min, s_max = min(raw_scores), max(raw_scores)
                denom = s_max - s_min
                if denom < 1e-8:
                    norm = lambda s: 0.0
                else:
                    norm = lambda s: (s - s_min) / denom
            else:
                norm = lambda s: 0.0
            results = []
            for i in hits:
                if i < 0 or i >= n_paths or i >= len(contents):
                    continue
                raw = float(scores[i])
                results.append(
                    {
                        "path": paths[i],
                        # 补 idx：精排时用 snippet + 全文打分，缺 idx 只能拿到前 200 字符，
                        # 长文深处的稀有词（ID/端口/错误码）会因此被精排压下去。
                        # 必须 int()：i 来自 np.argsort，是 np.int64，直接进 dict 会让
                        # Flask jsonify 抛 TypeError → 纯 BM25 部署下一命中就 500
                        # （2026-10-05 复测 _bm25_search 时暴露的原有缺陷）。
                        "idx": int(i),
                        "score": round(raw, 4),
                        "display_score": round(max(0.0, min(norm(raw), 1.0)), 4),
                        "snippet": contents[i][:200].strip(),
                    }
                )
            return results
        except Exception as e:
            log.warning("BM25 检索失败", exc_info=True)
            raise SearchUnavailable(f"BM25 检索失败: {e}") from e

    @staticmethod
    def _clamp_alpha(val):
        """安全获取 hybrid_alpha，返回 0.0~1.0 范围内的浮点数"""
        try:
            if val is None:
                return 0.6
            v = float(val)
            return max(0.0, min(1.0, v))
        except (TypeError, ValueError):
            log.warning("search.py 异常: _clamp_alpha", exc_info=True)
            return 0.6

    @staticmethod
    def _rrf_merge(vec_cands, bm25_cands, pool, k=60):
        """候选池 = 向量 ∪ BM25，用 RRF（倒数排名融合）定序。

        背景：原实现候选池只取向量 top-N，BM25 仅在精排失败时兜底。
        实测含稀有词（ID/端口/错误码）的查询，纯向量候选池 14/25 漏掉目标笔记，
        并入 BM25 后降到 1/25 —— BM25 对精确词项敏感，与向量语义互补。

        RRF 用排名而非分数融合，天然免疫两路分数量纲不同（余弦 0~1 vs BM25 无界）。
        """
        merged = {}
        for rank, c in enumerate(vec_cands, 1):
            e = merged.setdefault(c["path"], {"cand": dict(c), "rrf": 0.0, "vec": True})
            e["rrf"] += 1.0 / (k + rank)
        for rank, r in enumerate(bm25_cands, 1):
            p = r["path"]
            e = merged.setdefault(p, {"cand": dict(r), "rrf": 0.0, "vec": False})
            e["rrf"] += 1.0 / (k + rank)
            if not e["vec"]:
                # BM25-only：无向量分，显式置 0，避免精排失败时兜底公式把原始 BM25 分当余弦用
                e["cand"]["score"] = 0.0
        return [e["cand"] for e in sorted(merged.values(), key=lambda x: x["rrf"], reverse=True)[:pool]]

    def search(self, query, top=5, with_status=False):
        """检索入口。

        with_status=True 时返回 (results, degraded_reason)：
        degraded_reason 为 None 表示检索正常执行（可能确实没有匹配）；
        非 None 表示**检索本身失败**，结果不可信。
        调用方据此区分「没查到」与「查失败」，不要把后者当空结果用。
        """
        try:
            # 2026-10-22：链路 importance → 标题/标签加成。
            # 索引剥掉了 frontmatter，标题和 tags 原来完全不参与检索；
            # 这里补上「查询词命中标题/标签就加分」，让标题点题的短笔记能被召回。
            results = self._apply_meta_boost(
                self._apply_importance(self._search_impl(query, top)), query
            )
            return (results, None) if with_status else results
        except SearchUnavailable as e:
            log.warning("检索不可用: %s", e)
            return ([], str(e)) if with_status else []

    def _search_impl(self, query, top=5):
        mode = get_setting("embedding_type")
        if mode != "llm":
            return self._bm25_search(query, top)

        try:
            return self._hybrid_search(query, top)
        except SearchUnavailable:
            raise
        except Exception as e:
            # 向量面依赖外部嵌入服务：服务不可达 / 超时 / 上游 5xx / 响应不是
            # 合法 JSON，都会让 urllib 或 json.loads 抛 URLError、HTTPError、
            # ValueError 等非 SearchUnavailable 异常。
            # 原实现任其穿透 _search_impl，而 search() 只捕获 SearchUnavailable，
            # 结果是整个请求 500，与文档登记的降级契约（见 docs/API.md 的
            # degraded/reason）相反：一次嵌入服务抖动就会打掉所有检索。
            raise SearchUnavailable(f"向量检索不可用: {type(e).__name__}: {e}")

    def _note_epoch(self, rel):
        """T4：取笔记的「写作日期」epoch 秒（惰性 + 缓存）。

        日期来源是 frontmatter 的 updated/created/date，**不是 mtime** ——
        实测多篇不同月份的笔记 mtime 被同步刷成了同一天，mtime 不可信。
        返回 None 表示该笔记没有可用日期（不参与衰减）。
        """
        cached = self._date_cache.get(rel)
        if cached is not None:
            return None if cached < 0 else cached
        val = -1
        try:
            f = NOTES_DIR / rel
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_FM_HEAD_BYTES)
            if head.startswith("---"):
                m = re.match(r"^---\n(.*?)\n---", head, re.DOTALL)
                if m:
                    for line in m.group(1).split("\n"):
                        k, _, v = line.partition(":")
                        if k.strip() in ("updated", "created", "date"):
                            ds = v.strip().strip('"').strip("'")
                            if ds:
                                try:
                                    dt = datetime.strptime(ds[:10], "%Y-%m-%d")
                                    val = dt.replace(tzinfo=timezone.utc).timestamp()
                                except ValueError:
                                    pass
                            if val > 0:
                                break
        except Exception:
            val = -1
        self._date_cache[rel] = val
        return None if val < 0 else val

    def _note_importance(self, rel):
        """T-23-a：取笔记的 frontmatter `importance`（惰性 + 缓存）。

        与 `_note_epoch` 同一套写法：只读文件头 `_FM_HEAD_BYTES`，不整篇 IO。
        返回 0.0~1.0 的浮点；没写、写坏、超范围一律返回 0.0（**不打 importance
        的笔记行为完全不变**，这是本功能的硬前提）。

        缓存 `_importance_cache` 与 `_date_cache` 同生命周期，变更/删除时一起失效。
        """
        cached = self._importance_cache.get(rel)
        if cached is not None:
            return cached
        val = 0.0
        try:
            f = NOTES_DIR / rel
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_FM_HEAD_BYTES)
            if head.startswith("---"):
                m = re.match(r"^---\n(.*?)\n---", head, re.DOTALL)
                if m:
                    for line in m.group(1).split("\n"):
                        k, _, v = line.partition(":")
                        if k.strip() == "importance":
                            ds = v.strip().strip('"').strip("'")
                            if ds:
                                try:
                                    val = float(ds)
                                except ValueError:
                                    val = 0.0
                            break
        except Exception:
            val = 0.0
        if val < 0.0:
            val = 0.0
        elif val > 1.0:
            val = 1.0
        self._importance_cache[rel] = val
        return val

    def _note_meta_text(self, rel):
        """2026-10-22：取笔记 frontmatter 的 title + tags，拼成可检索文本（惰性 + 缓存）。

        背景（实测）：`_collect_docs` 会把 frontmatter 整段剥掉再入索引，
        于是**标题和标签完全不参与检索**。后果：一篇标题写着「kb-1.0.4」的笔记，
        正文里只要没再出现「知识库」「版本」这些词，按标题搜就永远搜不到。
        抽 30 篇实测 6 篇的标题词不在正文里 —— 这些笔记等于「标题不可搜」。

        这里只读文件头 `_FM_HEAD_BYTES`（不整篇 IO），与 `_note_epoch` /
        `_note_importance` 同一套写法与缓存生命周期。
        返回小写字符串；没有 title/tags 返回空串（此时不参与加权，行为不变）。
        """
        cached = self._meta_cache.get(rel)
        if cached is not None:
            return cached
        txt = ""
        try:
            f = NOTES_DIR / rel
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_FM_HEAD_BYTES)
            if head.startswith("---"):
                m = re.match(r"^---\n(.*?)\n---", head, re.DOTALL)
                if m:
                    parts = []
                    for line in m.group(1).split("\n"):
                        k, _, v = line.partition(":")
                        k = k.strip()
                        if k in ("title", "tags", "canonical_tags", "aliases"):
                            v = v.strip().strip('"').strip("'")
                            if v and v != "[]":
                                parts.append(v)
                    txt = " ".join(parts).lower()
        except Exception:
            txt = ""
        self._meta_cache[rel] = txt
        return txt

    def _apply_meta_boost(self, results, query):
        """2026-10-22：查询词命中「标题/标签」时给该笔记加分（就地重排并返回）。

        解决的是「标题可搜」缺失：正文没提但标题/tags 明确点题的笔记，
        原来在向量和 BM25 两路都吃亏（正文太短、语义重心不在查询词上）。

        计分：命中几个查询词就乘 (1 + 0.25 × 命中数)，上限 ×2.0。
        用乘法保持与 importance/decay 一致的量纲，不把低分笔记直接顶到第一。
        全部未命中时零开销原样返回。
        """
        if not results:
            return results
        q_tokens = {t for t in _tokenize(query) if len(t) >= 2}
        if not q_tokens:
            return results
        changed = False
        for r in results:
            meta = self._note_meta_text(r.get("path"))
            if not meta:
                continue
            hits = sum(1 for t in q_tokens if t in meta)
            if not hits:
                continue
            boost = min(1.0 + 0.25 * hits, 2.0)
            base = float(r.get("_importance_final",
                               r.get("_decay_final",
                                     r.get("display_score", r.get("score", 0.0)))))
            r["_meta_hits"] = hits
            r["_meta_final"] = round(base * boost, 6)
            changed = True
        if changed:
            results.sort(
                key=lambda x: x.get("_meta_final",
                                    x.get("_importance_final",
                                          x.get("_decay_final",
                                                x.get("display_score", 0.0)))),
                reverse=True,
            )
        return results

    def _apply_importance(self, results):
        """T-23-a：按 frontmatter `importance` 做加权排序（就地重排并返回）。

        公式：final = base × (1 + importance × IMPORTANCE_BOOST)
        base 取 `_decay_final`（若已做时间衰减）否则 `display_score`。

        为什么用乘法加成而不是加法：加法会让低分笔记靠 importance 反超高分笔记，
        等于把 importance 当成「置顶开关」；乘法保持原排序的相对次序，只在
        重要度相同时放大差距。

        **importance 全为 0 时零开销原样返回**（不打 importance 的笔记行为完全不变）。
        """
        if not results:
            return results
        boosted = False
        for r in results:
            imp = self._note_importance(r.get("path"))
            if imp > 0:
                boosted = True
                base = float(r.get("_decay_final", r.get("display_score", r.get("score", 0.0))))
                r["_importance"] = round(imp, 4)
                r["_importance_final"] = round(base * (1.0 + imp * IMPORTANCE_BOOST), 6)
        if not boosted:
            return results
        results.sort(
            key=lambda x: x.get("_importance_final", x.get("_decay_final", x.get("display_score", 0.0))),
            reverse=True,
        )
        return results

    def _apply_time_decay(self, results, tau_days):
        """T4：对检索结果按笔记新旧做加权排序（就地重排并返回）。

        公式：final = base × 1 / (1 + age_days / tau_days)
        base 取 display_score（0~1 归一化），保证与 tau 无关的量纲。
        tau_days <= 0 或结果为空时原样返回（零开销，默认关闭）。
        没有日期的笔记不加权，保持原分（避免把「无日期」误判为「很旧」）。
        """
        if tau_days <= 0 or not results:
            return results
        now = time.time()
        for r in results:
            ep = self._note_epoch(r.get("path"))
            if ep is None:
                continue
            age_days = max(0.0, (now - ep) / 86400.0)
            base = float(r.get("display_score", r.get("score", 0.0)))
            r["_decay_factor"] = round(1.0 / (1.0 + age_days / tau_days), 4)
            r["_decay_final"] = round(base * r["_decay_factor"], 6)
        results.sort(key=lambda x: x.get("_decay_final", x.get("display_score", 0.0)), reverse=True)
        return results

    def _hybrid_search(self, query, top=5):
        alpha = self._clamp_alpha(get_setting("hybrid_alpha", None))
        pool = max(top * 4, 20)
        bm25_pool = self._bm25_search(query, top=pool)
        candidates = self._rrf_merge(
            self._vec_search(query, top=pool),
            bm25_pool,
            pool,
        )
        if not candidates:
            return bm25_pool[:top]

        try:
            docs = [_rerank_doc(c, self.contents, query) for c in candidates]
            reranked = _cached_rerank(query, docs[:20], min(top, len(docs)))
            degrade = _rerank_degrade_take()
            if reranked:
                results = []
                for rr in reranked:
                    idx = rr["index"]
                    if idx < len(candidates):
                        c = candidates[idx]
                        raw_score = float(rr.get("relevance_score", c["score"]))
                        display_score = raw_score
                        if display_score < 0 or display_score > 1:
                            display_score = 1 / (1 + np.exp(-display_score))
                        results.append(
                            {
                                "path": c["path"],
                                "score": round(raw_score, 4),
                                "display_score": round(max(0.0, min(display_score, 1.0)), 4),
                                "snippet": c["snippet"],
                                "reranker_score": round(raw_score, 4),
                            }
                        )
                # T4: 精排后按笔记新旧做时间衰减重排（decay_tau_days<=0 时零开销）
                return self._apply_time_decay(results, _decay_tau())

            # 精排无可用结果时，按向量+BM25 融合排序兜底，避免空结果
            # 复用上面已算出的 bm25_pool：原实现在这里重算一遍 BM25
            # （每次都要全库分词打分），是纯重复开销。
            bm25_map = {r["path"]: r for r in bm25_pool}
            fused = []
            for c in candidates:
                p = c["path"]
                v = float(c.get("score", 0.0))
                # 使用 BM25 的 display_score（已 0~1 归一化），而非原始得分
                bm25_norm = float((bm25_map.get(p) or {}).get("display_score", 0.0))
                fused.append((p, alpha * v + (1.0 - alpha) * bm25_norm, c))
            fused.sort(key=lambda x: x[1], reverse=True)
            # 精排缺失时保证 BM25 强命中候选有保底名额（可置 0 关断，见常量注释）
            fused = _apply_bm25_floor(fused, bm25_map, top)
            if degrade:
                # 机制级修复（2026-10-07）：降级不再静默 —— 日志带 query/耗时/
                # 候选数/模式，结果元数据带 degrade 模式，保底名额逻辑不变。
                log.warning(
                    "search.py 精排降级: query=%r 模式=%s 耗时=%.3fs 候选数=%d → 融合兜底+保底名额(SLOTS=%d)",
                    query, degrade["mode"], degrade["elapsed_s"], degrade["n_docs"],
                    FALLBACK_BM25_SLOTS,
                )
            results = []
            for p, score, c in fused[:top]:
                item = {
                    "path": p,
                    "score": round(score, 4),
                    "display_score": round(max(0.0, min(score, 1.0)), 4),
                    "snippet": c["snippet"],
                }
                if degrade:
                    item["degrade"] = degrade["mode"]
                results.append(item)
            return self._apply_time_decay(results, _decay_tau())
        except Exception:
            degrade = _rerank_degrade_take()
            log.warning("search.py 异常: search", exc_info=True)
            if degrade:
                # 兜底组装也炸了：仍把已登记的降级模式带上，不影响原返回结构
                for c in candidates[:top]:
                    c["degrade"] = degrade["mode"]
            return candidates[:top]

    def mentions(self, needle, limit=50, offset=0, max_lines_per_note=8):
        """全库精确扫描：哪些笔记提到了 needle（含行号）。

        与 search() 的分工（2026-09-29 新增）：
        - search()  = 相关性排序，只回 top-N（问「哪个最相关」）
        - mentions()= 确定性覆盖，回全库所有提到该字符串的笔记（问「哪些提到了」）

        背景（实测）：RAG top5 对全库实体的平均覆盖率只有 4.6%
        （对长尾实体尤其明显），「哪些笔记提到 X」类问题必然漏。
        本方法直接全库扫描，覆盖率 ~90%（受大小写/分词边界影响，非 100%）。

        为什么读原文而不是用 self.contents：
        self.contents 是剥掉 YAML frontmatter 的 body（见 _collect_docs），
        ① 行号会系统性偏移 21~22 行；② 只在 frontmatter 里提到的笔记会漏
        （实测会漏掉这类笔记）。读原文这两个问题都没有。

        性能：全库千级篇目实测 47~136ms，可接受。
        """
        needle = (needle or "").strip()
        try:
            limit = max(1, min(int(limit), 200))
            offset = max(0, int(offset))
        except Exception:
            limit, offset = 50, 0
        if not needle:
            return {"needle": needle, "total": 0, "total_lines": 0,
                    "offset": offset, "limit": limit, "items": []}

        items, total_lines = [], 0
        for f in NOTES_DIR.rglob("*.md"):
            rel = str(f.relative_to(NOTES_DIR))
            if is_excluded_path(rel, exclude_archived=True):
                continue
            if "{{title}}" in rel or f.name.startswith("{{"):
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if needle not in txt:
                continue
            txt = txt.replace("\r\n", "\n").replace("\r", "\n")
            ls = txt.split("\n")
            line_nos = [i + 1 for i, ln in enumerate(ls) if needle in ln]
            if not line_nos:
                continue
            total_lines += len(line_nos)
            items.append({
                "path": rel,
                "hit_lines": line_nos[:max_lines_per_note],
                "hit_count": len(line_nos),
            })

        # 命中多的排前，同数量按路径稳定排序
        items.sort(key=lambda x: (-x["hit_count"], x["path"]))
        total = len(items)
        page = items[offset:offset + limit]
        for it in page:
            try:
                txt = (NOTES_DIR / it["path"]).read_text(encoding="utf-8", errors="ignore")
                txt = txt.replace("\r\n", "\n").replace("\r", "\n")
                ls = txt.split("\n")
                it["samples"] = [
                    {"line": n, "text": ls[n - 1].strip()[:160]}
                    for n in it["hit_lines"][:3]
                    if 0 < n <= len(ls)
                ]
            except Exception:
                it["samples"] = []
        return {
            "needle": needle,
            "total": total,
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit,
            "items": page,
        }


search_index = SearchIndex()


def _init_index():
    loaded = search_index.load()
    if not loaded:
        search_index.build()
    elif get_setting("embedding_type") == "llm" and search_index.embeddings is None:
        # 走统一门闸（单例线程 + 断点 + 分片）：直接起 build_embeddings 会绕过它，
        # 与启动自愈/写入触发的重建并发改同一批索引文件（2026-11-08 独立审查指出）。
        search_index._schedule_embed_rebuild()
    # 先执行一次增量刷新（启动时同步已变更文件）
    threading.Thread(
        target=search_index.incremental_refresh, daemon=True
    ).start()

    # 定期后台刷新循环（每 5 分钟扫描一次文件变更，防止写操作漏了触发刷新）
    def _periodic_refresh_loop():
        while True:
            time.sleep(300)
            try:
                search_index.incremental_refresh()
            except Exception:
                log.warning("search.py 索引保存失败", exc_info=True)

    threading.Thread(target=_periodic_refresh_loop, daemon=True).start()