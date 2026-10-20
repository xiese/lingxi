"""scan_new 端点最小冒烟测试（拆分重构 2026-10-08 补）。

背景：拆分前 scan_new 在全套测试里零覆盖。按任务约束只补 1 个最小冒烟用例：
真实打 POST /admin/scan-new，断言 200 + 响应契约（7 个字段的键与类型），
不触达 LLM（造的笔记带 tags，不会进入待打标签队列），不碰生产库（fixture 全隔离）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402

# 需要在 fixture 里改写的路径类常量（各 kb_lib 模块在 import 时绑了这些名字，
# 只 patch config 不够——routes_admin 等模块持有自己的引用）。
_PATH_VALUES = {
    "KB_ROOT": "kb_root",
    "NOTES_DIR": "notes",
    "TOKEN_FILE": ".api_tokens.json",
    "LOG_FILE": ".audit_log.jsonl",
    "SETTINGS_FILE": ".kb_settings.json",
    "INDEX_FILE": ".search_index.pkl",
    "EMBEDDINGS_FILE": ".embeddings.npy",
    "FAISS_FILE": ".faiss.index",
    "INDEX_MANIFEST_FILE": ".index_manifest.json",
    "MCP_USAGE_LOG": ".kb_mcp_usage.jsonl",
}


def _patch_paths_everywhere(tmp: Path, notes: Path):
    """把 tmp 下的路径常量 patch 进所有已加载的 kb_lib 模块。"""
    values = {k: (tmp if v == "kb_root" else notes if v == "notes" else tmp / v)
              for k, v in _PATH_VALUES.items()}
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
    tmp = Path(tempfile.mkdtemp(prefix="kbscan-"))
    notes = tmp / "笔记"
    notes.mkdir()

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    try:
        AU._init_db()          # 在新库里建审计表，避免 warning 噪音
        S.init_master()        # 在隔离目录里生成 master token

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, notes, tmp
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_new_冒烟_200与响应契约(client):
    """POST /admin/scan-new：200 且 7 字段契约不变（拆分重构冒烟）。"""
    c, h, notes, _ = client
    # 造一篇带 tags 的近期笔记：走扫描/自动修复路径但不会进 LLM 待打标签队列
    (notes / "20-知识").mkdir(parents=True, exist_ok=True)
    (notes / "20-知识" / "冒烟.md").write_text(
        "---\ntitle: 冒烟\ntags: [冒烟测试]\n---\n\n正文内容。", encoding="utf-8")
    r = c.post("/admin/scan-new", headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()
    assert set(body) == {"scanned_period", "issues_found", "issues", "auto_fixed",
                         "fixed", "auto_tagged", "safety"}
    assert body["scanned_period"] == "最近24小时"
    assert body["safety"] == "未删除/修改任何受保护文件"
    assert isinstance(body["issues"], list) and isinstance(body["fixed"], list)
    assert body["issues_found"] == len(body["issues"])
    assert body["auto_fixed"] == len(body["fixed"])
    assert body["auto_tagged"] == 0  # 笔记已有 tags，未触发 LLM，不允许被追加
