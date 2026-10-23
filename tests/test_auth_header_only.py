"""鉴权只认 Authorization 头（T13，2026-10-23）。

背景：`get_token()` 曾留 `?token=` 查询参数回退。URL 会进浏览器历史、代理日志、
Referer 与监控采样，令牌一旦落到这些地方就无法收回。全仓核查确认没有调用方
必须靠查询参数传令牌（前端一律走 Authorization 头，SSE 走 fetch + POST +
Authorization，下载与图片走 fetch + Blob），因此去掉该回退。

本文件只覆盖这一个判据：查询参数不再被当成令牌，Header 仍然有效。
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


def _patch_paths_everywhere(tmp: Path, notes: Path):
    """把各模块里的路径常量指向临时库（照抄 test_security_regressions 的写法）。"""
    import kb_lib.config as C

    patchers = [
        mock.patch.object(C, "KB_ROOT", tmp),
        mock.patch.object(C, "NOTES_DIR", notes),
        mock.patch.object(C, "TOKEN_FILE", tmp / ".api_tokens.json"),
        mock.patch.object(C, "SETTINGS_FILE", tmp / ".kb_settings.json"),
        mock.patch.object(C, "LOG_FILE", tmp / ".audit_log.jsonl"),
        mock.patch.object(C, "INDEX_FILE", tmp / ".search_index.json"),
        mock.patch.object(C, "INDEX_MANIFEST_FILE", tmp / ".index_manifest.json"),
    ]
    for mod_name in ("kb_lib.routes", "kb_lib.audit", "kb_lib.store", "kb_lib.search"):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for attr, val in (("KB_ROOT", tmp), ("NOTES_DIR", notes)):
            if hasattr(mod, attr):
                patchers.append(mock.patch.object(mod, attr, val))
    for p in patchers:
        p.start()
    return patchers


@pytest.fixture()
def client_and_master():
    tmp = Path(tempfile.mkdtemp(prefix="kb-t13-"))
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
        yield c, master, app
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_get_token_header_only(client_and_master):
    """get_token() 只从 Authorization 头取值。"""
    from kb_lib.auth import get_token

    _, _, app = client_and_master
    with app.test_request_context("/?token=query-token",
                                 headers={"Authorization": "Bearer header-token"}):
        assert get_token() == "header-token"

    with app.test_request_context("/?token=query-token"):
        assert get_token() == "", "查询参数不该再被当成令牌"


def test_query_token_rejected_on_protected_route(client_and_master):
    """受保护路由带**真实 master 令牌**的 ?token= 必须 401。

    用真实令牌是关键：随便一个无效字符串本来就会 401，那样测不出回退是否被去掉。
    """
    c, master, _ = client_and_master
    r = c.get(f"/notes?token={master}")
    assert r.status_code == 401, f"期望 401（查询参数不算令牌），实际 {r.status_code}"


def test_query_token_rejected_on_master_route(client_and_master):
    """master 路由带**真实 master 令牌**的 ?token= 必须 401/403。"""
    c, master, _ = client_and_master
    r = c.get(f"/admin/debug-auth?token={master}")
    assert r.status_code in (401, 403), f"期望 401/403，实际 {r.status_code}"


def test_header_still_works(client_and_master):
    """master 令牌走 Authorization 头仍然有效（回归保护）。"""
    c, master, _ = client_and_master
    r = c.get("/admin/debug-auth", headers={"Authorization": f"Bearer {master}"})
    assert r.status_code == 200, f"期望 200，实际 {r.status_code}"
    body = r.get_json()
    assert body.get("auth_header_present") is True
    assert body.get("is_master") is True
    assert body.get("token_len") == len(master)
