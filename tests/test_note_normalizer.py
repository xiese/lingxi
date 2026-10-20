#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note_normalizer 单测：目录识别、别名、日期、重名、归档、命名上限。

规则唯一来源: kb_migrate_lib.py（经 kb_rules_export.py 导出为 rules.yaml）。
运行: python3 -m pytest test_note_normalizer.py -q
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# 模块位置随部署布局而变：工具链目录（本地）/ services 目录（服务端）
for _cand in (
    os.environ.get("KB_NORMALIZER_DIR", ""),
    HERE,
    os.path.join(HERE, "..", "scripts", "kb_lib", "services"),
):
    if _cand and os.path.exists(os.path.join(_cand, "note_normalizer.py")):
        sys.path.insert(0, os.path.abspath(_cand))
        break

# 服务端类型表（scripts/kb_lib/config.py TYPE_MAP 的值域）
SERVER_TYPES = {"knowledge", "project", "writing", "diary", "record", "archive", "dream"}


def _find_rules():
    """规则文件：线上那份优先（服务端布局），否则用导出脚本现生成（本地布局）。

    两者都没有时跳过本模块（pytest.skip）：规则文件由 kb_migrate_lib.py 导出，
    属于生产侧产物，被 .gitignore 排除；开发库没有它属正常，不该让整个测试
    套件在收集阶段就中断。
    """
    env = os.environ.get("KB_RULES")
    if env and os.path.exists(env):
        return env
    deployed = os.path.abspath(os.path.join(HERE, "..", "笔记", ".kb", "rules.yaml"))
    if os.path.exists(deployed):
        return deployed
    exporter = os.path.join(HERE, "kb_rules_export.py")
    if not os.path.exists(exporter):
        import pytest
        pytest.skip(
            "缺少规则源：笔记/.kb/rules.yaml 不存在且本仓库无 kb_rules_export.py。"
            "设置 KB_RULES=/path/to/rules.yaml 可运行本模块。",
            allow_module_level=True,
        )
    out = os.path.join(tempfile.mkdtemp(prefix="kbrules-"), "rules.yaml")
    subprocess.run(
        [sys.executable, exporter, "-o", out],
        check=True, capture_output=True,
    )
    return out


RULES_PATH = _find_rules()


def _kb_root():
    """最小假笔记库：只建重名判定需要的目录。"""
    return tempfile.mkdtemp(prefix="kbroot-")


