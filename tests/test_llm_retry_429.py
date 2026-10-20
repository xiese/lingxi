"""LLM 429 限流：重试 + 退避 + 真实原因透出（2026-10-19）。

背景：上游 8318 返回 429 时，`llm_chat_with_retry` 把它当普通异常重试 3 次
（固定 2/4 秒、不读 Retry-After），最终包成 `LLMUnavailable("HTTP Error 429")`，
`/api/llm/ask` 再统一回 503「LLM 服务不可用」——限流被伪装成服务故障。

本文件钉住三条不变量：
  1. 429 必须单独识别并重试（次数可观测）；
  2. 最终错误文本必须带 429 / rate limit，不能只剩「不可用」；
  3. 退避必须生效：指数增长 + 抖动，且尊重 Retry-After；
  4. 429 响应必须永远带得出 Retry-After（上游没给就兜底）；
  5. 最终分类以最后一次尝试为准（429 → 500 不得仍报限流）。

全部用临时 KB_ROOT（conftest 已设），不触达真实上游。
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

from test_security_regressions import client  # noqa: F401  复用隔离 Flask app fixture

RATE_BODY = json.dumps({
    "error": {"message": "Upstream rate limit exceeded, please retry later",
              "type": "rate_limit_error"},
}).encode()


def _guard_dev_root():
    """发现 .kb-env=prod 就拒绝运行（红绿测试只许在临时/开发库跑）。"""
    root = Path(os.environ.get("KB_ROOT", ""))
    marker = root / ".kb-env"
    assert "kbtest-root-" in str(root), f"KB_ROOT 不是临时测试目录：{root}"
    if marker.exists():
        assert marker.read_text(encoding="utf-8").strip() != "prod", (
            f"拒绝在 .kb-env=prod 的库上跑测试：{root}"
        )


def _http_429(url, retry_after=None, body=RATE_BODY):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, io.BytesIO(body))


def _ok_response(text="好的"):
    payload = json.dumps({
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    }).encode()
    resp = io.BytesIO(payload)
    resp.status = 200
    return resp


@pytest.fixture()
def llm_env(monkeypatch):
    """把 llm 模块的上游调用与睡眠换成可观测的假实现。"""
    _guard_dev_root()
    import kb_lib.llm as L

    state = {"calls": 0, "sleeps": [], "responses": []}

    def fake_urlopen(req, timeout=None):
        state["calls"] += 1
        item = state["responses"].pop(0) if state["responses"] else None
        if isinstance(item, Exception):
            raise item
        return item if item is not None else _ok_response()

    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(L.time, "sleep", lambda s: state["sleeps"].append(s))
    monkeypatch.setattr(L, "load_settings", lambda: {
        "llm_base": "http://127.0.0.1:8318/v1",
        "llm_model": "deepseek-v4-flash",
        "llm_key": "test-key",
    })
    return L, state


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_429_会重试且最终错误带限流原因(llm_env):
    """① 会重试 ② 错误文本带 429 / rate limit ③ 退避生效。"""
    L, state = llm_env
    state["responses"] = [_http_429("http://127.0.0.1:8318/v1/chat/completions") for _ in range(3)]

    with pytest.raises(L.LLMUnavailable) as ei:
        L.llm_chat_with_retry(MSGS, retries=3)

    err = ei.value
    assert state["calls"] == 3, f"429 应重试到 3 次，实际 {state['calls']} 次"
    text = str(err)
    assert "429" in text, f"错误文本没带状态码：{text!r}"
    assert "rate limit" in text.lower(), f"错误文本没带限流原因：{text!r}"
    assert getattr(err, "rate_limited", False) is True, "未标记为限流错误，路由无法区分 429/503"
    assert getattr(err, "status", None) == 429

    sleeps = state["sleeps"]
    assert len(sleeps) == 2, f"3 次尝试应有 2 次退避，实际 {sleeps}"
    assert all(s > 0 for s in sleeps), f"退避时长必须为正：{sleeps}"
    assert sleeps[1] > sleeps[0], f"退避应指数增长：{sleeps}"
    assert sleeps[0] <= 3.0 and sleeps[1] <= 6.0, f"退避含抖动但不应超过基数 1.5 倍：{sleeps}"


def test_429_尊重_retry_after_头(llm_env):
    """上游给了 Retry-After 就按它等，不再用自算的短退避。"""
    L, state = llm_env
    state["responses"] = [
        _http_429("http://127.0.0.1:8318/v1/chat/completions", retry_after=7),
        _ok_response("恢复后的答案"),
    ]

    out = L.llm_chat_with_retry(MSGS, retries=3)

    assert out == "恢复后的答案"
    assert state["calls"] == 2, f"429 后应重试一次即成功，实际调用 {state['calls']} 次"
    assert state["sleeps"] and state["sleeps"][0] >= 7, f"未尊重 Retry-After：{state['sleeps']}"


def test_429_后成功则正常返回(llm_env):
    """429 是可恢复错误：重试成功不得再抛异常。"""
    L, state = llm_env
    state["responses"] = [_http_429("http://127.0.0.1:8318/v1/chat/completions"), _ok_response("答案")]

    assert L.llm_chat_with_retry(MSGS, retries=3) == "答案"
    assert len(state["sleeps"]) == 1


def test_非429_不标记限流(llm_env):
    """400 等非限流错误仍按原路径失败，不能被误标成 429。"""
    L, state = llm_env
    state["responses"] = [
        urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b'{"error":"bad"}'))
        for _ in range(3)
    ]

    with pytest.raises(L.LLMUnavailable) as ei:
        L.llm_chat_with_retry(MSGS, retries=3)
    assert getattr(ei.value, "rate_limited", False) is False
    assert "400" in str(ei.value)


def test_ask_路由把限流转成_429_而不是_503(client, llm_env, monkeypatch):
    """③ 端到端：/api/llm/ask 遇上游 429 必须回 429 + rate limit 文案。"""
    L, state = llm_env
    import kb_lib.routes.routes_llm as RL

    monkeypatch.setattr(RL, "build_qa_context", lambda q, with_citations=True: ("", [], []), raising=False)
    state["responses"] = [_http_429("http://127.0.0.1:8318/v1/chat/completions") for _ in range(3)]

    c, h, _, _ = client
    r = c.post("/api/llm/ask", json={"q": "测试限流"}, headers=h)
    body = r.get_data(as_text=True)
    assert r.status_code == 429, f"上游 429 被包成了 {r.status_code}：{body[:300]}"
    low = body.lower()
    assert "429" in body or "rate limit" in low, f"响应体没透出限流原因：{body[:300]}"


def test_上游429无retry_after头_响应仍带retry_after(client, llm_env, monkeypatch):
    """上游 429 不带 Retry-After（应用层限流器常见）时，响应头必须兜底给出。"""
    L, state = llm_env
    import kb_lib.routes.routes_llm as RL

    monkeypatch.setattr(RL, "build_qa_context", lambda q, with_citations=True: ("", [], []), raising=False)
    state["responses"] = [_http_429("http://127.0.0.1:8318/v1/chat/completions") for _ in range(3)]

    c, h, _, _ = client
    r = c.post("/api/llm/ask", json={"q": "无限流头的 429"}, headers=h)

    assert r.status_code == 429, f"应回 429，实际 {r.status_code}"
    ra = r.headers.get("Retry-After")
    assert ra, f"429 响应缺 Retry-After 头：{dict(r.headers)}"
    assert int(ra) >= 1, f"Retry-After 不是正整数：{ra!r}"


def test_混合序列_429后500_以末次为准(llm_env):
    """429 → 500 → 500：最终必须按 500 分类，不能残留限流标记和旧 Retry-After。"""
    L, state = llm_env
    state["responses"] = [
        _http_429("http://127.0.0.1:8318/v1/chat/completions", retry_after=5),
        urllib.error.HTTPError("http://x", 500, "Internal Server Error", {},
                               io.BytesIO(b'{"error":{"message":"upstream boom"}}')),
        urllib.error.HTTPError("http://x", 500, "Internal Server Error", {},
                               io.BytesIO(b'{"error":{"message":"upstream boom"}}')),
    ]

    with pytest.raises(L.LLMUnavailable) as ei:
        L.llm_chat_with_retry(MSGS, retries=3)

    err = ei.value
    assert err.rate_limited is False, f"末次是 500，不应仍标限流：{err.rate_limited}"
    assert err.retry_after is None, f"末次是 500，不应残留旧 Retry-After：{err.retry_after}"
    assert err.status == 500, f"status 应取末次 500，实际 {err.status}"
    assert "500" in str(err), f"detail 应含 500：{str(err)!r}"


def test_非429状态码以末次为准(llm_env):
    """400 → 500 → 500：status 不得锁存成第一个状态码。"""
    L, state = llm_env
    state["responses"] = [
        urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b'{"error":"x"}'))
        for code in (400, 500, 500)
    ]

    with pytest.raises(L.LLMUnavailable) as ei:
        L.llm_chat_with_retry(MSGS, retries=3)
    assert ei.value.status == 500, f"status 应取末次 500，实际 {ei.value.status}"
    assert "500" in str(ei.value)
