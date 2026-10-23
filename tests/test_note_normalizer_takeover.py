#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note_normalizer 落点判据测试（T-10）。

背景：`note_normalizer.plan_server_write()` 有一条「落点目录不存在就放弃接管」
的判据（`services/note_normalizer.py:601`），意图是「存量迁移没跑完时，不要让
新写入凭空新建目标态目录、造成同一项目两份目录」。

问题：这条判据过严。**父目录明明存在、只差一层子目录**的情况也被放弃，
导致带 subdir 的写入绝大多数根本没被规范化。

本测试**只测这条判据本身**，不测业务映射（那是 rules.yaml 的职责）：
每个场景的 subdir 直接给完整路径，`dir_map` 里做恒等映射，这样落点完全由
`dir_map` 决定，判据是否放行一目了然。

**测试自带一份最小假规则**，不读生产 rules.yaml——生产规则含业务目录名，
仓库闸门禁止业务名入库。

运行：python3 -m pytest tests/test_note_normalizer_takeover.py -q -s
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.environ.get("KB_NORMALIZER_DIR", ""),
    os.path.join(HERE, "..", "scripts", "kb_lib", "services"),
):
    if _cand and os.path.exists(os.path.join(_cand, "note_normalizer.py")):
        sys.path.insert(0, os.path.abspath(_cand))
        break


# 最小假规则：dir_map 全做恒等映射，落点 = subdir 原样。
# 这样「接管与否」只取决于落点目录存在性判据，不受别名/类型映射干扰。
FAKE_RULES = """
version: 1
generated_from: "test-fixture"
note: "测试用最小规则，不含任何业务目录"
folders:
  index: "00-索引"
  projects: "10-项目"
  knowledge: "20-知识"
  record: "30-记录"
  dream: "60-梦境"
  experience: "70-经验"
  archive: "50-档案"
  inbox: "99-待整理"
dir_map:
  - ["10-项目", "10-项目", "m"]
  - ["20-知识", "20-知识", "k"]
  - ["30-记录", "30-记录", "k"]
  - ["30-写作", "30-记录", "k"]
  - ["40-日记", "40-日记", "k"]
  - ["50-档案", "50-档案", "z"]
  - ["60-梦境", "60-梦境", "k"]
dir_flags:
  "10-项目": "m"
  "20-知识": "k"
  "30-记录": "k"
  "40-日记": "k"
  "50-档案": "z"
  "60-梦境": "k"
unmatched_top:
  "10-项目": "m"
  "20-知识": "k"
  "30-记录": "k"
  "40-日记": "k"
  "50-档案": "z"
  "60-梦境": "k"
unmatched_exact: {}
target_dirs: []
aliases: {}
archive_prefixes: ["50-档案/2026-09", "50-档案/2026-07"]
archive_pending_batch: "待定"
kind_subdirs: {}
kind_subdir_order: []
dirname_kind: {}
kind_map: {}
status_map: {}
type_map: {}
tag_lower_map: {}
name_caps: {"log": 11, "knowledge": 20}
log_hint: "日志|开发记录|研究轮|调参|排障|修复|事故|复核|实施记录|复盘|实验"
log_subdir: "更新"
log_subdir_skip: []
troubleshoot_hint: "排障|修复"
troubleshoot_subdir: "运维"
date_re: "^(\\\\d{4}-\\\\d{2}-\\\\d{2})"
top_order: ["10-项目", "20-知识", "30-记录", "40-日记", "50-档案", "60-梦境"]
topfile_hint: []
"""


def _plan(notes_dir, **kw):
    from note_normalizer import plan_server_write
    return plan_server_write(notes_dir, **kw)


def _make_lib(dirs):
    root = tempfile.mkdtemp(prefix="kbtake-")
    notes = os.path.join(root, "笔记")
    os.makedirs(os.path.join(notes, ".kb"), exist_ok=True)
    with open(os.path.join(notes, ".kb", "rules.yaml"), "w", encoding="utf-8") as f:
        f.write(FAKE_RULES)
    for d in dirs:
        os.makedirs(os.path.join(notes, d), exist_ok=True)
    return notes


# 14 种场景：(名称, 库中已有目录, subdir, type, 期望是否接管)
# 「期望」按收窄后的判据给出：落点存在→接管；缺一层且父目录在→接管；
# 缺两层及以上→不接管；inbox→豁免接管；无 subdir→不接管（:586，本轮不改）。
SCENARIOS = [
    # —— 落点已存在：本来就会接管，作为对照组 ——
    ("S01 项目-落点存在",        ["10-项目/示例A"],          "10-项目/示例A",  "project",   True),
    ("S02 知识-落点存在",        ["20-知识/运维"],           "20-知识/运维",   "knowledge", True),
    ("S03 记录-落点存在",        ["30-记录"],                "30-记录",        "record",    True),
    ("S04 归档-落点存在",        ["50-档案/2026-09"],        "50-档案/2026-09", "archive",  True),
    ("S05 梦境-落点存在",        ["60-梦境"],                "60-梦境",        "dream",     True),

    # —— 落点缺一层、父目录在：本次改动新增的接管范围 ——
    ("S06 项目-缺一层",          ["10-项目"],                "10-项目/示例B",  "project",   True),
    ("S07 知识-缺一层",          ["20-知识"],                "20-知识/研究",   "knowledge", True),
    ("S08 归档-缺一层",          ["50-档案"],                "50-档案/2026-07", "archive",  True),
    ("S09 日记-缺一层",          ["40-日记"],                "40-日记/2026",   "diary",     True),

    # —— 落点缺两层及以上：仍然不接管（防止造出整条新分支）——
    ("S10 项目-缺两层",          ["10-项目"],                "10-项目/甲/乙",  "project",   False),
    ("S11 归档-缺两层",          ["50-档案"],                "50-档案/2026-09/待定", "archive", False),

    # —— 边界：inbox 豁免；无 subdir 由 :586 拦下（本轮不改）——
    ("S12 未识别项目-进待整理",  ["99-待整理"],              "完全没听过的项目名", "project", True),
    ("S13 知识-无 subdir",       ["20-知识"],                "",               "knowledge", False),
    ("S14 日记-无 subdir",       ["40-日记"],                "",               "diary",     False),
]


@pytest.mark.parametrize("name,dirs,subdir,note_type,expect", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_landing_rule(name, dirs, subdir, note_type, expect):
    notes = _make_lib(dirs)
    r = _plan(notes, title="测试标题", note_type=note_type, subdir=subdir)
    got = r is not None
    if got:
        print(f"{name}: 接管 -> {r['folder']}")
    else:
        print(f"{name}: 未接管")
    assert got == expect, f"{name}: 期望接管={expect}，实际={got}"


def test_takeover_rate(capsys):
    """汇总接管率，作为改前/改后对比的基线数字。"""
    taken = 0
    rows = []
    for name, dirs, subdir, note_type, expect in SCENARIOS:
        notes = _make_lib(dirs)
        r = _plan(notes, title="测试标题", note_type=note_type, subdir=subdir)
        if r is not None:
            taken += 1
            rows.append(f"  ✅ {name} -> {r['folder']}")
        else:
            rows.append(f"  ❌ {name} 未接管")
    with capsys.disabled():
        print(f"\n接管率: {taken}/{len(SCENARIOS)}")
        print("\n".join(rows))
    assert taken >= 9, f"接管率 {taken}/{len(SCENARIOS)} 低于收窄判据的预期下限 9"
