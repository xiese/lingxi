#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线全量重建知识库向量索引（绕开在线的 300s 单轮预算）。

问题形态（2026-11-08）：
    .embeddings.npy 停在 1985 行、paths 已 2075 行 → 启动即判「索引产物不一致」、
    向量检索被禁用；而**每次写入**触发的增量刷新都会试图全量重建，在线持锁时
    300s 只跑到 320/2074 → 超预算强制回滚 → 下次写入再来一遍。
    结果：单次写入要等约 7 分钟，向量面永远补不上。

本脚本在服务之外把全部正文的向量一次算完，并按 search.py 的既有格式
（原子写 .embeddings.npy + faiss + manifest）落盘；随后重启 kb-server 即对齐。

用法：
    python3 kb-rebuild-embeddings.py --dry-run   # 只算 1 批，打印外推耗时，不落盘
    python3 kb-rebuild-embeddings.py             # 正式重建 + 落盘
"""
import os
import sys
import time
from pathlib import Path

# 数据根与代码目录是分开的（生产形态：代码在 <KB_ROOT>/current/，数据在 <KB_ROOT>/）。
# KB_ROOT 必须显式给：不给的话 config.py 会按代码位置推算成 <KB_ROOT>/current，
# 于是 load() 读不到主索引（paths=0，白跑一轮）。
_KB_ROOT = os.environ.get("KB_ROOT")
if not _KB_ROOT:
    sys.exit("请先指定知识库数据根，例如：\n    KB_ROOT=$HOME/kb python3 kb-rebuild-embeddings.py")
os.environ["KB_ROOT"] = _KB_ROOT
sys.path.insert(0, os.environ.get("KB_CODE", str(Path(_KB_ROOT) / "current" / "scripts")))

import numpy as np  # noqa: E402

from kb_lib import config  # noqa: E402
from kb_lib.search import (  # noqa: E402
    SearchIndex, embedding_api, write_index_manifest,
    _atomic_np_save, EMBEDDINGS_FILE, FAISS_FILE,
)

BATCH = 16


def main():
    dry = "--dry-run" in sys.argv
    si = SearchIndex()
    ok = si.load()
    n = len(si.contents or [])
    print("load=%s paths=%d contents=%d vector_ready=%s" % (
        ok, len(si.paths or []), n, si.is_vector_search_ready()), flush=True)
    if not n:
        print("没有 contents，退出")
        return 1
    plan = (n + BATCH - 1) // BATCH
    print("批次 %d × %d 条" % (plan, BATCH), flush=True)

    if dry:
        t = time.time()
        v = embedding_api(si.contents[:BATCH], task="retrieval.passage")
        dt = time.time() - t
        print("dry-run: 1 批用时 %.2fs，形状 %s；按此外推全量约 %.0fs"
              % (dt, getattr(v, "shape", None), dt * plan))
        return 0

    t0 = time.time()
    vecs = []
    for i in range(0, n, BATCH):
        vecs.append(embedding_api(si.contents[i:i + BATCH], task="retrieval.passage"))
        if (i // BATCH) % 10 == 0:
            print("  %d/%d  用时 %.0fs" % (i + BATCH, n, time.time() - t0), flush=True)
    emb = np.vstack(vecs)
    print("嵌入完成 %s 用时 %.0fs" % (emb.shape, time.time() - t0), flush=True)

    if emb.shape[0] != len(si.paths or []):
        print("❌ 结果与 paths 不对齐：emb=%d paths=%d"
              % (emb.shape[0], len(si.paths or [])))
        return 2

    si.embeddings = emb
    ids = si._ensure_faiss_ids()
    si.faiss_index = si._build_faiss_index(emb, ids)
    si._prune_faiss_ids()
    si._save_faiss_ids()
    _atomic_np_save(EMBEDDINGS_FILE, emb)
    if si.faiss_index is not None:
        import faiss
        tmp = str(FAISS_FILE) + ".tmp"
        faiss.write_index(si.faiss_index, tmp)
        os.replace(tmp, FAISS_FILE)
    write_index_manifest(
        n_paths=len(si.paths or []), n_contents=n,
        n_embeddings=int(emb.shape[0]),
        n_faiss=int(si.faiss_index.ntotal) if si.faiss_index is not None else 0,
        index_version=si._index_version,
        path=config.INDEX_MANIFEST_FILE,
        extra={"consistent": True, "reason": "ok", "source": "offline-rebuild"},
    )
    print("✅ 落盘完成：embeddings 与 paths 已对齐；重启 kb-server 后生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
