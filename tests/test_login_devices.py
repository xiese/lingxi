"""第 15 项：登录设备与使用设备日志（安全）。

验收口径（17:34 C 节 + 17:47 评审）：
    ① 登录记录：时间、IP、设备/UA、令牌名、成功或失败、失败原因
    ② 设备清单：每台设备首次和最近使用时间、使用次数，可以一键吊销
    ③ 同一 IP 失败 5 次后限速 15 分钟；主令牌不受影响，不锁死
    ④ 登录日志默认保留 90 天
    ⑤ 吊销设备后该设备的令牌立即失效

只读 + 临时库，不碰生产数据。
"""
import importlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

AUDIT = ROOT / "scripts" / "kb_lib" / "audit.py"
AUTH = ROOT / "scripts" / "kb_lib" / "routes" / "routes_auth.py"


def _strip_comments(src: str) -> str:
    """先剥注释再断言，避免命中注释文字（之前踩过这个坑）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    return src


@pytest.fixture
def tmp_audit(tmp_path, monkeypatch):
    """把审计库指到临时文件，避免和真实库/其他测试互相污染。

    之前这两条测试直接用全局库：单独跑绿、全量跑红（被别的用例写进历史记录），
    属于典型的「测试之间串味」。
    """
    import kb_lib.audit as audit
    monkeypatch.setattr(audit, "_audit_db", lambda: tmp_path / "t.db")
    monkeypatch.setattr(audit, "_DB_INITED", False)
    yield audit
    monkeypatch.setattr(audit, "_DB_INITED", False)


# ── ① 表结构：字段齐全 ──────────────────────────────────────────────────

def test_登录表字段齐全():
    src = _strip_comments(AUDIT.read_text(encoding="utf-8"))
    m = re.search(r"CREATE TABLE IF NOT EXISTS login_log \((.*?)\)\s*\"\"\"", src, re.S)
    assert m, "找不到 login_log 建表语句"
    cols = m.group(1)
    for col in ("time", "ip", "device", "token_name", "token_prefix", "ok", "reason"):
        assert col in cols, f"login_log 缺字段 {col}"


def test_登录表与审计表分开建():
    """登录是事件流，不能混进 audit_log（那边按 max_log_days 清，口径不同）。"""
    src = _strip_comments(AUDIT.read_text(encoding="utf-8"))
    assert "CREATE TABLE IF NOT EXISTS audit_log" in src
    assert "CREATE TABLE IF NOT EXISTS login_log" in src
    assert src.count("CREATE TABLE") >= 2


# ── ② 写入与查询的真实行为（临时库）────────────────────────────────────

@pytest.fixture()
def audit_db(tmp_path, monkeypatch):
    import kb_lib.config as C
    monkeypatch.setattr(C, "KB_ROOT", tmp_path)
    import kb_lib.audit as A
    monkeypatch.setattr(A, "_DB_INITED", False)
    return A


def test_写一条成功登录能查出来(audit_db):
    A = audit_db
    A.login_log(True, ip="203.0.113.5", device="iPhone Edge", token_name="手机",
                token_prefix="kb-abcdef12...", reason="领取令牌成功")
    r = A.query_logins(page=1, limit=10)
    assert r["total"] == 1
    it = r["items"][0]
    assert it["ok"] == 1
    assert it["device"] == "iPhone Edge"
    assert it["ip"] == "203.0.113.5"
    assert it["token_name"] == "手机"
    assert it["reason"] == "领取令牌成功"
    assert it["time"], "必须有时间"


def test_失败登录要记录原因(audit_db):
    A = audit_db
    A.login_log(False, ip="203.0.113.6", device="未知设备", reason="同 IP 失败过多，已锁定")
    r = A.query_logins(ok=False)
    assert r["total"] == 1
    assert r["items"][0]["ok"] == 0
    assert "锁定" in r["items"][0]["reason"]


def test_按成败过滤是真的过滤(audit_db):
    A = audit_db
    A.login_log(True, ip="203.0.113.7", device="D1", reason="ok")
    A.login_log(False, ip="203.0.113.7", device="D1", reason="bad")
    assert A.query_logins(ok=True)["total"] == 1
    assert A.query_logins(ok=False)["total"] == 1
    assert A.query_logins()["total"] == 2


def test_设备清单聚合首次最近与次数(audit_db, monkeypatch):
    """设备清单以 audit_log（真实请求流水）为主，login_log 只补失败次数。

    2026-10-19 修正：原实现只查 login_log，导致用存好令牌的老设备永远查不到（列表 0 台）。
    这里模拟「只有 audit_log 有记录」的真实场景。

    C3 口径：按 Token 分组，所以两台设备必须各用一把令牌，否则会被并成一条
    （同一把令牌从两台机器访问＝一把令牌一个条目）。
    """
    A = audit_db

    def _fake_req(device, ip, ts, tk):
        conn = A._get_db()
        conn.execute(
            "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, tk, device, ip, "notes", "/notes", "GET", 200, ""),
        )
        conn.close()

    _fake_req("iPad", "203.0.113.8", "2026-10-19T01:00:00", "tk1234567890...")
    _fake_req("iPad", "203.0.113.8", "2026-10-19T02:00:00", "tk1234567890...")
    _fake_req("iPad", "203.0.113.8", "2026-10-19T03:00:00", "tk1234567890...")
    _fake_req("Pixel", "203.0.113.9", "2026-10-19T04:00:00", "tk0987654321...")
    # 失败次数只来自 login_log
    A.login_log(False, ip="203.0.113.8", device="iPad", reason="bad")

    items = {d["device"]: d for d in A.query_devices()}
    assert set(items) == {"iPad", "Pixel"}, f"实际 {set(items)}"
    ipad = items["iPad"]
    assert ipad["uses"] == 3, f"使用次数应等于 audit_log 请求数，实际 {ipad['uses']}"
    assert ipad["fails"] == 1, f"失败次数应从 login_log 补，实际 {ipad['fails']}"
    assert ipad["first_seen"] <= ipad["last_seen"]
    assert ipad["last_ip"] == "203.0.113.8"


# ── ③ 失败 5 次锁 15 分钟 ───────────────────────────────────────────────

def test_失败不足5次不锁(audit_db):
    A = audit_db
    for _ in range(4):
        A.login_log(False, ip="203.0.113.10", device="D", reason="bad")
    assert A.login_locked("203.0.113.10") is False


def test_失败5次就锁(audit_db):
    A = audit_db
    for _ in range(5):
        A.login_log(False, ip="203.0.113.11", device="D", reason="bad")
    assert A.login_locked("203.0.113.11") is True


def test_锁只影响本IP不影响别人(audit_db):
    A = audit_db
    for _ in range(5):
        A.login_log(False, ip="203.0.113.12", device="D", reason="bad")
    assert A.login_locked("203.0.113.12") is True
    assert A.login_locked("203.0.113.13") is False


def test_成功的登录不参与锁定计数(audit_db):
    """「连续失败」只看失败；成功记录不能把自己锁死。"""
    A = audit_db
    for _ in range(5):
        A.login_log(True, ip="203.0.113.14", device="D", reason="ok")
    assert A.login_locked("203.0.113.14") is False


def test_锁定窗口只有15分钟(audit_db):
    src = _strip_comments(AUDIT.read_text(encoding="utf-8"))
    m = re.search(r"LOGIN_LOCK_SECONDS\s*=\s*([^\n]+)", src)
    assert m, "找不到 LOGIN_LOCK_SECONDS"
    assert "15 * 60" in m.group(1) or "900" in m.group(1), f"锁定时长应为 15 分钟，实际 {m.group(1)}"
    m2 = re.search(r"LOGIN_FAIL_MAX\s*=\s*(\d+)", src)
    assert m2 and m2.group(1) == "5", "失败次数门槛应为 5"


# ── ④ 保留 90 天 ────────────────────────────────────────────────────────

def test_登录日志默认保留90天(audit_db):
    A = audit_db
    assert A.LOGIN_LOG_DAYS == 90


def test_过期登录记录能被清掉_新记录保留(audit_db):
    A = audit_db
    A.login_log(True, ip="203.0.113.15", device="旧设备", reason="old")
    # 手工把这条改成 100 天前
    conn = A._get_db()
    old = (datetime.now() - timedelta(days=100)).isoformat()
    conn.execute("UPDATE login_log SET time=?", (old,))
    conn.close()
    A.login_log(True, ip="203.0.113.16", device="新设备", reason="new")

    cutoff = (datetime.now() - timedelta(days=A.LOGIN_LOG_DAYS)).isoformat()
    deleted = A.cleanup_login_log(cutoff)
    assert deleted == 1
    left = A.query_logins()["items"]
    assert [x["device"] for x in left] == ["新设备"]


# ── ⑤ 路由接线：入口真的记了 ────────────────────────────────────────────

def test_登录申请入口写了登录记录():
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def auth_request()")
    j = src.index("def list_requests()", i)
    seg = src[i:j]
    assert "login_log(" in seg, "登录申请入口没有写登录记录"


def test_入口先查锁定再发申请():
    """顺序不能反：先锁定判断，再走原限流，最后才发申请。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def auth_request()")
    j = src.index("def list_requests()", i)
    seg = src[i:j]
    p_lock = seg.index("login_locked(")
    p_rate = seg.index("_check_auth_rate_limit(")
    p_issue = seg.index('s["requests"][rid]')
    assert p_lock < p_rate < p_issue, "锁定判断必须在限流和发申请之前"


