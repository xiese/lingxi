"""C2 存储层改造回归测试（台账 #23）：Token 哈希化 + 只显示一次。

对应设计 `/srv/notes/.dsh/plans/C1C2C3-design.md` §7.1（C2-1 ~ C2-16）。
所有用例一律跑在临时 KB_ROOT 上，开头有 prod 闸（硬约束 3）。

覆盖的不变量：
    ① 存储里查不到明文令牌，键全部是 64 位十六进制
    ② 正确令牌能过鉴权、错误令牌 401
    ③ 哈希是确定性的（同明文同哈希），不同明文不同哈希
    ④ status 首次返回 token、二次 410；approve 不返回 token
    ⑤ prefix 是哈希前 12 位，能直接吊销（D1 回归）
    ⑥ pepper 缺失自动生成并落盘 0600；文件存在时复用（重启后旧令牌可用）
    ⑦ 迁移脚本：明文键 → 哈希键，原令牌仍能鉴权；幂等；拒绝 prod；先备份
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

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

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _assert_not_prod(root: Path):
    """硬约束 3：.kb-env=prod 一律拒绝运行。"""
    env = root / ".kb-env"
    if env.exists() and env.read_text(encoding="utf-8").strip() == "prod":
        pytest.skip("拒绝在 prod 库上跑")


def _patch_paths_everywhere(tmp: Path, notes: Path):
    from unittest import mock

    values = {
        "KB_ROOT": tmp,
        "NOTES_DIR": notes,
        "TOKEN_FILE": tmp / ".api_tokens.json",
        "LOG_FILE": tmp / ".audit_log.jsonl",
        "SETTINGS_FILE": tmp / ".kb_settings.json",
        "INDEX_FILE": tmp / ".search_index.pkl",
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


@pytest.fixture()
def kb(monkeypatch):
    """隔离 Flask app + 临时 KB_ROOT（pepper 自然落在临时目录，天然隔离）。"""
    monkeypatch.delenv("KB_TOKEN_PEPPER", raising=False)
    monkeypatch.delenv("KB_TOKEN_PEPPER_PREVIOUS", raising=False)

    tmp = Path(tempfile.mkdtemp(prefix="kbc2-"))
    _assert_not_prod(tmp)
    notes = tmp / "笔记"
    notes.mkdir()

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S
    import kb_lib.routes.routes_auth as RA

    patchers = _patch_paths_everywhere(tmp, notes)
    S._PEPPER_CACHE.clear()
    RA._AUTH_REQ_LIMIT.clear()
    try:
        AU._DB_INITED = False   # 换了库根，必须在新目录重建审计表
        AU._init_db()
        S.init_master()
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield {
            "client": c,
            "master": {"Authorization": f"Bearer {master}"},
            "root": tmp,
            "notes": notes,
            "store": S,
            "token_file": tmp / ".api_tokens.json",
        }
    finally:
        for p in reversed(patchers):
            p.stop()
        S._PEPPER_CACHE.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _issue(kb, name="c2-token", perm="r"):
    r = kb["client"].post("/auth/token", json={"name": name, "permission": perm},
                          headers=kb["master"])
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["token"]


def _tokens(kb):
    return json.loads(kb["token_file"].read_text(encoding="utf-8"))["tokens"]


# ── ① 存储里没有明文 ────────────────────────────────────────────────

def test_C2_1_库里查不到明文且键全是64位十六进制(kb):
    tk = _issue(kb)
    raw = kb["token_file"].read_text(encoding="utf-8")
    assert tk not in raw, "store.json 里出现了明文令牌"
    assert tk[:16] not in raw, "store.json 里出现了明文前缀"
    assert tk[-4:] not in raw, "store.json 里出现了明文末四位"
    for k in _tokens(kb):
        assert _HEX64.match(k), f"令牌键不是 64 位十六进制: {k!r}"


def test_C2_1b_记录里不含任何明文派生字段(kb):
    tk = _issue(kb)
    rec = list(_tokens(kb).values())[0]
    assert "token" not in rec
    assert rec["prefix"] == kb["store"].token_hash(tk)[:12]
    assert rec["id"].startswith("tk_") and len(rec["id"]) == 13
    # prefix 是哈希前 12 位，与明文没有任何可还原关系
    assert not tk.startswith(rec["prefix"])


# ── ② 鉴权路径 ──────────────────────────────────────────────────────

def test_C2_2_正确令牌能通过鉴权(kb):
    tk = _issue(kb)
    h = {"Authorization": f"Bearer {tk}"}
    assert kb["client"].get("/dashboard", headers=h).status_code == 200


def test_C2_3_篡改一位令牌返回401(kb):
    tk = _issue(kb)
    bad = tk[:-1] + ("A" if tk[-1] != "A" else "B")
    assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert kb["client"].get("/dashboard", headers={"Authorization": "Bearer 完全不是令牌"}).status_code == 401


def test_C2_3b_哈希是确定性的(kb):
    """同一明文 + 同一 pepper → 同一哈希（迁移后存量令牌仍可用的前提）。

    本实现用的是确定性哈希（sha256(pepper+明文)），不是加盐哈希，
    所以「同明文同哈希」是正确语义；反过来两次签发得到的是两把不同明文，
    因此键自然不同（见下一条）。
    """
    tk = _issue(kb)
    h1 = kb["store"].token_hash(tk)
    h2 = kb["store"].token_hash(tk)
    assert h1 == h2
    assert h1 != kb["store"].token_hash(tk + "x")


def test_C2_3c_两次签发是两把不同令牌与不同哈希(kb):
    t1 = _issue(kb, name="dup")
    t2 = _issue(kb, name="dup")
    assert t1 != t2, "两次签发拿到了同一把明文（随机源有问题）"
    assert kb["store"].token_hash(t1) != kb["store"].token_hash(t2)
    assert len(_tokens(kb)) == 2, "两次签发应各占一条记录"


# ── ③ 只显示一次 ────────────────────────────────────────────────────

def _new_request(kb, name="审批设备"):
    r = kb["client"].post("/auth/request", json={"name": name, "permission": "r"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    return d["request_id"], d["claim"]


def test_C2_10_status首次返回token_二次410(kb):
    rid, claim = _new_request(kb)
    assert kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"]).status_code == 200

    url = f"/auth/request/{rid}/status?claim={claim}"
    first = kb["client"].get(url)
    assert first.status_code == 200
    tk = first.get_json().get("token")
    assert tk, "首次查询没有下发令牌"

    second = kb["client"].get(url)
    assert second.status_code == 410, second.get_data(as_text=True)
    assert second.get_json().get("code") == "claim_consumed"
    assert "token" not in second.get_json()

    # 下发的那把真的能用，且库里只有哈希
    assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 200
    assert tk not in kb["token_file"].read_text(encoding="utf-8")


def test_C2_14_approve不返回token(kb):
    rid, _ = _new_request(kb)
    r = kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    assert r.status_code == 200
    body = r.get_json()
    assert "token" not in body, f"approve 仍返回明文: {body}"
    assert body.get("status") == "approved"
    assert _tokens(kb) == {}, "approve 不该签发令牌（应等客户端首次查询）"


def test_C2_11_claim错误返回403不是410(kb):
    rid, _ = _new_request(kb)
    kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    r = kb["client"].get(f"/auth/request/{rid}/status?claim=错的claim")
    assert r.status_code == 403, r.get_data(as_text=True)


def test_C2_12_领取后claim已消费且令牌入库(kb):
    rid, claim = _new_request(kb)
    kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    kb["client"].get(f"/auth/request/{rid}/status?claim={claim}")
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    rec = data["requests"][rid]
    assert rec["claim_consumed_at"], "claim 未标记消费"
    assert rec["claim_hash"] == "", "claim_hash 未清空"
    assert rec["delivered_at"], "delivered_at 未写入"
    assert len(data["tokens"]) == 1


def test_C2_11b_已消费后错误claim仍返回403不泄露状态(kb):
    """P1-1：已消费时必须先验 claim。

    旧实现先判 410 再验 claim，已消费时任意错误 claim 都能拿到 410；
    配合未消费时的 403，攻击者就能靠状态码区分「这个 rid 存不存在、批没批」。
    """
    rid, claim = _new_request(kb)
    kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    url = f"/auth/request/{rid}/status?claim={claim}"
    assert kb["client"].get(url).status_code == 200, "首次领取应成功"

    # 本人拿原 claim 重放 → 410（认得出是本人，只是来晚了）
    again = kb["client"].get(url)
    assert again.status_code == 410, again.get_data(as_text=True)

    # 换一个错误 claim → 403，与「未消费 + 错 claim」表现一致，状态不可探测
    for bad in ("错的claim", "", claim + "x"):
        r = kb["client"].get(f"/auth/request/{rid}/status?claim={bad}")
        assert r.status_code == 403, (bad, r.status_code, r.get_data(as_text=True))
        assert "claim_consumed" not in r.get_data(as_text=True)

    # 不存在的 rid 仍是 404（与上面 403 一样，都问不出审批状态）
    assert kb["client"].get("/auth/request/req_不存在/status?claim=x").status_code == 404


def test_C2_11c_旧记录只有delivered_at时不再下发token(kb):
    """P1-1 / DS 第 10 条：旧记录可能只有 delivered_at 没有 claim_consumed_at。

    只判 claim_consumed_at 会把这类记录当「没领过」，二次签发一枚新明文令牌，
    与「只显示一次」直接冲突。两个字段任一存在都必须 410。
    """
    rid, claim = _new_request(kb)
    kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    # 造旧形态：有 delivered_at、无 claim_consumed_at，claim_hash 仍是原值
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    rec = data["requests"][rid]
    rec["delivered_at"] = "2026-10-01T00:00:00"
    rec.pop("claim_consumed_at", None)
    kb["token_file"].write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    kb["store"]._invalidate_cache()

    before = len(_tokens(kb))
    r = kb["client"].get(f"/auth/request/{rid}/status?claim={claim}")
    assert r.status_code == 410, r.get_data(as_text=True)
    assert r.get_json().get("code") == "claim_consumed"
    assert "token" not in r.get_json(), "旧记录被二次下发了令牌"
    assert len(_tokens(kb)) == before, "旧记录被二次签发了令牌"


def test_C2_13_并发轮询只签发一份(kb):
    rid, claim = _new_request(kb)
    kb["client"].post(f"/auth/approve/{rid}", headers=kb["master"])
    url = f"/auth/request/{rid}/status?claim={claim}"
    codes = []
    lock = threading.Lock()

    def poll():
        c = kb["client"].application.test_client()
        r = c.get(url)
        with lock:
            codes.append(r.status_code)

    ts = [threading.Thread(target=poll) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(200) == 1, f"应恰好 1 个 200，实际 {codes}"
    assert codes.count(410) == 9, f"其余应为 410，实际 {codes}"
    assert len(_tokens(kb)) == 1, "并发下签发了多把令牌"


# ── ④ 列表口径 & prefix 吊销（D1 回归）────────────────────────────

def test_C2_4_会话列表只给哈希前缀(kb):
    tk = _issue(kb)
    body = kb["client"].get("/auth/sessions", headers=kb["master"]).get_json()
    item = body["sessions"][0]
    assert tk not in json.dumps(body, ensure_ascii=False)
    assert item["prefix"] == kb["store"].token_hash(tk)[:12]
    assert len(item["prefix"]) == 12


def test_C2_5_设备列表不泄露明文与完整哈希(kb):
    tk = _issue(kb)
    kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"})
    raw = kb["client"].get("/auth/devices", headers=kb["master"]).get_data(as_text=True)
    assert tk not in raw
    assert kb["store"].token_hash(tk) not in raw, "设备列表泄露了完整哈希"
    assert _HEX64.search(raw) is None, "设备列表出现了 64 位十六进制串"


def test_C2_6_用返回的prefix能成功吊销(kb):
    """D1 回归：改造前 prefix 是明文前 12 位 + '...'，永远匹配不上。"""
    tk = _issue(kb)
    prefix = kb["client"].get("/auth/sessions", headers=kb["master"]).get_json()["sessions"][0]["prefix"]
    r = kb["client"].delete(f"/auth/revoke/{prefix}", headers=kb["master"])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["revoked"] == 1
    assert _tokens(kb) == {}
    assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 401


def test_C2_6b_带省略号的旧式hint也能吊销(kb):
    tk = _issue(kb)
    prefix = kb["client"].get("/auth/sessions", headers=kb["master"]).get_json()["sessions"][0]["prefix"]
    r = kb["client"].delete(f"/auth/revoke/{prefix}...", headers=kb["master"])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _tokens(kb) == {}


def test_C2_6c_审计日志不记明文前缀(kb):
    tk = _issue(kb)
    kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"})
    import sqlite3
    conn = sqlite3.connect(str(kb["root"] / ".audit.db"))
    rows = [r[0] for r in conn.execute("SELECT token_prefix FROM audit_log")]
    conn.close()
    text = json.dumps(rows, ensure_ascii=False)
    assert tk not in text
    assert tk[:12] not in text, "审计日志记了明文前 12 位"
    # 记的是哈希前缀，正好等于记录里的 prefix 字段（设备页反查靠它）
    assert kb["store"].token_hash(tk)[:12] in text


# ── ⑤ pepper ────────────────────────────────────────────────────────

def test_C2_15_pepper缺失时自动生成且0600(kb):
    root = kb["root"]
    pf = root / ".kb_pepper"
    p1 = kb["store"].get_pepper(root)
    assert pf.exists(), "pepper 没有落盘"
    assert oct(pf.stat().st_mode & 0o777) == "0o600", oct(pf.stat().st_mode & 0o777)
    assert len(p1) == 64
    # 清缓存再取，值不变（同一个文件复用）
    kb["store"]._PEPPER_CACHE.clear()
    assert kb["store"].get_pepper(root) == p1


def test_C2_16_pepper来自环境变量且代码无字面量(kb, monkeypatch):
    tk = _issue(kb)
    h_file = kb["store"].token_hash(tk)

    # P1-2 起，库里有 pepper_id 时换 pepper 会直接报错（防止静默全站 401）。
    # 这条用例只验证「环境变量优先」，所以每次换之前把指纹当老库抹掉。
    def _as_legacy():
        data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
        data.get("meta", {}).pop("pepper_id", None)
        kb["token_file"].write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        kb["store"]._PEPPER_CACHE.clear()
        kb["store"]._PEPPER_ID_CHECKED.clear()

    _as_legacy()
    monkeypatch.setenv("KB_TOKEN_PEPPER", "a" * 64)
    h_a = kb["store"].token_hash(tk)
    _as_legacy()
    monkeypatch.setenv("KB_TOKEN_PEPPER", "b" * 64)
    h_b = kb["store"].token_hash(tk)
    assert len({h_file, h_a, h_b}) == 3, "换 pepper 后哈希没变"

    src = (ROOT / "scripts/kb_lib/store.py").read_text(encoding="utf-8")
    # 代码里不允许出现 pepper 字面量：赋值给 pepper 的只能是环境变量/文件/随机数
    assert not re.search(r"PEPPER\s*=\s*[\"'][0-9a-f]{16,}", src), "store.py 里有 pepper 字面量"


# ── ⑤b P1-2：pepper 漂移必须显式报错，不能静默 401 ──────────────────

def test_C2_17_pepper与库中记录不一致时报错(kb, monkeypatch):
    """迁移脚本写了 meta["pepper_id"]，运行时换 pepper 必须炸，而不是静默全站 401。"""
    tk = _issue(kb)                       # 触发一次哈希，库里落下 pepper_id
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    recorded = data["meta"]["pepper_id"]
    assert recorded, "首次算哈希后没有补写 meta.pepper_id"

    monkeypatch.setenv("KB_TOKEN_PEPPER", "c" * 64)
    kb["store"]._PEPPER_CACHE.clear()
    kb["store"]._PEPPER_ID_CHECKED.clear()
    with pytest.raises(RuntimeError) as ei:
        kb["store"].token_hash(tk)
    assert "pepper 与库中记录不一致" in str(ei.value)
    # 鉴权路径同样炸，不会退化成「查不到 → 401」的静默失败
    # （TESTING=True 下异常直接抛出，等价于生产环境的 500）
    with pytest.raises(RuntimeError):
        kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"})


def test_C2_17b_老库没有pepper_id时补写而不报错(kb):
    """未迁移的老库（meta 无 pepper_id）首次算哈希应补写当前指纹，不报错。"""
    tk = _issue(kb)
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    pid = data["meta"]["pepper_id"]
    assert pid == kb["store"].pepper_id(kb["store"].get_pepper(kb["root"]))
    assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 200


def test_C2_18_pepper文件被并发创建成空文件时不自行生成(kb):
    """P2-1：FileExistsError 后读到空值，只能重试/报错，绝不自己写别人的文件。"""
    root = kb["root"]
    pf = root / ".kb_pepper"
    pf.write_text("", encoding="utf-8")
    kb["store"]._PEPPER_CACHE.clear()
    with pytest.raises(RuntimeError) as ei:
        kb["store"].get_pepper(root)
    assert "内容为空" in str(ei.value)
    assert pf.read_text(encoding="utf-8") == "", "空文件被别人写入了 pepper"


def test_C2_19_pepper文件路径支持字符串根目录(tmp_path):
    """P3：KB_ROOT 传字符串时不能 AttributeError。"""
    import kb_lib.store as S
    assert S._pepper_file(str(tmp_path)) == tmp_path / ".kb_pepper"
    assert S._pepper_file(tmp_path) == tmp_path / ".kb_pepper"



def test_C2_8b_pepper文件存在时复用_重启后旧令牌仍可用(kb):
    tk = _issue(kb)
    pepper = (kb["root"] / ".kb_pepper").read_text(encoding="utf-8").strip()
    # 模拟「重启」：清掉内存里的缓存与 store 缓存，pepper 只能从文件读回来
    kb["store"]._PEPPER_CACHE.clear()
    kb["store"]._invalidate_cache()
    assert kb["store"].get_pepper(kb["root"]) == pepper
    assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 200


# ── ⑥ 迁移脚本 ──────────────────────────────────────────────────────

def _migrate_mod():
    import importlib
    return importlib.import_module("kb_migrate_token_hash")


def _seed_plaintext(kb, tokens):
    """造明文键的存量令牌（迁移前的形态）。"""
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    for tk, perm in tokens.items():
        data["tokens"][tk] = {
            "name": tk, "permission": perm, "device": f"dev-{tk}",
            "created": "2026-10-01T00:00:00", "expires_at": "2099-01-01T00:00:00",
            "status": "active",
        }
    kb["token_file"].write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    kb["store"]._invalidate_cache()


def test_C2_7_迁移脚本把明文键换成哈希且原令牌仍能鉴权(kb):
    plain = {"mig-plain-aaa": "r", "mig-plain-bbb": "rw", "mig-plain-ccc": "r"}
    _seed_plaintext(kb, plain)
    before = len(_tokens(kb))
    assert before == 3

    M = _migrate_mod()
    res = M.migrate(kb["root"], apply=True)
    assert res["status"] == "ok"
    assert res["migrated"] == 3

    toks = _tokens(kb)
    assert len(toks) == 3, "迁移后条数变了"
    for k in toks:
        assert _HEX64.match(k), f"仍有明文键: {k!r}"
    assert set(plain) & set(toks) == set()

    # 关键验收：原明文令牌仍能鉴权（客户端无感）
    for tk in plain:
        assert kb["client"].get("/dashboard", headers={"Authorization": f"Bearer {tk}"}).status_code == 200
    data = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    assert data["meta"]["schema"] == 2
    assert data["meta"]["pepper_id"]


def test_C2_7b_迁移脚本幂等(kb):
    _seed_plaintext(kb, {"mig-idem-1": "r", "mig-idem-2": "rw"})
    M = _migrate_mod()
    M.migrate(kb["root"], apply=True)
    first = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    res2 = M.migrate(kb["root"], apply=True)
    assert res2["status"] == "noop"
    second = json.loads(kb["token_file"].read_text(encoding="utf-8"))
    first["meta"].pop("migrated_at", None)
    second["meta"].pop("migrated_at", None)
    assert first == second


def test_C2_8_迁移脚本拒绝prod库(kb):
    _seed_plaintext(kb, {"mig-prod-1": "r"})
    (kb["root"] / ".kb-env").write_text("prod", encoding="utf-8")
    before = kb["token_file"].read_text(encoding="utf-8")
    M = _migrate_mod()
    with pytest.raises(RuntimeError):
        M.migrate(kb["root"], apply=True)
    assert kb["token_file"].read_text(encoding="utf-8") == before, "prod 库被改写了"
    assert M.main(["--kb-root", str(kb["root"])]) == 2


def test_C2_9_迁移脚本先备份且权限0600(kb):
    _seed_plaintext(kb, {"mig-bak-1": "r"})
    before = kb["token_file"].read_text(encoding="utf-8")
    M = _migrate_mod()
    res = M.migrate(kb["root"], apply=True)
    bak = Path(res["backup"])
    assert bak.exists()
    assert oct(bak.stat().st_mode & 0o777) == "0o600"
    assert bak.read_text(encoding="utf-8") == before, "备份内容与迁移前不一致"


def test_C2_9b_dry_run不写盘(kb):
    _seed_plaintext(kb, {"mig-dry-1": "r"})
    before = kb["token_file"].read_text(encoding="utf-8")
    M = _migrate_mod()
    res = M.migrate(kb["root"], apply=False)
    assert res["status"] == "dry-run" and res["migrated"] == 1
    assert kb["token_file"].read_text(encoding="utf-8") == before


def test_C2_20_锁外读pepper_id失败时不许标记为已校验(kb, monkeypatch):
    """P2-新回归：锁外读 meta 失败（并发写盘读到半截）不能当成「库里没有指纹」。

    旧实现：读失败 → recorded="" → 进补写事务 → 锁内发现实际有值只跳过写入、
    不比对 → 随后把错误 pepper 塞进 _PEPPER_ID_CHECKED，此后永不比对。
    """
    import json as _json

    tk = _issue(kb)                                   # 落下 pepper_id
    recorded = _json.loads(kb["token_file"].read_text(encoding="utf-8"))["meta"]["pepper_id"]
    assert recorded

    monkeypatch.setenv("KB_TOKEN_PEPPER", "d" * 64)
    S = kb["store"]
    S._PEPPER_CACHE.clear()
    S._PEPPER_ID_CHECKED.clear()

    # 让「锁外那一次读」失败：直接把这个函数读文件前的状态下手——
    # 通过把 json.loads 打桩，使锁外读抛异常，锁内读（同一函数第二次调用）放行。
    real_loads = _json.loads
    seen = {"n": 0}

    def flaky_loads(txt, *a, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ValueError("模拟读到半截文件")
        return real_loads(txt, *a, **kw)

    monkeypatch.setattr(_json, "loads", flaky_loads)
    with pytest.raises(RuntimeError) as ei:
        S.token_hash(tk)
    monkeypatch.setattr(_json, "loads", real_loads)
    assert "pepper 与库中记录不一致" in str(ei.value)
    # 关键断言：失败那一次不许被缓存成「已校验」
    assert not any(pid != recorded for _, pid in S._PEPPER_ID_CHECKED)


def test_C2_21_锁内复核也失败时不许标记已校验(kb, monkeypatch):
    """P2-新的第二种绕过：锁外读失败 + 锁内复核也失败（load_store 抛错）。

    旧实现 except Exception 只打 warning，流程照旧落到 add(key)——
    一次比对都没成功，pepper 却被永久当成「已验证」，回到全站静默 401。
    """
    import json as _json

    tk = _issue(kb)
    recorded = _json.loads(kb["token_file"].read_text(encoding="utf-8"))["meta"]["pepper_id"]
    assert recorded

    monkeypatch.setenv("KB_TOKEN_PEPPER", "e" * 64)
    S = kb["store"]
    S._PEPPER_CACHE.clear()
    S._PEPPER_ID_CHECKED.clear()

    real_loads = _json.loads
    seen = {"n": 0}

    def flaky_loads(txt, *a, **kw):
        seen["n"] += 1
        raise ValueError("模拟读盘持续失败")

    monkeypatch.setattr(_json, "loads", flaky_loads)
    try:
        S.token_hash(tk)          # 不 raise（本次没校验成），但绝不能标记为已校验
    except RuntimeError:
        pass
    monkeypatch.setattr(_json, "loads", real_loads)

    assert not any(pid != recorded for _, pid in S._PEPPER_ID_CHECKED), (
        "锁内复核失败时把错误 pepper 标记成了已校验（fail-open）"
    )


def test_C2_22_事务内复核失败不许留下已校验标记(kb, monkeypatch):
    """P2-残余第三种：pending 分支（_TX_ACTIVE.on=True）在复核之前就 add(key)。

    该路径只在「锁外读失败」时可达：锁外读成功且不一致，148 行当场 raise，
    进不了 pending 分支。所以造数是「锁外读抛异常 + 锁内读到不一致」。
    """
    import json as _json

    tk = _issue(kb)
    S = kb["store"]

    # 阶段一：事务内首次算哈希 → pending 分支把 pepper_id 补写落盘
    with S.store_transaction() as st:
        S.token_hash(tk)
    _real = _json.loads
    recorded = _real(kb["token_file"].read_text(encoding="utf-8"))["meta"]["pepper_id"]
    assert recorded

    # 阶段二：换 pepper + 清缓存；让「锁外那次读」抛异常
    monkeypatch.setenv("KB_TOKEN_PEPPER", "f" * 64)
    S._PEPPER_CACHE.clear()
    S._PEPPER_ID_CHECKED.clear()

    real_loads = _json.loads

    def flaky(txt, *a, **kw):
        # 只让「未持锁时」那次读失败（store_transaction 内部 load_store 是持锁读，放行）
        if not getattr(S._TX_ACTIVE, "on", False):
            raise ValueError("模拟读到半截文件")
        return real_loads(txt, *a, **kw)

    monkeypatch.setattr(_json, "loads", flaky)
    with pytest.raises(RuntimeError):
        with S.store_transaction() as st:
            S.token_hash(tk)
    monkeypatch.setattr(_json, "loads", real_loads)

    leaked = {k for k in S._PEPPER_ID_CHECKED if k[1] != recorded}
    assert not leaked, f"复核失败却留下已校验标记：{leaked}"
    assert not getattr(S._PEPPER_PENDING, "pid", None)


def test_C2_23_事务内抛异常后pending不残留(kb):
    """DS P1：调用方在 with 块内抛异常时，pending 必须被清掉。

    否则 threading.local 残留会被同线程下一个无关事务顺手落盘并标记。
    """
    S = kb["store"]
    with pytest.raises(ValueError):
        with S.store_transaction() as s:
            S._PEPPER_PENDING.pid = "deadbeef"
            S._PEPPER_PENDING.keys = {("/x", "deadbeef")}
            raise ValueError("调用方炸了")
    assert not getattr(S._PEPPER_PENDING, "pid", None), "yield 抛异常后 pending.pid 残留"
    assert not getattr(S._PEPPER_PENDING, "keys", set()), "yield 抛异常后 pending.keys 残留"


def test_C2_24_save失败时不许留下已校验标记(kb, monkeypatch):
    """GLM #1：标记必须晚于落盘。save_store 抛错时不能认「已校验」。"""
    import json as _json

    tk = _issue(kb)
    S = kb["store"]
    recorded = _json.loads(kb["token_file"].read_text(encoding="utf-8"))["meta"]["pepper_id"]
    assert recorded

    def boom(_s):
        raise OSError("磁盘满")

    monkeypatch.setattr(S, "save_store", boom)
    S._PEPPER_ID_CHECKED.clear()
    with pytest.raises(OSError):
        with S.store_transaction() as st:
            S._PEPPER_PENDING.pid = recorded
            S._PEPPER_PENDING.keys = {("/y", recorded)}
    assert not S._PEPPER_ID_CHECKED, "save_store 失败却把 key 标记成已校验（假绿）"
