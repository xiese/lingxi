# -*- coding: utf-8 -*-
"""批 1：查重分数失真修复 — 模板套话剥离的回归测试。

背景
----
服务端 `kb_create_project_log` 等入口生成**同一套五节模板**的正文，导致
主题完全不同的笔记前 200–400 字字面一致。查重 query 与送精排的文档都
带这层套话，于是「套话」成了相似度证据，无关笔记也拿高分。

本测试锁三件事：
1. `clean_for_match` 确实把套话剥干净（frontmatter / `# 标题` / 五节标题 /
   固定套话行 / `> 一句话：` 前缀）；
2. 剥完仍保留真正的正文（不能把内容一起吃掉）；
3. 端到端：套五节模板的无关内容（番茄炒蛋做法）查重最高分 < 0.5。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest

from kb_lib.services.text_clean import (
    clean_for_match,
    strip_boilerplate,
    strip_frontmatter,
)


# ── 夹具：两份「套了同一套模板」的无关内容 ──────────────────────────────

TEMPLATE = """## 背景
本记录用于沉淀 `{topic}` 本次开发、调试或配置变更的过程，避免后续遗忘关键决策和验证方式。
## 关键发现
### 本次目标
- 记录 {topic} 的关键结论
### 修改文件
- {file}
## 实施步骤
1. 明确本次目标和影响范围。
2. 检查相关文件、配置、服务状态或接口行为。
3. 按最小变更原则修改实现或配置。
4. 执行验证命令，确认功能可用。
5. 将结果写入知识库并重建/检查索引。
## 验证结果
- {verify}
## 后续建议
继续观察实际使用效果；如果复现率不足，补充更严格的自动化规则和监控。
"""

TOMATO = TEMPLATE.format(
    topic="番茄炒蛋",
    file="番茄炒蛋做法.md",
    verify="成品酸甜适口，鸡蛋嫩滑。",
)

DOCKER = TEMPLATE.format(
    topic="Docker 镜像清理",
    file="scripts/clean.sh",
    verify="磁盘占用从 40G 降到 12G。",
)


def _wrap(title, body):
    """模拟服务端写入时的实际形态：frontmatter + `# 标题` + 正文。"""
    fm = "---\ntitle: %s\ntype: 项目\n---" % title
    return fm + "\n\n# " + title + "\n\n" + body


# ── 1. 单测：剥套话 ───────────────────────────────────────────────────

def test_strip_frontmatter():
    text = "---\ntitle: x\ntype: 项目\n---\n# 标题\n正文"
    assert strip_frontmatter(text).lstrip().startswith("# 标题")
    # 幂等：没有 frontmatter 时原样返回
    assert strip_frontmatter("# 标题\n正文") == "# 标题\n正文"
    assert strip_frontmatter("") == ""


def test_strip_boilerplate_removes_template_lines():
    out = strip_boilerplate(TOMATO)
    # 五节标题、模板子标题、固定套话行都不该留下
    for junk in (
        "## 背景",
        "## 关键发现",
        "## 实施步骤",
        "## 验证结果",
        "## 后续建议",
        "### 本次目标",
        "### 修改文件",
        "本记录用于沉淀",
        "避免后续遗忘关键决策和验证方式",
        "明确本次目标和影响范围",
        "按最小变更原则修改实现或配置",
        "继续观察实际使用效果",
    ):
        assert junk not in out, "套话未剥净: %s" % junk


def test_strip_boilerplate_keeps_real_content():
    out = strip_boilerplate(TOMATO)
    # 真正的正文必须留下 —— 否则就是「把内容一起吃掉了」
    assert "番茄炒蛋" in out
    assert "番茄炒蛋做法.md" in out
    assert "成品酸甜适口，鸡蛋嫩滑。" in out


def test_strip_boilerplate_quote_prefix_keeps_summary():
    """`> 一句话：xxx` 只剥前缀，摘要本身要留（摘要可能带主题词）。"""
    out = strip_boilerplate("> 一句话：这是关于波场哈希尾数的记录\n正文")
    assert "一句话" not in out
    assert "波场哈希尾数" in out


def test_clean_for_match_strips_h1_and_frontmatter():
    text = _wrap("番茄炒蛋", TOMATO)
    out = clean_for_match(text)
    assert not out.startswith("---")
    assert "# 番茄炒蛋" not in out
    assert "番茄炒蛋" in out  # 主题词保留
    assert "## 背景" not in out


def test_clean_for_match_falls_back_when_empty():
    """全篇都是套话时退回原文 —— 送空文档给精排比送套话更失真。"""
    only_boilerplate = "## 背景\n本记录用于沉淀 `x` 本次开发、调试或配置变更的过程，避免后续遗忘关键决策和验证方式。\n"
    assert clean_for_match(only_boilerplate) == only_boilerplate
    assert clean_for_match("") == ""


# ── 2. 核心断言：套模板的无关内容，清洗后几乎无公共词 ────────────────────

def test_template_wrapper_no_longer_dominates():
    """两份套同一模板的无关内容，清洗后的公共内容应远小于清洗前。

    这是「分数失真」的机理证明：清洗前两者开头大段字面一致，
    清洗后只剩各自主题词。
    """
    a_raw, b_raw = _wrap("番茄炒蛋", TOMATO), _wrap("Docker 镜像清理", DOCKER)
    a_clean, b_clean = clean_for_match(a_raw), clean_for_match(b_raw)

    # 清洗前：模板行（长句）两边都有
    assert "本记录用于沉淀" in a_raw and "本记录用于沉淀" in b_raw
    # 清洗后：模板行消失
    assert "本记录用于沉淀" not in a_clean
    assert "本记录用于沉淀" not in b_clean
    # 各自主题词仍在，且不互相串
    assert "番茄炒蛋" in a_clean and "番茄炒蛋" not in b_clean
    assert "Docker" in b_clean and "Docker" not in a_clean


def test_clean_output_is_shorter_than_template_overhead():
    """清洗后长度应显著小于原文 —— 证明剥掉的是套话而非内容。"""
    raw = _wrap("番茄炒蛋", TOMATO)
    clean = clean_for_match(raw)
    assert len(clean) < len(raw) * 0.7, (
        "清洗后仍有 %d/%d 字符，套话可能没剥净" % (len(clean), len(raw))
    )


# ── 3. 端到端：真实查重打分 ───────────────────────────────────────────

@pytest.fixture(scope="module")
def dedup_env(tmp_path_factory):
    """临时实例：只读地拿 search_index，不碰生产索引。"""
    import kb_lib.search as search_mod
    from kb_lib.routes import _find_existing_doc

    return search_mod, _find_existing_doc


def test_unrelated_template_content_scores_below_0_5(dedup_env):
    """套五节模板的无关内容（番茄炒蛋做法）查重最高分 < 0.5。

    只读地用真实索引打分：把番茄炒蛋正文当「新文档」去查已有笔记，
    分数应低于 0.5 —— 修前它会被模板套话抬到高位。
    """
    search_mod, _find_existing_doc = dedup_env
    query = clean_for_match("番茄炒蛋 " + TOMATO[:400])
    try:
        results, _degraded = search_mod.search_index.search(query, top=5, with_status=True)
    except Exception as exc:  # 索引不可用（CI 无索引）→ 跳过，不算失败
        pytest.skip("索引不可用，跳过端到端打分：%s" % exc)
    if not results:
        pytest.skip("索引为空，跳过端到端打分")
    top = max(float(r.get("score") or 0) for r in results)
    assert top < 0.5, "无关内容查重得分 %.4f 过高（模板套话未被剥离）" % top
