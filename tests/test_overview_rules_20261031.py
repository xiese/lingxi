# -*- coding: utf-8 -*-
"""知识库规则反哺（2026-10-31）—— 第 1/2/4 条的验收测试。

第 1 条：写「项目总览」时检查检索四件套（tags/aliases/importance），缺则 warning
第 2 条：payload 未显式传 status → 提示会落成 draft
第 4 条：写 10-项目/<项目>/ 下的笔记 → 该项目进入「总览待刷新」队列

环境隔离由 conftest.py 负责（KB_ROOT 指向临时目录）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from kb_lib.services import write_policy as wp


def _warns(kinds):
    return [w.get("kind") for w in (kinds or [])]


# ─────────────── 第 1 条：总览四件套 ───────────────

def test_总览缺tags和aliases要告警(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="项目总览",
        content="# 某项目 · 项目总览\n\n正文……", notes_dir=notes,
        tags=[], aliases=[],
    )
    kinds = _warns(warnings)
    assert "overview_missing_retrieval_fields" in kinds
    miss = next(w["missing"] for w in warnings
                if w["kind"] == "overview_missing_retrieval_fields")
    assert "tags" in miss and "aliases" in miss


def test_总览带全字段不告警(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="项目总览",
        content="---\nimportance: 1.0\n---\n# 某项目 · 项目总览\n", notes_dir=notes,
        tags=["项目总览", "某项目"], aliases=["某项目"],
    )
    assert "overview_missing_retrieval_fields" not in _warns(warnings)


def test_普通笔记不触发总览检查(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="某个开发记录",
        content="# 记录\n", notes_dir=notes, tags=[], aliases=[],
    )
    assert "overview_missing_retrieval_fields" not in _warns(warnings)


# ─────────────── 第 2 条：status 默认提示（默认关，按需开）───────────────

def test_status提示默认关闭不产生噪声(tmp_path):
    """status_default_notice 默认 false —— 否则所有不传 status 的写入都多一条 warning。

    实测踩过：默认 true 会让 test_write_policy_hygiene_split.py 的 3 个老用例
    （断言 warns == []）失败 —— 那是行为契约变更，不是老用例错。
    """
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    assert wp.STATUS_DEFAULT_NOTICE is False
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="某方案",
        content="# 方案\n", notes_dir=notes, status="",
    )
    assert "status_defaulted_to_draft" not in _warns(warnings)


def test_status开关打开时会提示(tmp_path, monkeypatch):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    monkeypatch.setattr(wp, "STATUS_DEFAULT_NOTICE", True)
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="某方案",
        content="# 方案\n", notes_dir=notes, status="",
    )
    assert "status_defaulted_to_draft" in _warns(warnings)


def test_status传了就不提示(tmp_path, monkeypatch):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    monkeypatch.setattr(wp, "STATUS_DEFAULT_NOTICE", True)
    _, warnings = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="某方案",
        content="# 方案\n", notes_dir=notes, status="active",
    )
    assert "status_defaulted_to_draft" not in _warns(warnings)


# ─────────────── 第 4 条：总览待刷新队列 ───────────────

def test_写项目笔记会入队(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    proj = wp.mark_overview_pending("10-项目/某项目/某个开发记录.md",
                                    action="created", notes_dir=notes)
    assert proj == "某项目"
    q = notes / ".kb" / "overview_pending.json"
    assert q.is_file(), "队列文件没生成"
    data = json.loads(q.read_text(encoding="utf-8"))
    assert "某项目" in data["pending"]
    assert data["pending"]["某项目"]["action"] == "created"


def test_写非项目笔记不入队(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "20-知识").mkdir(parents=True)
    assert wp.mark_overview_pending("20-知识/某知识.md", notes_dir=notes) is None
    assert not (notes / ".kb" / "overview_pending.json").exists()


def test_总览自身不入队(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    assert wp.mark_overview_pending("10-项目/某项目/项目总览.md", notes_dir=notes) is None


def test_多次写入去重合并(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "项目A").mkdir(parents=True)
    (notes / "10-项目" / "项目B").mkdir(parents=True)
    wp.mark_overview_pending("10-项目/项目A/a.md", "created", notes)
    wp.mark_overview_pending("10-项目/项目B/b.md", "created", notes)
    wp.mark_overview_pending("10-项目/项目A/a2.md", "saved", notes)
    data = json.loads((notes / ".kb" / "overview_pending.json").read_text(encoding="utf-8"))
    assert set(data["pending"]) == {"项目A", "项目B"}
    assert data["pending"]["项目A"]["action"] == "saved"   # 后者覆盖
    assert data["interval_min"] == wp.OVERVIEW_REFRESH_INTERVAL_MIN


def test_队列写失败不影响主流程(tmp_path):
    """notes_dir 传一个不可写路径，函数必须安静返回 None，不抛异常。"""
    assert wp.mark_overview_pending("10-项目/某项目/x.md", notes_dir="/proc/nonexistent") is None


# ─────────────── 规则常量确实从 rules.yaml 读到了 ───────────────

def test_规则常量已加载():
    assert "tags" in wp.OVERVIEW_REQUIRED_FIELDS
    assert "aliases" in wp.OVERVIEW_REQUIRED_FIELDS
    # title 已从清单移除：总览的 title 恒为「项目总览」才会走到检查，检查它没有意义
    assert "title" not in wp.OVERVIEW_REQUIRED_FIELDS
    assert wp.STATUS_DEFAULT_NOTICE is False      # 兜底默认 = 不改变现状
    assert wp.OVERVIEW_REFRESH_ENABLED is True    # rules.yaml 里显式开启
    assert isinstance(wp.OVERVIEW_REFRESH_INTERVAL_MIN, int)
    assert wp.OVERVIEW_PENDING_FILE.endswith(".json")
    # 第 2 条只留规则声明，不留死代码常量
    assert not hasattr(wp, "REQUIRED_FIELDS")


# ─────────────── 2026-10-31 三路审查后的加固（每条对应一个审查发现）───────────────

def test_直接放在项目根下的文件不入队(tmp_path):
    """`10-项目/x.md` 只有两段，不是项目笔记 —— 旧判据 len<2 会把它当项目名入队。"""
    notes = tmp_path / "笔记"
    (notes / "10-项目").mkdir(parents=True)
    assert wp.mark_overview_pending("10-项目/散记.md", notes_dir=notes) is None
    assert not (notes / ".kb" / "overview_pending.json").exists()


def test_队列文件损坏会先备份再重建(tmp_path):
    """损坏时旧行为是静默重置为 {}（全部 pending 无声消失）；现在必须先留一份 .corrupt-*。"""
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "项目A").mkdir(parents=True)
    q = notes / ".kb" / "overview_pending.json"
    q.parent.mkdir(parents=True)
    q.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert wp.mark_overview_pending("10-项目/项目A/a.md", notes_dir=notes) == "项目A"
    corrupt = list((notes / ".kb").glob("overview_pending.json.corrupt-*"))
    assert corrupt, "损坏文件没有被备份下来"
    assert "这不是合法 JSON" in corrupt[0].read_text(encoding="utf-8")


def test_并发写入不丢更新(tmp_path):
    """waitress 是多线程，队列是 read-modify-write：不加锁会丢项目条目。"""
    import threading
    notes = tmp_path / "笔记"
    projs = [f"项目{i:02d}" for i in range(24)]
    for p in projs:
        (notes / "10-项目" / p).mkdir(parents=True)

    def w(p):
        wp.mark_overview_pending(f"10-项目/{p}/x.md", "created", notes)

    ts = [threading.Thread(target=w, args=(p,)) for p in projs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    data = json.loads((notes / ".kb" / "overview_pending.json").read_text(encoding="utf-8"))
    assert set(data["pending"]) == set(projs), "并发写入丢了项目条目"


def test_importance_只认frontmatter不认正文(tmp_path):
    """正文里出现 importance 这个词，不能被当成「已设置 importance」。"""
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "某项目").mkdir(parents=True)
    _, w = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="项目总览",
        content="# 某项目 · 项目总览\n\n正文里随口提到 importance 这个词而已。\n",
        notes_dir=notes, tags=["x"], aliases=["y"])
    miss = next(x["missing"] for x in w if x["kind"] == "overview_missing_retrieval_fields")
    assert "importance" in miss, "正文里的同名字样不该被算作已设置"

    _, w2 = wp.validate_write_hygiene(
        note_type="project", subdir="某项目", title="项目总览",
        content="---\nimportance: 1.0\n---\n# 某项目 · 项目总览\n",
        notes_dir=notes, tags=["x"], aliases=["y"])
    assert "overview_missing_retrieval_fields" not in _warns(w2)


def test_队列路径越界被跳过(tmp_path, monkeypatch):
    """overview_pending_file 若被配成绝对路径，必须拒绝写入 notes_dir 之外。"""
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "项目A").mkdir(parents=True)
    outside = tmp_path / "外面.json"
    monkeypatch.setattr(wp, "OVERVIEW_PENDING_FILE", str(outside))
    assert wp.mark_overview_pending("10-项目/项目A/a.md", notes_dir=notes) is None
    assert not outside.exists(), "越界路径不该被写入"


def test_删除类动作也会入队(tmp_path, monkeypatch):
    """deleted 在 FORCE_REFRESH_ACTIONS 里，on_note_changed 会早退 ——
    队列标记必须排在早退之前，否则删除永不触发总览刷新。"""
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "项目A").mkdir(parents=True)
    monkeypatch.setattr(wp, "OVERVIEW_REFRESH_ENABLED", True)
    wp.on_note_changed(action="deleted", force_search_refresh=True,
                       rel_path="10-项目/项目A/gone.md", notes_dir=notes)
    data = json.loads((notes / ".kb" / "overview_pending.json").read_text(encoding="utf-8"))
    assert "项目A" in data["pending"]
    assert data["pending"]["项目A"]["action"] == "deleted"


def test_移动后源和目标都入队(tmp_path, monkeypatch):
    notes = tmp_path / "笔记"
    (notes / "10-项目" / "项目A").mkdir(parents=True)
    (notes / "10-项目" / "项目B").mkdir(parents=True)
    monkeypatch.setattr(wp, "OVERVIEW_REFRESH_ENABLED", True)
    wp.mark_overview_pending("10-项目/项目A/x.md", "moved", notes)
    wp.mark_overview_pending("10-项目/项目B/x.md", "moved", notes)
    data = json.loads((notes / ".kb" / "overview_pending.json").read_text(encoding="utf-8"))
    assert set(data["pending"]) == {"项目A", "项目B"}