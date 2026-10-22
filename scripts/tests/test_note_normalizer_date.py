#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-08 回归测试：note_normalizer 的日期保留。

背景（2026-10-22）：`build_name()` 里 `date_re.sub("_", stem)` 无条件把日期抹掉，
非事件型笔记的日期前缀因此丢失——`2026-10-19 项目进展` 被写成 `项目进展.md`。
事件型（is_log）本来就会重新拼 `YYYY-MM-DD_短标题`，所以只有非事件型受影响。

判据（对应任务清单 T-08 验收标准）：
1. 日期开头的非事件型笔记保留日期前缀；
2. 事件型仍拼 `YYYY-MM-DD_短标题`；
3. 不带日期的笔记行为不变。

依赖：`NoteNormalizer` 构造时要读 `.kb/rules.yaml`（本机开发库没有这个文件，
在生产机知识库根的 `笔记/.kb/rules.yaml`）。测试用环境变量
`KB_TEST_RULES` 指定；没给或文件不存在时整组 skip，不假装通过。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

RULES_ENV = "KB_TEST_RULES"


@pytest.fixture(scope="module")
def normalizer(tmp_path_factory):
    rules_src = os.environ.get(RULES_ENV, "")
    if not rules_src or not Path(rules_src).is_file():
        pytest.skip(f"未提供 {RULES_ENV}（指向 rules.yaml），跳过")
    root = tmp_path_factory.mktemp("kb-nn-date") / "笔记"
    (root / ".kb").mkdir(parents=True)
    shutil.copy2(rules_src, root / ".kb" / "rules.yaml")

    from kb_lib.services.note_normalizer import NoteNormalizer

    return NoteNormalizer(kb_root=str(root), rules_path=str(root / ".kb" / "rules.yaml"))


def _name(normalizer, stem, typ="knowledge"):
    rel, _fm, _warn = normalizer.normalize(
        {"title": stem, "content": f"# {stem}\n\n正文。", "type": typ, "subdir": "示例项目"}
    )
    return Path(rel).name


def test_日期开头的非事件型笔记保留日期(normalizer):
    assert _name(normalizer, "2026-10-19 项目进展") == "2026-10-19_项目进展.md"


def test_下划线日期分隔也保留(normalizer):
    assert _name(normalizer, "2026-10-19_项目进展") == "2026-10-19_项目进展.md"


def test_事件型仍拼日期前缀(normalizer):
    assert _name(normalizer, "2026-10-19 排障记录") == "2026-10-19_排障记录.md"


def test_日期在中间的事件型仍归位到日期前缀(normalizer):
    assert _name(normalizer, "排障记录 2026-10-19") == "2026-10-19_排障记录.md"


def test_无日期笔记行为不变(normalizer):
    assert _name(normalizer, "普通笔记标题") == "普通笔记标题.md"
