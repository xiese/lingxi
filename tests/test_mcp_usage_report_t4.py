"""T4：MCP 使用统计上报链路（知识库上报接口 + MCP 桥上报）。

背景：统计页 `GET /api/mcp-usage` 读 KB_ROOT/.kb_mcp_usage.jsonl，但 MCP 桥只
写本机日志、从不跨机上报，导致生产统计停在 2026-08-19（total=200 / today=0）。
口径（D-e）：桥每次调用都 POST 到 `/api/mcp-usage/report` 入库，统计页只读库。

本文件钉住：
  1. 上报接口合法请求 → 200 且文件恰好多一行，字段与 log_usage 输出一致；
  2. 缺 tool / 缺 client / ok 非布尔 → 400，且不落盘；
  3. 超大 payload → 400（>64KB 直接拒），4KB 以上单行 → 截断（丢超大字段）；
  4. 桥 log_usage 上报成功会发一次 POST；上报抛异常时工具调用照常返回。

全部用临时 KB_ROOT（conftest 已设，前缀 kbtest-root-），发现 .kb-env=prod 直接拒跑。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 默认指向**仓库内**的桥，测试才真正覆盖本次改动；KB_MCP_SERVER_PATH 仍可覆盖
BRIDGE_PATH = Path(os.environ.get(
    "KB_MCP_SERVER_PATH", str(ROOT / "scripts" / "kb_mcp_server.py")))


def _guard_dev_root(root: Path):
    """发现 .kb-env=prod 就拒绝运行（红绿测试只许在临时/开发库跑）。"""
    assert "kbtest-root-" in str(root), f"KB_ROOT 不是临时测试目录：{root}"
    marker = root / ".kb-env"
    if marker.exists():
        assert marker.read_text(encoding="utf-8").strip() != "prod", (
            f"拒绝在 .kb-env=prod 的库上跑测试：{root}"
        )


def _patch_paths(tmp: Path, notes: Path):
    """把所有已加载 kb_lib 模块里的路径常量指向临时库（同 test_security_regressions）。"""
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


@pytest.fixture()
def kb(tmp_path):
    """隔离 Flask app + rw 设备令牌 + 临时用量文件。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbtest-root-"))
    _guard_dev_root(tmp)
    (tmp / "笔记").mkdir()
    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths(tmp, tmp / "笔记")
    try:
        AU._init_db()
        S.init_master()
        with S.store_transaction() as s:
            # C2：键是 sha256(pepper + 明文)，不能再拿明文当键写
            for _tk, _perm in (("t4-rw", "rw"), ("t4-ro", "r")):
                _rec = {
                    "name": _tk, "permission": _perm, "device": "pytest",
                    "created": "2026-10-19T00:00:00",
                    "expires_at": "2099-01-01T00:00:00", "status": "active",
                }
                S.token_record(_tk, _rec)
                s["tokens"][S.token_hash(_tk)] = _rec
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        yield {
            "client": c,
            "rw": {"Authorization": "Bearer t4-rw"},
            "ro": {"Authorization": "Bearer t4-ro"},
            "root": tmp,
            "usage": tmp / ".kb_mcp_usage.jsonl",
        }
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _post(kb, body, headers=None):
    return kb["client"].post(
        "/api/mcp-usage/report", json=body, headers=headers or kb["rw"])


# ── A. 上报接口 ──

