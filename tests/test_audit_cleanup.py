"""审计日志清理与按类筛选测试（2026-10-18 第 4 项改造）。

锁死的不变量：
    ① 分类口径按 **method 列** 分读写，不按 action 名前缀猜
       （生产 action 是 notes/search/index，没有 read*/write* 这种前缀）
    ② `cleanup/preview` 给的「将删除 X 条」必须与真正删掉的条数一致
       —— 预览说 3 条、实际删 5 条，等于拿错数字骗维护者点确认
    ③ 全清必须显式 `confirm=true`，漏传就 400，不许默认清空
    ④ 备份是**勾选框**：不传 backup 就不落文件；传了才在 `_assets/` 生成 JSON，
       且备份里的条数必须等于随后删除的条数
    ⑤ 非法参数（days 非整数 / days<1 / mode 未知）一律 400，不许 500
    ⑥ 清理后总数真的减少，不是只回了个 ok

隔离方式照抄 tests/test_dashboard_counts.py。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_PATH_CONSTS = {
    "KB_ROOT": None,
    "NOTES_DIR": None,
    "TOKEN_FILE": None,
    "LOG_FILE": None,
    "SETTINGS_FILE": None,
    "INDEX_FILE": None,
    "EMBEDDINGS_FILE": None,
    "FAISS_FILE": None,
    "INDEX_MANIFEST_FILE": None,
    "MCP_USAGE_LOG": None,
}


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
def client():
    tmp = Path(tempfile.mkdtemp(prefix="kbaudit-"))
    notes = tmp / "笔记"
    notes.mkdir()

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    try:
        # _DB_INITED 是模块级全局：上一个用例已置 True 时，这里再调 _init_db()
        # 会直接 return，表就建在旧 tmp 目录里，本用例拿到的是空库。
        AU._DB_INITED = False
        AU._init_db()
        S.init_master()

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, tmp
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _seed(days_ago_map):
    """按「几天前」写若干条日志，method 覆盖 GET/POST/其他。

    days_ago_map: [(天数, action, method, path, 条数), ...]
    """
    import kb_lib.audit as AU
    from datetime import datetime, timedelta

    for days, action, method, path, n in days_ago_map:
        t = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        conn = AU._get_db()
        for _ in range(n):
            conn.execute(
                "INSERT INTO audit_log(time,token_prefix,device,ip,action,path,method,status,extra)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (t, "test", "dev", "127.0.0.1", action, path, method, 200, ""),
            )
        conn.close()


def _total(client):
    """只数测试自己 seed 进去的 notes 行。

    不能直接用 /admin/audit/stats 的 total —— 每次请求本身也会写一条审计日志，
    断言 total 会随「调用了几次接口」漂移，跟清理逻辑无关。
    """
    import kb_lib.audit as AU
    conn = AU._get_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='notes'").fetchone()[0]
    conn.close()
    return n


def test_分类按method列分读写不按action名猜(client):
    """action 全是 notes/search 这种真名，读写只能靠 method 判。

    期望值从库里直接数，不看接口返回——每次请求本身都会写一条审计日志，
    直接断言固定数字会随「这个用例调了几次接口」漂移，那是假绿。
    """
    _seed([
        (0, "notes", "GET", "/notes/x", 3),
        (0, "notes", "POST", "/notes/x", 2),
        (0, "auth", "GET", "/auth/login", 4),
        (0, "cron", "PING", "/internal/tick", 1),
    ])
    import kb_lib.audit as AU
    conn = AU._get_db()
    # 期望值必须复刻生产口径（auth 优先，读写都排除 auth 行），
    # 否则测的是我自己写的 SQL，不是被测逻辑。
    AUTH = "(action='auth' OR path LIKE '/auth/%')"
    exp_read = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE method='GET' AND NOT {AUTH}").fetchone()[0]
    exp_write = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE method IN ('POST','PUT','PATCH','DELETE') AND NOT {AUTH}").fetchone()[0]
    exp_auth = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE {AUTH}").fetchone()[0]
    exp_sys = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE NOT {AUTH} AND method NOT IN ('GET','POST','PUT','PATCH','DELETE')").fetchone()[0]
    conn.close()
    # 四类之和必须等于表总行数：任何一类漏算，都会在这里暴露。
    total = conn_total = exp_read + exp_write + exp_auth + exp_sys

    c, h, _ = client
    # 先数表总行数，再请求：这次 GET 本身会在响应之后才写审计行，
    # 先请求后数会多出 1 行，让「四类之和 = 总行数」恒不成立。
    import kb_lib.audit as AU2
    conn = AU2._get_db()
    rows = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    cats = c.get("/admin/audit/stats", headers=h).get_json()["categories"]
    assert cats["read"] + cats["write"] + cats["auth"] + cats["system"] == rows, (cats, rows)
    assert cats["read"] == exp_read, (cats, exp_read)
    assert cats["write"] == exp_write, (cats, exp_write)
    assert cats["auth"] == exp_auth, (cats, exp_auth)
    assert cats["system"] == exp_sys, (cats, exp_sys)
    # 四类之和 = 表总行数：任何一类漏算，界面上就有记录永远筛不到。


def test_按类筛选只返回该类(client):
    _seed([
        (0, "notes", "GET", "/notes/x", 3),
        (0, "notes", "POST", "/notes/x", 2),
    ])
    c, h, _ = client
    d = c.get("/admin/audit?category=write", headers=h).get_json()
    assert d["total"] == 2, d
    assert all(e["method"] == "POST" for e in d["entries"]), d["entries"]


def test_未知分类回400(client):
    c, h, _ = client
    r = c.get("/admin/audit?category=不存在的类", headers=h)
    assert r.status_code == 400, r.get_data(as_text=True)[:200]


def test_预览条数与实删条数一致(client):
    """预览说几条就必须删几条——不一致等于用假数字骗确认。"""
    _seed([
        (10, "notes", "GET", "/notes/x", 3),
        (2, "notes", "GET", "/notes/y", 2),
    ])
    c, h, _ = client
    before = _total(client)
    assert before == 5

    pv = c.get("/admin/audit/cleanup/preview?days=7", headers=h).get_json()
    assert pv["will_delete"] == 3, pv

    r = c.post("/admin/audit/cleanup", json={"mode": "days", "days": 7, "backup": False}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["deleted"] == pv["will_delete"], r.get_json()
    assert _total(client) == before - pv["will_delete"]


def test_全清必须显式confirm(client):
    _seed([(0, "notes", "GET", "/notes/x", 2)])
    c, h, _ = client
    r = c.post("/admin/audit/cleanup", json={"mode": "all"}, headers=h)
    assert r.status_code == 400, r.get_data(as_text=True)[:200]
    assert _total(client) == 2, "没传 confirm 却把日志清了"

    r = c.post("/admin/audit/cleanup", json={"mode": "all", "confirm": True}, headers=h)
    assert r.status_code == 200
    # 断言「seed 进去的行真的没了」，不写绝对数：
    # 前一次被拒的请求本身也写了一条审计记录，绝对数会随调用次数漂移。
    assert r.get_json()["deleted"] >= 2, r.get_json()
    assert _total(client) == 0


def test_备份勾选才落文件且条数对得上(client):
    _seed([(10, "notes", "GET", "/notes/x", 4)])
    c, h, tmp = client
    r = c.post("/admin/audit/cleanup", json={"mode": "days", "days": 7, "backup": True}, headers=h)
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["backup_path"], body
    fp = tmp / body["backup_path"]
    assert fp.exists(), body["backup_path"]
    rows = json.loads(fp.read_text(encoding="utf-8"))
    assert len(rows) == body["deleted"], (len(rows), body)
    assert all(row["method"] == "GET" for row in rows)


def test_备份必须包含全部待删记录不截断(client):
    """审查点出的隐患：export_rows 曾有 limit=200000 硬上限。

    备份的意义是「删了还能找回」，少导一条就等于那一条被删了没留底。
    这里造 260 条待删记录，断言备份文件里的条数与实删条数严格相等。
    """
    _seed([(30, "notes", "GET", "/notes/x", 1) for _ in range(260)])
    c, h, tmp = client
    before = c.get("/admin/audit/cleanup/preview?days=7", headers=h).get_json()["will_delete"]
    assert before >= 260, before
    r = c.post("/admin/audit/cleanup", json={"mode": "days", "days": 7, "backup": True}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["backup_path"], body
    rows = json.loads((tmp / body["backup_path"]).read_text(encoding="utf-8"))
    assert len(rows) == body["deleted"], (len(rows), body["deleted"])
    assert len(rows) >= 260, len(rows)


def test_不勾备份就不落文件(client):
    _seed([(10, "notes", "GET", "/notes/x", 2)])
    c, h, tmp = client
    r = c.post("/admin/audit/cleanup", json={"mode": "days", "days": 7, "backup": False}, headers=h)
    assert r.status_code == 200
    assert r.get_json()["backup_path"] is None
    assert not (tmp / "_assets").exists(), "没勾备份却写了 _assets 文件"


@pytest.mark.parametrize("body", [
    {"mode": "days", "days": "七天"},
    {"mode": "days", "days": 0},
    {"mode": "days", "days": -3},
    {"mode": "unknown"},
    {},
])
def test_非法清理参数一律400(client, body):
    _seed([(0, "notes", "GET", "/notes/x", 1)])
    c, h, _ = client
    r = c.post("/admin/audit/cleanup", json=body, headers=h)
    assert r.status_code == 400, (body, r.status_code, r.get_data(as_text=True)[:200])
    assert _total(client) == 1, f"{body} 把日志删了"


def test_分页参数非法回400不崩(client):
    """page=abc 必须 400；limit 越界按既有语义裁剪到 [1,200]，不许 500。"""
    c, h, _ = client
    assert c.get("/admin/audit?page=abc", headers=h).status_code == 400
    for q in ("limit=-1", "limit=99999"):
        r = c.get(f"/admin/audit?{q}", headers=h)
        assert r.status_code == 200, (q, r.status_code)
