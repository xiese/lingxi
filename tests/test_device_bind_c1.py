"""C1 设备绑定回归测试（台账 #22）：一把 AG 一把专属 Token + 首次绑定。

对应设计文档（C1C2C3-design.md）§3 的判定表，逐格覆盖：
    ① 未绑定 + 不带 X-KB-Device      → 200，且记录里不写 bound_device
    ② 未绑定 + 带 X-KB-Device        → 200，记录里写入 bound_device
    ③ 已绑定 + 同设备                → 200
    ④ 已绑定 + 不同设备              → 403「此令牌已绑定其他设备」+ 审计
    ⑤ 已绑定 + 不带标识              → 403
    ⑥ 主令牌 + 任意/缺失标识         → 200，永不绑定
    ⑦ 解绑后回到未绑定，可换设备重绑
    ⑧ unbind 非 master 调用          → 403
    ⑨ 绑定/解绑/冲突都写审计

隔离方式照抄 tests/test_token_hash_c2.py：临时 KB_ROOT + prod 闸。
"""
from __future__ import annotations

import json
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

def _assert_not_prod():
    """硬约束：真实 KB_ROOT 下 `.kb-env` 为 prod 时立即停手。

    必须在打补丁**之前**读：一旦 `_patch_paths_everywhere()` 跑过，KB_ROOT 已经
    指向 mkdtemp 出来的临时目录，那里永远没有 `.kb-env`，这个闸就恒为 False，
    等于摆设（返工第 3 条）。
    """
    from kb_lib.config import KB_ROOT as real_root
    env = Path(real_root) / ".kb-env"
    if env.exists() and env.read_text(encoding="utf-8").strip() == "prod":
        # 不许 skip：skip 会让 18 条用例静默转绿，混杂在既有 2 个 skipped 里难以察觉。
        pytest.fail(f"拒绝在 prod 库上跑：{env}")


def _patch_paths_everywhere(tmp: Path, notes: Path):
    values = {
        "KB_ROOT": tmp,
        "NOTES_DIR": notes,
        "TOKEN_FILE": tmp / ".api_tokens.json",
        "LOG_FILE": tmp / ".audit_log.jsonl",
        "SETTINGS_FILE": tmp / ".kb_settings.json",
        "INDEX_FILE": tmp / ".search_index.json",
        "EMBEDDINGS_FILE": tmp / ".embeddings.npy",
        "FAISS_FILE": tmp / ".faiss.index",
        "INDEX_MANIFEST_FILE": tmp / ".index_manifest.json",
        "MCP_USAGE_LOG": tmp / ".kb_mcp_usage.jsonl",
    }
    patchers = []
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "")
        if not name.startswith("kb_lib"):
            continue
        for key, val in values.items():
            if hasattr(mod, key):
                patchers.append(mock.patch.object(mod, key, val))
    for p in patchers:
        p.start()
    return patchers


@pytest.fixture
def kb():
    """临时库 + 隔离 Flask 应用。返回 (client, master_headers, tmp, 签发函数)。"""
    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import

    _assert_not_prod()  # 打补丁之前读真实 KB_ROOT，之后读到的只是临时目录
    tmp = Path(tempfile.mkdtemp(prefix="kbbind-"))
    notes = tmp / "笔记"
    notes.mkdir()
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    try:
        AU._DB_INITED = False
        AU._init_db()
        S.init_master()
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        mh = {"Authorization": f"Bearer {master}"}

        def issue(name="ag-1", perm="rw"):
            r = c.post("/auth/token", headers=mh,
                       json={"name": name, "device": name, "permission": perm})
            assert r.status_code == 200, r.data[:300]
            return r.get_json()["token"]

        yield c, mh, tmp, issue
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _record(tmp: Path, token: str) -> dict:
    """从落盘文件里读这把令牌的记录（按哈希键查，明文不参与）。"""
    from kb_lib.store import token_hash
    s = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))
    return s["tokens"][token_hash(token)]


def _get(c, token, dev=None, path="/api/templates"):
    h = {"Authorization": f"Bearer {token}"}
    if dev is not None:
        h["X-KB-Device"] = dev
    return c.get(path, headers=h)


