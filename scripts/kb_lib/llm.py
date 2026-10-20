"""知识库 API — LLM 聊天函数"""

import json, math, time, random
import logging
import urllib.error
import urllib.request

from .config import load_settings


log = logging.getLogger("kb.llm")

# 429 退避参数：指数增长 + 抖动（避免多实例同时重试再撞限流）。
_RATE_LIMIT_BASE = 1.5      # 首次退避基数（秒）
_RATE_LIMIT_MAX = 20.0      # 单次退避上限（秒），上游 Retry-After 过大时截断
_RATE_LIMIT_JITTER = 0.25   # ±25% 抖动


def _llm_request(msgs, max_tokens, temp, timeout=30):
    """构建并发送 LLM 请求，返回响应文本或抛出异常。

    推理模型会把 max_tokens 大量消耗在 reasoning_content 上，额度偏小时
    content 可能为空。此时按 finish_reason 判断是否被截断，截断则升额度重试。
    """
    s = load_settings()
    if not (s.get("llm_base") or "").strip() or not (s.get("llm_model") or "").strip():
        raise RuntimeError(
            "未配置 LLM 服务：请在 Web 界面「设置」填写 LLM 接口地址、密钥与模型名，"
            "或写入 .kb_settings.json 的 llm_base / llm_model（见 docs/CONFIGURATION.md）"
        )
    # 推理模型的思考长度不可控（实测同一 prompt 思考 950~10900 字），
    # 固定额度会被思考吃光导致 content 为空。按阶梯升额度重试，够用即返回。
    budgets = [max_tokens] + [b for b in (16000, 32000) if b > max_tokens]
    for budget in budgets:
        data = json.dumps({
            "model": s["llm_model"],
            "messages": msgs,
            "max_tokens": budget,
            "temperature": temp,
        }).encode()
        req = urllib.request.Request(
            f"{s['llm_base']}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {s['llm_key']}",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        ch = resp["choices"][0]
        msg = ch["message"]
        text = (msg.get("content") or "").strip()
        if text:
            return text
        # 正文为空且因额度耗尽被截断 → 下一档额度重试；非截断则重试无意义
        if ch.get("finish_reason") != "length":
            break
    # 全部额度都只产出思考：返回空串交由调用方兜底。
    # 不返回 reasoning_content —— 那是思考过程，不是答案；当标签用时会被写进笔记。
    return ""



class LLMUnavailable(RuntimeError):
    """LLM 调用失败 —— 与「模型正常返回了一段文本」严格区分。

    原实现失败时返回字符串 `"[LLM错误] ..."`，调用方无从分辨，于是：
    - /api/llm/ask 把错误串当答案回给用户（HTTP 200，看起来是正常回答）
    - /api/llm/reorganize 把它塞进 JSON 解析，再降级成 {"raw": 错误串}
    - 标签推荐把它按逗号切分，产出「标签」= ["[LLM错误] ..."]
    改为抛异常后，各路由能如实返回 503，前端也能给出「服务不可用」而不是
    假装回答了。

    2026-10-19：新增 status / rate_limited / retry_after 三个属性。上游限流
    （HTTP 429）是可恢复错误，不该和「服务挂了」共用 503 —— 路由据此回 429，
    并把真实原因（含 429 / rate limit 字样）透给用户。
    """

    def __init__(self, message, status=None, rate_limited=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited
        self.retry_after = retry_after


def _read_error_body(e, limit=400):
    """尽力读出 HTTPError 的响应体（读失败不影响主流程）。"""
    try:
        raw = e.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw)[:limit]


def _extract_error_message(body):
    """从网关错误体里取人能看懂的那一句（OpenAI 风格 error.message 优先）。"""
    try:
        d = json.loads(body)
    except Exception:
        return body.strip()
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        for k in ("detail", "message"):
            if d.get(k):
                return str(d[k])
    return body.strip()


def _retry_after_seconds(e):
    """解析 Retry-After（秒数或 HTTP 日期），拿不到返回 None。"""
    headers = getattr(e, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        sec = float(str(raw).strip())
    except (TypeError, ValueError):
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            sec = (parsedate_to_datetime(str(raw)) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return None
    return sec if sec > 0 else None


def _classify_error(e):
    """把底层异常翻译成 (是否限流, Retry-After 秒, 人话原因)。

    限流必须能被单独识别：此前 429 和「连不上」走同一条路，最终只剩
    「HTTP Error 429: Too Many Requests」被包成 503，用户看不到限流。
    """
    if isinstance(e, urllib.error.HTTPError):
        body = _read_error_body(e)
        detail = _extract_error_message(body)
        if e.code == 429:
            reason = detail or "Too Many Requests"
            return True, _retry_after_seconds(e), f"上游限流 HTTP 429（rate limit）: {reason}"
        return False, _retry_after_seconds(e), f"上游返回 HTTP {e.code}: {detail or e.reason}"
    return False, None, str(e) or e.__class__.__name__


def _rate_limit_delay(attempt, retry_after):
    """429 的退避时长：尊重 Retry-After，否则指数退避 + 抖动。

    抖动只加在「自算退避」这条路上（有意取舍）：Retry-After 是上游明确要求的
    等待时长，再叠加抖动只会拖慢恢复；防惊群由指数退避那条路的抖动负责。
    """
    if retry_after is not None:
        return min(float(retry_after), _RATE_LIMIT_MAX)
    base = min(_RATE_LIMIT_BASE * (2 ** attempt), _RATE_LIMIT_MAX)
    return base * (1 + random.uniform(-_RATE_LIMIT_JITTER, _RATE_LIMIT_JITTER))


def retry_after_header(e):
    """把异常的 retry_after 换算成响应头里的秒数（≥1，与退避共用同一上限）。

    上游 429 经常不带 Retry-After（应用层限流器常见），此时退回
    _RATE_LIMIT_BASE 向上取整的兜底值 —— 429 响应必须永远带得出这个头，
    否则客户端只能盲猜等待时间。
    """
    ra = getattr(e, "retry_after", None)
    try:
        sec = float(ra)
    except (TypeError, ValueError):
        sec = float(math.ceil(_RATE_LIMIT_BASE))
    if sec <= 0:
        sec = float(math.ceil(_RATE_LIMIT_BASE))
    return max(1, int(math.ceil(min(sec, _RATE_LIMIT_MAX))))


def llm_chat(msgs, max_tokens=800, temp=0.3):
    try:
        return _llm_request(msgs, max_tokens, temp, 30)
    except Exception as e:
        limited, retry_after, reason = _classify_error(e)
        log.warning("llm.py 异常: llm_chat", exc_info=True)
        raise LLMUnavailable(reason, status=429 if limited else None,
                             rate_limited=limited, retry_after=retry_after) from e


def llm_chat_with_retry(msgs, max_tokens=800, temp=0.3, retries=3):
    """带重试的 LLM 调用。

    429 单独识别：指数退避 + 抖动、尊重 Retry-After；重试耗尽后抛出的
    LLMUnavailable 带 rate_limited=True 和真实原因，调用方可回 429 而非 503。
    其他异常保持原有「重试 3 次、2/4 秒退避」行为不变。
    """
    last_err = None
    last_reason = ""
    rate_limited = False
    status = None
    retry_after = None
    last_delay = None
    for attempt in range(retries):
        try:
            return _llm_request(msgs, max_tokens, temp, 60)
        except Exception as e:
            log.debug("llm.py 异常: llm_chat_with_retry", exc_info=True)
            limited, ra, reason = _classify_error(e)
            last_err, last_reason = e, reason
            # 分类只认「最后一次尝试」：末次不是 429 就必须摘掉限流标记和旧
            # Retry-After，否则 429 → 500 的序列会被报成「限流请稍后重试」。
            rate_limited = limited
            retry_after = ra
            if limited:
                status = 429
            elif isinstance(e, urllib.error.HTTPError):
                status = e.code
            else:
                status = None
            if attempt < retries - 1:
                if limited:
                    delay = _rate_limit_delay(attempt, ra)
                    log.warning("LLM 上游限流 429，%.2f 秒后重试（第 %d/%d 次）",
                                delay, attempt + 1, retries - 1)
                else:
                    delay = 2 ** (attempt + 1)
                last_delay = delay
                time.sleep(max(delay, 0))
    # 上游 429 没带 Retry-After 时，用最后一次实际退避时长兜底，
    # 保证路由侧无条件设得出 Retry-After 头。
    if rate_limited and retry_after is None:
        retry_after = last_delay if last_delay is not None else float(math.ceil(_RATE_LIMIT_BASE))
    raise LLMUnavailable(last_reason, status=status, rate_limited=rate_limited,
                         retry_after=retry_after) from last_err