class TestNoteNormalizer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from note_normalizer import NoteNormalizer
        cls.NoteNormalizer = NoteNormalizer

    def setUp(self):
        self.root = _kb_root()
        self.n = self.NoteNormalizer(self.root, rules_path=RULES_PATH)

    def norm(self, **kw):
        note = {
            "title": kw.pop("title", "测试标题"),
            "content": kw.pop("content", "正文内容"),
            "type": kw.pop("type", "knowledge"),
            "status": kw.pop("status", ""),
            "tags": kw.pop("tags", []),
            "subdir": kw.pop("subdir", ""),
            "project": kw.pop("project", ""),
            "created": kw.pop("created", ""),
        }
        note.update(kw)
        return self.n.normalize(note)

    # ── 1. 目录识别：别名 ──
    def test_dir_by_alias(self):
        rel, fm, _w = self.norm(title="示例插件笔记", subdir="示例应用")
        self.assertEqual(os.path.dirname(rel), "10-项目/DSH")

    def test_dir_by_full_old_path_alias(self):
        rel, _fm, _w = self.norm(title="服务器运维记录", subdir="10-项目/运维")
        self.assertEqual(os.path.dirname(rel), "20-知识/运维")

    # ── 2. 别名正则 / 长别名优先（「示例」不得吃掉「示例项目」）──
    def test_longest_alias_wins(self):
        rel, _fm, _w = self.norm(title="示例项目 结算", subdir="示例项目")
        self.assertEqual(os.path.dirname(rel), "10-项目/示例/示例")

    def test_deep_subdir_prefix_extends(self):
        rel, _fm, _w = self.norm(title="结算错位", subdir="示例/示例")
        self.assertEqual(os.path.dirname(rel), "10-项目/示例/示例")

    # ── 2b. 深层路径保留（对齐 kb_migrate_lib.match_dir）──
    def test_deep_tail_preserved_on_project_merge(self):
        """旧目录 10-项目/chatgpt2api/专题 → 前缀替换后保留 /专题 尾巴。"""
        rel, _fm, _w = self.norm(
            title="专题研究", old_path="10-项目/chatgpt2api/专题/某笔记.md"
        )
        self.assertEqual(os.path.dirname(rel), "10-项目/2api/chatgpt2api/专题")

    def test_archive_deep_tail_preserved(self):
        rel, _fm, _w = self.norm(
            title="旧记录", old_path="50-档案/文档收敛_2026-07-11/历史迭代碎记/某笔记.md"
        )
        self.assertTrue(rel.startswith("50-档案/2026-07/"), rel)

    def test_unmatched_knowledge_subdir_kept_in_place(self):
        """未列出的 20-知识 子目录原位保留（UNMATCHED_TOP 的 k 标记）。"""
        rel, _fm, _w = self.norm(
            title="概率笔记", old_path="20-知识/概率与博彩数学/某笔记.md"
        )
        self.assertEqual(os.path.dirname(rel), "20-知识/概率与博彩数学")

    def test_unknown_project_goes_inbox(self):
        rel, fm, w = self.norm(title="完全认不出来的东西", subdir="zzz不存在zzz")
        self.assertTrue(rel.startswith("99-待整理/"), rel)
        self.assertTrue(any("未识别" in x or "待整理" in x for x in w), w)
        self.assertEqual(fm["status"], "draft")

    # ── 3. 归档：回归测试（原缺陷：归档笔记被扔进收件箱）──
    def test_archive_note_keeps_archive_and_status(self):
        rel, fm, _w = self.norm(
            title="知识库项目迭代记录",
            old_path="50-档案/文档收敛_2026-07-11/历史迭代碎记/知识库项目迭代_记录_2026-07-04_20_10.md",
        )
        self.assertTrue(rel.startswith("50-档案/"), rel)
        self.assertNotIn("99-待整理", rel)
        self.assertEqual(fm["status"], "archived")
        self.assertEqual(fm["type"], "archive")

    def test_archive_subdir_also_archived(self):
        rel, fm, _w = self.norm(title="旧版本快照", subdir="50-档案/2026-09")
        self.assertTrue(rel.startswith("50-档案/"), rel)
        self.assertEqual(fm["status"], "archived")

    # ── 4. 日期：事件型加日期前缀 ──
    def test_date_prefix_for_troubleshoot(self):
        rel, fm, _w = self.norm(
            title="DSh「反复断开」2026-09-29 复核：外壳被强杀",
            subdir="DSH",
            content="排障正文" * 100,  # 超过 300 字节，避免被空壳判定压成 draft
        )
        name = os.path.basename(rel)
        self.assertTrue(name.startswith("2026-09-29_"), name)
        self.assertEqual(fm["status"], "done")

    def test_hollow_event_note_stays_draft(self):
        """空壳（正文<300字节且状态为空）保持 draft 待清理，与迁移工具链一致。"""
        _rel, fm, _w = self.norm(
            title="2026-09-29 排障：短记录", subdir="DSH", content="短"
        )
        self.assertEqual(fm["status"], "draft")

    def test_date_from_created_fallback(self):
        rel, _fm, _w = self.norm(title="排障记录：某个崩溃", subdir="DSH", created="2026-08-01")
        self.assertTrue(os.path.basename(rel).startswith("2026-08-01_"), rel)

    # ── 5. 命名长度上限（方案验收线：p90 ≤25、无 >40）──
    def test_name_len_cap_log(self):
        rel, _fm, _w = self.norm(
            title="2026-09-29 这是一个非常非常非常长的排障记录标题用来测试截断行为是否生效",
            subdir="DSH",
        )
        name = os.path.basename(rel)
        self.assertLessEqual(len(name), 25, name)

    def test_name_len_cap_knowledge(self):
        rel, _fm, _w = self.norm(
            title="这是一个特别特别特别特别特别特别特别特别长的知识型标题需要被截断处理掉多余部分",
            subdir="DSH",
        )
        self.assertLessEqual(len(os.path.basename(rel)), 25, rel)

    def test_project_name_stripped(self):
        rel, _fm, _w = self.norm(title="DSH 插件机制说明", subdir="DSH")
        name = os.path.basename(rel)
        self.assertNotIn("DSH", name)
        self.assertIn("插件机制说明", name)

    # ── 6. 重名加序号 ──
    def test_dedup_suffix(self):
        d = os.path.join(self.root, "10-项目/DSH")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "插件机制说明.md"), "w").close()
        rel, _fm, _w = self.norm(title="DSH 插件机制说明", subdir="DSH")
        self.assertEqual(os.path.basename(rel), "插件机制说明_2.md")
        self.assertLessEqual(len(os.path.basename(rel)), 25)

    # ── 7. 体裁另存字段，type 只写服务端 7 类 ──
    def test_kind_field_and_server_type(self):
        """type 为空时走工具链兜底：项目目录 + 事件型 -> log，标题含「排障」进 排障/。"""
        for title, want_dir in (
            ("2026-09-29 排障：崩溃根因", "10-项目/DSH/排障/"),
            ("2026-09-29 开发日志", "10-项目/DSH/日志/"),
        ):
            rel, fm, _w = self.norm(title=title, subdir="DSH", type="")
            self.assertIn(fm["type"], SERVER_TYPES, (title, fm["type"]))
            self.assertIn("kind", fm, title)
            self.assertEqual(fm["kind"], "log", title)
            self.assertTrue(rel.startswith(want_dir), (title, rel))

    def test_kind_from_type_map(self):
        """调用方给了工具链认识的体裁词 -> 直接采信（对齐 TYPE_MAP）。"""
        for raw, want in (("排障", "troubleshoot"), ("日志", "log"),
                          ("配置", "config"), ("研究", "conclusion")):
            _rel, fm, _w = self.norm(title="某笔记", subdir="DSH", type=raw)
            self.assertEqual(fm["kind"], want, raw)

    def test_dirname_kind_when_type_unknown(self):
        """type 不认识时看目录末段：落在 排障/ 目录 -> troubleshoot。"""
        _rel, fm, _w = self.norm(
            title="某笔记", old_path="10-项目/DSH/排障/某笔记.md", type="project"
        )
        self.assertEqual(fm["kind"], "troubleshoot")

    # ── 13. 自动发现现有项目目录（设计文档第 25 行）──
    def test_discover_existing_project_dir(self):
        """10-项目 下真实存在的目录，即使规则表里没有也要认下来。"""
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "10-项目", "暴躁餐厅"))
            os.makedirs(os.path.join(td, "10-项目", "_assets"))
            n = self.NoteNormalizer(td, rules_path=RULES_PATH)
            self.assertEqual(n.resolve_dir(project="暴躁餐厅"), ("10-项目/暴躁餐厅", "m"))
            # _assets 这类下划线目录不得进入自动发现集合
            self.assertNotIn("_assets", n._discover_projects())

    def test_discover_new_project_immediately(self):
        """新建项目目录后立即生效，无需重启（缓存按 mtime 失效）。"""
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "10-项目"))
            n = self.NoteNormalizer(td, rules_path=RULES_PATH)
            self.assertEqual(n.resolve_dir(project="全新项目")[0], "99-待整理")
            os.makedirs(os.path.join(td, "10-项目", "全新项目"))
            os.utime(os.path.join(td, "10-项目"))  # 父目录 mtime 变化
            self.assertEqual(n.resolve_dir(project="全新项目"), ("10-项目/全新项目", "m"))

    def test_knowledge_type_for_knowledge_dir(self):
        rel, fm, _w = self.norm(title="运维知识整理", subdir="20-知识/运维")
        self.assertEqual(fm["type"], "knowledge")

    def test_project_type_for_project_dir(self):
        rel, fm, _w = self.norm(title="DSH 架构说明", subdir="DSH")
        self.assertEqual(fm["type"], "project")

    # ── 8. 状态映射 ──
    def test_status_map_from_legacy(self):
        _rel, fm, _w = self.norm(title="DSH 说明", subdir="DSH", status="final")
        self.assertEqual(fm["status"], "done")

    def test_status_unknown_to_draft(self):
        _rel, fm, _w = self.norm(title="DSH 说明", subdir="DSH", status="乱七八糟")
        self.assertEqual(fm["status"], "draft")

    # ── 9. 标签小写化 ──
    def test_tags_lowercased(self):
        _rel, fm, _w = self.norm(title="DSH 说明", subdir="DSH", tags=["DSH", "Android", "DSH"])
        self.assertEqual(fm["tags"], ["dsh", "android"])

    # ── 10. 收件箱不是死胡同：落点在扫描范围内 ──
    def test_inbox_is_scan_dir(self):
        rel, _fm, _w = self.norm(title="认不出的笔记")
        self.assertTrue(rel.startswith("99-待整理/"), rel)

    # ── 11. front matter 必备字段 ──
    def test_frontmatter_fields(self):
        _rel, fm, _w = self.norm(title="DSH 说明", subdir="DSH")
        for k in ("title", "type", "status", "created", "updated", "tags", "kind"):
            self.assertIn(k, fm, k)
        self.assertLessEqual(len(fm["title"]), 40)

    # ── 12. 短标题取自原文件名（对齐 plan_note），不是 front matter 的 title ──
    def test_short_name_comes_from_filename(self):
        """短标题取原文件名；「dsh」小写不匹配目录名「DSH」（大小写敏感），故不剥。"""
        rel, _fm, _w = self.norm(
            title="完全不同的长标题",
            old_path="10-项目/DSH/插件机制说明.md",
        )
        self.assertEqual(os.path.basename(rel), "插件机制说明.md")
        self.assertNotIn("完全不同的长标题", os.path.basename(rel))

    def test_project_name_strip_is_case_sensitive(self):
        """剥项目名大小写敏感：小写 dsh 不被目录名 DSH 剥掉（与工具链实测一致）。"""
        rel, _fm, _w = self.norm(
            title="x", old_path="10-项目/DSH/示例插件机制说明.md"
        )
        self.assertEqual(os.path.basename(rel), "示例插件机制说明.md")

    def test_root_readme_goes_pending(self):
        """一级根目录 README：工具链实测落 99-待整理/10-项目（10-项目 未列 UNMATCHED_EXACT）。"""
        rel, _fm, _w = self.norm(title="项目索引", old_path="10-项目/README.md")
        self.assertEqual(os.path.dirname(rel), "99-待整理/10-项目")

    def test_project_subdir_readme_kept_in_place(self):
        """项目子目录下的 README 原位保留。"""
        rel, _fm, _w = self.norm(title="索引", old_path="10-项目/DSH/README.md")
        self.assertEqual(os.path.dirname(rel), "10-项目/DSH")

    def test_root_loose_file_matched_by_keyword(self):
        """一级根目录散件按文件名关键词归项目（TOPFILE_HINT）。"""
        rel, _fm, _w = self.norm(title="斗地主算法说明", old_path="10-项目/斗地主调参记录.md")
        self.assertEqual(os.path.dirname(rel), "10-项目/斗地主")

    def test_project_name_strip_case_sensitive_sub2api(self):
        """Sub2API 不得被小写目录名 sub2api 剥掉（大小写敏感）。"""
        rel, _fm, _w = self.norm(
            title="升级到0.1.160", old_path="10-项目/sub2api/Sub2API升级到0.1.160_2026-07-18.md"
        )
        self.assertEqual(os.path.basename(rel), "Sub2API升级到0.1.160.md")


