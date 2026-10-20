# -*- coding: utf-8 -*-
"""第 5 项：正文本地路径探测（POST /api/path-probe）的接口测试。

为什么要有这个接口：正文里写的 `/srv/notes/示例/docs/a.md` 到底是「本机能点开的笔记」
还是「别的服务器上的文件」，光看字符串分不出来。维护者否掉了「按文件名在库里搜」
的方案 —— 库里搜到同名笔记 ≠ 本机真有这个文件，点进去是空的。
所以口径是**不猜，只 stat**：可点的一定是本机真实存在且在库内的文件。

锁死不变量：
    ①  本机存在且在库内 → kind=local_note，返回库内相对路径（可点）
    ②  本机存在但不在库内 → kind=local_file，note=None（不可点）
    ③  本机不存在 → kind=remote，note=None（不可点）
    ④  行号/章节尾巴（`:12`、`#L9`、` 第 2~18 章`）先剥再 stat，不能因此误判成 remote
    ⑤  只认绝对路径：相对写法 `./docs/a.md` → kind=invalid
    ⑥  paths 不是数组 → 400
    ⑦  paths 为空 → 200 且 probed 为空（不是 400）
    ⑧  重复路径只返回一次；超过 300 条被截断
    ⑨  无 token → 401
    ⑩  other_server_prefixes 只改徽标文字，不改 kind（配了前缀但本机存在，仍然 local_*）
    ⑪  目录算 local_file，不是 remote
    ⑫  权限不足（stat 抛 PermissionError）算 local_file，不能算 remote

隔离方式照抄 tests/test_links_resolve_batch.py。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 造「本机不存在」的路径不能写死 /srv/notes/...：CI runner 不是 root，
# stat("/srv/notes/xxx") 会先抛 PermissionError，被正确判成 local_file（本机存在但读不了），
# 用例就假失败。这里挑一个当前进程一定没有权限、且一定不存在的根级前缀。
def _pick_ghost_root() -> str:
    for cand in ("/kb-ghost-not-exist", "/nonexistent-kb-root", "/var/empty/kb-ghost"):
        if not os.path.exists(cand):
            return cand
    return "/kb-ghost-not-exist"


GHOST_ROOT = _pick_ghost_root()

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


def _write(p: Path, text: str = "# x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def client():
    """临时笔记库 + 隔离 Flask 应用（不碰生产数据）。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbprobe-"))
    notes = tmp / "笔记"
    notes.mkdir()

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    try:
        AU._init_db()
        S.init_master()

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, notes, tmp
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _probe(c, h, paths):
    return c.post("/api/path-probe", headers=h, json={"paths": paths})


# ── ①②③ 三种 kind ──

def test_库内笔记返回可跳转的相对路径(client):
    c, h, notes, _ = client
    fp = _write(notes / "20-知识" / "有这篇.md")
    r = _probe(c, h, [str(fp)])
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    d = r.get_json()["probed"][str(fp)]
    assert d["kind"] == "local_note"
    assert d["note"] == "20-知识/有这篇.md"


def test_本机文件但不在库内_不可点(client):
    c, h, notes, tmp = client
    fp = _write(tmp / "外部目录" / "脚本.py", "print(1)")
    r = _probe(c, h, [str(fp)])
    d = r.get_json()["probed"][str(fp)]
    assert d["kind"] == "local_file"
    assert d["note"] is None


def test_本机不存在_标为其他服务器(client):
    c, h, _, _ = client
    # 不能用 /srv/notes/... 造「不存在」：CI runner 不是 root，stat 会先抛
    # PermissionError（那是「本机存在但读不了」=local_file），用例就假失败了。
    ghost = GHOST_ROOT + "/根本不存在的目录/x.md"
    r = _probe(c, h, [ghost])
    d = r.get_json()["probed"][ghost]
    assert d["kind"] == "remote"
    assert d["note"] is None


# ── ④ 行号/章节尾巴 ──

@pytest.mark.parametrize("tail", [":12", ":12-18", "#L9", "#L9-L20", " 第 2~18 章", " 第 3 节"])
def test_带行号章节后缀仍能认成本机文件(client, tail):
    c, h, notes, _ = client
    fp = _write(notes / "20-知识" / "带尾巴.md")
    raw = str(fp) + tail
    r = _probe(c, h, [raw])
    d = r.get_json()["probed"][raw]
    # 关键：不能因为整串 stat 失败就误判成 remote
    assert d["kind"] == "local_note", d
    assert d["clean"] == str(fp)
    assert d["tail"].strip() == tail.strip()


