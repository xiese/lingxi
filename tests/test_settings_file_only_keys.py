"""settings「文件级键」白名单的回归测试（2026-10-25）。

背景：embed.py 会读 `embedding_proxy` / `embedding_request_gap` /
`embedding_tokens_per_min` 三个调优项，但它们是手工填在 .kb_settings.json 里、
设置页没有输入框的「文件级键」，不在 DEFAULT_SETTINGS 内。
结果每次启动都误报「settings 含未知键（拼错将静默无效）」——
告警噪音会让真正的拼写错误被淹没。

钉三件事：
    ① 这三个键带值时不再触发「未知键」告警（修复生效）
    ② 真正拼错的键仍然告警（修复没有把告警整个关掉 —— 这是最容易假绿的地方）
    ③ 文件级键不混进 DEFAULT_SETTINGS（否则前端一致性测试会红）
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
    import importlib
    import kb_lib.config as c
    importlib.reload(c)
    monkeypatch.setattr(c, "SETTINGS_FILE", tmp_path / ".kb_settings.json")
    c._settings_cache = None
    c._settings_cache_mtime = 0
    c._settings_key_cache.clear()
    return c


def _unknown_lines(caplog):
    return [r.message for r in caplog.records if "未知键" in str(r.message)]


def test_文件级键不再误报未知键(cfg, caplog):
    cfg.SETTINGS_FILE.write_text(
        json.dumps({
            "embedding_base": "http://127.0.0.1:3097/v1",
            "embedding_proxy": "",
            "embedding_request_gap": 0.2,
            "embedding_tokens_per_min": 0,
        }),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        s = cfg.load_settings()
    assert not _unknown_lines(caplog), f"文件级键仍被当成未知键：{_unknown_lines(caplog)}"
    # 宽进语义不变：值照样透传
    assert s["embedding_request_gap"] == 0.2
    assert s["embedding_tokens_per_min"] == 0


def test_真拼错的键仍然告警(cfg, caplog):
    """防「把告警整个关掉」这种假绿修复。"""
    cfg.SETTINGS_FILE.write_text(
        json.dumps({"embedding_request_gapp": 0.2}),  # 故意多一个 p
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        cfg.load_settings()
    assert _unknown_lines(caplog), "拼错的键没告警，告警被整体关掉了"


def test_文件级键不在默认设置里(cfg):
    """进 DEFAULT_SETTINGS 就会要求前端补输入框，那是另一件事。"""
    overlap = cfg.FILE_ONLY_SETTINGS & set(cfg.DEFAULT_SETTINGS)
    assert not overlap, f"这些键同时在两张表里，会逼前端加输入框：{sorted(overlap)}"
