# -*- coding: utf-8 -*-
"""第 7-4 项：功能插件机制的接口层测试。

与 tests/test_plugins.py 的分工：
    test_plugins.py  → 纯服务层（扫描、manifest 校验、启停状态文件）
    本文件            → 真起 Flask 应用，走鉴权 + 路由全链路

锁死不变量：
    ①  GET /api/plugins 返回全部合法插件，带 enabled 标记（设置页用）
    ②  GET /api/plugins/enabled 只返回已启用的（前端启动挂载用）
    ③  POST /api/plugins/<name>/toggle 关掉后，enabled 清单里就没有它
    ④  toggle 不存在的插件 → 404（不写脏记录）
    ⑤  无 token → 401
    ⑥  插件目录坏掉/不存在 → 200 + 空列表，不让前端启动流程中断

隔离方式照抄 tests/test_templates_api.py。
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


def _mk_plugin(root: Path, name: str, mount="dash", title=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "name": name,
        "title": title or ("插件 " + name),
        "version": "1.0.0",
        "description": "demo",
        "mount": mount,
        "entry": "plugin.js",
        "permissions": [],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "plugin.js").write_text("window.kbPlugins=window.kbPlugins||{};", encoding="utf-8")
    return d


@pytest.fixture
def client():
    """临时笔记库 + 临时插件目录 + 隔离 Flask 应用。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbplug-"))
    notes = tmp / "笔记"
    notes.mkdir()
    pdir = tmp / "web" / "plugins"
    pdir.mkdir(parents=True)
    _mk_plugin(pdir, "alpha", mount="dash")
    _mk_plugin(pdir, "beta", mount="note-detail")
    _mk_plugin(pdir, "gamma", mount="settings")

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    # _patch_paths_everywhere 内部已经 start()，这里不能再 start（Python 3.12 会抛
    # RuntimeError: Patch is already started，3.10/3.11 只是碰巧没炸）

    # plugins.py 在 import 时把 PLUGINS_DIR 定成常量，且 state_file() 读 KB_ROOT
    from kb_lib.services import plugins as P
    # 注意：.start() 返回的是「替换后的对象」而不是 patcher，
    # 直接 append 进去后面 p.stop() 会炸；必须先把 patcher 存下来再 start
    for _p in (mock.patch.object(P, "PLUGINS_DIR", pdir),
               mock.patch.object(P, "state_file", lambda: tmp / ".kb_plugins.json")):
        _p.start()
        patchers.append(_p)

    try:
        # 每次都用新临时库，必须强制重新建表：_DB_INITED 是模块级标志，
        # 被别的用例置 True 后这里就会跳过建表，随后 audit_log 报 no such table
        AU._DB_INITED = False
        AU._init_db()
        S.init_master()

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, pdir, tmp
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ── 清单 ──

def test_插件清单返回全部插件带enabled标记(client):
    c, hdr, _, _ = client
    r = c.get("/api/plugins", headers=hdr)
    assert r.status_code == 200
    d = r.get_json()
    names = [x["name"] for x in d["plugins"]]
    assert names == ["alpha", "beta", "gamma"]
    assert all(x["enabled"] is True for x in d["plugins"])
    # 前端要靠 url 注入脚本，必须给全
    alpha = [x for x in d["plugins"] if x["name"] == "alpha"][0]
    assert alpha["url"] == "./plugins/alpha/plugin.js"
    assert alpha["mount"] == "dash"


def test_已启用清单只返回开着的(client):
    c, hdr, _, _ = client
    c.post("/api/plugins/beta/toggle", headers=hdr, json={"enabled": False})
    d = c.get("/api/plugins/enabled", headers=hdr).get_json()
    assert [x["name"] for x in d["plugins"]] == ["alpha", "gamma"]


def test_无token返回401(client):
    c, _, _, _ = client
    assert c.get("/api/plugins").status_code == 401
    assert c.get("/api/plugins/enabled").status_code == 401


def test_插件目录不存在时返回空列表不报错(client):
    c, hdr, pdir, _ = client
    shutil.rmtree(pdir)
    r = c.get("/api/plugins", headers=hdr)
    assert r.status_code == 200
    assert r.get_json()["plugins"] == []
    r2 = c.get("/api/plugins/enabled", headers=hdr)
    assert r2.status_code == 200
    assert r2.get_json()["plugins"] == []


# ── 启停 ──

def test_关掉插件后清单里enabled变false(client):
    c, hdr, _, _ = client
    r = c.post("/api/plugins/alpha/toggle", headers=hdr, json={"enabled": False})
    assert r.status_code == 200
    assert r.get_json() == {"name": "alpha", "enabled": False}
    items = {x["name"]: x for x in c.get("/api/plugins", headers=hdr).get_json()["plugins"]}
    assert items["alpha"]["enabled"] is False
    assert items["beta"]["enabled"] is True


def test_不带enabled参数时按当前状态取反(client):
    c, hdr, _, _ = client
    r1 = c.post("/api/plugins/alpha/toggle", headers=hdr, json={})
    assert r1.get_json()["enabled"] is False
    r2 = c.post("/api/plugins/alpha/toggle", headers=hdr, json={})
    assert r2.get_json()["enabled"] is True


def test_重新打开后回到已启用清单(client):
    c, hdr, _, _ = client
    c.post("/api/plugins/gamma/toggle", headers=hdr, json={"enabled": False})
    assert "gamma" not in [x["name"] for x in c.get("/api/plugins/enabled", headers=hdr).get_json()["plugins"]]
    c.post("/api/plugins/gamma/toggle", headers=hdr, json={"enabled": True})
    assert "gamma" in [x["name"] for x in c.get("/api/plugins/enabled", headers=hdr).get_json()["plugins"]]


def test_toggle不存在的插件返回404(client):
    c, hdr, _, tmp = client
    r = c.post("/api/plugins/nope/toggle", headers=hdr, json={"enabled": False})
    assert r.status_code == 404
    # 不能留下脏状态文件
    assert not (tmp / ".kb_plugins.json").exists()


def test_toggle非法插件名返回404(client):
    c, hdr, _, _ = client
    r = c.post("/api/plugins/..%2Fetc/toggle", headers=hdr, json={"enabled": False})
    assert r.status_code in (400, 404)


def test_启停状态跨请求持久(client):
    c, hdr, _, tmp = client
    c.post("/api/plugins/beta/toggle", headers=hdr, json={"enabled": False})
    data = json.loads((tmp / ".kb_plugins.json").read_text(encoding="utf-8"))
    assert data["disabled"] == ["beta"]
    # 再查一次仍是关的
    items = {x["name"]: x for x in c.get("/api/plugins", headers=hdr).get_json()["plugins"]}
    assert items["beta"]["enabled"] is False
