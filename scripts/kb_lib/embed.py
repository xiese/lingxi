"""知识库 API — 嵌入模型（直接使用 config.py 的 mtime 缓存）"""

import json, time
import logging
import urllib.request, urllib.error
import numpy as np

from .config import load_settings


# ── 共享 API 调用（带指数退避重试）──

log = logging.getLogger("kb.embed")

import threading as _threading
_EMB_WIN=[]; _EMB_LOCK=_threading.Lock()
# 单次嵌入请求等待配额的上限（秒）；超时后降级而不是无限阻塞工作线程
_EMB_WAIT_MAX = float(__import__('os').environ.get('KB_EMB_WAIT_MAX', '120'))
# 单次 _api_call 的重试总预算（秒）。指数退避的睡眠总和不再超过它，
# 使「服务不可达」从实测 254 秒收敛到约 60 秒量级。
_HTTP_CALL_BUDGET = 90.0

def _emb_rate_wait(tpm, deadline=None):
    """等待嵌入配额窗口。deadline 到点仍未放行则返回 False。

    原实现是 `while True`：配额打满且窗口不前进时永不返回，而调用方在
    Flask 工作线程里，8 个线程被卡住就整站不可用（连 /health 都排队）。
    给出上限后，调用方可以降级（跳过嵌入、退回 BM25）而不是无限等。
    """
    if deadline is None:
        deadline = time.time() + _EMB_WAIT_MAX
    while True:
        with _EMB_LOCK:
            now=time.time(); cut=now-60
            while _EMB_WIN and _EMB_WIN[0][0]<cut: _EMB_WIN.pop(0)
            used=sum(n for _,n in _EMB_WIN)
            if (not _EMB_WIN) or (used+6000<=tpm): return True
            if now >= deadline:
                log.warning("嵌入配额等待超时（%ss），本轮降级", _EMB_WAIT_MAX)
                return False
            wait=60-(now-_EMB_WIN[0][0])+0.3
        time.sleep(max(0.5,min(wait,30,deadline-time.time())))

def _emb_rate_record(t):
    with _EMB_LOCK: _EMB_WIN.append((time.time(), int(t or 0)))

def _api_call(endpoint, body, timeout=60, retries=3):
    """通用嵌入/精排 API 请求，带指数退避重试
    4xx 错误不重试（客户端问题），5xx/网络错误重试

    重试总时长受 _HTTP_CALL_BUDGET 约束：服务不可达时原实现的
    退避总和可达 126 秒、再叠加每轮 60 秒连接超时，实测 254 秒才失败，
    而这段时间调用方（含 search 的请求路径）一直被阻塞。
    预算耗尽即抛最后一次异常，交给调用方按自己的契约降级。
    """
    s = load_settings()
    data = json.dumps(body).encode()
    _proxy = (s.get("embedding_proxy") or "").strip()
    if _proxy:
        _opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": _proxy, "https": _proxy})
        )
    else:
        _opener = urllib.request.build_opener()
    _started = time.monotonic()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{s['embedding_base']}/{endpoint}",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {s['embedding_key']}",
                },
            )
            resp = _opener.open(req, timeout=timeout or 10)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 429（限流）与 5xx（服务端）都退避重试；其余 4xx 直接抛
            retryable = (e.code == 429) or (e.code >= 500)
            if retryable and attempt < retries - 1:
                delay = None
                try:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra is not None:
                        delay = float(ra)
                except Exception:
                    log.debug("embed.py 异常: _api_call", exc_info=True)
                    delay = None
                if delay is None or delay <= 0:
                    delay = 2 ** (attempt + 1)
                delay = min(delay, 60)
                if time.monotonic() - _started + delay > _HTTP_CALL_BUDGET:
                    log.warning("嵌入/精排 API 重试预算耗尽（%.0fs），放弃重试",
                                time.monotonic() - _started)
                    raise
                time.sleep(delay)
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                delay = min(2 ** (attempt + 1), 60)
                if time.monotonic() - _started + delay > _HTTP_CALL_BUDGET:
                    log.warning("嵌入/精排 API 重试预算耗尽（%.0fs），放弃重试",
                                time.monotonic() - _started)
                    raise
                time.sleep(delay)
            else:
                raise