def test_领取令牌才记成功登录_且只记第一次():
    """轮询会反复拿到同一令牌，只有首次交付算一次登录成功。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def request_status(")
    j = src.index("def approve(", i)
    seg = src[i:j]
    assert "first_delivery" in seg, "没有区分首次交付"
    assert "login_log(" in seg and "True" in seg, "领取令牌时没有记成功登录"
    # first_delivery 必须来自 delivered_at 是否为空
    assert re.search(r"first_delivery\s*=\s*not\s+r\.get\(\s*[\"']delivered_at[\"']\s*\)", seg), \
        "first_delivery 判定口径不对（应是 delivered_at 为空）"
    # 必须初始化，避免未绑定
    assert re.search(r"first_delivery\s*=\s*False", seg), "first_delivery 没初始化"


def test_两个查询接口存在且要求主令牌():
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    for route, fn in (('"/auth/logins"', "def auth_logins("), ('"/auth/devices"', "def auth_devices(")):
        assert route in src, f"缺接口 {route}"
        i = src.index(route)
        j = src.index(fn, i)
        # 装饰器写在 @bp.route 之后、def 之前，检查这一段
        between = src[i:j]
        assert "@require_master" in between, f"{route} 没有 @require_master，任何人都能看登录记录"


def test_设备清单带出可吊销的令牌():
    """设备清单要能直接给「吊销」按钮，否则验收第 ② 条做不到。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def auth_devices(")
    j = src.index('@bp.route("/auth/revoke/', i)
    seg = src[i:j]
    assert "is_expired(" in seg, "没有排除已过期令牌"
    assert '"tokens"' in seg, "设备清单没有带出该设备的令牌"
    assert "prefix" in seg, "吊销需要令牌前缀，清单里没有"


def test_吊销仍走原来的revoke接口():
    """不新增吊销入口，沿用既有 DELETE /auth/revoke/<hint>（它已有误删保护）。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    assert '@bp.route("/auth/revoke/<hint>", methods=["DELETE"])' in src
    assert "MIN_REVOKE_HINT" in src

# ─── 第 1 轮审查指出的两条「静默失效」───

def test_新设备判定真的能生效(tmp_audit):
    """第一轮审查：is_new_device 用了不存在的 _connect，异常被吞后永远 False，
    新设备推送永远不触发。这里真跑一次，确认返回 True/False 而不是恒 False。"""
    audit = tmp_audit
    # 生产代码的顺序是「先查 is_new_device，再写成功记录」，测试也按这个顺序来。
    assert audit.is_new_device("测试设备A") is True, "空库时应判为新设备（恒 False = 用了不存在的函数）"
    audit.login_log(True, ip="203.0.113.10", device="测试设备A")
    assert audit.is_new_device("测试设备A") is False, "同一设备第二次登录不该再算新设备"


def test_新设备判定是先后顺序_不是先写后查():
    """GLM 主审：先插入再查 count 会把本条数进去，并发下两台新设备会双双漏推。
    这里校验生产代码里 is_new_device 的调用在 login_log 之前。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index('if status == "approved" and first_delivery:')
    j = src.index("return jsonify(res)", i)
    seg = src[i:j]
    pos_new = seg.find("login_success_once(")
    pos_log = seg.find("login_log(")
    assert pos_new >= 0, "没找到 login_success_once 调用"
    assert pos_log >= 0, "没找到 login_log 调用"
    assert pos_new < pos_log, "先写了成功记录再判新设备，并发下会漏推"


def test_最近IP按时间取而不是字典序(tmp_audit):
    """MAX(ip) 是字典序最大，不是最近一次。用「先大后小」的顺序能测出来。

    2026-10-19：设备清单数据源改为 audit_log，这里同样写 audit_log 验证。
    """
    audit = tmp_audit

    def _fake_req(ip, device, ts):
        conn = audit._get_db()
        conn.execute(
            "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, "tk1234567890...", device, ip, "notes", "/notes", "GET", 200, ""),
        )
        conn.close()

    _fake_req("203.0.113.10", "设备B", "2026-10-19T01:00:00")
    _fake_req("127.0.0.1", "设备B", "2026-10-19T02:00:00")  # 字典序更小，但更晚
    rows = [r for r in audit.query_devices() if r["device"] == "设备B"]
    assert rows, "查不到设备B"
    assert rows[0]["last_ip"] == "127.0.0.1", f"last_ip 取成了 {rows[0]['last_ip']}，说明用的是 MAX(ip)"


def test_前端事件清单与后端一致():
    """前后端事件键必须一一对应，漏一个某项就永远勾不上。"""
    import re
    from kb_lib.push_qq import PUSH_EVENTS
    app = (ROOT / "scripts/web/app.js").read_text(encoding="utf-8")
    i = app.index("{k:'qq_push_events'")
    j = app.index("}}", i)
    seg = app[i:j]
    # 去掉字段自身的属性（hint 等），只看 opts 里的键
    if "opts:{" in seg:
        seg = seg[seg.index("opts:{") + 6:]
    # 只认「键名 + 冒号 + 单引号」且键长 ≥ 4 的，避免把 esc(o)、t:"x" 这类算进来；
    # 值里可能带「（待接入）」这类中文后缀，不影响键名提取。
    ui = set(re.findall(r"\b([a-z][a-z_]{3,}):'", seg))
    back = set(PUSH_EVENTS.keys())
    assert ui == back, f"前端缺 {sorted(back - ui)}，后端缺 {sorted(ui - back)}"

def test_90天清理真的有调用点(tmp_audit):
    """第 15 项要求「日志默认保留 90 天」。

    只有 cleanup_login_log() 却没人调，就是死代码，等于没做保留策略
    （GLM 主审第 1 轮点出的问题）。这里验两件事：
      ① maybe_cleanup_login_log 真的会删掉超期记录
      ② 每天只清一次
    """
    audit = tmp_audit
    old = (datetime.now() - timedelta(days=200)).isoformat()
    fresh = datetime.now().isoformat()
    audit.login_log(True, ip="203.0.113.10", device="老设备", reason="")
    conn = audit._get_db()
    conn.execute("UPDATE login_log SET time=?", (old,))
    conn.close()
    audit.login_log(True, ip="203.0.113.10", device="新设备", reason="")

    n = audit.maybe_cleanup_login_log()
    assert n >= 1, "超 90 天的记录没有被清掉"

    res = audit.query_logins(limit=50)
    rows = res["items"] if isinstance(res, dict) else res
    devices = {r["device"] for r in rows}
    assert "老设备" not in devices, "超期记录还在"
    assert "新设备" in devices, "未超期记录被误删"

    # 同一天再调一次：必须「不进入清理函数」，而不是「进去了恰好删了 0 条」。
    # 只断言返回值 0 会漏检——把去重去掉它也返回 0（因为没有新超期数据），
    # 所以这里直接数调用次数。
    calls = []
    real = audit.cleanup_login_log
    audit.cleanup_login_log = lambda before: calls.append(before) or 0
    try:
        audit.maybe_cleanup_login_log()
    finally:
        audit.cleanup_login_log = real
    assert calls == [], f"同一天又跑了一次清理（调用了 {len(calls)} 次），去重没生效"


def test_登录入口会触发清理():
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def auth_request(")
    j = src.index('@bp.route("/auth/requests")', i)
    body = src[i:j]
    assert "maybe_cleanup_login_log()" in body, "登录入口没触发 90 天清理，保留策略不会真正执行"

def test_锁定期内不再记失败_防止无限锁定():
    """锁定期内如果继续写失败记录，攻击者持续请求就能把窗口一直往后推，
    导致该 IP 被无限期锁定（正常用户也被锁死）。"""
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("if login_locked(ip):")
    j = src.index("if not _check_auth_rate_limit(ip):", i)
    seg = src[i:j]
    assert "login_log(" not in seg, "锁定期内还在写失败记录，会无限延长锁定"
    assert "429" in seg, "锁定期没有返回 429"

def test_连续失败口径_成功即清零(tmp_audit):
    """GLM 主审：如果只数窗口内累计失败，一整天零散输错会累加成锁定；
    而且 NAT/校园网共用出口会连坐。口径必须是「连续」——成功一次就清零。"""
    audit = tmp_audit
    for _ in range(4):
        audit.login_log(False, ip="203.0.113.20", device="X", reason="密码错")
    assert audit.login_failures_since_success("203.0.113.20") == 4
    audit.login_log(True, ip="203.0.113.20", device="X", reason="")
    assert audit.login_failures_since_success("203.0.113.20") == 0, "成功一次后失败计数没有清零"
    for _ in range(2):
        audit.login_log(False, ip="203.0.113.20", device="X", reason="密码错")
    assert audit.login_failures_since_success("203.0.113.20") == 2


def test_锁定是真的按连续失败判定(tmp_audit):
    """不能只检查「函数里出现了某个名字」——那是假安全。
    这里真跑：4 次失败 + 1 次成功，之后不该被锁；再失败 5 次才该锁。"""
    audit = tmp_audit
    for _ in range(4):
        audit.login_log(False, ip="203.0.113.30", device="Y", reason="密码错")
    assert audit.login_locked("203.0.113.30") is False, "才 4 次就锁了"
    audit.login_log(True, ip="203.0.113.30", device="Y", reason="")
    for _ in range(4):
        audit.login_log(False, ip="203.0.113.30", device="Y", reason="密码错")
    assert audit.login_locked("203.0.113.30") is False, "成功清零后累计 8 次被锁，说明口径还是「累计」"
    for _ in range(1):
        audit.login_log(False, ip="203.0.113.30", device="Y", reason="密码错")
    assert audit.login_locked("203.0.113.30") is True, "连续 5 次失败没有锁"

def test_新设备并发只判一次(tmp_audit):
    """GLM 主审：裸 COUNT==0 无锁时，同一新设备并发首登会双双判新、重复推送。
    这里开 8 个线程同时判，只有 1 个能拿到 True。"""
    import threading
    audit = tmp_audit
    results = []
    lock = threading.Lock()

    def worker():
        # 生产代码用的是「判定 + 写入」原子版
        r = audit.login_success_once(ip="203.0.113.40", device="并发设备", reason="")
        with lock:
            results.append(r)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(True) == 1, f"并发下判新设备 {results.count(True)} 次，应只判 1 次"

def test_判定与写入共用同一把锁_不是靠巧合串行():
    """GLM 主审第 3 点：只给判定加锁没用，必须「判定+写入」在同一把锁内。

    注意：这条不能只靠并发跑来验——login_log 自身也有 _LOG_LOCK，
    去掉外层锁时结果可能「碰巧」还是对的，跑出来是全绿（假绿）。
    所以这里直接校验代码结构：login_success_once 的整个函数体都在 with _NEW_DEV_LOCK 里，
    并且 login_log 的调用位置在这个 with 块内部。
    """
    src = (ROOT / "scripts/kb_lib/audit.py").read_text(encoding="utf-8")
    i = src.index("def login_success_once(")
    # 取到下一个顶层 def 为止；它是文件最后一个函数时取到结尾
    j = src.find("\ndef ", i + 10)
    body = src[i:] if j < 0 else src[i:j]
    # 只看真正的代码行（缩进 >=4 且不是注释/文档字符串），
    # 否则变异成 `if True:` 时文档字符串里的 "with _NEW_DEV_LOCK" 会让检查假通过
    code_lines = [l.strip() for l in body.splitlines()
                  if l.startswith("    with ") or l.startswith("        ")]
    has_with = any(l.startswith("with _NEW_DEV_LOCK:") for l in code_lines)
    assert has_with, "login_success_once 没有真正获取锁"
    # 必须是 with 真正获取锁，而不是 if True: 这类「看起来像」的写法
    assert "_NEW_DEV_LOCK = threading.Lock()" in src, "_NEW_DEV_LOCK 不是真的线程锁"
    pos_with = body.index("with _NEW_DEV_LOCK:")
    # 按行找真正的调用行（注释里也出现过 login_log 字样，不能用裸 index）
    call = None
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("login_log(True") or s.startswith("login_log("):
            call = ln
            break
    assert call, "login_success_once 里没有调用 login_log"
    indent = len(call) - len(call.lstrip())
    assert indent >= 8, f"login_log 不在 with 块内（缩进 {indent}）"


def test_调用方用的是原子版而不是分两步():
    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index('if status == "approved" and first_delivery:')
    j = src.index("return jsonify(res)", i)
    seg = src[i:j]
    assert "login_success_once(" in seg, "调用方没用原子版 login_success_once"

def test_空设备名不当新设备推(tmp_audit):
    """设备名为空时无法判断是不是新设备，不能返回 True 去推「新设备登录」，
    否则每次轮询都会推一条空白通知。"""
    audit = tmp_audit
    assert audit.is_new_device("") is False, "空设备名被判成新设备"
    assert audit.login_success_once(ip="203.0.113.50", device="", reason="") is False, \
        "空设备名在原子版里被判成新设备"


# ── ⑥ 2026-10-19 返工：设备清单合并与 master 令牌 ─────────────────────────

def test_同一台设备换令牌后按Token分成两条(audit_db):
    """C3 口径：按 Token 分组 —— 同一台手机换令牌（重新登录/轮换/过期重领）
    后，audit_log 里是两个令牌分组，设备列表里就是**两条**。

    理由：一条 = 一把令牌。同一台手机的两把令牌是两次独立授权，各有独立的
    有效期与「吊销」按钮；合并成一条后用户点吊销不知道该吊销哪一把，
    另一把仍然能继续访问。所以这里断言 2 条，不是 1 条。

    反过来「同一把令牌换 UA」才该合成一条（见下一条用例）。
    """
    A = audit_db
    ua = "Mozilla/5.0 (Linux; Android 12; 22041216C) AppleWebKit/537.36"
    _raw_audit(A, ua, "203.0.113.21", "AAAA11112222...", ts="2026-10-19T01:00:00")
    _raw_audit(A, ua, "203.0.113.21", "BBBB33334444...", ts="2026-10-19T02:00:00")

    items = A.query_devices()
    assert len(items) == 2, f"换令牌（令牌不同）应按令牌分成 2 条，实际 {len(items)} 条"
    by_tk = {it["token_prefix"]: it for it in items}
    assert set(by_tk) == {"AAAA11112222", "BBBB33334444"}, by_tk
    for it in items:
        assert it["uses"] == 1, f"每把令牌各 1 次，实际 {it['uses']}"
    assert by_tk["AAAA11112222"]["last_seen"] == "2026-10-19T01:00:00"
    assert by_tk["BBBB33334444"]["last_seen"] == "2026-10-19T02:00:00"


def test_同一设备UA变化会合并成一条(audit_db):
    """C3 口径：按 Token 分组 —— 同一把令牌换 UA（WebView 升级）、换 IP，
    仍然是**一条**：UA 只是展示字段，不参与分组。"""
    A = audit_db

    def _req(device, ip, ts, tk):
        conn = A._get_db()
        conn.execute(
            "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, tk, device, ip, "notes", "/notes", "GET", 200, ""),
        )
        conn.close()

    _req("Mozilla/5.0 (Linux; Android 12; 22041216C) AppleWebKit/537.36", "203.0.113.30",
         "2026-10-19T01:00:00", "SAME11112222...")
    _req("Mozilla/5.0 (Linux; Android 13; 22041216C) AppleWebKit/537.36", "198.51.100.31",
         "2026-10-19T02:00:00", "SAME11112222...")

    items = A.query_devices()
    assert len(items) == 1, f"同一令牌换 UA 应合成 1 条，实际 {len(items)} 条"
    assert items[0]["uses"] == 2, f"次数应相加，实际 {items[0]['uses']}"
    # 最近一次请求的 IP 与 UA 都要跟着走（D2：不是签发时 IP）
    assert items[0]["last_ip"] == "198.51.100.31", items[0]["last_ip"]
    assert "Android 13" in items[0]["device"], items[0]["device"]


def test_master令牌标了is_master(audit_db):
    """master 存在 store["master"] 不在 tokens 表，必须单独并入并打 is_master 标记，
    否则前端会给出「吊销」按钮，删掉 master 维护者自己就登不进来了。"""
    A = audit_db
    conn = A._get_db()
    conn.execute(
        "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-10-19T01:00:00", "Q-8elwgvN7EB...", "手机", "203.0.113.40",
         "notes", "/notes", "GET", 200, ""),
    )
    conn.close()

    src = _strip_comments(AUTH.read_text(encoding="utf-8"))
    i = src.index("def auth_devices(")
    j = src.index('@bp.route("/auth/revoke/', i)
    seg = src[i:j]
    assert 's.get("master")' in seg, "没有并入 master 令牌"
    assert "is_master" in seg, "master 令牌没打 is_master 标记"
    assert "不过期" in seg, "master 令牌没有给出有效期说明"

    js = (ROOT / "scripts" / "web" / "app.js").read_text(encoding="utf-8")
    assert "t.is_master" in js, "前端没区分 master，会给出吊销按钮"


def _raw_audit(A, device, ip, token_prefix="", ts="2026-10-19T03:00:00"):
    """直接往 audit_log 写一条，绕过 audit_log() 对 Flask g/request 的依赖。

    注意必须用 fixture 传进来的 A（已把 KB_ROOT 指到 tmp_path 并重置 _DB_INITED），
    否则会写进上一个测试遗留的库，读的却是新库 —— 测试会假绿。
    """
    if not A._DB_INITED:
        A._init_db()
    conn = A._get_db()
    conn.execute(
        "INSERT INTO audit_log(time,token_prefix,device,ip,action,path,method,status,extra)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (ts, token_prefix, device, ip, "auth_request", "/notes", "GET", 200, ""),
    )
    conn.commit()
    conn.close()


def test_Mac和Linux桌面浏览器不算自动化(audit_db):
    """真人用 Mac / Linux 桌面浏览器访问，label 就是「Mac」「Linux」。

    按系统名归类会把这些真人设备排到一堆脚本后面去，等于看不见。
    只有明确的自动化 UA（curl/python/node/扫描器）才该排后面。
    """
    A = audit_db
    # 时间戳必须不同：三条 last_seen 一样时排序结果本来就稳定，
    # 测不出「Mac 被当成自动化排后面」——变异体不会被抓住，测试就是假绿。
    _raw_audit(A, "curl/8.5.0", "198.51.100.3", ts="2026-10-19T03:00:00")
    _raw_audit(A, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "198.51.100.1",
               ts="2026-10-19T02:00:00")
    _raw_audit(A, "Mozilla/5.0 (X11; Linux x86_64)", "198.51.100.2",
               ts="2026-10-19T01:00:00")
    items = A.query_devices()
    labels = [it["label"] for it in items]
    # Mac/Linux 必须在 curl 前面
    assert labels.index("Mac") < labels.index("curl"), f"Mac 被当成自动化排后面了：{labels}"
    assert labels.index("Linux") < labels.index("curl"), f"Linux 被当成自动化排后面了：{labels}"


def test_失败次数合并时不翻倍(audit_db):
    """login_log 按 device 整名统计，不是按 (device, token_prefix)。

    同一个 device 名出现在多个 audit 分组时，每份都带着同一个总数，
    合并相加会翻倍。必须按设备名去重后再求和。

    C3 口径：按 Token 分组后，同一 UA 的两把令牌是**两条**（各自独立授权），
    所以「不翻倍」这条改成按条目各自校验：每条都带同一个 device 名的
    完整失败数（3），不能相加成 6。
    """
    A = audit_db
    ua = "Mozilla/5.0 (Linux; Android 12; 22041216C) AppleWebKit/537.36"
    # 同一个 UA、两个令牌分组
    _raw_audit(A, ua, "203.0.113.31", "AAAA1111...", ts="2026-10-19T01:00:00")
    _raw_audit(A, ua, "203.0.113.31", "BBBB2222...", ts="2026-10-19T02:00:00")
    # login_log 里这条 device 名有 3 次失败
    A.login_log(False, ip="203.0.113.31", device=ua, reason="x")
    A.login_log(False, ip="203.0.113.31", device=ua, reason="x")
    A.login_log(False, ip="203.0.113.31", device=ua, reason="x")

    items = A.query_devices()
    assert len(items) == 2, f"两把令牌应按 Token 分成 2 条，实际 {len(items)}"
    for it in items:
        assert it["fails"] == 3, f"失败次数翻倍了：{it['fails']}（应为 3）"


def test_安卓UA机型解析(audit_db):
    """UA 安卓段格式多样，取错机型会把不同设备并成一台。

    实测踩过的三种：
      - 带语言区段 `Android 12; zh-cn; 22041216C Build/..` 不能取 zh-cn
      - 机型带空格 `Android 12; Pixel 6 Build/..` 不能只取首词（Pixel 6 vs Pixel 7）
      - 无 Build 段 `Android 12; SM-G991B)` 不能带出 `) AppleWebKit`
    """
    from kb_lib.audit import device_family

    cases = {
        "Mozilla/5.0 (Linux; Android 12; zh-cn; 22041216C Build/SP1A) AppleWebKit/537.36":
            "安卓 22041216C",
        "Mozilla/5.0 (Linux; Android 12; 22041216C Build/SP1A) AppleWebKit/537.36":
            "安卓 22041216C",
        "Mozilla/5.0 (Linux; Android 12; Pixel 6 Build/SQ3A) AppleWebKit/537.36":
            "安卓 Pixel 6",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A) AppleWebKit/537.36":
            "安卓 Pixel 7",
        "Mozilla/5.0 (Linux; Android 12; Redmi K50 Build/SKQ1) AppleWebKit/537.36":
            "安卓 Redmi K50",
        "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36":
            "安卓 SM-G991B",
    }
    for ua, want in cases.items():
        got = device_family(ua)
        assert got == want, f"机型解析错：{ua[:60]} -> {got}（应为 {want}）"
    # 语言区段不能造成「同一台设备拆成两条」
    a = device_family("Mozilla/5.0 (Linux; Android 12; zh-cn; 22041216C Build/SP1A) AppleWebKit")
    b = device_family("Mozilla/5.0 (Linux; Android 12; 22041216C Build/SP1A) AppleWebKit")
    assert a == b, f"带语言区段与不带被拆成两台：{a} vs {b}"
    # Pixel 6 / Pixel 7 不能并成一台
    p6 = device_family("Mozilla/5.0 (Linux; Android 12; Pixel 6 Build/SQ3A) AppleWebKit")
    p7 = device_family("Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A) AppleWebKit")
    assert p6 != p7, f"Pixel 6 和 Pixel 7 被并成一台：{p6}"


