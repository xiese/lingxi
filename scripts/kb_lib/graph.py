"""知识库图谱计算 — wiki 双链 + 语义相似度自动连线。

对外契约（v1.0.0 冻结）::

    compute_graph(min_sim=0.80, max_links=8,
                  include_semantic=True, include_wiki=True)
        -> {"nodes": [{"id", "title", "dir", "tags", "group", "degree"}],
            "links": [{"source", "target", "type", "weight"}],
            "meta": {"node_count", "link_count", "wiki_count",
                     "semantic_count", "min_sim", "max_links", "generated_at"}}

设计要点：
- wiki 边：沿用原有 [[双链]] 解析逻辑（含标题 / 文件名回退解析），type="wiki"。
- 语义边：search_index.paths + search_index.embeddings，先 L2 归一化，再按行分批
  （每批 128 行）算余弦相似度；每篇取 top-N 且必须 >= min_sim，用
  (min(i,j), max(i,j)) 去重成无向边，weight=相似度（4 位小数），type="semantic"。
- 缓存：模块级 dict + 锁 + TTL 300s；依据 embeddings 的 mtime / 大小 / 形状与
  笔记最新 mtime 失效。
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .auth import parse_fm
from .config import EMBEDDINGS_FILE, NOTES_DIR, is_excluded_path
from .search import search_index
from .services.links import extract_links, resolve_wikilink

log = logging.getLogger("kb.graph")

CACHE_TTL = 300            # 缓存有效期（秒）
SEMANTIC_BATCH = 128       # 语义相似度分流行数
DEFAULT_MIN_SIM = 0.80
DEFAULT_MAX_LINKS = 8
MAX_LINKS_CAP = 50

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

_cache_lock = threading.Lock()
_cache = {}                # key -> {"data":..., "ts":..., "sig":...}


# ── 缓存 ──

def invalidate_cache():
    """清空图谱缓存（笔记写入 / 索引重建后调用）。"""
    with _cache_lock:
        _cache.clear()


def _scan_notes():
    """一次遍历笔记目录，返回 (文件列表, 最新 mtime, 文件数)。

    compute_graph 既需要签名（判断缓存是否有效）又需要文件列表（解析双链），
    以前两处各自 rglob 一遍，等于每次计算都走两趟目录树。这里合并成一次遍历。

    文件列表显式排序：rglob 的顺序取决于文件系统，而 title_index / stem_index
    用 setdefault 记录「标题 → 路径」，不排序时同名笔记哪一篇胜出会随机变化，
    导致同一份笔记集在不同机器上算出不同的 wiki 边。
    """
    files, latest = [], 0.0
    try:
        for f in NOTES_DIR.rglob("*.md"):
            files.append(f)
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt > latest:
                latest = mt
    except Exception:
        log.warning("graph.py 异常: _scan_notes", exc_info=True)
        return [], 0.0, -1
    files.sort()
    return files, latest, len(files)


def _notes_signature(files=None, latest=None, count=None):
    """笔记集合签名：最新 mtime + 文件数。

    调用方已做过 _scan_notes 时直接传入，避免重复遍历。
    """
    if files is None:
        files, latest, count = _scan_notes()
    return (round(latest, 3), count)


def _embed_signature():
    """embeddings 签名：文件 mtime + 大小 + 内存中形状。"""
    mtime = size = 0
    try:
        st = EMBEDDINGS_FILE.stat()
        mtime, size = int(st.st_mtime), int(st.st_size)
    except OSError:
        pass
    shape = None
    emb = getattr(search_index, "embeddings", None)
    if emb is not None and hasattr(emb, "shape"):
        try:
            shape = tuple(int(x) for x in emb.shape)
        except Exception:
            shape = None
    return (mtime, size, shape)


def _graph_signature(files=None, latest=None, count=None):
    """图谱签名：笔记集合签名 + embeddings 签名，缓存失效判定依据。"""
    return (_notes_signature(files, latest, count), _embed_signature())


def _cache_lookup(key, now):
    """查缓存，返回 (缓存条目, TTL 内可复用的结果)。

    结果非 None → 未过期直接返回；为 None → 已过期或未命中，
    条目（可能为 None）交回调用方做签名比对。
    """
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit["ts"] < CACHE_TTL:
        return hit, hit["data"]
    return hit, None


def _cache_touch(key, now):
    """签名未变：刷新 TTL 并复用旧结果；条目已被并发清空则返回 None。"""
    with _cache_lock:
        cur = _cache.get(key)
        if cur is None:
            return None
        cur["ts"] = now
        return cur["data"]


def _cache_store(key, data, now, sig):
    """写入缓存。"""
    with _cache_lock:
        _cache[key] = {"data": data, "ts": now, "sig": sig}


# ── 语义向量 ──

def _resolve_embeddings():
    """返回 (paths, normalized_embeddings)；向量不可用时第二项为 None。"""
    si = search_index
    paths = list(getattr(si, "paths", None) or [])
    emb = getattr(si, "embeddings", None)
    if emb is None or not paths:
        try:
            si.load()
        except Exception:
            log.warning("graph.py 异常: search_index.load", exc_info=True)
        paths = list(getattr(si, "paths", None) or [])
        emb = getattr(si, "embeddings", None)
    if emb is None and EMBEDDINGS_FILE.exists():
        try:
            arr = np.load(str(EMBEDDINGS_FILE))
            if paths and int(arr.shape[0]) == len(paths):
                emb = arr
        except Exception:
            log.warning("graph.py 异常: 读取 embeddings", exc_info=True)
            emb = None
    if emb is None or not paths:
        return paths, None
    arr = np.asarray(emb)
    if arr.ndim != 2 or int(arr.shape[0]) != len(paths):
        log.warning("graph.py: embeddings 与 paths 数量不一致，跳过语义连线")
        return paths, None
    arr = np.array(arr, dtype=np.float32, copy=True)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.maximum(norms, 1e-12)      # L2 归一化
    return paths, arr


def _semantic_links(paths, arr, node_ids, min_sim, max_links):
    """分批计算余弦相似度，返回去重后的语义无向边。"""
    n = arr.shape[0]
    best = {}                                  # (i, j) i<j -> 相似度
    for start in range(0, n, SEMANTIC_BATCH):
        end = min(start + SEMANTIC_BATCH, n)
        block = arr[start:end] @ arr.T         # (batch, n)
        for r in range(end - start):
            i = start + r
            if paths[i] not in node_ids:
                continue
            row = block[r]
            row[i] = -1.0                      # 排除自身
            cand = np.nonzero(row >= min_sim)[0]
            if cand.size == 0:
                continue
            if cand.size > max_links:
                vals = row[cand]
                keep = np.argpartition(-vals, max_links - 1)[:max_links]
                cand = cand[keep]
            for j in cand:
                j = int(j)
                if j == i or paths[j] not in node_ids:
                    continue
                key = (i, j) if i < j else (j, i)
                s = float(row[j])
                if s > best.get(key, -1.0):
                    best[key] = s
    links = [
        {"source": paths[i], "target": paths[j],
         "type": "semantic", "weight": round(s, 4)}
        for (i, j), s in best.items()
    ]
    links.sort(key=lambda l: (l["source"], l["target"]))
    return links


def _build_semantic_links(include_semantic, ids, min_sim, max_links):
    """构建语义边：返回 (links, degraded)。

    degraded=True 表示向量面暂不可用（索引重建窗口或异常），只降级不炸请求。
    """
    if not include_semantic:
        return [], False
    try:
        paths, arr = _resolve_embeddings()
        if arr is None:
            # 索引 / 向量在重建窗口内暂不可用 → 降级为仅 wiki 边
            return [], True
        return _semantic_links(paths, arr, ids, min_sim, max_links), False
    except Exception:
        log.warning("graph.py 异常: 语义连线", exc_info=True)
        return [], True


# ── 节点 / 目录 / 标签 ──

def _norm_tags(meta):
    """frontmatter tags 归一化为字符串列表。"""
    raw = None
    if isinstance(meta, dict):
        raw = meta.get("tags")
        if raw is None:
            raw = meta.get("tag")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    if isinstance(raw, (list, tuple, set)):
        out = []
        for t in raw:
            s = str(t).strip()
            if s:
                out.append(s)
        return out
    return []


def _dir_of(rel):
    parent = str(Path(rel).parent)
    return "" if parent == "." else parent.replace("\\", "/")


def _group_of(rel, tags):
    """group：优先一级目录，其次第一个标签。"""
    parts = str(rel).split("/")
    if len(parts) > 1 and parts[0]:
        return parts[0]
    if tags:
        return tags[0]
    return "未分类"


def _build_nodes(files=None):
    nodes, ids = [], set()
    file_cache = {}
    title_index, stem_index = {}, {}
    for f in (_scan_notes()[0] if files is None else files):
        rel = str(f.relative_to(NOTES_DIR))
        if is_excluded_path(rel, exclude_assets=True, exclude_archived=False):
            continue
        t = f.stem
        tags = []
        try:
            text = f.read_text(encoding="utf-8")
            meta, _ = parse_fm(text)
            t = meta.get("title") or f.stem
            tags = _norm_tags(meta)
            file_cache[rel] = text
        except Exception:
            log.debug("graph.py 异常: 读取笔记", exc_info=True)
        ids.add(rel)
        title_index.setdefault(str(t), rel)
        stem_index.setdefault(Path(rel).stem, rel)
        nodes.append({
            "id": rel,
            "title": t,
            "dir": _dir_of(rel),
            "tags": tags,
            "group": _group_of(rel, tags),
            "degree": 0,
        })
    return nodes, ids, file_cache, title_index, stem_index


def _resolve_wikilink(source_rel, raw_target, ids, title_index, stem_index):
    """兼容包装：解析逻辑已抽到 `services/links.py`（图谱与阅读共用）。"""
    return resolve_wikilink(source_rel, raw_target, ids, title_index, stem_index)


def _build_wiki_links(file_cache, ids, title_index, stem_index):
    """解析 [[双链]] 生成 wiki 边（type="wiki"，weight=1.0，按节点序去重）。"""
    links = []
    seen_links = set()
    for rel, text in file_cache.items():
        try:
            for lk in extract_links(rel, text, ids, title_index, stem_index):
                target = lk.get("resolved")
                if target and target != rel:
                    k = (rel, target)
                    if k not in seen_links:
                        links.append({"source": rel, "target": target,
                                      "type": "wiki", "weight": 1.0})
                        seen_links.add(k)
        except Exception:
            log.warning("graph.py 异常: wiki 双链解析", exc_info=True)
    return links


# ── 主入口 ──

def _normalize_graph_params(min_sim, max_links, include_semantic, include_wiki):
    """归一化入口参数：min_sim 收敛到 [0,1]，max_links 收敛到 [1, MAX_LINKS_CAP]。

    非法输入（无法 float/int）回落各自默认值；两个开关统一转 bool。
    """
    try:
        min_sim = float(min_sim)
    except (TypeError, ValueError):
        min_sim = DEFAULT_MIN_SIM
    min_sim = min(1.0, max(0.0, min_sim))
    try:
        max_links = int(max_links)
    except (TypeError, ValueError):
        max_links = DEFAULT_MAX_LINKS
    max_links = min(MAX_LINKS_CAP, max(1, max_links))
    include_semantic = bool(include_semantic)
    include_wiki = bool(include_wiki)
    return min_sim, max_links, include_semantic, include_wiki


def _apply_degrees(nodes, links):
    """按当前边集回填节点 degree（原地改 nodes，返回 nodes 供链式使用）。"""
    degree = {}
    for l in links:
        degree[l["source"]] = degree.get(l["source"], 0) + 1
        degree[l["target"]] = degree.get(l["target"], 0) + 1
    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)
    return nodes


def _graph_result(nodes, links, min_sim, max_links):
    """组装图谱响应（模块 docstring 冻结的对外契约结构）。"""
    wiki_count = sum(1 for l in links if l["type"] == "wiki")
    semantic_count = sum(1 for l in links if l["type"] == "semantic")
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "node_count": len(nodes),
            "link_count": len(links),
            "wiki_count": wiki_count,
            "semantic_count": semantic_count,
            "min_sim": round(min_sim, 4),
            "max_links": max_links,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    }


def compute_graph(min_sim=DEFAULT_MIN_SIM, max_links=DEFAULT_MAX_LINKS,
                  include_semantic=True, include_wiki=True):
    """计算图谱（节点 + wiki 边 + 语义边），带 TTL 缓存。"""
    min_sim, max_links, include_semantic, include_wiki = _normalize_graph_params(
        min_sim, max_links, include_semantic, include_wiki)

    key = (round(min_sim, 4), max_links, include_semantic, include_wiki)
    now = time.time()
    hit, cached = _cache_lookup(key, now)
    if cached is not None:
        return cached

    note_files, note_latest, note_count = _scan_notes()
    sig = _graph_signature(note_files, note_latest, note_count)
    if hit is not None and hit["sig"] == sig:
        reused = _cache_touch(key, now)
        if reused is not None:
            return reused

    nodes, ids, file_cache, title_index, stem_index = _build_nodes(note_files)

    links = []
    if include_wiki:
        links = _build_wiki_links(file_cache, ids, title_index, stem_index)

    semantic_links, degraded = _build_semantic_links(
        include_semantic, ids, min_sim, max_links)
    links.extend(semantic_links)

    _apply_degrees(nodes, links)
    result = _graph_result(nodes, links, min_sim, max_links)

    if degraded:
        # 向量面暂不可用（索引重建窗口）→ 不写缓存，下一次请求立即重试
        log.warning("graph.py: 语义连线暂不可用，本次结果不缓存")
        return result
    _cache_store(key, result, now, sig)
    return result
