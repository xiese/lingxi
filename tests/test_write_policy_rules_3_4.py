"""规则 3（新项目必须先在项目表登记）与规则 4（一个项目只有一个总览入口）的单元覆盖。

背景：项目区写入规则 v2 里写了五条规则，但规则 3、规则 4 之前**只有文案没有代码**
——`10-项目/README.md` 的项目表从来没被任何一行代码读过。结果是：
  · 谁都能随手开一个新目录（「一篇一目录」的唯一复发入口）；
  · 已经有 `项目总览.md` 的项目，再写一篇还会多生成一份 `<名>项目.md`（双入口）。

本文件钉住四条边界：
  A. 新建目录未登记 → 拦（project_dir_unregistered）
  B. 存量目录未登记 → **放行**（历史欠账不能把正常写入锁死）
  C. 台账不可读（README 缺失）→ **放行**（守卫失效必须退化成不拦）
  D. 已有总览入口时，再写主档/再写总览 → warning，且 project_doc 不再生成第二份

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
    """建一个最小笔记库：10-项目/README.md + 两个已登记项目目录。"""
    root = tmp / "笔记"
    proj = root / "10-项目"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text(readme, encoding="utf-8")
    (proj / "甲库").mkdir()
    (proj / "乙架").mkdir()
    return root


def _hygiene(notes_dir, title="某篇笔记", subdir="", note_type="project"):
    from kb_lib.services import write_policy

    # 台账是 mtime 缓存的，测试之间必须清掉，否则换库后读到上一个库的结果
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


# ── 规则 3：新建目录必须登记 ──

def test_未登记的新目录被拦(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="丙站")
    assert err is not None, "未登记的新目录必须被拦"
    assert err["error"] == "project_dir_unregistered"
    assert "丙站" in err["detail"]


def test_已登记的项目目录放行(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="甲库")
    assert err is None, err


def test_已登记项目带子目录放行(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="甲库/更新")
    assert err is None, err


def test_存量目录未登记也放行(tmp_path):
    """历史欠账：目录已经在，就不该因为没登记而拦正常写入。"""
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "老项目").mkdir()   # 目录在，但 README 没登记
    err, _ = _hygiene(notes, subdir="老项目")
    assert err is None, err


def test_台账缺失时放行(tmp_path):
    """守卫失效必须退化成不拦 —— 不能因为读不到 README 把全库写入锁死。"""
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "README.md").unlink()
    err, _ = _hygiene(notes, subdir="丙站")
    assert err is None, err


def test_非project类型不适用规则3(tmp_path):
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="丙站", note_type="knowledge")
    assert err is None, err


def test_README里的链接目标也算登记(tmp_path):
    """只有链接、表格首列写法不规范时也要认（宁可放宽）。"""
    readme = "| 名称 | 备注 |\n|---|---|\n| 丁势 | [丁势项目.md](./丁势项目.md) |\n"
    notes = _make_kb(tmp_path, readme)
    err, _ = _hygiene(notes, subdir="丁势")
    assert err is None, err


def test_README改动后缓存失效(tmp_path):
    """README 加了新项目，同一个进程里下一次写入必须立刻放行（mtime 缓存）。"""
    notes = _make_kb(tmp_path)
    err, _ = _hygiene(notes, subdir="戊赛")
    assert err is not None, "改动前应被拦"

    readme = notes / "10-项目" / "README.md"
    readme.write_text(README + "| 戊赛 | [戊赛项目.md](./戊赛项目.md) | [戊赛/](./戊赛/) |\n",
                      encoding="utf-8")
    err, _ = _hygiene(notes, subdir="戊赛")
    assert err is None, "README 登记后必须立刻放行，不能被旧缓存挡住"


# ── 规则 4：一个项目只有一个总览入口 ──

def test_已有主档再写总览告警(tmp_path):
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "甲库项目.md").write_text("# 甲库\n", encoding="utf-8")
    err, warns = _hygiene(notes, title="项目总览", subdir="甲库")
    assert err is None, err
    kinds = [w.get("kind") for w in warns]
    assert "project_dual_entry" in kinds, warns


def test_已有总览再写主档告警(tmp_path):
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "项目总览.md").write_text("# 总览\n", encoding="utf-8")
    err, warns = _hygiene(notes, title="甲库项目", subdir="甲库")
    assert err is None, err
    kinds = [w.get("kind") for w in warns]
    assert "project_dual_entry" in kinds, warns


def test_只有一个入口时不告警(tmp_path):
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "项目总览.md").write_text("# 总览\n", encoding="utf-8")
    err, warns = _hygiene(notes, title="普通笔记", subdir="甲库")
    assert err is None, err
    assert "project_dual_entry" not in [w.get("kind") for w in warns], warns


def test_规则4不硬拦(tmp_path):
    """双入口只提示不拦 —— 硬拦会让存量项目的正常写入全部失败。"""
    notes = _make_kb(tmp_path)
    (notes / "10-项目" / "甲库" / "甲库项目.md").write_text("# 甲库\n", encoding="utf-8")
    err, _ = _hygiene(notes, title="项目总览", subdir="甲库")
    assert err is None


# ── 规则 4 的生成侧：已有人工入口就不再生成第二份 ──

def test_人工入口存在时不生成第二份(tmp_path):
    from kb_lib.services import project_doc as PD

    notes = _make_kb(tmp_path)
    pdir = notes / "10-项目" / "甲库"
    entry = pdir / "项目总览.md"
    entry.write_text("---\ntitle: 甲库总览\n---\n\n# 甲库 · 项目总览\n\n## 干什么\n\n（手写）\n",
                     encoding="utf-8")
    before = entry.read_text(encoding="utf-8")

    note = pdir / "某篇笔记.md"
    note.write_text("# 某篇笔记\n", encoding="utf-8")
    out = PD.on_project_note_written(notes, note, "10-项目/甲库/某篇笔记.md", "project")

    assert out is None, f"已有人工入口时不该生成第二份，实际生成：{out}"
    assert not (pdir / "甲库项目.md").exists(), "不该多生成 甲库项目.md"
    assert entry.read_text(encoding="utf-8") == before, "人工入口一个字都不许动"


def test_没有入口时照常生成(tmp_path):
    from kb_lib.services import project_doc as PD

    notes = _make_kb(tmp_path)
    pdir = notes / "10-项目" / "乙架"
    note = pdir / "第一篇.md"
    note.write_text("# 第一篇\n", encoding="utf-8")
    out = PD.on_project_note_written(notes, note, "10-项目/乙架/第一篇.md", "project")

    assert out == "10-项目/乙架/乙架项目.md", out
    assert (pdir / "乙架项目.md").is_file()


def test_普通同名笔记不算入口(tmp_path):
    """`<目录名>.md` 是常见普通笔记名，不能被当成入口把生成挡掉。"""
    from kb_lib.services import project_doc as PD

    notes = _make_kb(tmp_path)
    pdir = notes / "10-项目" / "乙架"
    (pdir / "乙架.md").write_text("# 乙架的一篇普通笔记\n", encoding="utf-8")
    note = pdir / "第一篇.md"
    note.write_text("# 第一篇\n", encoding="utf-8")
    out = PD.on_project_note_written(notes, note, "10-项目/乙架/第一篇.md", "project")

    assert out == "10-项目/乙架/乙架项目.md", out
