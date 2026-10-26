"""规则 3 补丁：项目**内部**新开子目录也必须拦（2026-10-26 第二轮）。

背景（生产实测）：
  `note_normalizer` 判定「事件型/日志类」笔记后，会自动把落点从
  `10-项目/<项目>/` 追加成 `10-项目/<项目>/日志/`（或 `排障/`）。
  同时 `project_doc.on_project_note_written()` 拿 `note_path.parent` 当项目目录，
  于是把「日志」当成了项目名 —— 它会往 `10-项目/<项目>/日志/` 里再生成一份
  `日志项目.md`。结果一个项目下多出「日志/」这个假项目目录 + 一个假主档。

  2026-10-26 审查时生产上有 5 处这种残留（各项目下的「日志」「配置」「运维」
  子目录里各躺着一份同名的 `*项目.md`）。

本文件钉住三条边界：
  A. 项目内**合法**子目录（配置/更新/专题/日志/排障）→ 放行（这是设计允许的落点）
  B. 项目内**非法**子目录（其它任何名字）→ 拦（project_subdir_unregistered）
  C. 目录已存在 → 放行（存量欠账不能锁死写入，与规则 3 同一口径）

用抽象代号而非真实业务名（仓库闸门禁止业务名进仓库）。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

TYPE_MAP = {"project": "10-项目", "knowledge": "20-知识", "archive": "50-档案"}

README = """# 10-项目

## 按领域导航

| 项目 | 主档 | 子目录 |
|------|------|--------|
| 甲库 | [甲库项目.md](./甲库项目.md) | [甲库/](./甲库/) |
| 乙架 | [乙架项目.md](./乙架项目.md) | [乙架/](./乙架/) |
"""


def _make_kb(tmp: Path, readme: str = README):
    root = tmp / "笔记"
    proj = root / "10-项目"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text(readme, encoding="utf-8")
    (proj / "甲库").mkdir()
    (proj / "乙架").mkdir()
    return root


def _hygiene(notes_dir, title="某篇笔记", subdir="", note_type="project"):
    from kb_lib.services import write_policy

    write_policy._LEDGER_CACHE.clear()
    return write_policy.validate_write_hygiene(
        note_type=note_type,
        subdir=subdir,
        title=title,
        content="x" * 900,
        notes_dir=notes_dir,
        type_map=TYPE_MAP,
        sanitize_fn=None,
    )


# ── A. 合法子目录放行 ──

@pytest.mark.parametrize("sub", ["配置", "更新", "专题", "日志", "排障"])
def test_项目内合法子目录放行(tmp_path, sub):
    """这五个是规则里写明的分类，note_normalizer 会自动往这写，必须放行。"""
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir=f"甲库/{sub}")
    assert err is None, err


# ── B. 非法子目录拦截 ──

def test_项目内非法子目录被拦(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="甲库/戊类")
    assert err is not None, "项目内自造分类目录必须被拦"
    assert err["error"] == "project_subdir_unregistered"
    assert "戊类" in err["detail"]


def test_项目内非法子目录不因项目已登记而放行(tmp_path):
    """甲库已登记，不代表可以在它下面随便开新目录 —— 这是「一篇一目录」的复发口。"""
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="甲库/丁专题二")
    assert err is not None
    assert err["error"] == "project_subdir_unregistered"


# ── C. 存量放行 ──

def test_已存在的子目录放行(tmp_path):
    """目录已经在 = 历史欠账，不能因为没在名单里就把写入锁死。"""
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "老分类").mkdir()
    err, _ = _hygiene(notes, subdir="甲库/老分类")
    assert err is None, err


def test_台账缺失时子目录守卫也放行(tmp_path):
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "README.md").unlink()
    err, _ = _hygiene(notes, subdir="甲库/戊类")
    assert err is None, "守卫失效必须退化成不拦"


def test_非project类型不受子目录规则约束(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="某分类/子分类", note_type="knowledge")
    assert err is None, err


# ── D. project_doc 不再把分类子目录当项目 ──

def test_日志目录下不生成日志项目md(tmp_path):
    """核心 bug：往 <项目>/日志/ 写笔记，不能生成 <项目>/日志/日志项目.md。

    正确行为是「往上找到真项目目录」，于是在项目根生成 `甲库项目.md`
    —— 日志是项目的一部分，项目该有主档，只是不该建在日志子目录里。
    """
    from kb_lib.services import project_doc as PD

    notes = _make_kb(tmp_path)
    pdir = notes / "10-项目" / "甲库"
    logdir = pdir / "日志"
    logdir.mkdir()
    note = logdir / "2026-01-01_某次修复.md"
    note.write_text("# 某次修复\n", encoding="utf-8")

    out = PD.on_project_note_written(
        notes, note, "10-项目/甲库/日志/2026-01-01_某次修复.md", "project"
    )

    assert not (logdir / "日志项目.md").exists(), "不该在日志子目录里生成 日志项目.md"
    assert out is None or out == "10-项目/甲库/甲库项目.md", f"主档必须落在项目根：{out}"


def test_项目根上仍照常生成主档(tmp_path):
    """修 bug 不能把正常生成一起干掉。"""
    from kb_lib.services import project_doc as PD

    notes = _make_kb(tmp_path)
    pdir = notes / "10-项目" / "乙架"
    note = pdir / "第一篇.md"
    note.write_text("# 第一篇\n", encoding="utf-8")

    out = PD.on_project_note_written(notes, note, "10-项目/乙架/第一篇.md", "project")
    assert out == "10-项目/乙架/乙架项目.md", out
