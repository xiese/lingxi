"""第 18 项：/health 冷启动首次访问不再触发全盘扫描。

背景（2026-10-18 待办第 18 项）：
    生产机内存紧张（available 373M、swap 已用 1872M），冷启动后第一次访问
    /health 会卡住。实测 count_stale 本身只要 0.023s（千级篇），不是它慢，
    而是冷启动时进程页要从 swap 换回物理内存，任何重活都会放大这个代价。

修法：/health 只读 15s 缓存（peek_stale），未命中返回 stale=null + 起后台线程
预热（prime_stale_async），请求本身绝不扫描文件系统。

本测试锁三件事：
    1. 冷启动（缓存空）时 health 不调用 count_stale —— 这是「不卡」的根；
    2. 冷启动返回 stale=None 且 stale_cached=False，不能返回 0 冒充「跟得上」；
    3. 缓存命中时返回真实值且 stale_cached=True；过期但有过历史值时返回旧值。
"""

import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


# ── 纯逻辑部分：直接测 SearchIndex 的两个新方法（不依赖 Flask） ──────────────

def _fresh_search_mod(monkeypatch):
    """每个用例拿一份干净的 search 模块，避免缓存互相污染。"""
    import importlib

    import kb_lib.search as sm

    importlib.reload(sm)
    return sm


def test_peek_stale_冷启动不扫盘(monkeypatch, tmp_path):
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)

    called = {"n": 0}

    def _boom(self):
        called["n"] += 1
        raise AssertionError("冷启动时 peek_stale 不允许调用 count_stale")

    monkeypatch.setattr(sm.SearchIndex, "count_stale", _boom)

    count, cached = idx.peek_stale()
    assert called["n"] == 0, "peek_stale 不该触发全盘扫描"
    assert count is None, "冷启动应返回 None（未知），不能返回 0"
    assert cached is False


def test_peek_stale_命中缓存返回真实值(monkeypatch):
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)

    sm._stale_cache["count"] = 7
    sm._stale_cache["ts"] = time.time()

    count, cached = idx.peek_stale()
    assert (count, cached) == (7, True)


def test_peek_stale_过期但有过历史值时返回旧值并标记未命中(monkeypatch):
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)

    sm._stale_cache["count"] = 3
    sm._stale_cache["ts"] = time.time() - (sm._STALE_CACHE_TTL + 5)

    count, cached = idx.peek_stale()
    assert count == 3, "有过历史值时给旧值，比给 None 有用"
    assert cached is False, "但必须标记为未命中，调用方才知道该预热"


def test_prime_stale_async_起后台线程且不阻塞(monkeypatch):
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)

    ran = {"n": 0}

    def _fake_count_stale(self):
        ran["n"] += 1
        sm._stale_cache["count"] = 42
        sm._stale_cache["ts"] = time.time()
        return 42

    monkeypatch.setattr(sm.SearchIndex, "count_stale", _fake_count_stale)

    t0 = time.time()
    started = idx.prime_stale_async()
    elapsed = time.time() - t0

    assert started is True
    assert elapsed < 0.1, f"起线程不该阻塞，实测 {elapsed:.3f}s"

    for _ in range(200):
        if ran["n"]:
            break
        time.sleep(0.01)
    assert ran["n"] == 1, "后台线程应真的执行了 count_stale"
    assert sm._stale_cache["count"] == 42


def test_prime_stale_async_不重复起线程(monkeypatch):
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)

    import threading

    gate = threading.Event()
    ran = {"n": 0}

    def _slow_count_stale(self):
        ran["n"] += 1
        gate.wait(2)
        return 1

    monkeypatch.setattr(sm.SearchIndex, "count_stale", _slow_count_stale)

    assert idx.prime_stale_async() is True
    for _ in range(100):
        if ran["n"]:
            break
        time.sleep(0.01)
    # 第一个线程还在跑，再调不该再起
    assert idx.prime_stale_async() is False
    gate.set()
    # 实现用模块级 _stale_prime_thread 判断「是否已有线程在跑」，不是锁
    for _ in range(300):
        t = getattr(sm, "_stale_prime_thread", None)
        if t is not None and not t.is_alive():
            break
        time.sleep(0.01)
    assert ran["n"] == 1, f"并发调用只该跑一次，实际 {ran['n']} 次"
    # 再调一次：锁已释放，应允许重新起（不是永久卡死）
    assert idx.prime_stale_async() is True
    for _ in range(200):
        if ran["n"] == 2:
            break
        time.sleep(0.01)
    assert ran["n"] == 2, "锁释放后应能再次预热"


