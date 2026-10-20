# -*- coding: utf-8 -*-
"""第 7-1 项：批量解析双链（POST /api/links/resolve-batch）的接口测试。

为什么要这个接口：一篇笔记里几十个双链，前端逐个调 /api/links/resolve
会打几十次请求，每次都要重建一遍全库链接索引，手机端直接卡住。
批量接口一次返回全部目标 → 路径/null，前端据此标断链。

锁死不变量：
    ①  能解析的目标返回真实相对路径，解析不到的返回 null（断链）
    ②  结果按目标原文做 key，重复目标只算一次（去重）
    ③  targets 不是数组 → 400
    ④  targets 为空/全空白 → 200 且 resolved 为空（不是 400，前端不该为空格报错）
    ⑤  超过 500 个目标被截断（防恶意大数组拖垮索引构建）
    ⑥  无 token → 401
    ⑦  from 参与解析：同目录同名笔记优先命中同目录那篇

隔离方式照抄 tests/test_notes_tree.py：把 tmp 路径 patch 进所有已加载 kb_lib 模块。
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


def _write(p: Path, text: str = "# x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def client():
    """临时笔记库 + 隔离 Flask 应用（不碰生产数据）。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbwl-"))
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


def _post(c, h, payload):
    return c.post("/api/links/resolve-batch", headers=h, json=payload)


# ── ① 能解析 / 断链 ──

def test_能解析的目标返回路径_解析不到的返回null(client):
    c, h, notes, _ = client
    _write(notes / "20-知识" / "已有笔记.md")
    r = _post(c, h, {"from": "20-知识/来源.md", "targets": ["已有笔记", "根本不存在的笔记"]})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    res = r.get_json()["resolved"]
    assert res["已有笔记"] == "20-知识/已有笔记.md"
    assert res["根本不存在的笔记"] is None


# ── ② 去重 ──

def test_重复目标只返回一次(client):
    c, h, notes, _ = client
    _write(notes / "20-知识" / "甲.md")
    r = _post(c, h, {"from": "", "targets": ["甲", "甲", " 甲 ", "乙"]})
    body = r.get_json()
    assert body["count"] == 2, body
    assert set(body["resolved"]) == {"甲", "乙"}


# ── ③ targets 非数组 → 400 ──

def test_targets不是数组返回400(client):
    c, h, _, _ = client
    r = _post(c, h, {"from": "", "targets": "甲"})
    assert r.status_code == 400


# ── ④ 空 targets → 200 空结果 ──

def test_空targets返回空结果而不是报错(client):
    c, h, _, _ = client
    for payload in ({"from": "", "targets": []}, {"from": "", "targets": ["", "   "]}):
        r = _post(c, h, payload)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()["resolved"] == {}


# ── ⑤ 超过 500 截断 ──

def test_目标超过500个被截断(client):
    c, h, _, _ = client
    targets = [f"笔记{i}" for i in range(700)]
    body = _post(c, h, {"from": "", "targets": targets}).get_json()
    assert body["count"] == 500, f"应截断到 500，实际 {body['count']}"


# ── ⑥ 鉴权 ──

def test_无token返回401(client):
    c, _, _, _ = client
    assert c.post("/api/links/resolve-batch", json={"targets": ["甲"]}).status_code == 401


# ── ⑦ from 参与解析：同目录优先 ──

def test_from参与解析_同目录优先命中(client):
    c, h, notes, _ = client
    _write(notes / "10-项目" / "同名.md", "# 项目里的")
    _write(notes / "20-知识" / "同名.md", "# 知识里的")
    # 来源在 10-项目 目录下 → 应命中 10-项目/同名.md
    res = _post(c, h, {"from": "10-项目/来源.md", "targets": ["同名"]}).get_json()["resolved"]
    assert res["同名"] == "10-项目/同名.md", res