def test_自动化UA都排在真人后面(audit_db):
    """漏一个自动化 UA，它就会排进真人区把真设备挤下去。"""
    A = audit_db
    autos = [
        "curl/8.5.0",
        "Wget/1.21",
        "okhttp/4.9.0",
        "axios/1.6.0",
        "Apache-HttpClient/4.5.13",
        "PostmanRuntime/7.32.0",
        "Java/17.0.1",
        "Mozilla/5.0 (compatible; Googlebot/2.1)",
        "Mozilla/5.0 (compatible; bingbot/2.0)",
    ]
    _raw_audit(A, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "203.0.113.50",
               ts="2026-10-19T01:00:00")
    for i, ua in enumerate(autos):
        _raw_audit(A, ua, "203.0.113.%d" % (60 + i),
                   ts="2026-10-19T02:%02d:00" % i)
    items = A.query_devices()
    labels = [it["label"] for it in items]
    assert labels[0] == "iPhone", f"真人 iPhone 没排第一，实际：{labels[:3]}"
    idx_iphone = labels.index("iPhone")
    for ua in autos:
        # 每条自动化都必须排在 iPhone 之后
        pos = [i for i, l in enumerate(labels) if i > idx_iphone]
        assert pos, "自动化项没排到后面"


def test_HeadlessChrome不会被当成Linux真人(audit_db):
    """无头浏览器 UA 里带 `Linux`，device_label 会归成「Linux」。

    只看 label 就会把它当真人桌面浏览器排到前面。判据必须查原始 UA。
    """
    A = audit_db
    _raw_audit(A, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "203.0.113.70",
               ts="2026-10-19T01:00:00")
    _raw_audit(A, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) HeadlessChrome/120.0.0.0 Safari/537.36",
               "203.0.113.71", ts="2026-10-19T05:00:00")
    items = A.query_devices()
    labels = [it["label"] for it in items]
    assert labels[0] == "iPhone", f"HeadlessChrome 排到真人前面了：{labels}"
    assert labels.index("iPhone") < labels.index("Linux"), labels