def _events(action: str) -> list:
    import kb_lib.audit as AU
    conn = AU._get_db()
    rows = conn.execute(
        "SELECT token_prefix, action, path, status, extra FROM audit_log WHERE action=? ORDER BY id",
        (action,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── ① 未绑定 + 不带标识 → 放行，且保持未绑定 ─────────────────────────────

def test_未绑定不带标识放行且保持未绑定(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    r = _get(c, tk)
    assert r.status_code == 200, r.data[:300]
    assert "bound_device" not in _record(tmp, tk), "存量无感路径不许顺手绑定"


# ── ② 未绑定 + 带标识 → 首次绑定并放行 ──────────────────────────────────

def test_首次带标识即绑定(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    r = _get(c, tk, "ag-dev-A1")
    assert r.status_code == 200, r.data[:300]
    rec = _record(tmp, tk)
    assert rec.get("bound_device") == "ag-dev-A1", rec.get("bound_device")
    assert rec.get("bound_at"), "绑定时间没写"


# ── ③ 已绑定 + 同设备 → 放行 ────────────────────────────────────────────

def test_已绑定同设备放行(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    r = _get(c, tk, "ag-dev-A1")
    assert r.status_code == 200, r.data[:300]
    assert _record(tmp, tk)["bound_device"] == "ag-dev-A1", "同设备请求把绑定改掉了"


# ── ④ 已绑定 + 不同设备 → 403 + 文案 + 审计 ─────────────────────────────

def test_已绑定不同设备返回403且文案正确(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    r = _get(c, tk, "ag-dev-B2")
    assert r.status_code == 403, r.data[:300]
    body = r.get_json()
    assert body["error"] == "此令牌已绑定其他设备", body
    assert body["code"] == "device_mismatch", body
    assert _record(tmp, tk)["bound_device"] == "ag-dev-A1", "冲突请求改掉了绑定"


def test_绑定冲突写审计事件(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert _get(c, tk, "ag-dev-B2").status_code == 403
    evs = _events("device_mismatch")
    assert len(evs) == 1, evs
    assert evs[0]["status"] == 403
    assert "reason=mismatch" in (evs[0]["extra"] or ""), evs[0]
    assert "ag-dev-B2" in (evs[0]["extra"] or ""), evs[0]


# ── ⑤ 已绑定 + 不带标识 → 403 ───────────────────────────────────────────

def test_已绑定不带标识返回403(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    r = _get(c, tk)
    assert r.status_code == 403, r.data[:300]
    assert r.get_json()["code"] == "device_mismatch"
    evs = _events("device_mismatch")
    assert evs and "reason=missing" in (evs[-1]["extra"] or ""), evs


# ── ⑥ 主令牌不参与绑定 ──────────────────────────────────────────────────

def test_主令牌任意设备都放行且不绑定(kb):
    c, mh, tmp, _ = kb
    for dev in (None, "whatever-dev-1", "whatever-dev-2"):
        h = dict(mh)
        if dev is not None:
            h["X-KB-Device"] = dev
        r = c.get("/auth/devices", headers=h)
        assert r.status_code == 200, (dev, r.data[:200])
    s = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))
    assert "bound_device" not in (s.get("master") or {}), "主令牌被写入了绑定字段"


# ── ⑦ 解绑后回到未绑定，可重新绑 ────────────────────────────────────────

def test_解绑后可换设备重新绑定(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    tid = _record(tmp, tk)["id"]
    r = c.post(f"/auth/devices/{tid}/unbind", headers=mh)
    assert r.status_code == 200, r.data[:300]
    assert "bound_device" not in _record(tmp, tk), "解绑没清掉 bound_device"
    # 旧设备、新设备都按「未绑定」处理：带标识就重新绑
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert _record(tmp, tk)["bound_device"] == "ag-dev-A1"
    assert c.post(f"/auth/devices/{tid}/unbind", headers=mh).status_code == 200
    assert _get(c, tk, "ag-dev-C3").status_code == 200
    assert _record(tmp, tk)["bound_device"] == "ag-dev-C3", "解绑后没能改绑到新设备"


def test_解绑写审计(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    tid = _record(tmp, tk)["id"]
    assert c.post(f"/auth/devices/{tid}/unbind", headers=mh).status_code == 200
    evs = _events("auth_unbind")
    assert len(evs) == 1, evs
    assert "was_bound=ag-dev-A1" in (evs[0]["extra"] or ""), evs[0]


# ── ⑧ unbind 仅 master 可调 ─────────────────────────────────────────────

def test_解绑非主令牌调用返回403(kb):
    c, mh, tmp, issue = kb
    tk = issue(perm="rw")
    tid = _record(tmp, tk)["id"]
    r = c.post(f"/auth/devices/{tid}/unbind",
               headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 403, r.data[:300]
    assert r.get_json()["error"] == "需主Token", r.get_json()


# ── ⑨ 绑定本身也要能在审计里看见 ────────────────────────────────────────

def test_绑定后审计里有事件(kb):
    """首次绑定必须留下 action=device_bind 的事件，并带上该令牌标识。

    原来只断言「最近 5 条审计里出现该令牌前缀」——任何一次带令牌的请求都满足，
    等于没测绑定（返工第 5 条）。
    """
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    evs = _events("device_bind")
    assert len(evs) == 1, evs
    from kb_lib.store import token_hash
    assert evs[0]["token_prefix"] == token_hash(tk)[:12] + "...", evs[0]
    assert "dev=ag-dev-A1" in (evs[0]["extra"] or ""), evs[0]


def test_同设备重复请求不再写绑定事件(kb):
    """device_bind 只在首次绑定那一次写，重复请求不许刷审计。"""
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert len(_events("device_bind")) == 1, _events("device_bind")


def test_绑定冲突审计里的前缀能反查到令牌(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    assert _get(c, tk, "ag-dev-B2").status_code == 403
    from kb_lib.store import token_hash
    evs = _events("device_mismatch")
    assert evs[-1]["token_prefix"] == token_hash(tk)[:12] + "...", evs[-1]


# ── 附加：标识只做最小校验（非空即可），不做长度/字符集限制 ─────────────

def test_任意非空标识都能首次绑定并原样存(kb):
    """返工第 4a 条：短 ID、含空格、超长、中文都按标识原样存。

    任务书判定表没有「格式非法按没带处理」这一格，加了就等于改写判定表。
    """
    for i, dev in enumerate(("short", "has space here", "x" * 200, "中文设备名中文设备名")):
        c, mh, tmp, issue = kb
        tk = issue(name=f"ag-{i}")
        r = _get(c, tk, dev)
        assert r.status_code == 200, (dev[:12], r.data[:200])
        assert _record(tmp, tk).get("bound_device") == dev, f"{dev[:12]} 没按原样绑定"


def test_已绑定带非法标识仍按绑定冲突文案(kb):
    """返工第 4a 条：已绑定 + 标识不同 → 一律第 4 条 mismatch 文案。

    不能因为「格式看起来不合法」就回 missing 文案——那是自创的第 7 格。
    """
    c, mh, tmp, issue = kb
    tk = issue()
    assert _get(c, tk, "ag-dev-A1").status_code == 200
    for other in ("short", "has space here", "x" * 200, "中文设备名中文设备名"):
        r = _get(c, tk, other)
        assert r.status_code == 403, (other[:12], r.data[:200])
        assert r.get_json()["error"] == "此令牌已绑定其他设备", (other[:12], r.get_json())
        evs = _events("device_mismatch")
        assert "reason=mismatch" in (evs[-1]["extra"] or ""), evs[-1]
        assert _record(tmp, tk)["bound_device"] == "ag-dev-A1", "冲突请求改掉了绑定"


# ── 附加：D2 —— 审计记本次请求 IP，不是签发时 IP ────────────────────────

def test_审计记本次请求IP不是签发时IP(kb):
    c, mh, tmp, issue = kb
    tk = issue()
    # 签发时 IP 是测试客户端的默认来源；这里用不同来源发请求
    r = c.get("/api/templates",
              headers={"Authorization": f"Bearer {tk}"},
              environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert r.status_code == 200
    import kb_lib.audit as AU
    conn = AU._get_db()
    row = conn.execute("SELECT ip FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row["ip"] == "198.51.100.7", row["ip"]


# ── 附加：CF-Connecting-IP 只在受信代理来源时采信 ────────────────────────

def _last_ip():
    import kb_lib.audit as AU
    conn = AU._get_db()
    row = conn.execute("SELECT ip FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row["ip"]


def _get_with_ip(c, tk, remote, cf=None, xff=None):
    h = {"Authorization": f"Bearer {tk}"}
    if cf is not None:
        h["CF-Connecting-IP"] = cf
    if xff is not None:
        h["X-Forwarded-For"] = xff
    return c.get("/api/templates", headers=h, environ_base={"REMOTE_ADDR": remote})


def test_CF头在未配置受信代理时一律忽略(kb, monkeypatch):
    """返工第 4b 条：环回不等于受信。TRUSTED_PROXIES 为空时任何来源都不采信。"""
    import kb_lib.auth as A
    monkeypatch.setattr(A, "TRUSTED_PROXIES", set())
    c, mh, tmp, issue = kb
    tk = issue()
    for peer in ("127.0.0.1", "::1", "198.51.100.7"):
        r = _get_with_ip(c, tk, peer, cf="192.0.2.30")
        assert r.status_code == 200, r.data[:200]
        assert _last_ip() == peer, f"{peer} 来源无条件信了 CF 头：{_last_ip()}"


def test_CF头在受信代理来源时采信且校验合法性(kb, monkeypatch):
    """对端 IP 在 TRUSTED_PROXIES 里才采信；非法值丢弃，回落到对端地址。"""
    import kb_lib.auth as A
    monkeypatch.setattr(A, "TRUSTED_PROXIES", {"127.0.0.1"})
    c, mh, tmp, issue = kb
    tk = issue()
    r = _get_with_ip(c, tk, "127.0.0.1", cf="192.0.2.30")
    assert r.status_code == 200
    assert _last_ip() == "192.0.2.30", _last_ip()

    r = _get_with_ip(c, tk, "127.0.0.1", cf="not-an-ip")
    assert r.status_code == 200
    assert _last_ip() == "127.0.0.1", f"非法 CF 值被写进了审计：{_last_ip()}"

    # 不在受信列表里的来源，即使带了合法 CF 头也不采信
    r = _get_with_ip(c, tk, "198.51.100.7", cf="192.0.2.31")
    assert r.status_code == 200
    assert _last_ip() == "198.51.100.7", _last_ip()


def test_C1_空白设备头按没带处理(kb):
    """GLM 三节第 3 条：去空格后为空 = 没带。

    已绑定 + 空白头 → 403(missing)；未绑定 + 空白头 → 放行且不写 bound_device。
    """
    c, mh, tmp, issue = kb
    # 未绑定 + 空白头 → 放行，不绑
    t = issue("blank-1")
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "   "})
    assert r.status_code == 200
    assert not _record(tmp, t).get("bound_device"), "空白头不该写 bound_device"

    # 绑上之后 + 空白头 → 403 missing
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "dev-A"})
    assert r.status_code == 200
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "  \t "})
    assert r.status_code == 403
    assert "缺少设备标识" in r.get_json().get("error", "")


def test_C1_跨进程写盘后绑定判定立刻可见(kb):
    """GLM P2-2：30 秒 TTL 缓存会让另一个进程的解绑/改绑在本进程不可见。

    真实场景：kb-mcp 或另一个 worker 落盘解绑 → 本进程缓存仍是旧快照 →
    新设备带标识请求命中旧 bound → 403，且失败不刷新缓存，用户重试也解不开，
    最长卡 30 秒。

    判据（不看审计、不看 stub，直接看快路径读到什么）：
      ① 本进程读一次库，建立「未绑定」缓存；
      ② 另一个进程落盘把 bound_device 写成 dev-owner（mtime 前进）；
      ③ 本进程下一次认证必须读到 dev-owner —— 即带别的设备发请求必须 403，
         且审计里 bound=dev-owner。若仍读到旧快照（未绑定），请求会 200，
         这就是回归。
    """
    import kb_lib.auth as A
    import json as _json
    import os as _os

    c, mh, tmp, issue = kb
    t = issue("race-1")
    key = A.token_hash(t)

    # ① 建立缓存（未绑定）。为了让 30 秒 TTL 缓存处于有效状态，
    #    这里同时预热 _load_store_cached —— 它才是被判废对象。
    A._load_store_fresh(max_age=0)
    import kb_lib.store as _SS
    _SS._load_store_cached()
    assert _SS._CACHE["data"] is not None, "缓存没建立起来，用例前提不成立"

    # ② 另一个进程落盘绑定，mtime 前进 5 秒（不走本进程 save_store，缓存不会被清）
    tf = Path(tmp) / ".api_tokens.json"
    data = _json.loads(tf.read_text(encoding="utf-8"))
    data["tokens"][key]["bound_device"] = "dev-owner"
    data["tokens"][key]["bound_at"] = "2026-10-19T10:00:00"
    tf.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _os.utime(tf, (_os.path.getatime(tf), _os.path.getmtime(tf) + 5))

    # ③ 判据用「不带设备标识」的请求 —— 这是两条路径行为唯一相反的地方：
    #    读旧快照（以为未绑定）→ 第 6 条放行 200；
    #    读到盘上（已知绑定）  → 403 missing。
    #    注：带 dev-thief 的请求两条路径都会 403（读旧快照时会落到 _bind_device，
    #    事务内读到盘上的 dev-owner 同样 mismatch），区分不了，故不用它做判据。
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403, \
        f"跨进程绑定后本进程仍按旧的「未绑定」放行（30 秒缓存窗口）：{r.status_code}"

    import sqlite3 as _sq
    conn = _sq.connect(str(Path(tmp) / ".audit.db"))
    rows = conn.execute(
        "SELECT extra FROM audit_log WHERE action='device_mismatch' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    assert rows, "没有 device_mismatch 事件"
    assert "reason=missing" in (rows[0][0] or ""), f"应走 missing 分支：{rows[0][0]}"
    assert "bound=dev-owner" in (rows[0][0] or ""), \
        f"审计没带回实际绑定设备：{rows[0][0]}"


def test_C1_跨进程解绑后立刻可重新绑定(kb):
    """反方向同样要立刻可见：另一个进程解绑后，本进程不许还拿旧 bound 拦人。"""
    import kb_lib.auth as A
    import json as _json
    import os as _os

    c, mh, tmp, issue = kb
    t = issue("rebind-1")
    key = A.token_hash(t)
    assert c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "dev-A"}).status_code == 200

    tf = Path(tmp) / ".api_tokens.json"
    data = _json.loads(tf.read_text(encoding="utf-8"))
    data["tokens"][key].pop("bound_device", None)
    data["tokens"][key].pop("bound_at", None)
    tf.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _os.utime(tf, (_os.path.getatime(tf), _os.path.getmtime(tf) + 5))

    # 另一个进程解绑后，新设备应立刻能绑上（旧实现卡 30 秒 403）
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "dev-B"})
    assert r.status_code == 200, f"跨进程解绑后仍被旧快照拦住：{r.status_code}"
    assert _record(tmp, t)["bound_device"] == "dev-B"


def test_C3_匿名条目不算已吊销按UA族合并(kb):
    """GLM 二节存疑点：anon 前缀必须归一成空桶，不能出现在 revoked 列表里。"""
    c, mh, tmp, issue = kb
    # 无令牌访问 → audit 记 token_prefix='anon'
    for ua in ("curl/8.5.0", "curl/8.4.0"):
        c.get("/dashboard", headers={"User-Agent": ua})
    # ① 库里的匿名行确实是 token_prefix='anon'（证明造数有效）
    import sqlite3 as _sq
    conn = _sq.connect(str(Path(tmp) / ".audit.db"))
    anon_rows = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE token_prefix='anon'"
    ).fetchone()[0]
    conn.close()
    assert anon_rows >= 2, f"匿名请求没有写进审计（{anon_rows} 行）"

    # ② 接口层面：匿名桶不能出现在「已吊销」里
    r = c.get("/auth/devices", headers=mh)
    items = (r.get_json() or {}).get("devices") or []
    for it in items:
        p = it.get("token_prefix") or ""
        if p in ("", "anon"):
            assert it.get("revoked") is False, f"匿名条目被标成已吊销：{it}"
    # ③ 两个 curl 版本必须合并成一条（SQL 已把 anon 归一进同一个桶）
    curl = [it for it in items if "curl" in (it.get("device") or "").lower()]
    assert len(curl) == 1, f"匿名桶没按 UA 族合并：{len(curl)} 条 -> {[c.get('device') for c in curl]}"


def test_C3_令牌label走token_label不抢设备标题(kb):
    """令牌记录里的 label 要能在页面上看到（C3 要求），但它**不是设备标题**。

    分工：
      - `it["label"]`      = 设备行标题，恒为 device_label(设备名)（旧行为，前端
                             app.js 用它渲染设备名）；
      - `it["token_label"]`= 令牌自己的 label（优先取记录里的 label 字段）。
    早期这里断言过 `it["label"]` 取令牌 label，那是把两个字段混为一谈，
    会让设备页把「这台设备」显示成「某把令牌的名字」。
    """
    c, mh, tmp, issue = kb
    from kb_lib import store as S
    t = issue("label-1")
    h = S.token_hash(t)
    with S.store_transaction() as st:
        st["tokens"][h]["label"] = "显式标签XYZ"
    c.get("/dashboard", headers={"Authorization": f"Bearer {t}"})
    r = c.get("/auth/devices", headers=mh)
    items = (r.get_json() or {}).get("devices") or []
    assert items, f"设备列表为空，无法验证 label；返回={r.get_json()}"
    tl = [it.get("token_label") for it in items]
    assert "显式标签XYZ" in tl, f"token_label 没优先取记录里的 label：{tl}"
    # 设备标题不能被令牌 label 顶替
    dev_labels = [it.get("label") for it in items]
    assert "显式标签XYZ" not in dev_labels, f"设备标题被令牌 label 抢了：{dev_labels}"


def test_C1_same不重复写绑定审计(kb, monkeypatch):
    """GLM P2-1：_bind_device 在「库里已绑定同设备」时必须返回 "same"，不是 "bound"。

    缓存窗口内（另一个进程手里是绑定前的旧快照）每个请求都会走到 _bind_device；
    旧实现 cur == dev 也返回 "bound"，于是每次请求都写一条 device_bind 审计
    + 一次 flock 全量事务，并且 bound_at 被反复改写——审计语义从「首次绑定」
    劣化成「每请求一条」。

    直接单测 _bind_device 的返回值语义，再用两次绑定对比 bound_at。
    """
    import sqlite3
    from kb_lib import auth as _A
    c, mh, tmp, issue = kb
    t = issue("same-1")
    H = {"Authorization": f"Bearer {t}", "X-KB-Device": "same-dev-1"}
    assert c.get("/dashboard", headers=H).status_code == 200

    key = _A.token_hash(t)
    first_at = _record(tmp, t).get("bound_at")
    assert first_at, "首次绑定没写 bound_at"

    # 库里已绑定同设备 → 必须判 "same"，而不是 "bound"
    verdict, occupied = _A._bind_device(key, "same-dev-1")
    assert verdict == "same", f"已绑定同设备应返回 same，实际 {verdict}"
    assert occupied == "same-dev-1"
    # same 不许改写 bound_at
    assert _record(tmp, t).get("bound_at") == first_at, "same 分支改写了 bound_at"

    # 不同设备仍然是 mismatch
    v2, occ2 = _A._bind_device(key, "other-dev")
    assert v2 == "mismatch" and occ2 == "same-dev-1"


def test_C1_超长设备标识不会自锁(kb):
    """GLM P2-2：>256 字符的 ID 首次绑定成功后，第二次起必须仍然通过。

    旧实现只在写入处 dev[:256]，比较用未截断原值 → 超长 ID 永久 mismatch 403。
    """
    c, mh, tmp, issue = kb
    t = issue("longdev-1")
    long_dev = "D" * 400
    H = {"Authorization": f"Bearer {t}", "X-KB-Device": long_dev}
    assert c.get("/dashboard", headers=H).status_code == 200, "首次绑定失败"
    for _ in range(3):
        r = c.get("/dashboard", headers=H)
        assert r.status_code == 200, f"超长 ID 自锁：{r.status_code}"


def test_C1_same分支返回契约_tk_info顺序(kb, monkeypatch):
    """GLM/DS 双报的 P0：authenticate 的 "same" 分支必须返回 (tk, info)。

    写成 (info, dev) 会让调用方 g.tk=dict、g.ti=str：
    audit_log 里 tk[:12] 抛 TypeError、ti.get() 抛 AttributeError，
    首次绑定后 30 秒缓存窗口内每个请求都命中，普通请求审计全丢。

    直接进请求上下文调 authenticate()，走「缓存里 bound_device 为空 →
    _bind_device 返回 same」这条真实分支，然后校验返回契约。
    """
    from kb_lib import auth as _A
    from kb_lib import store as _S
    c, mh, tmp, issue = kb
    t = issue("same-3")
    H = {"Authorization": f"Bearer {t}", "X-KB-Device": "same-dev-3"}
    assert c.get("/dashboard", headers=H).status_code == 200

    # 关键：库里保持「已绑定」，但把缓存伪造成绑定**前**的旧快照
    # （另一个进程 kb-mcp-sse 刚帮这台设备绑上，本进程缓存还没过期）。
    # 这样快路径看到 bound 为空 → 落到 _bind_device → 事务内发现 cur == dev → "same"。
    key = _A.token_hash(t)
    stale = json.loads(json.dumps(_S.load_store()))
    stale["tokens"][key].pop("bound_device", None)
    stale["tokens"][key].pop("bound_at", None)
    # 模块级缓存是全局状态：用完必须还原，否则耦合到后续用例（GLM C13g P3）
    with _S._CACHE_LOCK:
        _saved = (_S._CACHE.get("data"), _S._CACHE.get("ts"))
    tk = info = None
    try:
        with _S._CACHE_LOCK:
            _S._CACHE["data"] = stale
            import time as _t
            _S._CACHE["ts"] = _t.time()
        with c.application.test_request_context("/dashboard", headers=H):
            tk, info = _A.authenticate()
    finally:
        with _S._CACHE_LOCK:
            _S._CACHE["data"], _S._CACHE["ts"] = _saved
    assert tk == t, f"第一个返回值必须是明文 token，实际 {type(tk).__name__}"
    assert isinstance(info, dict) and "permission" in info, \
        f"第二个返回值必须是 info 字典，实际 {type(info).__name__}"


def test_C3_绑定事件不算设备使用且抢绑失败方不入列表(kb):
    """C1 新增 device_bind/device_mismatch 事件后，设备列表口径不能被带偏。

    实测回归（DS C13g 第 2 条顺带挖出）：
      - device_bind 被当成一次普通请求 → uses 虚增 1；
      - device_mismatch 记的是**抢绑失败方**的设备 → 列表里凭空多一台
        从未真正登录过的设备。
    """
    import sqlite3 as _sq

    c, mh, tmp, issue = kb
    t = issue("bind-ev-1")
    H = {"Authorization": f"Bearer {t}", "X-KB-Device": "dev-ev-A"}
    assert c.get("/dashboard", headers=H).status_code == 200, "首次绑定失败"

    # 同一把令牌换设备来抢绑 → 403，并写 device_mismatch（设备是 dev-ev-THIEF）
    c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "dev-ev-THIEF"})

    conn = _sq.connect(str(Path(tmp) / ".audit.db"))
    acts = dict(conn.execute(
        "SELECT action, COUNT(*) FROM audit_log GROUP BY action").fetchall())
    conn.close()
    assert acts.get("device_bind", 0) >= 1, f"没造出 device_bind：{acts}"
    assert acts.get("device_mismatch", 0) >= 1, f"没造出 device_mismatch：{acts}"

    r = c.get("/auth/devices", headers=mh)  # 设备清单需 master
    assert r.status_code == 200, r.status_code
    items = r.get_json().get("devices") or []
    # 口径（r8 修正）：列表里的 device = 令牌的 bound_device（绑定后的真实设备），
    # 没有绑定才回落创建时写死的 device。原来取 device 字段，导致「创建时叫
    # bind-ev-1、实际绑到 dev-ev-A」的令牌在设备页一直显示 bind-ev-1，
    # 看不出到底哪台设备在用。
    names = {i.get("device") for i in items}
    assert "dev-ev-THIEF" not in names, f"抢绑失败方被当成设备列出：{names}"
    assert "dev-ev-A" in names, f"真实绑定设备丢了：{names}"
    assert "bind-ev-1" not in names, f"还在显示创建时的旧名字：{names}"
    row = [i for i in items if i.get("device") == "dev-ev-A"][0]
    # 真实请求 2 次：/auth/token 创建 + /dashboard；device_bind 与
    # device_mismatch 都不该算进去（旧实现是 3，虚增 1）。
    assert row.get("uses") == 2, f"uses 被绑定事件带偏：{row.get('uses')}"


def test_C1_只读事务不写盘(kb):
    """GLM P2-4：_bind_device 的 same/mismatch/gone 都不改字段，
    store_transaction 若无条件 save_store，每个请求都会全量落盘（写放大）。
    判据：只读事务跑完后，令牌文件的 mtime 和内容都不变。
    """
    import kb_lib.store as S

    c, mh, tmp, issue = kb
    t = issue("ro-tx-1")
    tf = Path(tmp) / ".api_tokens.json"
    before_mtime = tf.stat().st_mtime
    before_body = tf.read_text(encoding="utf-8")

    import time as _time
    _time.sleep(0.05)          # 让 mtime 有分辨率可区分
    with S.store_transaction() as s:
        _ = s.get("tokens")    # 只读，不改任何字段

    assert tf.stat().st_mtime == before_mtime, "只读事务也写盘了（mtime 变了）"
    assert tf.read_text(encoding="utf-8") == before_body, "只读事务改动了文件内容"


def test_C1_真改动的事务仍然落盘(kb):
    """反向保护：不能为了省写盘把真改动也跳过。"""
    import kb_lib.store as S

    c, mh, tmp, issue = kb
    t = issue("rw-tx-1")
    key = S.token_hash(t)
    with S.store_transaction() as s:
        s["tokens"][key]["marker_probe"] = "written"
    saved = json.loads((Path(tmp) / ".api_tokens.json").read_text(encoding="utf-8"))
    assert saved["tokens"][key].get("marker_probe") == "written", "真改动没落盘"


def test_C1_库里bound超长时读侧截断不自锁(kb):
    """GLM C13i P2-5：写入侧存 dev[:256]，读侧若用未截断原值比较，
    手工塞库或写入方漂移时正常设备会永久 403 自锁。
    """
    import kb_lib.auth as A
    import json as _json

    c, mh, tmp, issue = kb
    t = issue("longbound-1")
    key = A.token_hash(t)
    dev = "L" * 400

    # 直接改库：bound_device 存了超长值（模拟异常写入方）
    tf = Path(tmp) / ".api_tokens.json"
    data = _json.loads(tf.read_text(encoding="utf-8"))
    data["tokens"][key]["bound_device"] = dev
    tf.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 本进程带同一个（超长）设备标识：截断后两边同值 → 放行，不许 403
    r = c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": dev})
    assert r.status_code == 200, f"读侧没截断导致自锁：{r.status_code}"


def test_C1_设备标识首尾空白两侧同口径(kb):
    """GLM C13k P2-5 保留意见：写入 strip()[:256]、读侧只 strip 会让带空白的
    标识两侧不等。现在存/比统一走 _norm_dev，首尾空白必须不影响判定。"""
    c, mh, tmp, issue = kb
    t = issue("space-1")

    # 首次带首尾空白绑定
    assert c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "  dev-space  "}).status_code == 200
    # 去掉空白再来：同一个设备，必须放行
    assert c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "dev-space"}).status_code == 200
    # 空白内部不同才是真不同设备
    assert c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "dev space"}).status_code == 403


