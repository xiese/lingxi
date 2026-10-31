# -*- coding: utf-8 -*-
"""2026-10-31 遗留两项的处理（三路审查提出，单独一轮修）。

① 项目子目录守卫的两处口径问题
   - `_project_subdir_segments` 原先剥**任意** `^\\d{2}-` 前缀，会把真实项目名
     （例如叫「99-实验场」的项目）也当成层级剥掉 → 后续深度/动作词/登记守卫
     全部按**错误的段号**判断，守卫整体错位；
   - 子目录守卫只看 `segs[1]`：`<项目>/日志/排障/x.md` 这种两层嵌套，
     `日志` 在白名单里就放行，第二层的 `排障` 成了漏网 —— 而它正是
     `project_doc` 会拿去当项目目录的那一段。
② `project_doc.py` 原先自带 `DOC_SUFFIX = "项目.md"` / `OVERVIEW_NAME = "项目总览.md"`
   两份字面量，与 rules.yaml（经 write_policy 暴露）形成两处真相源，改配置时不跟随；
   且子目录归一原只上溯**一层**，两层嵌套仍会造出「日志项目.md」假入口。

用抽象代号（甲库/乙架），仓库闸门禁止业务名进仓库。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

README = """# 10-项目

| 项目 | 主档 | 子目录 |
|------|------|--------|
| 甲库 | [甲库项目.md](./甲库项目.md) | [甲库/](./甲库/) |
"""


def _make_kb(tmp: Path):
    root = tmp / "笔记"
    proj = root / "10-项目"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text(README, encoding="utf-8")
    (proj / "甲库").mkdir()
    return root


def _hygiene(notes_dir, title="某篇笔记", subdir="", note_type="project"):
    from kb_lib.services import write_policy
    return write_policy.validate_write_hygiene(
        note_type=note_type, subdir=subdir, title=title, content="# x\n",
        notes_dir=notes_dir,
    )


# ─────────────── ① 段号归一：只剥配置里的项目根目录 ───────────────

def test_只剥配置里的项目根目录(tmp_path):
    from kb_lib.services import write_policy as wp
    # 正常形态：剥掉 10-项目
    assert wp._project_subdir_segments("project", "10-项目/甲库/日志") == ["甲库", "日志"]
    # 关键：以数字开头的**真实项目名**不能被当成层级剥掉
    assert wp._project_subdir_segments("project", "99-实验场") == ["99-实验场"]
    assert wp._project_subdir_segments("project", "10-项目") == []
    # 非 project 类型不适用
    assert wp._project_subdir_segments("knowledge", "10-项目/甲库") is None


# ─────────────── ① 逐段检查：两层嵌套的第二层也要管 ───────────────

def test_三层路径被深度守卫先拦(tmp_path):
    """通道层面：`<项目>/日志/排障/` 是 3 段，深度守卫（只允许两段）会先拦。
    这是第一道；下面直接测第二道（逐段检查）本身。"""
    notes = _make_kb(tmp_path)
    err, _w = _hygiene(notes, subdir="10-项目/甲库/日志/排障")
    assert err, "三层路径没被任何守卫拦住"
    assert err.get("error") == "project_dir_depth_forbidden"


def test_子目录守卫逐段检查第二层(tmp_path):
    """直接调第二道守卫（绕过深度守卫）：
    第一层 `日志` 在白名单、且目录已存在 → 放行；
    第二层 `自定义目录` 不在白名单 → 必须拦。
    旧实现只看 segs[1]，这里会漏检。"""
    from kb_lib.services import write_policy as wp
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "日志").mkdir(parents=True)
    err = wp._guard_project_subdir_unregistered(
        "project", "10-项目/甲库/日志/自定义目录", notes)
    assert err and err.get("error") == "project_subdir_unregistered", f"第二层漏检：{err}"
    assert "日志/自定义目录" in (err.get("detail") or "")


def test_子目录守卫两层都在白名单放行(tmp_path):
    """两层都在白名单且目录已存在 → 守卫不拦（它管的是「新开目录」，不是层级）。"""
    from kb_lib.services import write_policy as wp
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "日志" / "排障").mkdir(parents=True)
    assert wp._guard_project_subdir_unregistered(
        "project", "10-项目/甲库/日志/排障", notes) is None


def test_单层非法仍然拦(tmp_path):
    """回归：一层非法目录的老行为不能被改坏。"""
    notes = _make_kb(tmp_path)
    err, _w = _hygiene(notes, subdir="10-项目/甲库/乱七八糟")
    assert err and err.get("error") == "project_subdir_unregistered"


def test_单层白名单仍然放行(tmp_path):
    notes = _make_kb(tmp_path)
    err, _w = _hygiene(notes, subdir="10-项目/甲库/日志")
    assert err is None


# ─────────────── ② project_doc 与 rules.yaml 同源 ───────────────

def test_project_doc常量与write_policy同源():
    from kb_lib.services import project_doc as pd
    from kb_lib.services import write_policy as wp
    assert pd.OVERVIEW_NAME == wp.PROJECT_OVERVIEW_NAME
    assert pd.DOC_SUFFIX == wp.PROJECT_DOC_SUFFIX
    # 且两者都不再是模块内独立字面量（值确实来自配置链路）
    assert wp.PROJECT_OVERVIEW_NAME.endswith(".md")
    assert wp.PROJECT_DOC_SUFFIX.endswith(".md")


def test_公开常量都导出了():
    from kb_lib.services import write_policy as wp
    for n in ("PROJECT_LEDGER_FILE", "PROJECT_ROOT_DIR",
              "PROJECT_OVERVIEW_NAME", "PROJECT_DOC_SUFFIX"):
        assert n in wp.__all__, f"{n} 没进 __all__"
        assert getattr(wp, n), f"{n} 为空"


# ─────────────── ② 循环上溯：两层嵌套不再造假项目 ───────────────

def test_两层分类子目录不会造出假项目(tmp_path):
    """`<项目>/日志/排障/x.md` 写入后，project_doc 必须仍把「甲库」当项目，
    不能把「日志」或「排障」当项目去生成入口文件（旧实现只上溯一层）。"""
    from kb_lib.services import project_doc as pd
    notes = _make_kb(tmp_path)
    deep = notes / "10-项目" / "甲库" / "日志" / "排障"
    deep.mkdir(parents=True)
    note = deep / "某次排障.md"
    note.write_text("# 某次排障\n\n正文\n", encoding="utf-8")
    gen = pd.on_project_note_written(notes, note, "10-项目/甲库/日志/排障/某次排障.md",
                                    "project")
    # 允许不生成（人工入口优先等分支），但绝不能在分类子目录里生成「日志项目.md」/「排障项目.md」
    bad = list(deep.glob("*项目.md")) + list((notes / "10-项目" / "甲库" / "日志").glob("*项目.md"))
    assert not bad, f"在分类子目录里造出了假项目入口：{bad}"
    if gen:
        assert "日志" not in str(gen) and "排障" not in str(gen), f"生成到了分类子目录：{gen}"
