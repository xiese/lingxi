"""kb_note_hygiene.scan 分支覆盖测试（任务二）。

全部用例只用 tmp_path 造 fixture，不碰真实仓库目录；scan 只读传入的 notes_dir。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kb_note_hygiene as hyg  # noqa: E402


# ── fixtures / helpers ──

def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def kinds(report: dict) -> list[str]:
    return sorted(i["kind"] for i in report["issues"])


def one(report: dict, kind: str) -> dict:
    return next(i for i in report["issues"] if i["kind"] == kind)


GOOD_BODY = (
    "# 干净笔记\n\n"
    "这是一篇内容足够的合法笔记，用于确认扫描器不会误报。\n"
    "包含：项目背景、实施记录、验证方式与后续注意事项四段。\n"
    "第二段继续补充足够的正文长度，避免触发空壳判定。\n"
    "第三段再补充一行，确保换行数量也满足阈值。\n"
)


def make_notes(root: Path) -> Path:
    notes = root / "笔记"
    notes.mkdir(parents=True, exist_ok=True)
    return notes


# ── 1. 干净笔记：零问题 ──

def test_clean_note_no_issues(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "干净.md",
          "---\ntitle: 干净\nupdated: 2026-10-07\n---\n\n" + GOOD_BODY)
    report = hyg.scan(notes)
    assert report["issues"] == []
    assert report["summary"]["scanned_md"] == 1
    assert report["summary"]["active_md"] == 1
    assert report["summary"]["critical"] == 0 and report["summary"]["warning"] == 0


# ── 2. 正文完全相同 → exact_duplicate（critical） ──

def test_exact_duplicate_body(tmp_path):
    notes = make_notes(tmp_path)
    body = "---\ntitle: 同文\n---\n\n" + GOOD_BODY
    write(notes / "20-知识" / "甲.md", body)
    write(notes / "20-知识" / "乙.md", body)
    report = hyg.scan(notes)
    issue = one(report, "exact_duplicate")
    assert issue["level"] == "critical"
    assert issue["paths"] == sorted(["20-知识/甲.md", "20-知识/乙.md"])


# ── 3. 归档副本不算重复（50-档案 排除） ──

def test_archive_copy_not_duplicate(tmp_path):
    notes = make_notes(tmp_path)
    body = "---\ntitle: 同文\n---\n\n" + GOOD_BODY
    write(notes / "20-知识" / "甲.md", body)
    write(notes / "50-档案" / "甲.md", body)
    report = hyg.scan(notes)
    assert "exact_duplicate" not in kinds(report)
    assert report["summary"]["active_md"] == 1
    assert report["summary"]["scanned_md"] == 2


# ── 4. 同名跨 10-项目/20-知识且知识侧非指针 → dual_main_same_name ──

def test_dual_main_same_name_warning(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "双主文.md", "---\ntitle: 双主文\n---\n\n" + GOOD_BODY)
    write(notes / "20-知识" / "双主文.md", "---\ntitle: 双主文\n---\n\n" + GOOD_BODY)
    report = hyg.scan(notes)
    issue = one(report, "dual_main_same_name")
    assert issue["level"] == "warning"
    assert "知识侧不像指针" in issue["detail"]
    # 同时命中正文完全相同的判定，属于预期叠加
    assert "exact_duplicate" in kinds(report)


def test_dual_main_allowed_when_knowledge_is_pointer(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "指针例.md", "---\ntitle: 指针例\n---\n\n" + GOOD_BODY)
    write(notes / "20-知识" / "指针例.md",
          "---\ntitle: 指针例\n---\n\n# 指针例\n\n权威来源：`10-项目/指针例.md`，本文仅为指针。\n")
    report = hyg.scan(notes)
    assert "dual_main_same_name" not in kinds(report)
    assert "same_basename" not in kinds(report)
    # 指针短文按设计豁免空壳判定
    assert "empty_shell" not in kinds(report)


# ── 5. 空壳笔记 → empty_shell；指针型短文豁免 ──

def test_empty_shell(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "空壳.md", "# 空壳\n")
    report = hyg.scan(notes)
    issue = one(report, "empty_shell")
    assert issue["level"] == "warning"
    assert issue["paths"] == ["10-项目/空壳.md"]
    assert "过短" in issue["detail"]


def test_archive_stub_pointer_not_flagged(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "20-知识" / "已归档.md",
          "---\narchived: true\n---\n\n# 已归档\n\n重定向：见 50-档案。\n")
    report = hyg.scan(notes)
    assert report["issues"] == []


# ── 6. 嵌套目录撞顶层目录名 → nested_top_dir ──

def test_nested_top_dir(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "20-知识" / "10-项目" / "错位.md", GOOD_BODY)
    report = hyg.scan(notes)
    issue = one(report, "nested_top_dir")
    assert issue["level"] == "warning"
    assert issue["paths"] == ["20-知识/10-项目"]
    # 50-档案 内的同名目录属正常镜像，不报
    write(notes / "50-档案" / "20-知识" / "历史.md", GOOD_BODY)
    report2 = hyg.scan(notes)
    paths = [i["paths"] for i in report2["issues"] if i["kind"] == "nested_top_dir"]
    assert paths == [["20-知识/10-项目"]]


# ── 7. 正文重复一级标题 → duplicate_h1 ──

def test_duplicate_h1(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "双标题.md",
          "---\ntitle: 双标题\n---\n\n# 双标题\n# 双标题\n\n" + GOOD_BODY.split("\n", 1)[1])
    report = hyg.scan(notes)
    issue = one(report, "duplicate_h1")
    assert issue["level"] == "warning"
    assert issue["paths"] == ["10-项目/双标题.md"]
    assert "2 个一级标题" in issue["detail"]


def test_leading_h1_lines_pure_function():
    assert hyg.leading_h1_lines("# A\n# B\n") == [1, 2]
    assert hyg.leading_h1_lines("---\ntitle: t\n---\n\n# A\n正文\n") == [5]
    assert hyg.leading_h1_lines("\n\n# A\n# B\n") == [3, 4]
    assert hyg.leading_h1_lines("正文开头没有标题\n") == []
    assert hyg.leading_h1_lines("---\ntitle: t\n---\n正文\n") == []
    # 第二处及以后的 H1 不算"正文开头"
    assert hyg.leading_h1_lines("# A\n正文\n# B\n") == [1]


# ── 8. 废弃路径标记 → forbidden_path（critical） ──

def test_forbidden_path(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "10-项目_知识库项目" / "旧.md", GOOD_BODY)
    report = hyg.scan(notes)
    issue = one(report, "forbidden_path")
    assert issue["level"] == "critical"
    assert issue["paths"] == ["10-项目/10-项目_知识库项目/旧.md"]


# ── 9. 同目录近重复 → near_duplicate（critical） ──

def test_near_duplicate_same_dir(tmp_path):
    notes = make_notes(tmp_path)
    base = ("知识库 笔记 卫生 扫描 测试 近重复 检测 同目录 相似 内容 片段 "
            "统计 脚本 报告 问题 严重 级别 分类 路径 详情 时间 生成 摘要 "
            "索引 标签 归档 项目 写作 日记 目录 结构 模板 隐藏 文件 ")
    write(notes / "20-知识" / "近重甲.md", "# 近重甲\n\n" + (base + "甲 ") * 6)
    write(notes / "20-知识" / "近重乙.md", "# 近重乙\n\n" + (base + "乙 ") * 6)
    report = hyg.scan(notes)
    issue = one(report, "near_duplicate")
    assert issue["level"] == "critical"
    assert set(issue["paths"]) == {"20-知识/近重甲.md", "20-知识/近重乙.md"}
    assert "Jaccard=" in issue["detail"]


# ── 10. 50-档案 根上散落 md → archive_root_litter ──

def test_archive_root_litter(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "50-档案" / "散落.md", GOOD_BODY)
    report = hyg.scan(notes)
    issue = one(report, "archive_root_litter")
    assert issue["paths"] == ["50-档案/散落.md"]


# ── 11. 同一项目双目录（部署后缀） → project_dir_fork ──

def test_project_dir_fork(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "foo" / "主.md", GOOD_BODY)
    write(notes / "10-项目" / "foo 部署" / "部署.md", GOOD_BODY)
    report = hyg.scan(notes)
    issue = one(report, "project_dir_fork")
    assert issue["paths"] == ["10-项目/foo", "10-项目/foo 部署"]


def test_project_dir_fork_not_triggered_alone(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "foo" / "主.md", GOOD_BODY)
    report = hyg.scan(notes)
    assert "project_dir_fork" not in kinds(report)


# ── 12. 测试渣命名残留 → test_junk ──

def test_test_junk(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "20-知识" / "测试渣残留.md", GOOD_BODY)
    report = hyg.scan(notes)
    assert one(report, "test_junk")["paths"] == ["20-知识/测试渣残留.md"]


# ── 13. 严重度排序与摘要统计 ──

def test_severity_sort_and_summary(tmp_path):
    notes = make_notes(tmp_path)
    write(notes / "20-知识" / "10-项目" / "错位.md", GOOD_BODY)   # warning
    write(notes / "10-项目" / "10-项目_知识库项目" / "旧.md", GOOD_BODY)  # critical
    report = hyg.scan(notes)
    levels = [i["level"] for i in report["issues"]]
    assert levels == sorted(levels, key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x])
    s = report["summary"]
    assert s["critical"] >= 1 and s["warning"] >= 1
    assert s["issues"] == len(report["issues"])
    assert s["scanned_md"] == 2 and s["active_md"] == 2


# ── 14. main() 契约：--json 可解析、退出码与严重度一致 ──

def test_main_json_and_exit_code(tmp_path, capsys):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "空壳.md", "# 空壳\n")
    rc = hyg.main(["--json", "--notes-dir", str(notes)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["summary"]["warning"] == 1
    assert one(payload, "empty_shell")["paths"] == ["10-项目/空壳.md"]


def test_main_quiet_ok_clean(tmp_path, capsys):
    notes = make_notes(tmp_path)
    write(notes / "10-项目" / "干净.md", "---\ntitle: 干净\n---\n\n" + GOOD_BODY)
    rc = hyg.main(["--json", "--quiet-ok", "--notes-dir", str(notes)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"] == []


def test_main_missing_dir(tmp_path, capsys):
    rc = hyg.main(["--notes-dir", str(tmp_path / "不存在")])
    assert rc == 2
    assert "notes dir missing" in capsys.readouterr().err