class TestPlanServerWriteLandingDir(unittest.TestCase):
    """方案 2（2026-10-16 用户拍板）：落点目录不存在时不接管，避免目录分裂。"""

    @classmethod
    def setUpClass(cls):
        import note_normalizer as nn
        cls.nn = nn

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kbland-")
        os.makedirs(os.path.join(self.root, ".kb"), exist_ok=True)
        shutil.copy(RULES_PATH, os.path.join(self.root, ".kb", "rules.yaml"))
        self.nn._SINGLETON.clear()
        self._mkdir("10-项目/知识库项目")

    def _mkdir(self, rel):
        os.makedirs(os.path.join(self.root, rel), exist_ok=True)

    def _plan(self, **kw):
        kw.setdefault("title", "写入规范化模块_上线记录")
        return self.nn.plan_server_write(self.root, **kw)

    def test_missing_landing_dir_not_taken_over(self):
        """落点 10-项目/知识库 不存在（存量迁移未跑），而 10-项目/知识库项目 存在 -> 不接管。"""
        r = self._plan(subdir="知识库项目", note_type="project")
        self.assertIsNone(r)

    def test_existing_landing_dir_still_taken_over(self):
        """落点目录已存在时照常接管（对照组，防止判据过度收敛）。"""
        self._mkdir("10-项目/DSH")
        r = self._plan(title="插件机制说明", subdir="DSH", note_type="project")
        self.assertIsNotNone(r)
        self.assertEqual(r["folder"], "10-项目/DSH")

    def test_inbox_exempt_from_landing_dir_check(self):
        """待整理区不存在也照常接管（用户拍板：认不出项目 -> 99-待整理）。"""
        r = self._plan(title="认不出归属的笔记", subdir="完全没见过的项目xyz", note_type="project")
        self.assertIsNotNone(r)
        self.assertTrue(r["rel"].startswith("99-待整理/"), r["rel"])


if __name__ == "__main__":

    unittest.main(verbosity=2)