def test_C1_新增根键也会触发落盘(kb):
    """GLM C13m 追加质询 4：_tx_snapshot 改为整份序列化后，
    写一个「枚举式快照不会比较到」的新根键，必须照样落盘（不许静默丢写）。
    """
    import kb_lib.store as S

    c, mh, tmp, issue = kb
    t = issue("newkey-1")
    tf = Path(tmp) / ".api_tokens.json"

    with S.store_transaction() as s:
        s["future_root_key"] = {"hello": "world"}

    saved = json.loads(tf.read_text(encoding="utf-8"))
    assert saved.get("future_root_key") == {"hello": "world"}, \
        "新增根键被判不脏，静默丢失"


def test_C1_审计里绝不出现明文令牌前缀(kb):
    """DS C13n 片2 P1-1：audit_log 直接取 g.tk[:12] 落盘。
    若任何一条认证路径把明文写进 g.tk，就等于把真实令牌前 12 位存进审计库。
    覆盖：check_auth（普通令牌）、require_master（master）、device_mismatch。
    """
    import sqlite3 as _sq

    c, mh, tmp, issue = kb
    t = issue("plain-1")
    master = mh["Authorization"].split()[-1]

    # ① 普通令牌请求
    c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "dev-plain"})
    # ② master 令牌走**普通**接口（check_auth 路径，才走 g.tk 那行）
    c.get("/dashboard", headers=mh)
    c.get("/api/templates", headers=mh)
    # ③ 绑定冲突
    c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "dev-other"})

    conn = _sq.connect(str(Path(tmp) / ".audit.db"))
    prefixes = [r[0] for r in conn.execute(
        "SELECT DISTINCT token_prefix FROM audit_log WHERE token_prefix IS NOT NULL")]
    conn.close()

    assert prefixes, "审计里没有任何 token_prefix，用例前提不成立"
    for p in prefixes:
        raw = (p or "").rstrip(".")
        # 普通令牌绝不许出现明文前缀
        assert not t.startswith(raw), f"审计里出现了令牌明文前缀：{p}"
    # master 例外：设备页靠 master token[:12] 匹配审计前缀，必须能对上，
    # 否则 master 会从设备列表消失（见 auth.py 注释与 KNOWN-ISSUES）
    assert any(p and master.startswith((p or "").rstrip(".")) for p in prefixes), \
        f"master 审计前缀匹配不上，设备页将认不出 master：{prefixes}"