def embedding_api(texts, task=None):
    """嵌入模型调用（自动截断超长文本，按请求预算分批）

    task: 可选任务适配器（Jina v5 专用），retrieval.query / retrieval.passage。
          仅当模型名以 jina-embeddings-v5 开头时才随请求发送，其它服务商自动忽略。
    MAX_CHARS = 6000
    MAX_TOTAL_CHARS = 12000
    """
    MAX_CHARS = 6000
    MAX_TOTAL_CHARS = 12000
    if isinstance(texts, str):
        texts = [texts]
    texts = [t[:MAX_CHARS] if isinstance(t, str) else t for t in texts]
    s = load_settings()
    model = s.get("embedding_model") or ""
    if not (s.get("embedding_base") or "").strip() or not model.strip():
        raise RuntimeError(
            "未配置嵌入服务：向量检索需要 embedding_base 与 embedding_model；"
            "仅用关键词检索时请把 embedding_type 设为 bm25（见 docs/CONFIGURATION.md）"
        )
    is_jina5 = str(model).startswith("jina-embeddings-v5")
    batches, cur, cur_len = [], [], 0
    for t in texts:
        n = len(t) if isinstance(t, str) else 0
        if cur and cur_len + n > MAX_TOTAL_CHARS:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(t)
        cur_len += n
    if cur:
        batches.append(cur)
    embeds = []
    try:
        _tpm = int(s.get("embedding_tokens_per_min") or 0)
    except Exception:
        log.debug("embed.py 异常: embedding_api", exc_info=True)
        _tpm = 0
    try:
        _gap = float(s.get("embedding_request_gap") or 0)
    except Exception:
        log.debug("embed.py 异常: embedding_api", exc_info=True)
        _gap = 0.0
    for _i, b in enumerate(batches):
        if _tpm > 0:
            if not _emb_rate_wait(_tpm):
                # 配额等待超时：已算出的部分照常返回，避免整批失败。
                # 调用方拿到短向量会退回 BM25，比把工作线程耗死在这里好。
                break
        elif _i and _gap > 0:
            time.sleep(_gap)
        body = {"model": model, "input": b}
        if task and is_jina5:
            body["task"] = task
        resp = _api_call("embeddings", body, retries=8)
        try:
            _ut = int((resp.get("usage") or {}).get("total_tokens") or 0)
        except Exception:
            log.debug("embed.py 异常: embedding_api", exc_info=True)
            _ut = 0
        _emb_rate_record(_ut)
        embeds.extend(d["embedding"] for d in sorted(resp["data"], key=lambda x: x["index"]))
    return np.array(embeds, dtype=np.float32)


def rerank_api(query, documents, top_n=5, timeout=60, retries=3):
    """调用精排模型（模型名可配置；默认 Jina reranker，避免写死不支持的模型导致 422）。

    - 单文档截断到 1000 字符、最多 12 篇，避免超上游上下文限制。
    - timeout/retries 透传给 _api_call：精排是可选增强（有向量+BM25 融合兜底），
      调用方应传较小的 timeout 与 retries=1，让慢请求快速失败而不是退避重试。

    2026-10-01：上限曾提到 20，因检索质量回退已回滚回 12（保持历史行为）。
    提到 20 后 A/B 实测（n=65 用例）：改前 61/65=94% → 改后 60/65=92%，净 -1。
    聚焦新漏用例多轮验证（各 6 轮）：改前 6/6 命中（排名 5）→ 改后 0/6，稳定复现。
    根因：目标本在候选池第 6 位，池扩到 20 后涌入 8 篇同主题近似笔记，
    精排被干扰项带偏，把原本排第 5 的正确目标挤出 top5。
    平均延迟/收益为正，但干扰项风险更大，净效果为负。
    另：早先 n=20 小样本报的「>3s 0%」是乐观数，n=100 实测为 7%。
    """
    s = load_settings()
    model = (s.get("rerank_model") or "").strip()
    if not model:
        return []  # 未配置精排模型时直接跳过，由融合结果兜底
    docs = [d[:1000] if isinstance(d, str) else d for d in (documents or [])][:12]
    if not docs:
        return []
    resp = _api_call("rerank", {
        "model": model,
        "query": query,
        "documents": docs,
        "top_n": top_n,
    }, timeout=timeout, retries=retries)
    return resp.get("results", [])