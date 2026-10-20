"""正文去套话：查重 / 精排共用的「清洗规则」单一真相。

背景（2026-10-17 修复）
----------------------
服务端 `kb_create_project_log` 等入口会生成**同一套五节模板**的正文：

    ## 背景
    本记录用于沉淀 `X` 本次开发、调试或配置变更的过程，避免后续遗忘关键决策和验证方式。
    ## 关键发现
    ### 本次目标 / ### 修改文件 / ### 关键实现
    ## 实施步骤
    1. 明确本次目标和影响范围。 … 5. 将结果写入知识库并重建/检查索引。
    ## 验证结果
    ## 后续建议

于是大量**主题完全不同**的笔记，前 200–400 字字面完全一致（模板套话 + `# 标题` 行）。
查重时：

1. 查重 query 用 `title + body[:400]`，而 body 此刻刚被插入 `# {title}`，套话全在；
2. `_rerank_doc` 送精排的文档 = `snippet(contents[:200]) + 命中词窗口`，
   而 snippet 就是 `# 标题` + 五节套话的前 200 字。

两者都把「套话」当成了正文相似度证据 —— 无关笔记（例如一份番茄炒蛋做法，
只要套了同一套模板）也能拿到高分，查重分数因此失真。

修法：规则**集中在本模块**，由调用点调用；禁止在调用点各自硬编码正则。

调用点
------
- `kb_lib/routes/__init__.py::_find_existing_doc` —— 清洗查重 query
- `kb_lib/search.py::_rerank_doc` —— 清洗送精排的 snippet 与命中窗口正文

设计取舍
--------
- **只在查重/精排两处清洗**，不改索引里的 `contents` 原文：索引正文还被
  阅读、图谱、导出等路径共用，清洗入库会污染这些用途。
- 剥完为空则**退回原文本**：宁可送带套话的正文，也不能送空文档给精排。
- `> 一句话：xxx` 只剥**前缀**、保留 xxx：摘要本身可能带主题信息，
  剥掉会让真正的主题词一起消失。
"""
from __future__ import annotations

import re

from .write_policy import REQUIRED_SECTIONS

__all__ = [
    "strip_frontmatter",
    "strip_boilerplate",
    "clean_for_match",
]

# ── 精确匹配的整行套话（去首尾空白后逐字比对）──────────────────────────
# 来源：scripts/kb_mcp_server.py::kb_create_project_log 生成的固定文案。
_BOILERPLATE_LINES = frozenset(
    {
        "本次未记录明显阻塞问题。",
        "继续观察实际使用效果；如果复现率不足，补充更严格的自动化规则和监控。",
        "1. 明确本次目标和影响范围。",
        "2. 检查相关文件、配置、服务状态或接口行为。",
        "3. 按最小变更原则修改实现或配置。",
        "4. 执行验证命令，确认功能可用。",
        "5. 将结果写入知识库并重建/检查索引。",
    }
)

# ── 正则匹配的整行套话 ────────────────────────────────────────────────
_BOILERPLATE_RES = (
    # 「本记录用于沉淀 `X` 本次开发、调试或配置变更的过程，避免后续遗忘…」
    re.compile(r"^本记录用于沉淀.*避免后续遗忘关键决策和验证方式。?$"),
)

# ── 标题行：`# 标题` / 五节标题 / 模板子标题 ────────────────────────────
# 一级标题（`# xxx`，无缩进）——写入时会自动插入，属模板而非正文。
_RE_H1 = re.compile(r"^#\s+\S")

# 五节模板标题（`## 背景` 等）+ 模板子标题（`### 本次目标` 等），
# 允许标题后带少量空白或 `：`。集合取自 write_policy.REQUIRED_SECTIONS，
# 保证「门禁要求哪几节」与「查重剥哪几节」永远同源。
_TEMPLATE_SECTION_TITLES = frozenset(
    re.sub(r"^#+\s*", "", s).strip() for s in REQUIRED_SECTIONS
) | frozenset(
    {
        "遇到的问题",
        "本次目标",
        "修改文件",
        "相关路径",
        "修改文件 / 相关路径",
        "关键实现",
    }
)

_RE_HEADING = re.compile(r"^(#{2,6})\s*(.+?)\s*[:：]?\s*$")

# `> 一句话：xxx` —— 只剥前缀
_RE_QUOTE_PREFIX = re.compile(r"^>\s*一句话\s*[:：]\s*")


def build_dedup_query(title, body, max_len=400):
    """构造查重 query：**先剥套话，再截断**。

    为什么必须这个顺序：服务端生成的五节模板笔记前 200–400 字**字面完全一致**，
    若先 `body[:400]` 再清洗，截到的全是套话，剥完可能只剩空串 —— 等于没查。
    先清洗再截断，截到的才是真正的内容。

    剥完为空时回退到 title（title 也为空则返回空串，调用方据此跳过查重）。
    """
    t = clean_for_match(title or "")
    b = clean_for_match(body or "")
    if not b:
        return t
    if not t:
        return b[:max_len]
    return (t + " " + b)[:max_len]


def strip_frontmatter(text):
    """剥掉开头的 YAML frontmatter（`---\\n…\\n---`）。

    幂等：没有 frontmatter 时原样返回。索引侧 `_collect_docs` 已剥过一次，
    这里再剥一次是为了让本模块单独调用时也成立。
    """
    if not text:
        return ""
    return re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.DOTALL)


def _is_template_heading(stripped: str) -> bool:
    """该行是否为模板标题行（五节标题 / 模板子标题）。"""
    m = _RE_HEADING.match(stripped)
    if not m:
        return False
    title = m.group(2).strip().rstrip("：:")
    return title in _TEMPLATE_SECTION_TITLES


def strip_boilerplate(text: str) -> str:
    """逐行剥掉模板套话，返回剩余正文。

    剥掉的内容：
    - 一级标题行（`# 标题`）—— 写入时自动插入的模板行
    - 五节模板标题与模板子标题行（`## 背景`、`### 本次目标`…）
    - 固定套话整行（`本记录用于沉淀…`、`本次未记录明显阻塞问题。`…）
    - `> 一句话：` 的**前缀**（保留冒号后的摘要内容）

    保留：`> 一句话：` 之后的摘要正文、以及一切非模板行。
    """
    if not text:
        return ""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _RE_H1.match(stripped):
            continue
        if _is_template_heading(stripped):
            continue
        if stripped in _BOILERPLATE_LINES:
            continue
        if any(r.match(stripped) for r in _BOILERPLATE_RES):
            continue
        # `> 一句话：xxx` → 只留 xxx
        m = _RE_QUOTE_PREFIX.match(stripped)
        if m:
            rest = stripped[m.end():].strip()
            if rest:
                out.append(rest)
            continue
        out.append(stripped)
    return "\n".join(out)


def clean_for_match(text: str) -> str:
    """查重 / 精排统一入口：frontmatter + 模板套话一次剥干净。

    剥完为空时退回原文本 —— 精排收到空文档会得零分，比带套话更失真。
    """
    if not text:
        return ""
    cleaned = strip_boilerplate(strip_frontmatter(text))
    return cleaned if cleaned.strip() else text