def test_C1_审计设备标识无法注入字段(kb):
    """GLM C13o P2-5：设备 ID 若含空格 / `=` / 换行，能伪造 extra 里的
    `bound=`、`reason=` 字段，或把审计行拆成多行。_audit_safe 必须挡住。
    """
    import sqlite3 as _sq

    c, mh, tmp, issue = kb
    t = issue("inject-1")
    assert c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": "dev-ok"}).status_code == 200
    # 注：换行在 HTTP 层就被 werkzeug 拒绝（ValueError: Header values must not
    # contain newline characters），传不到应用层。这里测能传进来的两种分隔符：
    # 空格（伪造新字段）与 `=`（伪造键值）。
    evil = "x 1=b reason=FAKE"
    r = c.get("/dashboard", headers={
        "Authorization": f"Bearer {t}", "X-KB-Device": evil})
    assert r.status_code == 403

    conn = _sq.connect(str(Path(tmp) / ".audit.db"))
    rows = conn.execute(
        "SELECT extra FROM audit_log WHERE action='device_mismatch'"
    ).fetchall()
    conn.close()
    assert rows, "没有 device_mismatch 事件"
    extra = rows[-1][0] or ""
    assert "\n" not in extra, f"审计被换行注入：{extra!r}"
    assert extra.count("reason=") == 1, f"出现了伪造的 reason 字段：{extra!r}"
    assert "dev=x_1_b_reason_FAKE" in extra, f"转义结果不符预期：{extra!r}"


