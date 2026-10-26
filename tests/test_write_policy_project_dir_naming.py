"""项目目录命名守卫（2026-10-26 新增）的单元覆盖。

背景：实测项目区里有 21 个目录是「一篇笔记一个目录」，目录名就是那次任务的标题
（<项目>-整合服务 / <项目>-算法池总览UI / <长任务名>与动态结算 …）。
根因是调用方把任务标题当 subdir 传，落点父目录存在就接管建目录。

本文件钉住两条新守卫的行为边界：
- _guard_project_dir_action_word：项目目录名不能以动作词结尾
- _guard_project_dir_depth：project 的有效 subdir 最多两段

同时钉住**不该被误伤**的合法项目名，避免守卫过宽把正常写入也拦掉。
（用抽象代号而非真实业务名，仓库闸门禁止业务名进仓库。）
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

TYPE_MAP = {"project": "10-项目", "knowledge": "20-知识", "archive": "50-档案"}


def _hygiene(title="守卫测试标题", subdir="", note_type="project"):
    from kb_lib.services.write_policy import validate_write_hygiene

    return validate_write_hygiene(
        note_type=note_type,
        subdir=subdir,
        title=title,
        content="x" * 900,
        notes_dir=None,
        type_map=TYPE_MAP,
        sanitize_fn=None,
    )


# ── 动作词结尾：必须拦（实测 21 个坏目录的模式样本） ──

@pytest.mark.parametrize(
    "bad",
    [
        "甲库-整合服务",
        "甲库-算法池总览UI",
        "甲库-开奖历史采集",
        "甲库-审查与修复",
        "甲库-服务器运维",
        "乙架-会话与插件治理",
        "丙站新架构",
        "丙站五游戏时效下注与动态结算",
        "丁势-统计走势图",
        "戊赛-测试库",
        "己盟-测试机",
    ],
)
def test_action_word_tail_rejected(bad):
    err, _ = _hygiene(subdir=bad)
    assert err is not None, f"应拦下：{bad}"
    assert err["error"] == "project_dir_action_word_forbidden"
    assert bad.split("/")[0] in err["detail"]


def test_action_word_rejected_under_subdir_too():
    """第二段是配置/更新/专题 时，仍以第一段（项目名）判定。"""
    err, _ = _hygiene(subdir="甲库-整合服务/更新")
    assert err is not None
    assert err["error"] == "project_dir_action_word_forbidden"


# ── 动作词结尾：不该误伤（合法项目名） ──

@pytest.mark.parametrize(
    "ok",
    [
        "甲库",
        "乙架",
        "丙站",
        "丁势",
        "戊赛",
        "己盟",
        "知识库",
        "魔兽RTS游戏",
        "sub2api",
        "tavern-cards",
        "手机桌宠",
        "画布",
        "工具集",
        "运维",
        "算法研究",
        "grokcli-2api",
    ],
)
def test_legal_project_name_passes(ok):
    err, _ = _hygiene(subdir=ok)
    assert err is None, f"不该拦：{ok} → {err}"


def test_short_name_with_action_tail_not_overblocked():
    """无分隔符时只在名字 ≥5 字才判动作词，短名（运维/画布）不受影响。"""
    err, _ = _hygiene(subdir="运维")
    assert err is None


def test_action_word_only_applies_to_project_type():
    """knowledge / archive 不受项目目录命名规则约束。"""
    err, _ = _hygiene(subdir="甲库-整合服务", note_type="knowledge")
    assert err is None
    err, _ = _hygiene(subdir="甲库-整合服务", note_type="archive")
    assert err is None


# ── 深度守卫 ──

def test_depth_two_allowed():
    for ok in ("甲库", "甲库/更新", "甲库/配置", "甲库/专题"):
        err, _ = _hygiene(subdir=ok)
        assert err is None, f"两段应放行：{ok} → {err}"


@pytest.mark.parametrize("bad", ["甲库/更新/子层", "a/b/c", "甲库/更新/子层/更深"])
def test_depth_over_two_rejected(bad):
    err, _ = _hygiene(subdir=bad)
    assert err is not None
    assert err["error"] == "project_dir_depth_forbidden"


def test_depth_guard_ignores_empty_segments():
    """多余斜杠不该被算成额外层级。"""
    err, _ = _hygiene(subdir="甲库//更新")
    assert err is None


def test_depth_guard_only_applies_to_project():
    """深度规则只约束 project；knowledge 的分类层级不该被这条拦。"""
    err, _ = _hygiene(subdir="工具研究/a/b", note_type="knowledge")
    assert err is None


def test_depth_guard_exempts_archive_mirror_structure():
    """archive 在 50-档案 下镜像原结构（20-知识/工具研究）是设计如此。"""
    err, _ = _hygiene(subdir="50-档案/20-知识/工具研究", note_type="archive")
    assert err is None


def test_depth_guard_exempts_top_dir_prefix():
    """create_note 允许带 "10-项目/" 前缀，这段前缀不算层级。"""
    err, _ = _hygiene(subdir="10-项目/某某项目/更新")
    assert err is None
    err, _ = _hygiene(subdir="10-项目/某某项目")
    assert err is None


def test_action_word_guard_exempts_top_dir_prefix():
    """带前缀时按剥掉前缀后的第一段判动作词。"""
    err, _ = _hygiene(subdir="10-项目/甲库-整合服务")
    assert err is not None
    assert err["error"] == "project_dir_action_word_forbidden"
