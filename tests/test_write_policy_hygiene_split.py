"""validate_write_hygiene 拆分后的守卫单元覆盖（重构配套）。

只补拆分前就存在的真实覆盖缺口，全部用例直接调公共服务函数：
- nested_top_dir_forbidden：subdir 里撞顶层目录名（含 type 首段豁免、70-经验 例外）
- same_basename_exists：notes_dir 传入时产生 warning 且不硬拦（50-档案 不计）
- subdir 绝对路径 / 空标题净化：路径穿越守卫的 subdir 侧与标题净化侧
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

TYPE_MAP = {"project": "10-项目", "knowledge": "20-知识", "archive": "50-档案"}


def _hygiene(title="ok", subdir="", note_type="project", notes_dir=None, **kw):
    from kb_lib.services.write_policy import validate_write_hygiene

    return validate_write_hygiene(
        note_type=note_type, subdir=subdir, title=title,
        content="x" * 900, notes_dir=notes_dir,
        type_map=kw.pop("type_map", TYPE_MAP), sanitize_fn=kw.pop("sanitize_fn", None),
        **kw,
    )


# ── nested_top_dir_forbidden ──

def test_nested_top_dir_rejected():
    err, _ = _hygiene(subdir="20-知识/10-项目", note_type="knowledge")
    assert err is not None
    assert err["error"] == "nested_top_dir_forbidden"
    assert "10-项目" in err["detail"]


def test_nested_top_dir_type_prefix_segment_exempted():
    """与当前 type 同名的首段不算嵌套撞名（create_note 语义）。"""
    err, _ = _hygiene(subdir="10-项目/某某项目/子目录", note_type="project")
    assert err is None


def test_nested_top_dir_owner_dir_alone_exempted():
    """70-经验 是用户私有目录，单独一段时例外。"""
    err, _ = _hygiene(subdir="70-经验", note_type="knowledge")
    assert err is None


def test_nested_top_dir_owner_dir_still_checked_below():
    """70-经验 例外只豁免它自己这一段；其下再撞顶层目录名照拦。"""
    err, _ = _hygiene(subdir="70-经验/10-项目", note_type="knowledge")
    assert err is not None
    assert err["error"] == "nested_top_dir_forbidden"
    assert "10-项目" in err["detail"]


def test_nested_top_dir_archive_mirror_exempted():
    """50-档案 下镜像原结构是设计如此。"""
    err, _ = _hygiene(subdir="50-档案/20-知识/工具研究", note_type="archive")
    assert err is None


# ── same_basename_exists（warning，不硬拦） ──

def test_same_basename_warns_not_blocks(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目").mkdir(parents=True)
    (notes / "50-档案").mkdir(parents=True)
    (notes / "10-项目" / "foo.md").write_text("x", encoding="utf-8")
    (notes / "50-档案" / "foo.md").write_text("x", encoding="utf-8")
    err, warns = _hygiene(title="foo", note_type="knowledge", notes_dir=notes)
    assert err is None
    assert len(warns) == 1
    assert warns[0]["kind"] == "same_basename_exists"
    # 50-档案 内的同名文件不计入
    assert warns[0]["paths"] == ["10-项目/foo.md"]


def test_same_basename_absent_no_warning(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "20-知识").mkdir(parents=True)
    err, warns = _hygiene(title="唯一标题", note_type="knowledge", notes_dir=notes)
    assert err is None and warns == []


def test_project_readme_exempt_from_basename_warning(tmp_path):
    notes = tmp_path / "笔记"
    (notes / "10-项目").mkdir(parents=True)
    (notes / "10-项目" / "README.md").write_text("x", encoding="utf-8")
    err, warns = _hygiene(title="README", note_type="project", notes_dir=notes)
    assert err is None and warns == []


# ── 路径穿越守卫：subdir 绝对路径 / 标题空净化 ──

def test_subdir_fullwidth_separator_rejected():
    """ASCII 首斜杠会被归一化剥掉（不报错、按相对路径用），全角分隔符则硬拦。"""
    for bad in ("／abs", "＼abs"):
        err, _ = _hygiene(subdir=bad)
        assert err is not None and err["error"] == "path_forbidden"
        assert "绝对路径" in err["detail"]
    # 真实行为：ASCII 首斜杠/反斜杠在归一化阶段即被剥掉，不会触发绝对路径守卫
    err, _ = _hygiene(subdir="/abs")
    assert err is None


@pytest.mark.parametrize("bad", ["", "   ", "...", "."])
def test_title_blank_or_dots_rejected(bad):
    err, _ = _hygiene(title=bad)
    assert err is not None and err["error"] == "path_forbidden"
    assert err["detail"] == "title 净化后为空"
