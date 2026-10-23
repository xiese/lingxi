# -*- coding: utf-8 -*-
"""批 3：move 接口 + 写入乐观锁（etag）的测试。

覆盖：
- etag：16 位、跨进程稳定、读写两侧一致
- check_precondition：If-Match / base_hash / 都不给 / 不匹配 / 完整 sha256
- move：dry_run 不落盘、目标已存在 409、路径穿越 400、源不存在 404、
        正常移动后源消失目标出现、update_links 改双链
- 批量 move：部分成功语义
- 乐观锁端到端：GET 拿 etag → 带对/错 base_hash 写
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from kb_lib.services.etag import content_etag, check_precondition  # noqa: E402


# ── etag 纯函数 ──

def test_etag_长度十六位十六进制():
    e = content_etag("hello")
    assert len(e) == 16
    assert all(c in "0123456789abcdef" for c in e)


def test_etag_同内容稳定_不同内容不同():
    a = "---\ntitle: x\n---\n正文"
    assert content_etag(a) == content_etag(a)
    assert content_etag(a) != content_etag(a + "!")


def test_etag_已知值_锁定实现不漂移():
    """锁死实现：sha256('abc') 前 16 位。改算法必须同步改这里。"""
    import hashlib
    assert content_etag("abc") == hashlib.sha256(b"abc").hexdigest()[:16]


def test_etag_处理None与bytes():
    assert content_etag(None) == content_etag("")
    assert content_etag(b"abc") == content_etag("abc")


def test_etag_跨进程稳定_不受PYTHONHASHSEED影响():
    """etag 不能像内置 hash() 那样随进程变化——否则跨进程比对失效。"""
    import subprocess
    code = ("import sys;sys.path.insert(0,%r);"
            "from kb_lib.services.etag import content_etag;"
            "print(content_etag('稳定性测试'))" % str(ROOT / "scripts"))
    outs = set()
    for seed in ("0", "1", "12345"):
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert len(outs) == 1
    assert outs.pop() == content_etag("稳定性测试")


# ── check_precondition ──

def test_不带版本号_放行且provided为None():
    ok, exp, prov = check_precondition("内容")
    assert ok is True and prov is None and exp == content_etag("内容")


def test_base_hash匹配_放行():
    ok, exp, prov = check_precondition("内容", base_hash=content_etag("内容"))
    assert ok is True and prov == exp


def test_IfMatch匹配_放行_带引号与W前缀也认():
    e = content_etag("内容")
    for v in (e, '"%s"' % e, 'W/"%s"' % e):
        ok, _, prov = check_precondition("内容", if_match=v)
        assert ok is True, v
        assert prov == e


def test_版本不匹配_拒绝():
    ok, exp, prov = check_precondition("新内容", base_hash=content_etag("旧内容"))
    assert ok is False
    assert exp == content_etag("新内容")
    assert prov == content_etag("旧内容")


def test_IfMatch优先于base_hash():
    ok, _, prov = check_precondition(
        "内容", if_match="deadbeefdeadbeef", base_hash=content_etag("内容"))
    assert ok is False and prov == "deadbeefdeadbeef"


def test_接受完整sha256():
    import hashlib
    ok, _, _ = check_precondition("内容", base_hash=hashlib.sha256("内容".encode()).hexdigest())
    assert ok is True


def test_etag加任意后缀_必须拒绝():
    """前缀匹配会误放行 `expected + 任意后缀`（GLM 审查 2026-10-17 指出）。"""
    e = content_etag("内容")
    ok, _, _ = check_precondition("内容", base_hash=e + "zzzz")
    assert ok is False, "etag 加后缀不应被放行"


def test_etag比十六位短_必须拒绝():
    e = content_etag("内容")
    ok, _, _ = check_precondition("内容", base_hash=e[:8])
    assert ok is False, "短于 16 位的值不应被放行"


def test_etag为别的文件hash_必须拒绝():
    ok, _, _ = check_precondition("内容", base_hash=content_etag("别的内容"))
    assert ok is False


# ── 集成层：真实 Flask test_client ──

_PATH_CONSTS = ["KB_ROOT", "NOTES_DIR", "TOKEN_FILE", "LOG_FILE", "SETTINGS_FILE",
                "INDEX_FILE", "EMBEDDINGS_FILE", "FAISS_FILE",
                "INDEX_MANIFEST_FILE", "MCP_USAGE_LOG"]


def _patch_paths_everywhere(tmp: Path, notes: Path):
    values = {
        "KB_ROOT": tmp, "NOTES_DIR": notes,
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
        if not getattr(mod, "__name__", "").startswith("kb_lib"):
            continue
        for key, val in values.items():
            if hasattr(mod, key):
                patchers.append(mock.patch.object(mod, key, val))
    for p in patchers:
        p.start()
    return patchers


@pytest.fixture()
def cli():
    tmp = Path(tempfile.mkdtemp(prefix="kb-b3-"))
    notes = tmp / "笔记"
    notes.mkdir(parents=True)

    from kb_lib import create_app
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


def _write(notes, rel, body="# 标题\n\n正文内容足够长以便通过门禁检查。\n"):
    fp = notes / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(body, encoding="utf-8")
    return fp


# ── move ──

def test_move_dry_run不落盘(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    r = c.post("/api/notes/10-项目/a.md/move",
               json={"to": "20-知识/b.md", "dry_run": True}, headers=h)
    assert r.status_code == 200
    d = r.get_json()
    assert d["dry_run"] is True
    assert (notes / "10-项目/a.md").exists()
    assert not (notes / "20-知识/b.md").exists()


def test_move_正常移动_源消失目标出现(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n" + "内容" * 500 + "\n")
    r = c.post("/api/notes/10-项目/a.md/move",
               json={"to": "20-知识/b.md"}, headers=h)
    assert r.status_code == 200, r.get_json()
    assert not (notes / "10-项目/a.md").exists()
    assert (notes / "20-知识/b.md").exists()


def test_move_目标已存在_409(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    _write(notes, "20-知识/b.md")
    r = c.post("/api/notes/10-项目/a.md/move",
               json={"to": "20-知识/b.md"}, headers=h)
    assert r.status_code == 409
    assert r.get_json()["error"] == "target_exists"
    # 两边都还在，未被覆盖
    assert (notes / "10-项目/a.md").exists()
    assert (notes / "20-知识/b.md").exists()


def test_move_源不存在_404(cli):
    c, h, notes, _ = cli
    r = c.post("/api/notes/10-项目/没有.md/move",
               json={"to": "20-知识/b.md"}, headers=h)
    assert r.status_code == 404


def test_move_路径穿越_400(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    r = c.post("/api/notes/10-项目/a.md/move",
               json={"to": "../../etc/passwd"}, headers=h)
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_target"
    assert (notes / "10-项目/a.md").exists()


def test_move_缺to_400(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    r = c.post("/api/notes/10-项目/a.md/move", json={}, headers=h)
    assert r.status_code == 400


def test_move_同名_400(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    r = c.post("/api/notes/10-项目/a.md/move",
               json={"to": "10-项目/a.md"}, headers=h)
    assert r.status_code == 400
    assert r.get_json()["error"] == "same_path"


def test_move_update_links_改双链_零断链(cli):
    """改名后指向它的双链应被改写，且改写后仍能解析到新路径。"""
    c, h, notes, _ = cli
    body_b = "# B\n\n" + "内容" * 500 + "\n"
    _write(notes, "10-项目/b.md", body_b)
    _write(notes, "10-项目/a.md",
           "# A\n\n" + "内容" * 500 + "\n\n引用 [[b]] 和 [[b#某标题]] 与 [[b|别名]]\n")

    r = c.post("/api/notes/10-项目/b.md/move",
               json={"to": "20-知识/b-新名.md", "update_links": True}, headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["links_updated"], "应报告改写了哪些笔记"

    a_text = (notes / "10-项目/a.md").read_text(encoding="utf-8")
    assert "[[b-新名]]" in a_text
    assert "[[b-新名#某标题]]" in a_text
    assert "[[b-新名|别名]]" in a_text
    assert "[[b]]" not in a_text

    # 零断链：改写后仍能解析到新路径
    rr = c.get("/api/links/resolve?target=b-新名", headers=h)
    assert rr.status_code == 200
    assert rr.get_json()["resolved"] == "20-知识/b-新名.md"


def test_move_不指定update_links则不动双链(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/b.md", "# B\n\n" + "内容" * 500 + "\n")
    _write(notes, "10-项目/a.md", "# A\n\n" + "内容" * 500 + "\n\n引用 [[b]]\n")
    r = c.post("/api/notes/10-项目/b.md/move",
               json={"to": "20-知识/b2.md"}, headers=h)
    assert r.status_code == 200
    assert (notes / "10-项目/a.md").read_text(encoding="utf-8").count("[[b]]") == 1


def test_move_改链接中途抛错_回滚到move前(cli, monkeypatch):
    """链接改写中途抛错 → 文件搬回原位、已改的链接还原、返回 500。

    背景：原实现「先落盘再改链接」，多篇逐篇写盘时中途失败会留下
    「文件已在新路径 + 一半链接还指向旧路径」的断链状态，且无回滚。
    这里注入一个在**第二篇**写盘时抛错的副作用，验证事务性收尾。
    """
    c, h, notes, _ = cli
    _write(notes, "10-项目/b.md", "# B\n\n" + "内容" * 500 + "\n")
    a_body = "# A\n\n" + "内容" * 500 + "\n\n引用 [[b]]\n"
    c_body = "# C\n\n" + "内容" * 500 + "\n\n也引用 [[b]]\n"
    _write(notes, "10-项目/a.md", a_body)
    _write(notes, "10-项目/c.md", c_body)

    from pathlib import Path as _P
    real_write = _P.write_text

    def flaky_write(self, data, *a, **kw):
        # 只在「把链接改写成新路径」的那次写盘上抛错（移动本身走 shutil.move，
        # 不经 write_text），模拟改链接中途失败。
        if isinstance(data, str) and "b-新名" in data:
            raise OSError("磁盘写入失败（注入）")
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(_P, "write_text", flaky_write)
    r = c.post("/api/notes/10-项目/b.md/move",
               json={"to": "20-知识/b-新名.md", "update_links": True}, headers=h)
    monkeypatch.undo()

    assert r.status_code == 500, r.get_json()
    assert r.get_json()["error"] == "link_rewrite_failed"
    assert r.get_json()["rolled_back"] is True
    # 文件搬回原位、新路径不残留
    assert (notes / "10-项目/b.md").exists(), "回滚后源文件必须回到原位"
    assert not (notes / "20-知识/b-新名.md").exists(), "回滚后目标不应存在"
    # 全库不留半改的链接
    left = [p for p in notes.rglob("*.md") if "b-新名" in p.read_text(encoding="utf-8")]
    assert left == [], f"回滚后仍有指向新路径的链接: {left}"
    assert (notes / "10-项目/a.md").read_text(encoding="utf-8") == a_body


def test_rollback_搬回失败_不动链接与索引(cli, monkeypatch, tmp_path):
    """搬回失败 → 保留「链接指新路径、文件在新路径」的自洽态。

    GLM 审查 2026-10-17：原顺序先还原链接再搬文件，搬回失败会留下
    「链接指旧路径、文件在新路径」的双向断裂，且索引把 dst 摘掉成孤儿。
    直接对 _rollback_move 做单元测试，避免依赖链接解析顺序。
    """
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n引用 [[b-新名]]\n")   # 已是「改后」状态
    dst = notes / "20-知识/b-新名.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("# B\n\n正文\n", encoding="utf-8")
    src = notes / "10-项目/b.md"

    from kb_lib.routes import routes_notes as rn
    import shutil as _sh
    real_move = _sh.move

    def fail_move(a, b, *args, **kw):
        if str(a).endswith("b-新名.md"):
            raise OSError("注入：搬回失败")
        return real_move(a, b, *args, **kw)

    monkeypatch.setattr(rn.shutil, "move", fail_move)
    # undo 里放一条「改前原文」，若被错误回滚，a.md 会变回 [[b]]
    undo = [("10-项目/a.md", "# A\n\n引用 [[b]]\n")]
    failures = rn._rollback_move(src, dst, "10-项目/b.md", "20-知识/b-新名.md", undo, "# B\n\n正文\n")
    monkeypatch.undo()

    assert "move_back_failed" in failures
    assert dst.exists(), "搬回失败时文件应留在新路径"
    a_txt = (notes / "10-项目/a.md").read_text(encoding="utf-8")
    assert "b-新名" in a_txt, "搬回失败时不应把链接改回旧路径（否则双向断裂）"


# ── 批量 move ──

def test_批量move_部分成功(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md")
    _write(notes, "10-项目/b.md")
    _write(notes, "10-项目/占位.md")
    r = c.post("/api/notes/move-batch", json={"items": [
        {"from": "10-项目/a.md", "to": "20-知识/a.md"},
        {"from": "10-项目/不存在.md", "to": "20-知识/x.md"},
        {"from": "10-项目/b.md", "to": "10-项目/占位.md"},  # 目标已存在
    ]}, headers=h)
    assert r.status_code == 200
    d = r.get_json()
    assert d["total"] == 3 and d["succeeded"] == 1
    assert (notes / "20-知识/a.md").exists()


def test_批量move_缺items_400(cli):
    c, h, _, _ = cli
    r = c.post("/api/notes/move-batch", json={}, headers=h)
    assert r.status_code == 400


# ── 乐观锁端到端 ──

def test_get返回etag且与内容匹配(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n正文\n")
    r = c.get("/notes/10-项目/a.md", headers=h)
    assert r.status_code == 200
    d = r.get_json()
    assert d["etag"] == content_etag(d["raw"])


def test_带正确base_hash写入成功(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n正文\n")
    etag = c.get("/notes/10-项目/a.md", headers=h).get_json()["etag"]
    r = c.post("/notes/10-项目/a.md",
               json={"content": "# A\n\n改过的正文\n", "base_hash": etag}, headers=h)
    assert r.status_code == 200, r.get_json()


def test_带错误base_hash写入_409(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n正文\n")
    r = c.post("/notes/10-项目/a.md",
               json={"content": "# A\n\n改过的正文\n",
                     "base_hash": "0000000000000000"}, headers=h)
    assert r.status_code == 409
    assert r.get_json()["error"] == "version_conflict"
    # 未落盘
    assert "改过的正文" not in (notes / "10-项目/a.md").read_text(encoding="utf-8")


def test_并发写_后写者拿409(cli):
    """两个客户端读到同一版本，A 先写成功，B 再写必须 409。"""
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n原始\n")
    etag = c.get("/notes/10-项目/a.md", headers=h).get_json()["etag"]

    r1 = c.post("/notes/10-项目/a.md",
                json={"content": "# A\n\nA 写的\n", "base_hash": etag}, headers=h)
    assert r1.status_code == 200

    r2 = c.post("/notes/10-项目/a.md",
                json={"content": "# A\n\nB 写的\n", "base_hash": etag}, headers=h)
    assert r2.status_code == 409, "后写者必须拿到 409"
    assert r2.get_json()["expected"] != etag


def test_IfMatch头写入(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n正文\n")
    etag = c.get("/notes/10-项目/a.md", headers=h).get_json()["etag"]
    r = c.post("/notes/10-项目/a.md", json={"content": "# A\n\n改\n"},
               headers={**h, "If-Match": etag})
    assert r.status_code == 200, r.get_json()


def test_不带版本号仍兼容(cli):
    c, h, notes, _ = cli
    _write(notes, "10-项目/a.md", "# A\n\n正文\n")
    r = c.post("/notes/10-项目/a.md", json={"content": "# A\n\n改\n"}, headers=h)
    assert r.status_code == 200, "兼容期不带版本号应仍可写"
