"""路径穿越与标题落盘回归测试（2026-09-18）。

背景（真机复现）：
  POST /notes 的 title 里含 `/` 时，`sanitize()` 保留分隔符 → 被当作目录分隔符
  → 目标目录不存在 → FileNotFoundError → HTTP 500。
  更严重：`../` 可逃出 NOTES_DIR，实测能写到笔记库外任意 .md，
  而 kb-server 以 root 运行。

本测试锁死三件事：
  1) 标题里的路径分隔符一律压平，落盘为单段文件名（不再 500）；
  2) `..` / 绝对路径 / 空字节一律 400 硬拦；
  3) 任何写入都不得逃出 NOTES_DIR。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda fn: fn)
    stub.raises = None
    sys.modules["pytest"] = stub

import pytest  # noqa: E402


# ────────────────────────── 单元层：纯函数 ──────────────────────────


def test_sanitize_title_压平所有路径分隔符():
    from kb_lib.auth import sanitize_title

    for raw in ("a/b", "a\\b", "a／b", "a＼b", "a/b/c"):
        got = sanitize_title(raw)
        assert "/" not in got and "\\" not in got, f"{raw!r} → {got!r} 仍含分隔符"


def test_sanitize_title_消除穿越():
    from kb_lib.auth import sanitize_title

    for raw in ("..", ".", "../../../../tmp/pwn", "x/../../../pwn", "/etc/cron.d/x"):
        got = sanitize_title(raw)
        assert ".." not in got.split("/"), f"{raw!r} → {got!r} 仍含 .. 段"
        assert not got.startswith("/"), f"{raw!r} → {got!r} 仍是绝对路径"


def test_sanitize_title_空标题兜底():
    from kb_lib.auth import sanitize_title

    for raw in ("", "   ", ".", "..", "...", None):
        assert sanitize_title(raw) == "未命名", f"{raw!r} 未兜底"


def test_sanitize_title_保留正常标题():
    from kb_lib.auth import sanitize_title

    assert sanitize_title("示例项目 门槛25 复盘") == "示例项目 门槛25 复盘"
    # 半角斜杠压成下划线，其余不动
    assert sanitize_title("示例/子目录 收尾") == "示例_子目录 收尾"


def test_is_within_围栏语义():
    from kb_lib.auth import is_within

    root = Path("/srv/kb/笔记")
    assert is_within(root / "10-项目" / "x.md", root)
    assert not is_within(Path("/tmp/x.md"), root)
    assert not is_within(root / ".." / ".." / "tmp" / "x.md", root)


# ────────────────────── 卫生层：validate_write_hygiene ──────────────────────


def _hygiene(title, subdir="", note_type="project"):
    from kb_lib.services.write_policy import validate_write_hygiene

    return validate_write_hygiene(
        note_type=note_type, subdir=subdir, title=title,
        content="x" * 900, notes_dir=None, type_map={}, sanitize_fn=None,
    )


def test_卫生层_拦截标题穿越():
    for raw in ("../../x", "..", "x/../../y"):
        err, _ = _hygiene(raw)
        assert err is not None, f"{raw!r} 未被拦截"
        assert err["error"] == "path_forbidden"


def test_卫生层_拦截subdir穿越():
    err, _ = _hygiene("ok", subdir="示例项目/../../../../tmp")
    assert err is not None and err["error"] == "path_forbidden"


def test_卫生层_拦截空字节():
    err, _ = _hygiene("ok\x00evil")
    assert err is not None and err["error"] == "path_forbidden"


def test_卫生层_放行正常标题与子目录():
    for t, sd in (("正常标题", ""), ("示例/子目录", ""), ("ok", "示例项目"), ("ok", "示例项目/子目录")):
        err, _ = _hygiene(t, subdir=sd)
        assert err is None, f"title={t!r} subdir={sd!r} 被误拦: {err}"


def test_ensure_inside_返回错误而非抛异常():
    from kb_lib.services.write_policy import ensure_inside

    root = Path("/srv/kb/笔记")
    err, rp = ensure_inside(root / "a" / "b.md", root)
    assert err is None and rp is not None

    err, rp = ensure_inside(Path("/tmp/evil.md"), root)
    assert err is not None and rp is None
    assert err["error"] == "path_forbidden"


# ────────────────────── 集成层：真实 Flask test_client ──────────────────────


@pytest.fixture
def client():
    """用临时笔记库起一个隔离的 Flask 应用（不碰生产数据）。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbtest-"))
    notes = tmp / "笔记"
    notes.mkdir()

    import kb_lib.config as C

    patchers = [
        (C, "KB_ROOT", tmp), (C, "NOTES_DIR", notes),
        (C, "TOKEN_FILE", tmp / ".api_tokens.json"),
        (C, "LOG_FILE", tmp / ".audit_log.jsonl"),
        (C, "SETTINGS_FILE", tmp / ".kb_settings.json"),
    ]
    from unittest import mock

    stack = [mock.patch.object(mod, name, val) for mod, name, val in patchers]
    for p in stack:
        p.start()
    try:
        import kb_lib.auth as A
        import kb_lib.routes as R
        import kb_lib.routes.routes_notes as RN

        with mock.patch.object(A, "KB_ROOT", tmp), \
             mock.patch.object(A, "NOTES_DIR", notes), \
             mock.patch.object(R, "NOTES_DIR", notes), \
             mock.patch.object(RN, "NOTES_DIR", notes):
            from kb_lib import create_app
            import importlib

            # kb_lib/__init__.py 里把 store 这个名字占用成了 dict，
            # 这里用 importlib 直接取子模块，确保在临时目录里生成 master token。
            _store = importlib.import_module("kb_lib.store")
            _store.init_master()

            app = create_app()
            app.config["TESTING"] = True
            c = app.test_client()
            master = re.search(
                r'"master":\s*\{[^}]*"token":\s*"([^"]+)"',
                (tmp / ".api_tokens.json").read_text(encoding="utf-8"),
            ).group(1)
            yield c, {"Authorization": f"Bearer {master}"}, notes, tmp
    finally:
        for p in reversed(stack):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _payload(title, subdir="示例项目"):
    return {"title": title, "content": "# t\n\n## 背景\n\nx\n",
            "type": "project", "subdir": subdir, "quality_check": False}