# ── ⑤ 只认绝对路径 ──

@pytest.mark.parametrize("rel", ["./docs/a.md", "docs/a.md", "../a.md", "a.md"])
def test_相对路径不识别(client, rel):
    c, h, _, _ = client
    r = _probe(c, h, [rel])
    assert r.get_json()["probed"][rel]["kind"] == "invalid"


# ── ⑥⑦⑧ 入参边界 ──

def test_paths不是数组返回400(client):
    c, h, _, _ = client
    r = c.post("/api/path-probe", headers=h, json={"paths": "/srv/notes/a.md"})
    assert r.status_code == 400


def test_paths为空返回200空结果(client):
    c, h, _, _ = client
    r = _probe(c, h, [])
    assert r.status_code == 200
    assert r.get_json()["probed"] == {}


def test_重复路径只算一次(client):
    c, h, notes, _ = client
    fp = str(_write(notes / "a.md"))
    r = _probe(c, h, [fp, fp, fp])
    assert r.get_json()["count"] == 1


def test_超过300条被截断(client):
    c, h, _, _ = client
    paths = ["/srv/notes/fake%d.md" % i for i in range(400)]
    r = _probe(c, h, paths)
    assert r.get_json()["count"] == 300


# ── ⑨ 鉴权 ──

def test_无token返回401(client):
    c, _, _, _ = client
    r = c.post("/api/path-probe", json={"paths": ["/srv/notes/a.md"]})
    assert r.status_code == 401


# ── ⑩ 前缀配置只改文字 ──

def test_服务器前缀只改徽标名不改可点性(client):
    c, h, notes, tmp = client
    fp = str(_write(notes / "a.md"))
    # 前缀命中本机真实存在的文件：名字换成 远程服务器，但仍然是 local_note（能点）
    from kb_lib.routes import routes_notes as RN
    # get_setting 已被直接 import 进 routes_notes 命名空间，patch 源头无效，必须 patch 这里
    pref = str(tmp) + "|远程服务器"
    with mock.patch.object(RN, "get_setting", lambda k, d=None: pref if k == "other_server_prefixes" else (d if d is not None else "")):
        r = _probe(c, h, [fp])
    d = r.get_json()["probed"][fp]
    assert d["server"] == "远程服务器"
    assert d["kind"] == "local_note"


def test_不存在但命中前缀_仍为remote(client):
    c, h, _, _ = client
    ghost = GHOST_ROOT + "/没有这个/x.md"
    from kb_lib.routes import routes_notes as RN
    with mock.patch.object(RN, "get_setting", lambda k, d=None: GHOST_ROOT + "|远程服务器" if k == "other_server_prefixes" else (d if d is not None else "")):
        r = _probe(c, h, [ghost])
    d = r.get_json()["probed"][ghost]
    assert d["kind"] == "remote"
    assert d["server"] == "远程服务器"


# ── ⑪ 目录 ──

def test_目录算本机文件不算其他服务器(client):
    c, h, notes, _ = client
    d0 = notes / "20-知识"
    d0.mkdir(parents=True, exist_ok=True)
    r = _probe(c, h, [str(d0)])
    assert r.get_json()["probed"][str(d0)]["kind"] == "local_file"


# ── ⑫ 权限不足 ──

def test_权限不足算本机文件不算其他服务器(client):
    c, h, _, _ = client
    from kb_lib.routes import routes_notes as RN
    with mock.patch.object(RN.os, "stat", side_effect=PermissionError("denied")):
        out = RN._probe_one("/srv/notes/有权限问题的.md", [])
    assert out["kind"] == "local_file"