def test_nanobot不会被bot关键词误伤(audit_db):
    """自定义设备名 nanobot-159 里有 "bot"，不能因此被当成自动化排后面。"""
    A = audit_db
    _raw_audit(A, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", "203.0.113.80",
               ts="2026-10-19T01:00:00")
    _raw_audit(A, "nanobot-159", "203.0.113.81", ts="2026-10-19T05:00:00")
    items = A.query_devices()
    labels = [it["label"] for it in items]
    # nanobot-159 是自定义设备名（真人），必须留在真人区
    assert "nanobot-159" in labels, labels
    # 它排在第一位说明没被当成自动化沉底（时间更近）
    assert labels[0] == "nanobot-159", f"nanobot-159 被沉底了：{labels}"


def test_缩减UA的安卓机不会被并成一条(audit_db):
    """新版 Chrome 安卓 UA 已缩减成 `Android 10; K`，Firefox 给 `Android 12; Mobile`。

    K / Mobile / Tablet 都不是机型。拿它们当机型，所有安卓机会被并成
    「安卓 K」一条 —— 设备页上等于只有一台安卓，比拆开更糟。
    机型不可信时必须退回用整段 UA 当身份。
    """
    from kb_lib.audit import device_family

    chrome_k10 = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
    chrome_k13 = ("Mozilla/5.0 (Linux; Android 13; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36")
    ff_mobile = "Mozilla/5.0 (Android 12; Mobile; rv:109.0) Gecko/109.0 Firefox/115.0"
    ff_tablet = "Mozilla/5.0 (Android 12; Tablet; rv:109.0) Gecko/109.0 Firefox/115.0"

    fams = [device_family(u) for u in (chrome_k10, chrome_k13, ff_mobile, ff_tablet)]
    assert len(set(fams)) == 4, f"缩减 UA 的安卓机被并成一条了：{fams}"
    for f in fams:
        # 只允许两种结果：可信机型「安卓 <型号>」，或不可信时的整段 UA「安卓 UA ...」
        assert f.startswith("安卓 "), f
        model = f[len("安卓 "):]
        if model.startswith("UA "):
            continue  # 机型不可信，退回整段 UA，这是预期行为
        assert model not in ("K", "Mobile", "Tablet"), f"机型取成了非机型字段：{f}"
        assert not re.match(r"^(rv|version|wv)[:\s]", model, re.I), f"机型取成了引擎版本：{f}"