# ── 路由部分：真的打 /health，确认不扫盘、字段正确 ──────────────────────────

@pytest.fixture
def health_client(monkeypatch):
    sm = _fresh_search_mod(monkeypatch)
    import kb_lib.routes.routes_general as rg

    # 不 reload：蓝图已在 import 时注册到 bp，reload 会重复注册端点导致 Flask 报错。
    # 只需替换模块级 search_index 引用即可。

    # 伪造索引：有 bm25、若干篇
    fake_index = types.SimpleNamespace()
    fake_index.bm25 = object()
    fake_index.paths = [f"p{i}.md" for i in range(58)]

    scan = {"n": 0, "threads": []}

    def _count_stale(self):
        import threading

        scan["n"] += 1
        scan["threads"].append(threading.current_thread().name)
        return 99

    fake_index.count_stale = types.MethodType(_count_stale, fake_index)
    fake_index.peek_stale = types.MethodType(sm.SearchIndex.peek_stale, fake_index)
    fake_index.prime_stale_async = types.MethodType(
        sm.SearchIndex.prime_stale_async, fake_index
    )
    monkeypatch.setattr(rg, "search_index", fake_index)

    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(rg.bp)
    client = app.test_client()
    return client, scan, sm


def test_health_冷启动不扫盘且返回null(health_client):
    client, scan, sm = health_client
    sm._stale_cache["count"] = 0
    sm._stale_cache["ts"] = 0.0

    t0 = time.time()
    r = client.get("/health")
    elapsed = time.time() - t0

    assert r.status_code == 200
    data = r.get_json()
    assert "stale" in data, "/health 必须显式返回 stale 字段"
    assert "stale_cached" in data, "/health 必须显式返回 stale_cached 字段"
    assert data["stale"] is None, "冷启动 stale 必须是 null，不能是 0"
    assert data["stale_cached"] is False, "冷启动必须明确告诉调用方「未预热」"
    assert data["indexed"] == 58
    assert elapsed < 0.5, f"/health 不该卡，实测 {elapsed:.3f}s"
    # 扫描可以发生，但必须在后台线程里；请求线程自己扫才是「卡」的根因
    import threading

    assert threading.current_thread().name not in scan["threads"], (
        f"扫描发生在请求线程内，等于没修：{scan['threads']}"
    )
    for _ in range(200):
        if scan["n"]:
            break
        time.sleep(0.01)
    assert scan["n"] >= 1, "后台预热应真的跑起来"
    assert all(t == "stale-prime" for t in scan["threads"]), scan["threads"]


def test_health_缓存命中返回真实值(health_client):
    client, scan, sm = health_client
    sm._stale_cache["count"] = 5
    sm._stale_cache["ts"] = time.time()

    data = client.get("/health").get_json()
    assert "stale_cached" in data
    assert data["stale"] == 5
    assert data["stale_cached"] is True


def test_health_无索引时返回未知而不是0(health_client, monkeypatch):
    """索引未加载时没有任何可信来源，必须报「未知」。
    旧写法返回 stale=0 且 stale_cached=True，与 docs/API.md 的约定自相矛盾。"""
    client, scan, sm = health_client
    import kb_lib.routes.routes_general as rg

    monkeypatch.setattr(rg.search_index, "bm25", None)
    data = client.get("/health").get_json()
    assert data["stale"] is None, "无索引时不能拿 0 充数"
    assert data["stale_cached"] is False, "无索引时这个值不是来自缓存"


# ── 调用方兼容：update_stats 不能把 null 写成 0 ─────────────────────────────

def test_stale_from_health_区分未知与0(monkeypatch):
    """直接测生产函数 _stale_from_health，不在测试里复制一份逻辑（复制=自己验自己）。"""
    import update_stats as us

    f = us._stale_from_health
    assert f({"stale": None}) is None, "未知不能写成 0"
    assert f({"stale": 0}) == 0
    assert f({"stale": 5}) == 5
    assert f({"stale": "7"}) == 7, "字符串数值也接受"
    assert f({"stale": "abc"}) is None, "脏值当未知，不猜 0"
    assert f({}) is None, "字段缺失说明响应异常，同样不猜 0"
    assert f(None) is None


