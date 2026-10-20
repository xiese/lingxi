"""总览统计口径测试（2026-10-18 第 8 项改造）。

维护者 2026-10-18 拍板的口径：六张卡统一为「主数字＝该分类目录下的一级子目录个数，
小字＝该分类下的笔记篇数」。

锁死的不变量：
    ① `projects` / `knowledge` / … 数的是**一级子目录个数**，不递归、不数文件
    ② `*_notes` 才是笔记篇数，且与改造前的计数规则完全一致（不改变既有含义）
    ③ `total` 仍是六类篇数合计，与原实现逐字节同义（不破坏已有调用方）
    ④ 以 `.` 或 `_` 开头的一级子目录不计入
    ⑤ 分类目录不存在或没有任何一级子目录时，`dir_fallback[键]` 为 True（前端据此注明）
    ⑥ 缓存不串味：改完目录结构后清缓存能立刻看到新数字

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


@pytest.fixture
def client():
    tmp = Path(tempfile.mkdtemp(prefix="kbdash-"))
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
        yield c, {"Authorization": f"Bearer {master}"}, notes
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _reset_dash_cache():
    from kb_lib.routes import routes_general as RG
    RG._DASHBOARD_CACHE["data"] = None
    RG._DASHBOARD_CACHE["ts"] = 0
    RG._DASHBOARD_CACHE["mtime"] = 0


def _write(p: Path, text: str = "内容\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _mk_layout(notes: Path):
    """构造一套可控目录结构。

    10-项目/：3 个有效子目录（含子目录里各 1 篇）+ 1 个 _assets + 1 个 .hidden
              + 根下 2 篇散落 md
    20-知识/：2 个有效子目录，各 2 篇
    30-写作/：只有 1 篇，无子目录（应触发 dir_fallback）
    50-档案/：2 个有效子目录（含 1 个 _旧版本 不计），各 1 篇
    """
    for name in ("甲项目", "乙项目", "丙项目"):
        _write(notes / "10-项目" / name / "笔记.md")
    _write(notes / "10-项目" / "_assets" / "附件.md")
    _write(notes / "10-项目" / ".hidden" / "隐藏.md")
    _write(notes / "10-项目" / "散落一.md")
    _write(notes / "10-项目" / "散落二.md")

    for name in ("主题甲", "主题乙"):
        _write(notes / "20-知识" / name / "一.md")
        _write(notes / "20-知识" / name / "二.md")

    _write(notes / "30-写作" / "单篇.md")

    _write(notes / "50-档案" / "整理一" / "旧一.md")
    _write(notes / "50-档案" / "整理二" / "旧二.md")
    _write(notes / "50-档案" / "_旧版本" / "旧版.md")


def test_主数字数一级子目录_小字数笔记篇数(client):
    c, h, notes = client
    _mk_layout(notes)
    _reset_dash_cache()

    r = c.get("/api/dashboard", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    d = r.get_json()

    # ① 主数字＝一级子目录个数（排除 . 与 _ 开头）
    assert d["projects"] == 3, d
    assert d["knowledge"] == 2, d
    assert d["archived"] == 2, d

    # ② 小字＝笔记篇数（**沿用改造前的原口径，逐字节不变**：递归数 .md，
    # 排除规则仍只作用于「相对 NOTES_DIR 的第一段」——即只有库根下的 _assets 被排除，
    # 分类目录内部的 _assets / .hidden 仍照旧计入。此处刻意断言 7 而非 5，
    # 就是为了锁死「只改主数字口径、不顺手改篇数口径」。）
    # 10-项目：3 个子目录各 1 篇 + _assets 1 篇 + .hidden 1 篇 + 根下 2 篇 = 7
    assert d["projects_notes"] == 7, d
    assert d["knowledge_notes"] == 4, d
    assert d["archived_notes"] == 3, d

    # ⑤ 30-写作 无子目录 → 退化为篇数并标记
    assert d["writing"] == 0, d
    assert d["writing_notes"] == 1, d
    assert d["dir_fallback"]["writing"] is True
    assert d["dir_fallback"]["projects"] is False


def test_total_仍是六类篇数合计(client):
    c, h, notes = client
    _mk_layout(notes)
    _reset_dash_cache()

    d = c.get("/api/dashboard", headers=h).get_json()
    manual = (
        d["projects_notes"] + d["knowledge_notes"] + d["writing_notes"]
        + d["records_notes"] + d["diaries_notes"] + d["archived_notes"]
    )
    assert d["total"] == manual, d
    # 与原实现同义：total 只数 .md 篇数，不掺目录个数
    assert d["total"] == 7 + 4 + 1 + 0 + 0 + 3, d


def test_六张卡键名齐全(client):
    c, h, notes = client
    _mk_layout(notes)
    _reset_dash_cache()

    d = c.get("/api/dashboard", headers=h).get_json()
    for k in ("projects", "knowledge", "writing", "records", "diaries", "archived"):
        assert k in d, (k, d)
        assert f"{k}_notes" in d, (k, d)
    assert "total" in d and "total_dirs" in d and "dir_fallback" in d


def test_缓存失效后能看到新目录(client):
    c, h, notes = client
    _mk_layout(notes)
    _reset_dash_cache()
    first = c.get("/api/dashboard", headers=h).get_json()["projects"]
    assert first == 3

    _write(notes / "10-项目" / "丁项目" / "新笔记.md")
    _reset_dash_cache()
    second = c.get("/api/dashboard", headers=h).get_json()
    assert second["projects"] == 4, second
    assert second["projects_notes"] == 8, second


def test_无token返回401(client):
    c, _, notes = client
    _mk_layout(notes)
    _reset_dash_cache()
    assert c.get("/api/dashboard").status_code == 401
