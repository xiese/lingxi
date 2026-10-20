"""反链接（backlinks）索引：随搜索索引增量维护，不做全库扫描。

数据来源
--------
直接用 `search_index.paths` + `search_index.contents`（已在内存里），
不读磁盘、不 rglob。索引换代时用签名（paths 长度 + 每篇 mtime 摘要）
判定是否需要整体重建。

增量维护
--------
- `update_note(rel, text)`：只重算**这一篇**的出链，删旧边插新边。
  写入 / 移动 / 删除笔记后调用，避免全库重扫。
- `rebuild()`：索引整体刷新后调用一次。

查询
----
- `backlinks_of(rel)`：谁链到 rel
- `outlinks_of(rel)`：rel 链到谁
"""
from __future__ import annotations

import threading
import zlib

from .links import extract_links, resolve_wikilink

_lock = threading.Lock()
# rel -> [(target_rel, raw)]，只保留已解析的边
_out = {}
# target_rel -> set(rel)
_in = {}
# 上次构建依据的签名
_sig = None


def _signature(index):
    """索引签名：paths 数量 + 每篇 mtime 的稳定摘要。

    用 crc32 而不是内置 hash()——后者受 PYTHONHASHSEED 影响，
    虽然进程内一致，但跨进程不可比，也不便于排查。
    """
    paths = list(getattr(index, "paths", []) or [])
    mtimes = getattr(index, "mtimes", {}) or {}
    if not paths:
        return (0, 0)
    acc = 0
    for p in paths:
        acc = (acc + zlib.crc32(("%s:%s" % (p, mtimes.get(p))).encode("utf-8"))) & 0xFFFFFFFF
    return (len(paths), acc)


def _index_links(index):
    """全量重建（索引换代时调用一次）。"""
    paths = list(getattr(index, "paths", []) or [])
    contents = list(getattr(index, "contents", []) or [])
    notes = [(p, _title_of(p)) for p in paths]
    ids, title_index, stem_index = _build_index(notes)
    out, inv = {}, {}
    for i, rel in enumerate(paths):
        text = contents[i] if i < len(contents) else ""
        links = []
        for lk in extract_links(rel, text, ids, title_index, stem_index):
            tgt = lk.get("resolved")
            if tgt and tgt != rel:
                links.append((tgt, lk.get("raw")))
                inv.setdefault(tgt, set()).add(rel)
        out[rel] = links
    return out, inv


def _title_of(rel):
    from pathlib import Path
    return Path(rel).stem


def _build_index(notes):
    from .links import build_link_index
    return build_link_index(notes)


def ensure(index):
    """确保索引与当前 search_index 同步（签名变了就重建）。"""
    global _out, _in, _sig
    sig = _signature(index)
    with _lock:
        if sig != _sig:
            _out, _in = _index_links(index)
            _sig = sig
    return _out, _in


def update_note(index, rel, text=None):
    """增量更新一篇笔记的出链（写入 / 移动 / 删除后调用）。

    完成后把签名同步为**当前索引**的签名：这样紧接着的 `ensure()`
    不会用索引里那份（可能尚未刷新的）contents 把本次增量结果冲掉。
    若索引随后真的换代，签名随之变化，`ensure()` 仍会整体重建 —— 语义正确。
    """
    global _sig
    with _lock:
        paths = list(getattr(index, "paths", []) or [])
        contents = list(getattr(index, "contents", []) or [])
        if rel not in paths:
            # 已删除（或尚未进索引）：清掉它的出链与反链
            for tgt, _raw in _out.pop(rel, []):
                s = _in.get(tgt)
                if s:
                    s.discard(rel)
                    if not s:
                        _in.pop(tgt, None)
            _sig = _signature(index)
            return
        if text is None:
            i = paths.index(rel)
            text = contents[i] if i < len(contents) else ""
        ids, title_index, stem_index = _build_index([(p, _title_of(p)) for p in paths])
        # 先撤旧边
        for tgt, _raw in _out.get(rel, []):
            s = _in.get(tgt)
            if s:
                s.discard(rel)
                if not s:
                    _in.pop(tgt, None)
        links = []
        for lk in extract_links(rel, text, ids, title_index, stem_index):
            tgt = lk.get("resolved")
            if tgt and tgt != rel:
                links.append((tgt, lk.get("raw")))
                _in.setdefault(tgt, set()).add(rel)
        _out[rel] = links
        _sig = _signature(index)


def remove_note(rel):
    """显式移除一篇笔记的全部链接关系（移动 / 删除用）。

    与 `update_note(index, rel)` 在 rel 已不在索引时那条分支的区别：
    这里**不依赖索引是否已刷新** —— 移动刚发生时旧路径可能还在
    `index.paths` 里，光看索引判断不出「它已经走了」。
    """
    global _sig
    with _lock:
        for tgt, _raw in _out.pop(rel, []):
            s = _in.get(tgt)
            if s:
                s.discard(rel)
                if not s:
                    _in.pop(tgt, None)
        _in.pop(rel, None)
        _sig = None  # 下次 ensure() 重新按索引校准


def backlinks_of(index, rel):
    """谁链到 rel（返回排序后的列表）。"""
    ensure(index)
    return sorted(_in.get(rel, set()))


def outlinks_of(index, rel):
    """rel 链到谁（返回去重后的目标列表）。"""
    ensure(index)
    return sorted({t for t, _r in _out.get(rel, [])})


def reset():
    """清空缓存（测试用）。"""
    global _out, _in, _sig
    with _lock:
        _out, _in, _sig = {}, {}, None