def test_章节后缀也能剥掉再stat(client):
    """原文明确要求支持「第 2~18 章」这类章节后缀（不只是行号）。"""
    c, h, notes, tmp = client
    fp = _write(notes / "a.md")
    r = c.post("/api/path-probe", json={"paths": [str(fp) + " 第 2~18 章", str(fp) + "第3章"]}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    for p in [str(fp) + " 第 2~18 章", str(fp) + "第3章"]:
        got = r.get_json()["probed"][p]
        assert got["clean"] == str(fp), got
        assert got["kind"] == "local_note", got
        assert got["tail"] in (" 第 2~18 章", "第3章"), got

def test_中文引号与破折号不被吃进路径(client):
    """正文里常写「见 “/srv/notes/a.md”」或「/srv/notes/a.md——说明」，标点不能算进路径。"""
    c, h, notes, tmp = client
    fp = _write(notes / "a.md")
    from kb_lib.routes.routes_notes import _ABS_PATH_RE
    for text in ['见 “%s” 这句' % fp, '%s——后面' % fp, '（%s）' % fp]:
        got = _ABS_PATH_RE.findall(text)
        assert got == [str(fp)], (text, got)

def test_库内软链指向库外不算可点(client):
    """库内放一个指向 /etc 的软链，不能因为路径在库内就当成可点笔记。"""
    c, h, notes, tmp = client
    link = notes / "evil.md"
    try:
        os.symlink("/etc/hostname", str(link))
    except (OSError, NotImplementedError):
        return
    r = c.post("/api/path-probe", json={"paths": [str(link)]}, headers=h)
    d = r.get_json()["probed"][str(link)]
    assert d["kind"] == "local_file", d
    assert d["note"] is None


# ─── 双模型审查整改（2026-10-18 维护者说「1 2 3 一起处理」）───

def test_隐藏路径不探测(client):
    """审查第 2 条：持有 r 权限 token 就能 stat 任意绝对路径 = 信息泄露探针。

    `/srv/notes/.ssh/id_rsa` 这类隐藏路径一律 invalid（前端不画徽标）。
    """
    c, h, notes, tmp = client
    cases = ["/srv/notes/.ssh/id_rsa", "/srv/notes/.kb_settings.json",
             "/srv/notes/.api_tokens.json", "/srv/notes/知识库/.git/config"]
    r = c.post("/api/path-probe", json={"paths": cases}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    for p in cases:
        assert r.get_json()["probed"][p]["kind"] == "invalid", p


def test_隐藏路径根本没去stat(client):
    """不只是结果标 invalid —— 必须真的没 stat 过。

    直接测 _probe_one（不经过 Flask），避免把框架自己的 stat 算进来。
    """
    from kb_lib.routes.routes_notes import _probe_one
    real_stat = os.stat
    calls = []

    def spy(p, *a, **kw):
        calls.append(str(p))
        return real_stat(p, *a, **kw)

    with mock.patch("kb_lib.routes.routes_notes.os.stat", side_effect=spy):
        d = _probe_one("/srv/notes/.ssh/id_rsa", [])
    assert d["kind"] == "invalid", d
    assert calls == [], "隐藏路径被 stat 了：%r" % calls


def test_普通路径仍能探测不受隐藏路径限制影响(client):
    """加闸不能误伤正常路径：临时库路径里没有 `.` 开头的段，照常 local_note。"""
    c, h, notes, tmp = client
    fp = _write(notes / "a.md")
    r = c.post("/api/path-probe", json={"paths": [str(fp)]}, headers=h)
    d = r.get_json()["probed"][str(fp)]
    assert d["kind"] == "local_note", d
    assert d["note"] == "a.md"


def test_路径形状不符不stat(client):
    """审查第 3 条：`_ABS_PATH_RE` 曾是死代码。现在拿它做接口自保。

    含空格、引号、反引号的字符串不是路径，直接 invalid。
    """
    from kb_lib.routes.routes_notes import _probe_one
    real_stat = os.stat
    calls = []

    def spy(p, *a, **kw):
        calls.append(str(p))
        return real_stat(p, *a, **kw)

    bad = ["/srv/notes/a b.md", "/srv/notes/a'b.md", "/srv/notes/a`b.md", "/srv/notes/a<b.md"]
    with mock.patch("kb_lib.routes.routes_notes.os.stat", side_effect=spy):
        got = [_probe_one(p, [])["kind"] for p in bad]
    assert got == ["invalid"] * len(bad), got
    assert calls == [], "非法形状被 stat 了：%r" % calls


def test_正常形状照常通过形状校验(client):
    """形状校验不能误伤中文路径和带点的文件名。"""
    from kb_lib.routes.routes_notes import _probe_one
    d = _probe_one("/srv/notes/知识库/笔记/a.b.md", [])
    assert d["kind"] != "invalid", d
