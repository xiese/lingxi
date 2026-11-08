"""update_stats.py 统计口径测试（任务三）。

假 KB_ROOT + 桩 API，验证 README 统计段 / 主档「数据状态」/ .dev_progress.json
三者口径一致，且只改统计字段、不动 completed/next 等既有内容。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import update_stats as us  # noqa: E402

API_RESPONSES = {
    "/health": {"indexed": 113, "stale": 1, "version": "5.9.3", "jieba": True},
    "/api/index/status": {"indexed": 114},
    "/dashboard": {
        "total": 280, "projects": 57, "knowledge": 79,
        "writing": 0, "diaries": 0, "archived": 19,
    },
    "/api/tags": {"total": 332},
    "/notes?limit=1": {"total": 280},
}


def fake_api(responses):
    def api_get(path, auth=False, retries=2):
        if path not in responses:
            raise RuntimeError(f"unexpected path {path}")
        return responses[path]
    return api_get


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """假 KB_ROOT：3 个目录各几篇 md + 三个统计目标文件。"""
    root = tmp_path / "kb"
    notes = root / "笔记"
    for sub in ("10-项目", "20-知识", "40-日记"):
        (notes / sub).mkdir(parents=True)
    (notes / "10-项目" / "甲.md").write_text("# 甲\n", encoding="utf-8")
    (notes / "10-项目" / "乙.md").write_text("# 乙\n", encoding="utf-8")
    (notes / "20-知识" / "丙.md").write_text("# 丙\n", encoding="utf-8")
    (notes / "40-日记" / "丁.md").write_text("# 丁\n", encoding="utf-8")
    (notes / "20-知识" / "非笔记.txt").write_text("not md", encoding="utf-8")

    readme = root / "README.md"
    readme.write_text("# 知识库\n\n## 特性\n\n- x\n\n## 文档\n\n- y\n", encoding="utf-8")

    proj = notes / "10-项目" / "知识库项目.md"
    proj.write_text(
        "---\ntitle: 知识库项目\nupdated: 2026-08-18 15:19\n---\n\n"
        "## 数据状态\n\n- indexed: 62, stale: 0\n\n## 文档路径约定\n\n内容\n",
        encoding="utf-8",
    )

    dev = root / ".dev_progress.json"
    dev.write_text(json.dumps({
        "version": "5.9.2",
        "completed": ["01-目录结构", "x"],
        "next": "持续维护",
        "created": "2026-06-30",
        "updated": "2026-07-11 10:58",
        "stats": {"indexed": 12, "notes_total": 70, "stale": 0},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(us, "KB_ROOT", root)
    monkeypatch.setattr(us, "NOTES_DIR", notes)
    monkeypatch.setattr(us, "PROJECT_NOTE", proj)
    monkeypatch.setattr(us, "README_PATH", readme)
    monkeypatch.setattr(us, "DEV_PROGRESS_PATH", dev)
    monkeypatch.setattr(us, "api_get", fake_api(API_RESPONSES))
    return root


# ── 核心口径：indexed 优先 /api/index/status ──

def test_get_dashboard_stats_prefers_index_status(kb, monkeypatch):
    stats = us.get_dashboard_stats()
    assert stats["indexed"] == 114           # 鉴权口径优先
    assert stats["total_md"] == 280          # dashboard.total
    assert stats["tags"] == 332
    assert stats["projects"] == 57 and stats["knowledge"] == 79
    assert stats["archived"] == 19 and stats["diaries"] == 0
    assert stats["stale"] == 1 and stats["version"] == "5.9.3"
    assert stats["jieba"] is True
    assert stats["excluded"] == 0             # notes.total == dashboard.total


def test_get_dashboard_stats_falls_back_to_health(kb, monkeypatch):
    responses = dict(API_RESPONSES)
    responses.pop("/api/index/status")
    monkeypatch.setattr(us, "api_get", fake_api(responses))
    stats = us.get_dashboard_stats()
    assert stats["indexed"] == 113           # /health 口径


def test_excluded_counts_filtered_away(kb, monkeypatch):
    responses = dict(API_RESPONSES)
    responses["/notes?limit=1"] = {"total": 200}
    monkeypatch.setattr(us, "api_get", fake_api(responses))
    stats = us.get_dashboard_stats()
    assert stats["filtered"] == 200
    assert stats["excluded"] == 80
    assert stats["not_indexed"] == 86        # 200 - 114


# ── 三个统计目标 ──

def test_three_targets_consistent(kb):
    stats = us.get_dashboard_stats()
    stats["total_on_disk"] = sum(1 for _ in us.NOTES_DIR.rglob("*.md"))
    assert stats["total_on_disk"] == 5       # 甲/乙/丙/丁 + 知识库项目.md；txt 不计

    assert us.update_readme(stats) is True
    assert us.update_project_note(stats) is True
    assert us.update_dev_progress(stats) is True

    readme = (kb / "README.md").read_text(encoding="utf-8")
    assert "## 统计" in readme
    assert "笔记总数：280 篇（索引 114 篇，文件系统 5 篇含 README/模板/隐藏文件）" in readme
    assert "标签数：332" in readme
    assert "stale=1" in readme

    proj = (us.PROJECT_NOTE).read_text(encoding="utf-8")
    assert "- indexed: 114, stale: 1, jieba: ON" in proj
    assert "仪表盘可见笔记: 280；文件系统 .md: 5" in proj
    assert "服务版本: 5.9.3" in proj
    assert "updated: " in proj

    dev = json.loads((kb / ".dev_progress.json").read_text(encoding="utf-8"))
    assert dev["stats"] == {
        "indexed": 114, "notes_total": 280, "stale": 1,
        "service": "running", "jieba": True,
    }
    # 只改统计相关字段，业务字段原样保留
    assert dev["version"] == "5.9.2"
    assert dev["completed"] == ["01-目录结构", "x"]
    assert dev["next"] == "持续维护"
    assert dev["created"] == "2026-06-30"
    assert dev["updated"] == datetime.now().strftime("%Y-%m-%d %H:%M")


def test_readme_inserts_missing_stats_section(kb):
    """根 README 被 auto-commit 覆盖丢统计段时必须能自救（2026-10-07 停摆根因）。"""
    stats = us.get_dashboard_stats()
    stats["total_on_disk"] = 164
    assert "## 统计" not in (kb / "README.md").read_text(encoding="utf-8")
    assert us.update_readme(stats) is True
    text = (kb / "README.md").read_text(encoding="utf-8")
    assert "## 统计" in text
    # 插到「## 文档」之前，且原文其他段完好
    assert text.index("## 统计") < text.index("## 文档")
    assert "## 特性" in text
    assert "笔记总数：280 篇（索引 114 篇，文件系统 164 篇含 README/模板/隐藏文件）" in text


def test_readme_replaces_existing_stats_section(kb, monkeypatch):
    root = kb
    readme = root / "README.md"
    readme.write_text(
        "# 知识库\n\n## 统计\n\n- 笔记总数：62 篇（索引 62 篇，文件系统 76 篇）\n"
        "- 标签数：146\n\n## 文档\n\n- y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(us, "README_PATH", readme)
    stats = us.get_dashboard_stats()
    stats["total_on_disk"] = 164
    assert us.update_readme(stats) is True
    text = readme.read_text(encoding="utf-8")
    assert "62 篇" not in text
    assert "114 篇" in text
    assert text.count("## 统计") == 1


def test_readme_missing_file_returns_false(kb, tmp_path, monkeypatch):
    monkeypatch.setattr(us, "README_PATH", tmp_path / "nope.md")
    stats = us.get_dashboard_stats()
    assert us.update_readme(stats) is False


def test_project_note_missing_file_returns_false(kb, tmp_path, monkeypatch):
    monkeypatch.setattr(us, "PROJECT_NOTE", tmp_path / "nope.md")
    stats = us.get_dashboard_stats()
    assert us.update_project_note(stats) is False


def test_dev_progress_rebuilds_when_corrupt(kb, tmp_path, monkeypatch):
    dev = tmp_path / ".dev_progress.json"
    dev.write_text("{ 这不是合法 JSON", encoding="utf-8")
    monkeypatch.setattr(us, "DEV_PROGRESS_PATH", dev)
    stats = us.get_dashboard_stats()
    assert us.update_dev_progress(stats) is True
    data = json.loads(dev.read_text(encoding="utf-8"))
    assert data["stats"]["indexed"] == 114
    assert data["stats"]["notes_total"] == 280
    assert data["updated"]


def test_dev_progress_creates_when_absent(kb, tmp_path, monkeypatch):
    dev = tmp_path / ".dev_progress.json"
    monkeypatch.setattr(us, "DEV_PROGRESS_PATH", dev)
    stats = us.get_dashboard_stats()
    assert us.update_dev_progress(stats) is True
    data = json.loads(dev.read_text(encoding="utf-8"))
    assert data["stats"]["stale"] == 1
