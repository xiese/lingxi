"""标签规范化与治理。

设计目标：
- 新建/更新笔记时自动规范大小写与同义标签
- 提供 dry-run 报告，便于批量治理
- 不强制 bulk rewrite 历史笔记（避免大面积改 frontmatter）
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable

# 安全加固：标签值不许带方括号、引号与控制字符——
# 它们会破坏 frontmatter 的 tags 行结构。
_TAG_UNSAFE_RE = re.compile(r"""[\[\]"']|[\x00-\x1f\x7f\u2028\u2029]""")
# 会破坏 frontmatter tags 行结构的字符：方括号（闭合/开启数组）与控制字符。
# 这类标签**整条丢弃**——早年是「删掉这些字符」，但删除会让前后片段粘在一起，
# 反而更难审查。
_TAG_STRUCT_BREAK_RE = re.compile(r"""[\[\]]|[\x00-\x1f\x7f\u2028\u2029]""")


def _safe_tag_value(v) -> str:
    """清洗单个标签：含结构破坏字符则整条丢弃，其余去掉引号与首尾空白。"""
    s = str(v)
    if _TAG_STRUCT_BREAK_RE.search(s):
        return ""
    return s.replace('"', "").replace("'", "").strip()

# 同义映射：别名 -> 规范标签（只放语义合并，不放大小写）
TAG_CANONICAL = {
    "迭代": "迭代优化",
    "迭代记录": "迭代优化",
    "版本迭代": "迭代优化",
    "自动迭代": "自动化迭代",
    "代码优化": "优化",
    "bug修复": "bug修复",
    "Bug修复": "bug修复",
    "项目记录": "开发日志",
    "cron-iter": "cron",
}

# 大小写规范（保留语义，统一展示）
CASE_CANONICAL = {
    "api": "API",
    "python": "Python",
    "grok": "Grok",
    "llm": "LLM",
    "agent": "Agent",
    "ocr": "OCR",
    "cron": "cron",
    "cronjob": "cronjob",
    "faiss": "FAISS",
    "mcp": "MCP",
    "sse": "SSE",
    "cli": "CLI",
}


def _split_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = re.split(r"[,，、\s]+", tags)
        return [p.strip() for p in parts if p and p.strip()]
    if isinstance(tags, (list, tuple, set)):
        out = []
        for t in tags:
            out.extend(_split_tags(t))
        return out
    s = str(tags).strip()
    return [s] if s else []


def normalize_tag(tag: str) -> str:
    t = str(tag or "").strip()
    if not t:
        return ""
    # 先同义
    if t in TAG_CANONICAL:
        t = TAG_CANONICAL[t]
    # 再大小写
    low = t.lower()
    if low in CASE_CANONICAL:
        return CASE_CANONICAL[low]
    return t


def normalize_tags(tags) -> list[str]:
    seen = set()
    out = []
    for t in _split_tags(tags):
        n = normalize_tag(t)
        if not n:
            continue
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def rewrite_tags_block(content: str, new_tags: list[str]) -> tuple[str, bool]:
    """把 frontmatter 里的 tags 替换成 new_tags，返回 (新内容, 是否改动)。

    覆盖三种写法，任一命中才算成功：
      1. `tags: [a, b]`      行内数组
      2. `tags:` 空值         补成行内数组
      3. `tags:` 后跟块序列    - a / - b 逐行，是 YAML 最常见的长写法

    原实现只处理第 1 种，且用 `if new_content != content` 判断是否落盘：
    块序列写法匹配不到 → 内容不变 → 不写盘，但调用方仍回报
    `{"action":"tagged"}`，用户以为打上了标签，实际一个都没写进去。
    返回布尔值让调用方按真实结果决定返回值，不再靠内容比较猜。
    """
    if not new_tags:
        return content, False
    # 安全加固：标签值拼进 frontmatter 行前先剔除方括号、引号与控制字符，
    # 避免破坏行结构；全部被清空则视为无可写内容。
    safe_tags = [_safe_tag_value(t) for t in new_tags]
    safe_tags = [t for t in safe_tags if t]
    if not safe_tags:
        return content, False
    inline = f"tags: [{', '.join(safe_tags)}]"

    # 1) 行内数组（可能跨行）
    new_content, n = re.subn(
        r"^tags:\s*\[[\s\S]*?\]", inline, content, count=1, flags=re.MULTILINE
    )
    if n:
        return new_content, True

    # 2) 块序列：tags: 之后的缩进 `- 值` 行整体替换
    block = re.compile(r"^tags:[ \t]*\n(?:[ \t]+-[^\n]*\n?)+", re.MULTILINE)
    new_content, n = block.subn(inline + "\n", content, count=1)
    if n:
        return new_content, True

    # 3) 空值 tags:
    new_content, n = re.subn(r"^tags:[ \t]*$", inline, content, count=1, flags=re.MULTILINE)
    if n:
        return new_content, True

    return content, False


def build_governance_report(tag_counts: dict) -> dict:
    """基于 /api/tags 返回的 {tag:count} 生成治理报告。"""
    counts = {str(k): int(v) for k, v in (tag_counts or {}).items() if k}
    total = sum(counts.values())
    # 大小写重复
    by_lower = defaultdict(list)
    for t, c in counts.items():
        by_lower[t.lower()].append((t, c))
    case_dups = {
        low: sorted(items, key=lambda x: -x[1])
        for low, items in by_lower.items()
        if len(items) > 1
    }
    # 同义建议
    synonym_hits = []
    for src, dst in TAG_CANONICAL.items():
        if src in counts and dst in counts and src != dst:
            synonym_hits.append({
                "from": src,
                "to": dst,
                "count": counts[src],
                "target_count": counts[dst],
            })
        elif src in counts and dst not in counts:
            # 规范名尚不存在，但仍建议改
            synonym_hits.append({
                "from": src,
                "to": dst,
                "count": counts[src],
                "target_count": 0,
            })
    synonym_hits.sort(key=lambda x: -x["count"])
    # 长尾
    long_tail = sorted(
        [(t, c) for t, c in counts.items() if c == 1],
        key=lambda x: x[0],
    )
    top = sorted(counts.items(), key=lambda x: -x[1])[:30]
    suggestions = []
    if case_dups:
        suggestions.append(f"发现 {len(case_dups)} 组大小写重复标签，建议统一大小写")
    if synonym_hits:
        suggestions.append(f"发现 {len(synonym_hits)} 个可合并同义标签（见 synonym_map）")
    if long_tail:
        suggestions.append(f"长尾单次标签 {len(long_tail)} 个，建议收敛到高频标签")
    if not suggestions:
        suggestions.append("标签分布较健康，无需立即批量改写")
    return {
        "total_tags": len(counts),
        "total_uses": total,
        "top_tags": [{"tag": t, "count": c} for t, c in top],
        "case_duplicates": {
            low: [{"tag": t, "count": c} for t, c in items]
            for low, items in case_dups.items()
        },
        "synonym_map": synonym_hits,
        "long_tail_count": len(long_tail),
        "long_tail_sample": [{"tag": t, "count": c} for t, c in long_tail[:40]],
        "suggestions": suggestions,
        "auto_normalize_on_write": True,
        "canonical_rules": {
            "semantic": TAG_CANONICAL,
            "case": CASE_CANONICAL,
        },
    }


def preview_normalize(tags: Iterable[str]) -> dict:
    raw = list(tags)
    return {"input": raw, "output": normalize_tags(raw)}


# 兼容旧名
governance_report = build_governance_report
