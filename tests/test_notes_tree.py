"""GET /api/notes/tree 全量笔记树接口测试（侧边栏 1652 篇只有约 200 篇入树的问题修复）。

背景：list_notes 的 limit 被硬裁剪到 200，前端侧边栏只能拿到约 12% 的笔记。
新接口 /api/notes/tree 复用 _get_all_notes() 的缓存全量列表，不做任何分页。

锁死不变量：
    ①  items 数量 == 全量笔记数（mocked _get_all_notes 的返回数）
    ②  count == total == len(items)
    ③  无 token 访问返回 401（与 /notes 既有鉴权行为一致）
    ④  传 limit/offset 查询参数也被忽略，全量返回不被裁剪
    ⑤  排除路径（50-档案）与内部跳过规则（.开头/README.md）不进入树
    ⑥  count/total/items 三键齐备，元素带 path/title/type/tags/modified/size/is_dir

fixture 与隔离方式照抄 tests/test_security_regressions.py：
    _patch_paths_everywhere 把 tmp 路径 patch 进所有已加载 kb_lib 模块。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
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
    """把 tmp 下的路径常量 patch 进所有已加载的 kb_lib 模块（同 test_security_regressions）。"""
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
    """用临时笔记库起一个隔离的 Flask 应用（不碰生产数据）。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbtree-"))
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


# ── helpers ──

def _fake_infos(n: int):
    """构造 n 条与 finfo() 输出同构的 info。"""
    items = []
    for i in range(n):
        items.append({
            "path": f"10-项目/笔记{i}.md",
            "title": f"笔记{i}",
            "type": "project",
            "tags": ["测试"],
            "status": "",
            "modified": "2026-10-07T00:00:00",
            "size": 100 + i,
            "is_dir": False,
        })
    return items


def _patch_all_notes(monkeypatch, items):
    """把 routes_notes._get_all_notes 换成固定返回（它被 from import 进该模块命名空间）。"""
    from kb_lib.routes import routes_notes
    monkeypatch.setattr(routes_notes, "_get_all_notes", lambda: items)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ── ① ② 全量、计数一致 ──

def test_tree_返回全量且计数一致(client, monkeypatch):
    c, h, _, _ = client
    infos = _fake_infos(37)
    _patch_all_notes(monkeypatch, infos)

    r = c.get("/api/notes/tree", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()

    assert len(body["items"]) == 37
    assert body["count"] == 37 == body["total"]
    assert body["count"] == body["total"] == len(body["items"])


def test_tree_items字段齐备(client, monkeypatch):
    c, h, _, _ = client
    _patch_all_notes(monkeypatch, _fake_infos(2))

    items = c.get("/api/notes/tree", headers=h).get_json()["items"]
    for it in items:
        for key in ("path", "title", "type", "tags", "modified", "size", "is_dir"):
            assert key in it, f"items 元素缺字段 {key}: {it}"


# ── ③ 鉴权 ──

def test_tree_无token返回401(client):
    c, _, _, _ = client
    assert c.get("/api/notes/tree").status_code == 401


def test_tree_只读token可访问(client):
    c, _, _, tmp = client
    import kb_lib.store as S
    # C2：键是 sha256(pepper + 明文)，不能再拿明文当键写
    tk = "dev-tree-r"
    with S.store_transaction() as s:
        rec = {
            "name": "dev-tree-r", "permission": "r", "device": "pytest",
            "created": "2026-10-07T00:00:00", "expires_at": "2099-01-01T00:00:00",
            "status": "active",
        }
        S.token_record(tk, rec)
        s["tokens"][S.token_hash(tk)] = rec
    h = {"Authorization": "Bearer dev-tree-r"}
    assert c.get("/api/notes/tree", headers=h).status_code == 200


# ── ④ 全量不被 limit 截断 ──

def test_tree_一千篇全部返回且忽略limit参数(client, monkeypatch):
    c, h, _, _ = client
    infos = _fake_infos(1000)
    _patch_all_notes(monkeypatch, infos)

    r = c.get("/api/notes/tree?limit=1&offset=999", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == body["total"] == len(body["items"]) == 1000
    # 首尾都在，证明没有 offset 也没有 limit 裁剪
    paths = [it["path"] for it in body["items"]]
    assert paths[0] == "10-项目/笔记0.md"
    assert paths[-1] == "10-项目/笔记999.md"


# ── ⑤ 排除路径与内部跳过规则 ──

def test_tree_排除路径与内部文件不进树(client):
    """走真实 _get_all_notes：PROTECTED 目录（如 00-索引）被 is_excluded_path 排除；
    . 开头与 README.md 被 _get_all_notes 自身规则跳过。
    注意：50-档案 在默认 is_excluded_path（exclude_archived=False）下不排除，
    树与 list_notes 既有口径一致，档案笔记应出现在 items 里。"""
    c, h, notes, _ = client
    _write(notes / "10-项目" / "正常.md", "---\ntitle: 正常\n---\n\n正文\n")
    _write(notes / "00-索引" / "目录.md", "---\ntitle: 目录\n---\n\n正文\n")
    _write(notes / "50-档案" / "旧稿.md", "---\ntitle: 旧稿\n---\n\n正文\n")
    _write(notes / ".隐藏.md", "---\ntitle: 隐藏\n---\n\n正文\n")
    _write(notes / "README.md", "# 说明\n")

    from kb_lib.routes import routes_notes
    routes_notes._invalidate_notes_cache()  # 刚落的盘，清掉可能残留的扫描缓存

    body = c.get("/api/notes/tree", headers=h).get_json()
    paths = [it["path"] for it in body["items"]]
    assert "10-项目/正常.md" in paths
    assert "50-档案/旧稿.md" in paths, "50-档案 与 list_notes 口径一致，不应被默认排除"
    assert "00-索引/目录.md" not in paths, "is_excluded_path 排除的目录不应进树"
    assert ".隐藏.md" not in paths and "README.md" not in paths, "内部跳过规则失效"
    assert body["count"] == body["total"] == len(paths) == 2