def test_内部字段不返回给调用方(audit_db):
    """`devices` / `tk` 是合并过程的中间字段，不能出现在接口返回值里。

    `devices` 会把这台设备在 audit_log 里出现过的所有原始设备名暴露给
    任何能调 `/auth/devices` 的人；`tk` 是令牌桶标识，更不该外泄。
    """
    A = audit_db
    ua = "Mozilla/5.0 (Linux; Android 12; 22041216C) AppleWebKit/537.36"
    _raw_audit(A, ua, "203.0.113.41", "AAAA1111...", ts="2026-10-19T01:00:00")

    items = A.query_devices()
    assert items, "应至少返回一台设备"
    for k in ("devices", "tk"):
        assert k not in items[0], f"内部字段 {k} 被返回了：{sorted(items[0].keys())}"


def test_不同设备名的失败次数会相加(audit_db):
    """合并后同一台设备可能对应多个原始设备名，各自的失败次数都要算。

    同一台手机系统从 Android 12 升到 13，UA 变了但族名相同；**同一把令牌**
    下会并成一条，两条设备名各有失败记录，只取最大值会漏掉一条。

    C3 口径：按 Token 分组，所以这里必须让两条 UA 共用同一把令牌，
    才能测「同一条目里多个设备名的失败次数相加」；给两把令牌就变成两条了。
    """
    A = audit_db
    ua12 = "Mozilla/5.0 (Linux; Android 12; 22041216C) AppleWebKit/537.36"
    ua13 = "Mozilla/5.0 (Linux; Android 13; 22041216C) AppleWebKit/537.36"
    _raw_audit(A, ua12, "203.0.113.51", "SAME1111...", ts="2026-10-19T01:00:00")
    _raw_audit(A, ua13, "203.0.113.51", "SAME1111...", ts="2026-10-19T02:00:00")
    # Android 12 那条失败 2 次，Android 13 那条失败 5 次
    for _ in range(2):
        A.login_log(False, ip="203.0.113.51", device=ua12, reason="x")
    for _ in range(5):
        A.login_log(False, ip="203.0.113.51", device=ua13, reason="x")

    items = A.query_devices()
    assert len(items) == 1, f"同一令牌应并成 1 条，实际 {len(items)}"
    assert items[0]["fails"] == 7, f"两条设备名的失败应相加为 7，实际 {items[0]['fails']}"


def test_设备展示名带机型(audit_db):
    """`device_label` 要给安卓机带上机型，否则设备页上几台安卓长得一模一样。

    踩过的坑：这里原先自己写 `;\s*([A-Za-z0-9_-]+)\s+build` 且没加 `re.I`，
    而 UA 里是大写 `Build`，永远匹配不上，机型后缀一直是空的。
    """
    from kb_lib.audit import device_label

    cases = {
        "Mozilla/5.0 (Linux; Android 12; 22041216C Build/SP1A) AppleWebKit/537.36":
            "安卓手机 12 · 22041216C",
        "Mozilla/5.0 (Linux; Android 13; Pixel 6 Build/TQ3A) AppleWebKit/537.36":
            "安卓手机 13 · Pixel 6",
        "Mozilla/5.0 (Linux; Android 12; zh-cn; Redmi K50 Build/SKQ1) AppleWebKit/537.36":
            "安卓手机 12 · Redmi K50",
    }
    for ua, want in cases.items():
        got = device_label(ua)
        assert got == want, f"\nUA: {ua}\n期望: {want}\n实际: {got}"
