# -*- coding: utf-8 -*-
"""第 7-3 项：主题插件的接口层测试（GET /api/themes）。

与 tests/test_themes.py 的分工见文件内注释；这里真起 Flask 应用，走鉴权 + 路由。

锁死不变量：
    ①  GET /api/themes 返回 {"themes": [{id,name,desc}, ...]}，字段齐全
    ②  classic 必须在列表里且始终最后一条（经典=原版界面，必须有退路）
    ③  themes/ 目录不存在 → 200，只有 classic，不能 500
    ④  名称/描述从 CSS 首行注释解析
    ⑤  非法 id 不返回（文件名带空格/中文/点开头的一律跳过）
    ⑥  无 token → 401

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


@pytest.fixture
def client():
    """临时主题目录 + 隔离 Flask 应用。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbtheme-"))
    notes = tmp / "笔记"
    notes.mkdir()
    tdir = tmp / "themes"
    tdir.mkdir()
    (tdir / "aurora.css").write_text(
        "/* name: 极光 | desc: 蓝紫渐变 · 明亮卡片 */\n"
        "html.kb-shell[data-theme=aurora]{--k-accent:#5b6cf9}\n",
        encoding="utf-8",
    )
    (tdir / "console.css").write_text(
        "/* name: 控制台 | desc: 暗色 · 等宽字体 · 热力图 */\n"
        "html.kb-shell[data-theme=console]{--bg:#0d1117}\n",
        encoding="utf-8",
    )
    (tdir / "base.css").write_text(
        "/* name: 基础外壳 | desc: 两套主题共用 */\n.kb-shell .ki{width:20px}\n",
        encoding="utf-8",
    )

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    # _patch_paths_everywhere 内部已经 start()，这里不能再 start（Python 3.12 会抛
    # RuntimeError: Patch is already started，3.10/3.11 只是碰巧没炸）
    # themes.py 在 import 时把 THEMES_DIR 定成了常量，需单独改
    from kb_lib.services import themes as TH
    theme_patcher = mock.patch.object(TH, "THEMES_DIR", tdir)
    theme_patcher.start()
    patchers.append(theme_patcher)

    try:
        AU._init_db()
        S.init_master()

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, tdir
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_列出主题_结构与字段齐全(client):
    c, h, _ = client
    r = c.get("/api/themes", headers=h)
    assert r.status_code == 200, r.data[:300]
    data = r.get_json()
    assert isinstance(data["themes"], list)
    for item in data["themes"]:
        assert set(item.keys()) == {"id", "name", "desc"}, item
        assert item["id"] and isinstance(item["name"], str)
    ids = [x["id"] for x in data["themes"]]
    # base.css 是共用外壳，不算可选主题，不能出现在列表里
    assert ids == ["aurora", "console", "classic"], ids


def test_classic必在且排最后(client):
    c, h, _ = client
    data = c.get("/api/themes", headers=h).get_json()
    classic = data["themes"][-1]
    assert classic["id"] == "classic"
    assert classic["name"] == "经典"
    # 只应出现一次
    assert [x["id"] for x in data["themes"]].count("classic") == 1


def test_目录不存在时只返回classic不500(client):
    c, h, tdir = client
    shutil.rmtree(tdir)
    r = c.get("/api/themes", headers=h)
    assert r.status_code == 200, r.data[:300]
    data = r.get_json()
    assert [x["id"] for x in data["themes"]] == ["classic"]


def test_名称与描述从首行注释解析(client):
    c, h, _ = client
    data = c.get("/api/themes", headers=h).get_json()
    by_id = {x["id"]: x for x in data["themes"]}
    assert by_id["aurora"]["name"] == "极光"
    assert by_id["aurora"]["desc"] == "蓝紫渐变 · 明亮卡片"
    assert by_id["console"]["name"] == "控制台"
    assert by_id["console"]["desc"] == "暗色 · 等宽字体 · 热力图"


def test_注释缺失时用id兜底(client):
    c, h, tdir = client
    (tdir / "plain.css").write_text(".kb-shell .x{color:red}\n", encoding="utf-8")
    data = c.get("/api/themes", headers=h).get_json()
    by_id = {x["id"]: x for x in data["themes"]}
    assert by_id["plain"]["name"] == "plain"
    assert by_id["plain"]["desc"] == ""


def test_非法id不返回(client):
    c, h, tdir = client
    # 中文名、带空格、点开头、classic 同名（classic 只能由代码兜底）
    (tdir / "中文主题.css").write_text("/* name: 中文 */\n.x{}\n", encoding="utf-8")
    (tdir / "has space.css").write_text("/* name: 空格 */\n.x{}\n", encoding="utf-8")
    (tdir / ".hidden.css").write_text("/* name: 隐藏 */\n.x{}\n", encoding="utf-8")
    (tdir / "classic.css").write_text("/* name: 假经典 */\n.x{}\n", encoding="utf-8")
    (tdir / "base.css").write_text("/* name: 假基础 */\n.x{}\n", encoding="utf-8")
    ids = [x["id"] for x in c.get("/api/themes", headers=h).get_json()["themes"]]
    assert "中文主题" not in ids
    assert "has space" not in ids
    assert ".hidden" not in ids
    assert "base" not in ids, "base.css 是共用外壳，不该当成可选主题"
    assert ids.count("classic") == 1
    assert ids[-1] == "classic"


def test_无token列主题401(client):
    c, h, _ = client
    assert c.get("/api/themes").status_code == 401
