"""settings 加固用例（2026-10-08 技术债盘点 #1）：

钉两条：
1. settings 文件手坏（非法 JSON / 顶层非对象）→ load_settings 回落 DEFAULT_SETTINGS 而非抛异常全站 500；
2. 未知键只 warning 不删除（宽进），且 DEFAULT 键的 setdefault 语义不变（正常文件全量透传）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """隔离一个 kb_lib.config 实例：SETTINGS_FILE 指到 tmp，缓存清空。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path))
    import kb_lib.config as c
    import importlib
    importlib.reload(c)
    sf = tmp_path / ".kb_settings.json"
    monkeypatch.setattr(c, "SETTINGS_FILE", sf)
    # 清掉 reload 后可能残留的模块级缓存
    c._settings_cache = None
    c._settings_cache_mtime = 0
    c._settings_key_cache.clear()
    return c


def test_settings坏文件回落默认不崩(cfg, caplog):
    cfg.SETTINGS_FILE.write_text("{ 这不是合法 JSON", encoding="utf-8")
    with caplog.at_level("WARNING"):
        s = cfg.load_settings()
    # 不抛异常 + 回落 DEFAULT（逐键核对默认值在位）
    assert s["rerank_enabled"] == cfg.DEFAULT_SETTINGS["rerank_enabled"]
    assert s["max_log_days"] == cfg.DEFAULT_SETTINGS["max_log_days"]
    assert any("解析失败" in r.message for r in caplog.records)


def test_settings顶层非对象回落(cfg):
    cfg.SETTINGS_FILE.write_text('["a", "b"]', encoding="utf-8")
    s = cfg.load_settings()
    assert s["max_log_days"] == cfg.DEFAULT_SETTINGS["max_log_days"]


def test_settings未知键只告警不删除(cfg, caplog):
    cfg.SETTINGS_FILE.write_text(json.dumps({"rerank_enabled": True, "拼错的键": 1}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        s = cfg.load_settings()
    assert s["拼错的键"] == 1                      # 宽进：不删
    assert s["rerank_enabled"] is True               # 正常键透传
    assert any("未知键" in r.message for r in caplog.records)


def test_settings不存在用默认(cfg, tmp_path):
    assert not cfg.SETTINGS_FILE.exists()
    s = cfg.load_settings()
    assert s == cfg.DEFAULT_SETTINGS
