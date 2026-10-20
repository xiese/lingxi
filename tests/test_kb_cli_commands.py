"""kb-cli commands 冒烟测试（任务四）。

覆盖：import 不炸、版本号常量、print_usage 控制台输出、
cmd_version 两种服务端响应、main() 入口的 help/version/未知命令分发。
全程不发真实网络请求：需要客户端的场景用 StubClient 或 monkeypatch。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "kb-cli"
sys.path.insert(0, str(CLI_DIR))

import kb_cli.commands as commands  # noqa: E402
import kb_cli.client as cli_client  # noqa: E402
import kb_cli.main as cli_main      # noqa: E402


class StubClient:
    """只实现 commands 用到的 get()。"""

    def __init__(self, raw=None, code=200, err=None):
        self._raw, self._code, self._err = raw, code, err
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self._raw, self._code, self._err


# ── import 与常量 ──

def test_import_and_version_constant():
    assert commands.CLIVERSION == "2.1.0"
    assert cli_client.CLIVERSION == commands.CLIVERSION
    # dispatch 表里每个命令都有对应实现
    for name in (
        "cmd_search", "cmd_read", "cmd_list", "cmd_create", "cmd_upload",
        "cmd_update", "cmd_delete", "cmd_related", "cmd_history", "cmd_status",
        "cmd_health", "cmd_rebuild", "cmd_tags", "cmd_ask", "cmd_graph",
        "cmd_config", "cmd_version",
    ):
        assert callable(getattr(commands, name)), name
    assert callable(commands.print_usage)
    # 纯函数小测：掩码与截断
    assert commands.mask_token("") == "❌ 未设置"
    assert commands.mask_token("a" * 20).endswith("aaaa") or "..." in commands.mask_token("a" * 20)
    assert commands.truncate("x" * 200, 10).endswith("...")


# ── print_usage ──

def test_print_usage(capsys):
    commands.print_usage()
    out = capsys.readouterr().out
    assert "知识库 CLI 工具 (kb-cli v2.1.0)" in out
    for token in ("search", "read", "list", "create", "upload", "update",
                  "delete", "related", "history", "status", "health", "rebuild",
                  "tags", "graph", "ask", "config", "version"):
        assert token in out, token
    assert "用法:" in out


# ── cmd_version ──

def test_cmd_version_with_server(capsys):
    raw = json.dumps({"server": "5.9.3", "skill": "1.2.3"}).encode("utf-8")
    stub = StubClient(raw=raw, code=200)
    commands.cmd_version(stub, [])
    out = capsys.readouterr().out
    assert "kb-cli v2.1.0" in out
    assert "服务端: v5.9.3 (skill: v1.2.3)" in out
    assert stub.calls == ["/api/version"]


def test_cmd_version_server_unreachable(capsys):
    stub = StubClient(raw=None, code=0, err="connection refused")
    commands.cmd_version(stub, [])
    out = capsys.readouterr().out
    assert out.strip() == "kb-cli v2.1.0"
    assert "服务端" not in out


# ── main() 入口 ──

def _patch_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)


def test_main_no_args_exits_zero(monkeypatch, capsys):
    _patch_argv(monkeypatch, ["kb-cli.py"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "用法:" in out and "kb-cli v2.1.0" in out


def test_main_help(monkeypatch, capsys):
    _patch_argv(monkeypatch, ["kb-cli.py", "help"])
    cli_main.main()          # help 分支直接 return，不退出
    out = capsys.readouterr().out
    assert "知识库 CLI 工具 (kb-cli v2.1.0)" in out
    assert "search" in out


def test_main_version_dispatches_without_network(monkeypatch, capsys):
    called = {}

    def fake_cmd_version(c, args):
        called["client"], called["args"] = c, args
        print("kb-cli v2.1.0 (stub)")

    monkeypatch.setattr(cli_main, "cmd_version", fake_cmd_version)
    monkeypatch.setattr(
        cli_main, "load_config",
        lambda: {"api_url": "http://127.0.0.1:8899", "token": ""},
    )
    _patch_argv(monkeypatch, ["kb-cli.py", "version"])
    cli_main.main()
    out = capsys.readouterr().out
    assert "kb-cli v2.1.0 (stub)" in out
    assert called["args"] == []
    assert called["client"].base_url == "http://127.0.0.1:8899"


def test_main_unknown_command(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_main, "load_config",
        lambda: {"api_url": "http://127.0.0.1:8899", "token": "t"},
    )
    _patch_argv(monkeypatch, ["kb-cli.py", "definitely-not-a-command"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "未知命令" in err


def test_main_requires_token(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_main, "load_config",
        lambda: {"api_url": "http://127.0.0.1:8899", "token": ""},
    )
    _patch_argv(monkeypatch, ["kb-cli.py", "search", "知识库"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1
    assert "Token 未设置" in capsys.readouterr().err


# ── client 层（只读本地逻辑，不打网络） ──

def test_client_request_bad_url_returns_error_not_raise():
    cfg = {"api_url": "http://127.0.0.1:1", "token": ""}
    c = cli_client.Client(cfg)
    raw, code, err = c.get("/health")
    assert raw is None and code == 0 and err


def test_check_response_and_encode():
    assert cli_client.check_response(b"{}", 200) is None
    assert "boom" in cli_client.check_response(b'{"error":"boom"}', 400)
    assert "HTTP 500" in cli_client.check_response(b"", 500)
    assert cli_client.encode_note_path("10-项目/示例 笔记.md") == "10-%E9%A1%B9%E7%9B%AE/%E7%A4%BA%E4%BE%8B%20%E7%AC%94%E8%AE%B0.md"