def test_C1_CF头非法值不被信任(kb, monkeypatch):
    """GLM C13o P2-3 条件 b：CF-Connecting-IP 解析失败时必须 fail-closed，
    回退到 socket 对端，不许采信客户端伪造的头。
    """
    import kb_lib.auth as A

    c, mh, tmp, issue = kb
    t = issue("cf-1")
    monkeypatch.setattr(A, "TRUSTED_PROXIES", {"127.0.0.1"})

    # ① 合法 CF 头 → 采信
    # 受信代理场景必须显式给出对端地址，否则 remote_addr 为空、走不到采信分支
    with c.application.test_request_context(
            "/dashboard", headers={"CF-Connecting-IP": "203.0.113.9"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert A.client_ip() == "203.0.113.9", "合法值没被采信"

    # ② 非法 CF 头 → 丢弃，回退对端（fail-closed）
    bogus = "not-an-ip; echo pwned"
    with c.application.test_request_context(
            "/dashboard", headers={"CF-Connecting-IP": bogus},
            environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        got = A.client_ip()
    assert got != bogus, f"非法值被采信：{got!r}"
    assert got == "127.0.0.1", f"未回退到对端地址，实际 {got!r}"


def test_C1_外部改库后立刻可见_误拒窗口为零(kb):
    """GLM C13p 建议 5：_load_store_fresh 的误拒窗口必须钉死。

    场景：另一个进程把设备绑上并落盘（本进程缓存还是旧快照），
    本进程下一次认证必须立刻见到新状态，不许有最长 30 秒的误判。
    """
    import kb_lib.auth as A
    import json as _json
    import os as _os
    import time as _time

    c, mh, tmp, issue = kb
    t = issue("window-1")
    key = A.token_hash(t)
    A._load_store_fresh(max_age=0)     # 建立缓存（此时未绑定）

    tf = Path(tmp) / ".api_tokens.json"
    data = _json.loads(tf.read_text(encoding="utf-8"))
    data["tokens"][key]["bound_device"] = "dev-owner"
    tf.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _os.utime(tf, (_os.path.getatime(tf), _os.path.getmtime(tf) + 5))

    _t0 = _time.perf_counter()
    r = c.get("/dashboard", headers={"Authorization": f"Bearer {t}"})  # 不带标识
    _elapsed = _time.perf_counter() - _t0

    assert r.status_code == 403, \
        f"外部绑定后本进程仍按旧快照放行（有误拒窗口）：{r.status_code}"
    assert _elapsed < 1.0, f"认证耗时 {_elapsed:.2f}s，超出预期"


def test_C3_master出现在设备列表且前缀对得上(kb):
    """本轮修 g.tk 时差点踩的坑：master 若改成哈希前缀，设备页
    （routes_auth.auth_devices 用 master["token"][:12] 匹配 audit 的
    token_prefix）就再也认不出 master，master 会从列表里消失。
    这里钉死：master 发过请求后，设备列表里必须有一条 master 记录。
    """
    c, mh, tmp, issue = kb
    master = mh["Authorization"].split()[-1]

    assert c.get("/dashboard", headers=mh).status_code == 200

    r = c.get("/auth/devices", headers=mh)
    assert r.status_code == 200
    items = r.get_json().get("devices") or []
    assert items, "设备列表为空"

    master_rows = [i for i in items
                   if (i.get("token_label") == "master"
                       or (i.get("tokens") and i["tokens"][0].get("is_master")))]
    assert master_rows, \
        f"master 没出现在设备列表里（前缀匹配断了）：{[i.get('token_prefix') for i in items]}"
    assert not any(i.get("revoked") for i in master_rows), "master 被标成已吊销"


def test_C3_抢绑失败行不得污染last_ip与last_seen(kb):
    """GLM FINAL_m2a P2：外层排除了 device_mismatch，但 last_ip/last_token
    两个相关子查询没排除。若组内最近一行恰是抢绑失败行，last_seen 取的是更早
    的正常请求、last_ip 却是抢绑方的 IP——同一行两个字段互相打架。
    实测复现：last_seen=10:00 / last_ip=203.0.113.99。
    """
    c, mh, tmp, issue = kb
    import sqlite3
    from pathlib import Path
    import kb_lib.audit as AU

    # 先让设备产生一条正常请求
    assert c.get("/dashboard", headers=mh).status_code == 200

    db = Path(tmp) / ".audit.db"
    assert db.exists(), "审计库不存在，用例前提不成立"
    conn = sqlite3.connect(db)
    dev = conn.execute(
        "SELECT device FROM audit_log WHERE device IS NOT NULL AND device!='' LIMIT 1"
    ).fetchone()[0]
    tkp = conn.execute(
        "SELECT token_prefix FROM audit_log WHERE device=? LIMIT 1", (dev,)
    ).fetchone()[0]
    # 再插一条更晚的抢绑失败行，IP 用一个显眼的值
    conn.execute(
        "INSERT INTO audit_log(time,token_prefix,device,ip,action) VALUES (?,?,?,?,?)",
        ("2999-01-01T00:00:00", tkp, dev, "203.0.113.77", "device_mismatch"),
    )
    conn.commit()
    conn.close()

    r = c.get("/auth/devices", headers=mh)
    assert r.status_code == 200
    rows = [i for i in (r.get_json().get("devices") or [])
            if i.get("device") == dev]
    assert rows, f"设备 {dev} 不在列表里"
    for row in rows:
        assert row.get("last_ip") != "203.0.113.77", \
            f"last_ip 取到了抢绑失败行的 IP，与 last_seen 口径打架：{row}"


def test_C3_令牌长度恒大于12所以输出侧省略号裁剪安全(kb):
    """GLM FINAL_m2a P3 的答复（不是修 bug，是钉死前提）。

    该条担心「长度 ≤12 的令牌整串入库，输出侧只裁带省略号的分支，
    短令牌会把完整凭证外发」。核实前提：所有令牌都由
    secrets.token_urlsafe(32) / (40) 生成，长度恒为 43 / 54 位，
    不存在 ≤12 的令牌，所以 tk[:-3] 之后必为 40 / 51 位，不会整串外发。

    这条把前提钉死：一旦将来有人把令牌生成改成短串，这条会先红。
    输出侧另外加了 [:12] 双保险，不依赖存储侧约定。
    """
    import inspect
    import re
    import kb_lib.store as S
    import kb_lib.routes.routes_auth as RA

    src = inspect.getsource(S) + inspect.getsource(RA)
    lens = {int(m) for m in re.findall(r"secrets\.token_urlsafe\((\d+)\)", src)}
    assert lens, "没找到任何 token_urlsafe 调用，用例前提不成立"
    assert min(lens) > 12, \
        f"存在长度可能 ≤12 的令牌生成（token_urlsafe{min(lens)}），" \
        f"输出侧省略号裁剪不再安全，必须改为显式截断"


def test_C3_已绑定但最近没用过的令牌也要显示出来(kb):
    """GLM FINAL r3 的 P1（真回归）：列表骨架是审计行，每行只挂「最后一次
    使用的前缀」那一条。一把已绑定、但之后没再用过的令牌，不在该设备行的
    最新前缀上，整页看不到它——也就没法对它调 unbind，C1 第 9 条闭环断掉。
    旧实现按 info["device"] 分组能带出该设备下所有活令牌，重写时丢了这张表。
    """
    import sqlite3
    from pathlib import Path
    import kb_lib.store as S

    c, mh, tmp, issue = kb
    r1 = issue("dev-same")          # 要保住的那把（只建不用）
    r2 = issue("dev-same")          # 用它产生审计行
    assert r1 != r2

    # 只让 r2 产生审计行，r1 一次都不打
    assert c.get("/dashboard", headers={"Authorization": f"Bearer {r2}",
                                        "X-KB-Device": "dev-same"}).status_code == 200

    s0 = S.load_store()
    recs = {i.get("prefix"): i for i in s0.get("tokens", {}).values()
            if (i.get("device") or "") == "dev-same"}
    assert len(recs) == 2, f"应有两把同设备令牌，实际 {len(recs)}"

    r = c.get("/auth/devices", headers=mh)
    rows = [i for i in (r.get_json().get("devices") or [])
            if i.get("device") == "dev-same"]
    assert rows, "设备 dev-same 不在列表里"
    shown = {(t.get("prefix") or "") for row in rows for t in (row.get("tokens") or [])}
    missing = set(recs) - shown
    assert not missing, \
        f"同设备名下这些活令牌从页面上消失了：{missing}（页面只有 {shown}）"
def test_C3_同一把令牌不会在一行里重复出现(kb):
    """P1 的另一半：同一令牌被两个审计设备行引用时不能出现两次；
    补挂逻辑不能把已挂上的那条再挂一遍。
    """
    c, mh, tmp, issue = kb
    tk = issue("dup-1")
    assert c.get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 200

    r = c.get("/auth/devices", headers=mh)
    rows = [i for i in (r.get_json().get("devices") or [])
            if i.get("device") == "dup-1"]
    assert rows, "设备 dup-1 不在列表里"
    for row in rows:
        names = [t.get("name") or "" for t in (row.get("tokens") or [])]
        assert names.count("dup-1") <= 1, f"同一把令牌在一行里重复出现：{names}"


def test_C3_无device令牌绑定后不再使用也要可见(kb):
    """GLM FINAL r3 的 P1（本回归最典型的死角）：

    令牌创建时 device 与 name 相同（如 "no-dev"），之后靠 C1 的
    X-KB-Device 请求头绑到**另一台**设备上；绑定之后，这台设备又用别的
    令牌产生过审计行，于是它在设备行上的「最新前缀」不是这把令牌。
      - 审计行的 device 取记录里的 `device` 字段（"no-dev"），不是绑定值；
      - 它也不在该行的最新前缀上。
    若补挂表只按 `device` 建键、不认 `bound_device`，这把令牌整页看不到，
    也就无法对它 unbind。
    """
    import kb_lib.store as S

    c, mh, tmp, issue = kb
    r = c.post("/auth/token", headers=mh,
               json={"name": "no-dev", "permission": "rw"})
    assert r.status_code == 200, r.data[:300]
    tk = r.get_json()["token"]

    s0 = S.load_store()
    key = [k for k, i in s0.get("tokens", {}).items() if i.get("name") == "no-dev"]
    assert key, "没建出令牌"
    key = key[0]
    assert s0["tokens"][key].get("device") == "no-dev", "前提不成立"

    # ① 用这把令牌绑到 dev-bind
    r2 = c.get("/dashboard", headers={"Authorization": f"Bearer {tk}",
                                      "X-KB-Device": "dev-bind"})
    assert r2.status_code == 200
    rec1 = S.load_store()["tokens"][key]
    assert rec1.get("bound_device") == "dev-bind", \
        f"没绑定成功：{rec1.get('bound_device')!r}"
    prefix = rec1.get("prefix")

    # ② 之后这台设备改用另一把令牌产生审计行，把它从「最新前缀」上挤下去
    other = issue("dev-bind")
    assert c.get("/dashboard", headers={"Authorization": f"Bearer {other}",
                                        "X-KB-Device": "dev-bind"}).status_code == 200

    resp = c.get("/auth/devices", headers=mh)
    assert resp.status_code == 200
    rows = [i for i in (resp.get_json().get("devices") or [])
            if i.get("device") == "dev-bind"]
    assert rows, "设备 dev-bind 不在列表里"
    shown = {(t.get("prefix") or "") for row in rows for t in (row.get("tokens") or [])}
    assert prefix in shown, \
        f"绑定后没再用过的令牌从页面上消失了，无法 unbind：{prefix} 不在 {shown}"


def test_C3_合并键不把anon当令牌分支(kb):
    """GLM FINAL r3 的 t2-P1（防御性）：_merge_key 若只判 `if tk:`，
    当 tk 是字面 'anon'（truthy）时会走进令牌分支，把 curl/python/各种壳
    全并成一条。当前 SQL 的 NULLIF 已把 'anon' 归一成 ''，所以端到端走不到
    这条；但判据不该依赖 SQL 层归一穿透，必须在 Python 层也显式排除。

    这里用 AST 把 _merge_key 抠出来单独执行，不依赖端到端能否构造出来。
    """
    import ast
    import textwrap
    import kb_lib.audit as AU

    src = textwrap.dedent(inspect.getsource(AU.query_devices))
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_merge_key":
            fn = node
            break
    assert fn is not None, "query_devices 里找不到 _merge_key，夹具变了？"

    mod = ast.Module(body=[fn], type_ignores=[])
    code = compile(mod, "<merge_key>", "exec")
    ns = {"device_family": AU.device_family}
    exec(code, ns)
    mk = ns["_merge_key"]

    # 字面 'anon' 必须走 UA 族分支，不能当令牌
    k1 = mk({"tk": "anon", "device": "curl/8.4.0"})
    k2 = mk({"tk": "anon", "device": "python-requests/2.31"})
    assert k1[0] == "ua" and k2[0] == "ua", \
        f"字面 anon 被当成令牌了：{k1} / {k2}"
    assert k1 != k2, f"不同 UA 族被并成同一条：{k1}"
    # 空值同样走 UA 分支
    assert mk({"tk": "", "device": "curl/8"})[0] == "ua"
    # 真令牌仍走令牌分支
    assert mk({"tk": "abc123def456", "device": "x"})[0] == "tk"


def test_C3_设备标题恒为设备名不被令牌名顶替(kb):
    """DS FINAL r4 P1（真回归）：旧实现 `it["label"] = device_label(dev)`，
    前端 app.js 用 `esc(it.label||it.device)` 渲染设备名标题。
    若在有令牌时改成 tks[0]["label"]，「这台设备」会显示成「某把令牌的名字」，
    同一设备的标题还会随最近使用哪把令牌而跳变。
    """
    c, mh, tmp, issue = kb
    issue("dev-title")                    # 令牌 name/device 都是 dev-title
    assert c.get("/dashboard",
                 headers={"Authorization": f"Bearer {issue('dev-title')}"}).status_code == 200

    r = c.get("/auth/devices", headers=mh)
    rows = [i for i in (r.get_json().get("devices") or [])
            if i.get("device") == "dev-title"]
    assert rows, "设备 dev-title 不在列表里"
    for row in rows:
        assert row.get("label") == "dev-title", \
            f"设备标题不是设备名：{row.get('label')!r}"


def test_C3_输出侧token_prefix不出现字面anon(kb):
    """DS r5：SQL 里虽然 NULLIF 过 'anon'，输出侧不能只依赖 SQL 归一。

    直接构造一条 token_prefix='anon' 的审计行，走 query_devices，
    断言对外字段是空串而不是 'anon'。

    注意：这条**反向验证不成立**（把输出侧那行去掉测试仍绿），因为 SQL 的
    `NULLIF(NULLIF(token_prefix,''),'anon')` 在查询时就已经把 'anon' 归一成
    空串了，输出侧看到的是 ''。所以它是**第二道保险**，不是唯一防线：
    保留它是为了在将来有人改 SQL 归一（或旧数据绕过归一）时仍不泄漏。
    """
    c, mh, tmp, issue = kb
    import kb_lib.audit as _A
    import sqlite3
    db = sqlite3.connect(str(_A._audit_db()), timeout=10)
    db.execute("INSERT INTO audit_log (time, action, device, token_prefix, ip, status) "
               "VALUES (?,?,?,?,?,?)",
               ("2026-10-19T11:00:00", "read", "anon-dev", "anon", "198.51.100.7", 200))
    db.commit(); db.close()
    rows = [r for r in _A.query_devices() if r.get("device") == "anon-dev"]
    assert rows, "anon-dev 没进设备列表"
    for r in rows:
        assert r.get("token_prefix") != "anon", f"字面 anon 外发了：{r.get('token_prefix')!r}"
        assert r.get("token_prefix") == "", f"anon 应归一成空串：{r.get('token_prefix')!r}"


def test_C3_以省略号结尾的短令牌不被误删后三位(kb):
    """DS r5：`tk.endswith("...")` 单独判截断不可靠。

    短令牌本身以 '...' 结尾时会被误删后三位，输出前缀和令牌记录对不上。
    改用「长度 > 12 且以 ... 结尾」判定：这里造一条 10 位、本身以 ... 结尾的
    前缀，断言输出保留原样（不砍后三位）。
    """
    c, mh, tmp, issue = kb
    import kb_lib.audit as _A
    import sqlite3
    db = sqlite3.connect(str(_A._audit_db()), timeout=10)
    db.execute("INSERT INTO audit_log (time, action, device, token_prefix, ip, status) "
               "VALUES (?,?,?,?,?,?)",
               ("2026-10-19T11:05:00", "read", "dots-dev", "ab...", "198.51.100.8", 200))
    db.commit(); db.close()
    rows = [r for r in _A.query_devices() if r.get("device") == "dots-dev"]
    assert rows, "dots-dev 没进设备列表"
    assert rows[0].get("token_prefix") == "ab...", \
        f"短令牌被误砍后三位：{rows[0].get('token_prefix')!r}"


def test_C3_路由层也按长度判省略号(kb):
    """audit.py 修了 endswith 误砍，路由层有同一份逻辑（routes_auth.py）。

    `token_prefix` 来自 audit.py（已清理，≤12 位），`last_token` 是存储原值
    （>12 位才带 "..."）。若路由层只判 endswith，恰好以 "..." 结尾的前缀
    会被砍成空，by_prefix 查不到 → 令牌挂不上、整行变「已吊销」。
    这里直接断言源码用的是长度+后缀双判据。
    """
    import inspect
    from kb_lib.routes import routes_auth as _RA
    src = inspect.getsource(_RA.auth_devices)
    assert 'raw_prefix.endswith("...")' in src
    assert 'len(raw_prefix) > 12 and raw_prefix.endswith("...")' in src, \
        "路由层仍用裸 endswith 判省略号，会误砍以 ... 结尾的前缀"


def test_C3_同设备多令牌只出一行(kb):
    """GLM r8 P1（真）：验收标准第 1 条是「每台设备一行」。

    audit 层按 (device, token_prefix) 分组，同一台设备用过两把令牌就出两行；
    补挂后每行的 tokens 又是同一份完整集合 —— 设备卡重复展示且完全一样。
    归并后判据：devSAME 只出现一次，uses 是两把令牌求和，tokens 是并集。
    """
    c, mh, tmp, issue = kb
    t1 = issue("tk-a")
    t2 = issue("tk-b")
    for t in (t1, t2, t1):
        c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "devSAME"})

    d = c.get("/auth/devices", headers=mh).get_json()
    rows = [r for r in (d.get("devices") or []) if r.get("device") == "devSAME"]
    assert len(rows) == 1, f"同一设备出了 {len(rows)} 行：{rows}"
    row = rows[0]
    names = sorted(t.get("name") for t in (row.get("tokens") or []))
    assert names == ["tk-a", "tk-b"], f"tokens 没取并集：{names}"
    # /auth/token 创建 2 次 + /dashboard 3 次 = 5，减去 device_bind 2 条 = 3
    assert row.get("uses") == 3, f"uses 没按设备求和：{row.get('uses')}"
    assert row.get("label") == "devSAME", f"标题不是设备名：{row.get('label')!r}"


def test_C3_归并后last_token与token_prefix同口径(kb):
    """GLM R9 h1 P3-5（真）：归并后 last_token 曾是带省略号的 15 位存储原值，
    而 token_prefix 是裸 12 位。两个字段都表示「最后使用的令牌前缀」，
    口径不一致会让前端当成两把不同令牌。判据：两者都 ≤12 位、都不带省略号。"""
    c, mh, tmp, issue = kb
    t1 = issue("tk-x")
    t2 = issue("tk-y")
    c.get("/dashboard", headers={"Authorization": f"Bearer {t1}", "X-KB-Device": "devLT"})
    c.get("/dashboard", headers={"Authorization": f"Bearer {t2}", "X-KB-Device": "devLT"})

    d = c.get("/auth/devices", headers=mh).get_json()
    row = next(r for r in d["devices"] if r.get("device") == "devLT")
    lt = row.get("last_token") or ""
    tp = row.get("token_prefix") or ""
    assert "..." not in lt, f"last_token 还带省略号：{lt!r}"
    assert len(lt) <= 12, f"last_token 超过 12 位：{len(lt)}"
    # 归并后两者必须指向同一把（最新那把）令牌
    assert lt == tp, f"last_token={lt!r} 与 token_prefix={tp!r} 不是同一把"


def test_C3_action为NULL的旧数据不被整行滤掉(kb):
    """GLM R9 h1 P3-4（真）：`NULL NOT IN (...)` 求值为 NULL，
    旧 JSONL 导入的数据 action 为空会被 device_mismatch 的过滤条件整行滤掉。
    改成 COALESCE(action,'') 后，这类设备必须仍能出现在列表里。"""
    import kb_lib.audit as AU
    c, mh, tmp, issue = kb
    t = issue("tk-null")
    c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "devNULL"})

    # 把这条流水改造成「action 为 NULL」的旧数据形态
    db = AU._audit_db()
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("UPDATE audit_log SET action=NULL WHERE device='devNULL'")
    con.commit()
    con.close()

    d = c.get("/auth/devices", headers=mh).get_json()
    devs = [r.get("device") for r in (d.get("devices") or [])]
    assert "devNULL" in devs, f"action 为 NULL 的旧数据被滤掉了：{devs}"