def test_report_合法请求落一行且统计立即可见(kb):
    usage = kb["usage"]
    assert not usage.exists(), "前置：临时库不该已有用量文件"

    r = _post(kb, {"client": "web-client", "tool": "kb_read", "ok": True,
                   "readonly": False, "path": "20-知识/钉子.md"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"ok": True, "recorded": True}

    lines = usage.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"应恰好落 1 行，实际 {len(lines)} 行：{lines!r}"
    row = json.loads(lines[0])
    assert row["client"] == "web-client" and row["tool"] == "kb_read"
    assert row["ok"] is True and row["readonly"] is False
    assert row["path"] == "20-知识/钉子.md", "顶层其他字段应原样并入"
    assert row["time"], "缺省 time 必须由服务端补 now"

    stats = kb["client"].get("/api/mcp-usage", headers=kb["rw"]).get_json()
    assert stats["total"] == 1
    assert stats["today"] == 1
    assert stats["ok"] == 1
    assert stats["tools"] == {"kb_read": 1}
    assert stats["last"]["tool"] == "kb_read"


def test_report_缺tool返回400且不落盘(kb):
    r = _post(kb, {"client": "web-client", "ok": True})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "tool" in r.get_json()["error"]
    assert not kb["usage"].exists(), "400 不该落盘"


def test_report_缺client与ok非布尔返回400(kb):
    assert _post(kb, {"tool": "kb_read", "ok": True}).status_code == 400
    assert _post(kb, {"client": "web-client", "tool": "kb_read"}).status_code == 400
    assert _post(kb, {"client": "web-client", "tool": "kb_read", "ok": "true"}).status_code == 400
    assert _post(kb, {"client": "web-client", "tool": "kb_read", "ok": True,
                      "time": "不是时间"}).status_code == 400
    assert not kb["usage"].exists()


def test_report_只读令牌403(kb):
    r = _post(kb, {"client": "web-client", "tool": "kb_read", "ok": True},
              headers=kb["ro"])
    assert r.status_code == 403
    assert not kb["usage"].exists()


def test_report_超大请求体400(kb):
    big = {"client": "web-client", "tool": "kb_read", "ok": True,
           "extra": {"blob": "x" * (70 * 1024)}}
    r = _post(kb, big)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "payload_too_large"
    assert not kb["usage"].exists()


def test_report_无content_length超限仍400(kb):
    """chunked 传输时 content_length 为 None，必须靠读取后的实际字节数兜底。"""
    import io as _io

    payload = json.dumps({"client": "web-client", "tool": "kb_read", "ok": True,
                          "extra": {"blob": "x" * (70 * 1024)}}).encode("utf-8")
    r = kb["client"].post(
        "/api/mcp-usage/report",
        input_stream=_io.BytesIO(payload),
        content_type="application/json",
        headers=kb["rw"],
        environ_overrides={
            "CONTENT_LENGTH": "",
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input_terminated": True,
        },
    )
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "payload_too_large"
    assert not kb["usage"].exists()


def test_report_单行超4KB截断而不是400(kb):
    """实际行为：**截断**（丢弃放不下的 extra 字段），不是 400。

    核心字段本身超限才 400；这里只让 extra 撑爆，所以返回 200 + dropped。
    """
    r = _post(kb, {"client": "web-client", "tool": "kb_read", "ok": True,
                   "extra": {"blob": "x" * 8000, "keep": "v"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True and body["recorded"] is True
    assert body["dropped"] == ["blob"], f"应只丢超大字段：{body}"

    line = kb["usage"].read_text(encoding="utf-8").splitlines()[0]
    assert len(line.encode("utf-8")) <= 4096, "落盘单行必须 ≤ 4KB"
    row = json.loads(line)
    assert "blob" not in row and row["keep"] == "v"


# ── B. MCP 桥 log_usage 上报 ──

@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    if not BRIDGE_PATH.exists():
        pytest.skip(f"找不到 MCP 桥：{BRIDGE_PATH}")
    if importlib.util.find_spec("fastmcp") is None:
        pytest.skip("未安装 fastmcp，跳过桥侧用例")
    spec = importlib.util.spec_from_file_location("kb_mcp_server_t4", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _guard_dev_root(Path(os.environ["KB_ROOT"]))
    monkeypatch.setattr(mod, "USAGE_LOG", tmp_path / ".kb_mcp_usage.jsonl")
    monkeypatch.setattr(mod, "KB_HOST", "http://127.0.0.1:1")
    monkeypatch.setattr(mod, "USAGE_REPORT", True)
    monkeypatch.setattr(mod, "get_token", lambda: "t4-token")
    return mod


def test_桥上报成功路径发一次POST(bridge, monkeypatch):
    seen = {}

    class FakeResp:
        def read(self, n=-1):
            return b'{"ok": true, "recorded": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    bridge.log_usage("kb_read", ok=True, path="20-知识/x.md")

    assert seen["url"].endswith("/api/mcp-usage/report"), seen
    assert seen["timeout"] == 2.0, "超时必须 2 秒，不能拖住工具调用"
    assert seen["auth"] == "Bearer t4-token"
    assert seen["body"]["tool"] == "kb_read" and seen["body"]["ok"] is True
    assert seen["body"]["extra"] == {"path": "20-知识/x.md"}
    # 本地文件照旧写一行
    lines = bridge.USAGE_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["tool"] == "kb_read"


def test_桥上报抛异常不影响工具返回(bridge, monkeypatch, capsys):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", boom)

    def fake_api(method, path, data=None, timeout=30):
        return {"path": path, "content": "钉子内容", "title": "钉子测试"}

    monkeypatch.setattr(bridge, "api", fake_api)
    out = bridge.kb_read(path="20-知识/钉子测试.md")

    assert "钉子内容" in out, "上报失败时工具调用必须照常返回"
    lines = bridge.USAGE_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "本地用量仍要落一行"
    err = capsys.readouterr().err
    assert "[kb_mcp_usage_report]" in err and "connection refused" in err
    assert err.count("\n") == 1, f"只许一行 stderr，实际：{err!r}"


def test_桥开关关闭时不发请求(bridge, monkeypatch):
    called = []
    monkeypatch.setattr(bridge, "USAGE_REPORT", False)
    monkeypatch.setattr(bridge.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1))
    bridge.log_usage("kb_read", ok=True)
    assert called == [], "KB_MCP_USAGE_REPORT=0 时不得上报"
    assert bridge.USAGE_LOG.exists(), "开关只关上报，本地日志照写"