def test_集成_标题含斜杠不再500(client):
    c, h, notes, _ = client
    r = c.post("/notes", json=_payload("示例/子目录 收尾"), headers=h)
    assert r.status_code == 201, f"仍失败: {r.status_code} {r.get_data(as_text=True)[:200]}"
    assert r.get_json()["path"] == "10-项目/示例项目/示例_子目录_收尾.md"


def test_集成_标题穿越被拒且不落盘(client):
    c, h, notes, tmp = client
    r = c.post("/notes", json=_payload("../../../../tmp/pwn"), headers=h)
    assert r.status_code == 400
    assert r.get_json()["error"] == "path_forbidden"
    assert not list(tmp.glob("*.md")), "库外被写了文件"


def test_集成_subdir穿越被拒且不落盘(client):
    c, h, notes, tmp = client
    r = c.post("/notes", json=_payload("ok", subdir="示例项目/../../../../tmp/evil"), headers=h)
    assert r.status_code == 400
    assert not list(tmp.glob("*.md")), "库外被写了文件"


def test_集成_所有落盘都在笔记库内(client):
    c, h, notes, tmp = client
    for t in ("正常标题", "示例/子目录", "全角／斜杠", "带..点..的标题"):
        r = c.post("/notes", json=_payload(t), headers=h)
        assert r.status_code in (201, 400), f"{t!r} → {r.status_code}"
    for f in notes.rglob("*.md"):
        f.resolve().relative_to(notes.resolve())  # 逃出即抛 ValueError
    assert not list(tmp.glob("*.md")), "库外被写了文件"