def test_C3_只绑定从未使用的设备也要出现在列表里(kb):
    """GLM R10 h1 第 2 条（实测确认的真缺口）。

    列表骨架是 audit_log 的普通请求流水，device_bind 被过滤掉。
    「绑上之后一次都没正常用过」的令牌在 audit_log 里没有其它行，
    整台设备就不可见、也解不了绑 —— 验收标准第 2 条断掉。
    判据：删掉该设备除 device_bind 外的所有审计行后，它仍要出现在列表里，
    且 uses=0（不伪造使用次数）、bound=True、tokens 含那把令牌。
    """
    import sqlite3
    import kb_lib.audit as AU

    c, mh, tmp, issue = kb
    t = issue("tk-bindonly")
    c.get("/dashboard", headers={"Authorization": f"Bearer {t}", "X-KB-Device": "devBINDONLY"})

    con = sqlite3.connect(str(AU._audit_db()))
    con.execute("DELETE FROM audit_log WHERE device='devBINDONLY' AND action!='device_bind'")
    con.commit()
    con.close()

    d = c.get("/auth/devices", headers=mh).get_json()
    rows = [r for r in (d.get("devices") or []) if r.get("device") == "devBINDONLY"]
    assert len(rows) == 1, f"只绑未用的设备没出现：{[r.get('device') for r in d.get('devices') or []]}"
    r = rows[0]
    assert r.get("uses") == 0, f"不该伪造使用次数：{r.get('uses')}"
    assert r.get("bound") is True, "绑定状态丢了"
    names = [x.get("name") for x in (r.get("tokens") or [])]
    assert names == ["tk-bindonly"], f"令牌没挂上：{names}"