def test_update_stats_源码里不再有把null当0的老写法():
    import update_stats as us

    import re

    src = open(us.__file__, encoding="utf-8").read()
    # 先剥注释：注释里提到老写法是文档，不是代码
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*#.*$", "", src)
    src = re.sub(r"\s+#[^\n]*$", "", src, flags=re.M)
    assert 'int(health.get("stale", 0) or 0)' not in src, (
        "update_stats 仍存在把 null 当 0 的老写法"
    )


# ── 真实链路：prime → 真实 count_stale → 写 _stale_cache → 下次 peek 命中 ──────

def test_真实链路_预热后peek能命中(monkeypatch, tmp_path):
    """不替换 count_stale，让它真的跑（扫真实 NOTES_DIR），验证缓存闭环。

    GLM 第 1 轮指出：前面几个用例把 count_stale 换成假实现，若真实实现
    根本不写 _stale_cache，测试照样全绿，而生产上 /health 会永远 null。
    """
    sm = _fresh_search_mod(monkeypatch)
    idx = sm.SearchIndex.__new__(sm.SearchIndex)
    idx.mtimes = {}

    # 造 2 个 md 文件，其中 1 个不在 mtimes 里 → stale 至少 1
    d = tmp_path
    (d / "a.md").write_text("x", encoding="utf-8")
    (d / "b.md").write_text("y", encoding="utf-8")
    monkeypatch.setattr(sm, "NOTES_DIR", d)

    before, cached_before = idx.peek_stale()
    assert before is None and cached_before is False, "冷启动应是未知"

    idx.prime_stale_async()
    for _ in range(300):
        t = getattr(sm, "_stale_prime_thread", None)
        if t is not None and not t.is_alive():
            break
        time.sleep(0.01)

    after, cached_after = idx.peek_stale()
    assert after is not None, "真实 count_stale 必须写缓存，否则 /health 永远 null"
    assert cached_after is True, "预热后应命中缓存"
    assert after >= 1, f"两个未入索引的文件至少 1 个 stale，实际 {after}"


def test_并发未命中只起一个扫描线程(monkeypatch):
    """20 个线程同时命中未命中分支，只能有一个真正跑 count_stale。

    GLM 第 4 条：竞态是否被检出取决于调度窗口，单轮有偶发漏判。
    这里连跑 5 轮，任一轮出现第二个线程即失败，把检出率压到可接受范围。
    """
    import threading

    for round_no in range(5):
        sm = _fresh_search_mod(monkeypatch)
        idx = sm.SearchIndex.__new__(sm.SearchIndex)

        gate = threading.Event()
        started = []
        lock = threading.Lock()

        def _slow(self):
            with lock:
                started.append(threading.current_thread().name)
            gate.wait(2)
            return 1

        monkeypatch.setattr(sm.SearchIndex, "count_stale", _slow)

        results = []

        def _call():
            results.append(idx.prime_stale_async())

        threads = [threading.Thread(target=_call) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)

        gate.set()
        time.sleep(0.1)
        assert len(started) == 1, (
            f"第 {round_no + 1} 轮并发起了 {len(started)} 个扫描线程：{started}"
        )
        assert results.count(True) == 1, (
            f"第 {round_no + 1} 轮只有一次调用应返回 True，实际 {results}"
        )



def test_stale为None时渲染成未知而不是字面None(monkeypatch):
    """GLM 第 2 轮第 3 条：update_stats 下游四处 f-string 会输出字面 None。"""
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    us = importlib.import_module("update_stats")

    assert us._stale_text(None) == "未知"
    assert us._stale_text(0) == "0"
    assert us._stale_text(7) == "7"

    src = open(us.__file__, encoding="utf-8").read()
    # 先剥注释，否则会命中注释里的描述文字（此前踩过这个坑）
    code = __import__("re").sub(r"/\*.*?\*/", "", src, flags=__import__("re").S)
    code = __import__("re").sub(r"^\s*#.*$", "", code, flags=__import__("re").M)
    # 四处渲染都必须走 _stale_text，不能直接插 stats['stale']
    import re as _re
    bad = _re.findall(r"stale=\{stats\['stale'\]\}", code)
    bad += _re.findall(r"stale: \{stats\['stale'\]\},", code)
    bad += _re.findall(r"stale=\{st\['stale'\]\}", code)
    assert not bad, f"仍有直接插值会写出字面 None：{bad}"
    assert code.count("_stale_text(") >= 4, "四处渲染都要走 _stale_text"
