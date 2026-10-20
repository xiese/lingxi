"""项目主文档自动生成/更新（T12）。

口径（2026-10-19 拍板）：项目主文档只用 **五段式**骨架 ——
`## 背景` / `## 当前状态` / `## 关键发现` / `## 最近笔记` / `## 后续建议`。
存量 19 份手工主文档**不回补、不改写**，本模块只处理「本次写入」触发的项目。

三个动作：
  1. 新建项目（项目目录下没有任何主文档）→ 按 `_模板/项目主文档.md` 生成一份；
  2. 项目已有主文档 → 只更新 `updated` 与「最近笔记」段（追加去重、最多 10 条）；
     其余四段是人写的，机器一个字都不碰；
  3. 落点父目录（如 `10-项目/` 根）已有 `<项目名>项目.md` / `<项目名>.md` 时，
     视为存量主文档，**既不生成也不更新**（存量保护）。

两条护栏：
  · `KB_PROJECT_DOC=0/off/false/no` 整体停用（默认开）；
  · 任何异常只记 warning，绝不让 `POST /notes` 失败（与 plan_server_write 一致）。

聚合刷新：同一项目 60 秒内多次写入只刷一次，模块级 dict 记
`{项目目录: 上次刷新时间}`，只在进程内，不落盘。首次生成不记账 ——
否则「建完立刻写第二篇」会被 60 秒窗口吞掉，测试与实感都对不上。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("kb.project_doc")

__all__ = [
    "SECTIONS",
    "REFRESH_INTERVAL",
    "enabled",
    "doc_name",
    "on_project_note_written",
]

# 五段式：顺序即契约，改动会让「顺序一致」的验收失效
SECTIONS = ["## 背景", "## 当前状态", "## 关键发现", "## 最近笔记", "## 后续建议"]
TEMPLATE_NAME = "项目主文档"
# 内置骨架：与 `笔记/_模板/项目主文档.md` 逐字一致。生产库的笔记目录不进仓库，
# 部署不会带模板过去，没有这份兜底就等于功能永远不生效。
BUILTIN_TEMPLATE = """---
title: 项目主文档
description: 新建项目时自动生成的项目主文档骨架（五段式）；机器只维护 updated 与「最近笔记」，其余四段由人写
schema_version: 1
---

# {{title}}

## 背景
一句话说明这个项目为什么建立、要解决什么问题。

## 当前状态
项目当前进展、正在做什么、卡在哪里。

## 关键发现
踩过的坑、验证过的结论、拍板过的口径。

## 最近笔记
- 项目下最近的笔记链接（自动维护，最多 10 条）

## 后续建议
下一步要做什么、有哪些待办。
"""
DOC_SUFFIX = "项目.md"
REFRESH_INTERVAL = 60.0          # 同项目聚合刷新窗口（秒）
MAX_RECENT = 10                  # 「最近笔记」最多保留条数

_ENABLED_OFF = {"0", "off", "false", "no"}
# {项目目录绝对路径: 上次刷新时间戳}，进程内，不落盘
_LAST_REFRESH: dict = {}
# {项目目录绝对路径: [窗口内被聚合掉的笔记相对路径]}，下次刷新时一并补进
# 「最近笔记」——否则 60 秒内后写的几篇永远进不了主文档，等于静默丢链。
_PENDING: dict = {}

_LIST_RE = re.compile(r"^\s*[-*]\s+")
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def enabled() -> bool:
    """总开关，默认开；KB_PROJECT_DOC=0 时整条链路不介入。"""
    return (os.environ.get("KB_PROJECT_DOC", "1") or "").strip().lower() not in _ENABLED_OFF


def doc_name(project_name: str) -> str:
    """项目名 → 主文档文件名（带 .md）；已以「项目」结尾的不重复加。"""
    name = str(project_name or "").strip()
    if not name:
        return DOC_SUFFIX
    # 目录名本身以「项目」结尾时（如「平铺项目」），只补后缀 .md，
    # 不能直接返回裸名——否则会生成一个没有扩展名的文件，后续扫描全落空。
    return name + ".md" if name.endswith("项目") else name + DOC_SUFFIX


def _all_main_doc_names(dir_name: str) -> list:
    """项目目录里「可能是主文档」的文件名，按优先级排好。

    三套命名在库里都真实存在过：`<目录名>项目.md`（五段式）、
    `<目录名>.md`、以及目录名本身已带「项目」时只剩 `<目录名>.md`。
    这里只用于**识别既有主文档**；「该不该新建」的判据见 `doc_name()`，
    两者故意不复用——识别放宽不会产生新文件，生成收紧才不会误建。

    注意 `<目录名>.md` 排在最后：用户手写「与目录同名」的普通笔记常用这个
    名字，只有当它确实是本模块生成的主文档（`type: project` + `tags: [项目]`）
    时才认（见 `_looks_generated`），否则一律当作普通笔记，不碰。
    """
    name = str(dir_name or "").strip()
    if not name:
        return [DOC_SUFFIX]
    primary = doc_name(name)
    names = [primary]
    for cand in (f"{name}项目.md", f"{name}.md"):
        if cand not in names:
            names.append(cand)
    return names


def _looks_generated(path: Path) -> bool:
    """该文件是不是本模块生成的主文档（而不是用户手写的同名普通笔记）。

    判据取生成时写的 frontmatter 指纹：`type: project` 且 tags 含「项目」。
    用户手写笔记几乎不会同时满足这两条；读不到或没有 frontmatter 一律判否，
    宁可漏认（下次再建一份新的）也不能误刷用户笔记。
    """
    try:
        raw = path.read_text(encoding="utf-8")[:2000]
    except Exception:
        return False
    m = _FM_RE.match(raw)
    if not m:
        return False
    fm = m.group(1)
    if not re.search(r"^type:\s*[\"']?project[\"']?\s*$", fm, re.MULTILINE):
        return False
    # tags 必须是本模块写的那一行（`tags: [项目]`），不能用「全文含『项目』」——
    # 标题、描述里出现「项目」二字太常见，那样判据几乎恒真，会误伤用户手写笔记。
    return bool(re.search(r"^tags:\s*\[\s*[\"']?项目[\"']?\s*\]\s*$", fm, re.MULTILINE))


def _is_main_doc(path: Path) -> bool:
    """该文件本身是不是本项目的主文档（避免自己给自己生成/更新）。

    只认「与本项目同名」的那几个名字，不能用「文件名以 项目.md 结尾」这种
    宽判据 —— `T12子项目.md` 会被误判成主文档，正常笔记反而拿不到生成/刷新。
    """
    return path.name in _all_main_doc_names(path.parent.name)


def _find_main_doc(project_dir: Path) -> Optional[Path]:
    """项目目录**内**的主文档：只认与本目录同名的精确名。

    这里不能用「文件名以 项目.md 结尾」的宽判据：项目目录里常有
    `T12子项目.md` 这类普通笔记，被误认成主文档后 `_refresh_doc()` 会改写
    它的 `updated`，静默破坏用户笔记。主文档平铺在父目录的情形由
    `_parent_has_main_doc()` 负责，与本函数职责不同。
    """
    try:
        if not project_dir.is_dir():
            return None
        names = _all_main_doc_names(project_dir.name)
        for name in names:
            f = project_dir / name
            if not f.is_file():
                continue
            # 一律验生成指纹：目录名本身以「项目」结尾时，生成名就是
            # `<目录名>.md`，与用户手写的同名普通笔记撞名，只靠「首位优先」挡不住。
            if _looks_generated(f):
                return f
    except OSError:
        log.warning("project_doc: 扫描项目目录失败 %s", project_dir, exc_info=True)
    return None


def _parent_has_main_doc(project_dir: Path) -> bool:
    """父目录（通常是 `10-项目/`）里已有本项目的主文档 → 存量项目，别动。

    生产库的主文档是平铺在 `10-项目/` 根的（如 `示例集项目.md`），项目正文
    在 `10-项目/示例集 走势监控/`。**注意名字不是简单拼接**：目录名
    是「示例集 走势监控」，主文档却是「示例集项目.md」（取了前缀）。

    所以这一层必须用宽判据：父目录里任何 `<某前缀>项目.md` 只要与目录名有
    公共前缀就认为是存量。宁可漏判成「要新建」（后续 `_find_main_doc` 会在
    目录内找不到、于是新建一份，不会覆盖别人），也不能误判成「无存量」后
    去改别人的文件。
    """
    parent = project_dir.parent
    name = str(project_dir.name or "").strip()
    try:
        if not parent.is_dir():
            return False
        # ① 精确名先查（快路径，覆盖「目录名 === 主文档前缀」的常见情形）
        #    兜底名（`<目录名>.md`）同样要求生成指纹，避免把用户手写笔记当存量。
        names = _all_main_doc_names(name)
        for cname in names:
            f = parent / cname
            if f.is_file() and _looks_generated(f):
                return True
        # ② 宽判据：父目录里 `*项目.md`，其前缀与目录名互相包含即视为存量。
        #    生产实例：目录「示例集 走势监控」←→ 文件「示例集项目.md」
        for f in parent.iterdir():
            if not (f.is_file() and f.name.endswith(DOC_SUFFIX)):
                continue
            prefix = f.name[: -len(DOC_SUFFIX)].strip()
            if not prefix:
                continue
            # 前缀必须够长（≥3 字符）才算数：否则「BTC」这类短名会把
            # `示例集项目.md` 当成自己的存量，新建项目反而拿不到主文档。
            if len(prefix) < 3 or len(name) < 3:
                continue
            if prefix in name or name in prefix:
                return True
    except OSError:
        log.warning("project_doc: 扫描父目录失败 %s", parent, exc_info=True)
    return False


def _read_template(notes_dir: Path) -> Optional[str]:
    """读 `_模板/项目主文档.md`；缺失时用内置骨架兜底，保证新库也能生成。

    生产库的 `笔记/` 是数据目录、不进代码仓库（`.gitignore` 里有 `笔记/`），
    所以部署不会带过去任何模板文件。如果只认磁盘模板，功能上线后永远静默
    跳过——这正是「代码绿了但没生效」的典型假绿。内置骨架与仓库里
    `笔记/_模板/项目主文档.md` 逐字一致，磁盘模板存在时仍以磁盘为准。
    """
    path = notes_dir / "_模板" / f"{TEMPLATE_NAME}.md"
    try:
        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            return raw
        log.info("project_doc: 模板为空，改用内置骨架：%s", path)
    except Exception:
        log.info(
            "project_doc: 模板缺失或不可读，改用内置骨架：%s", path,
        )
    return BUILTIN_TEMPLATE


def _split_template(raw: str):
    """模板 → (frontmatter 行列表, 正文)；无 frontmatter 时行列表为空。"""
    m = _FM_RE.match(raw or "")
    if not m:
        return [], (raw or "")
    return m.group(1).split("\n"), raw[m.end():]


def _template_sections_ok(raw: str) -> bool:
    """模板正文必须齐五段且顺序一致 —— 骨架是契约，缺一段就不生成。"""
    _, body = _split_template(raw)
    found = [ln.strip() for ln in body.split("\n") if ln.strip().startswith("## ")]
    return found == SECTIONS


def _quote(value: str) -> str:
    """YAML 双引号字符串转义（标题里可能有引号/反斜杠）。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _build_doc(project_name: str, note_rel: str, now: datetime) -> str:
    """生成主文档全文：固定 frontmatter + 五段正文。"""
    title = project_name
    # updated 精确到秒：分钟粒度会让「同一分钟内再写」看不出变化，
    # 也无法验证 60 秒聚合是否真的只刷一次。
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    date = now.strftime("%Y-%m-%d")
    # id 沿用 _build_frontmatter 的口径：type 前缀 + 秒级时间 + 4 位随机
    doc_id = f"proj-{now.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(2)}"
    fm = [
        "---",
        f'id: "{doc_id}"',
        f'title: "{_quote(title)}"',
        "type: project",
        "schema_version: 1",
        'content_layer: "curated"',
        "tags: [项目]",
        f"created: {date}",
        f"updated: {stamp}",
        "status: 进行中",
        "priority: 中",
        "---",
    ]
    body = [
        f"# {title}",
        "",
        SECTIONS[0],
        f"本项目于 {date} 建立。",
        "",
        SECTIONS[1],
        "- 状态：进行中",
        "",
        SECTIONS[2],
        "- （待补充）",
        "",
        SECTIONS[3],
        f"- [[{note_rel}]]",
        "",
        SECTIONS[4],
        "- （待补充）",
        "",
    ]
    return "\n".join(fm) + "\n\n" + "\n".join(body)


def _touch_status(body: str, now: datetime) -> str:
    """给「当前状态」段打一行「最近更新」，让状态段随项目改动可见地变化。

    只动**本模块生成的**状态段：必须已有 `- 状态：` 这类列表项才介入。
    人工写的主文档（散文式状态段）一律原样返回——T12 明确不改造存量 19 份，
    更不许把用户手写的内容替换成机器模板。
    """
    lines = body.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == SECTIONS[1])
    except StopIteration:
        return body
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[start + 1:end]
    if not any(ln.strip().startswith("- 状态：") for ln in section):
        return body  # 人工状态段，不碰
    stamp_line = f"- 最近更新：{now.strftime('%Y-%m-%d %H:%M')}"
    out = []
    replaced = False
    for ln in section:
        if ln.strip().startswith("- 最近更新："):
            if not replaced:
                out.append(stamp_line)
                replaced = True
            continue
        out.append(ln)
    if not replaced:
        # 插在最后一个列表项之后，保持列表连续
        last = max((i for i, ln in enumerate(out) if _LIST_RE.match(ln)), default=None)
        if last is None:
            out = [stamp_line] + out
        else:
            out = out[:last + 1] + [stamp_line] + out[last + 1:]
    return "\n".join(lines[:start + 1] + out + lines[end:])


def _append_recent(body: str, note_rels) -> str:
    """在「最近笔记」段追加若干链接：去重、最多 10 条，其余段原样返回。"""
    lines = body.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == SECTIONS[3])
    except StopIteration:
        log.warning("project_doc: 主文档缺「最近笔记」段，跳过追加")
        return body
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[start + 1:end]
    items = [ln for ln in section if _LIST_RE.match(ln)]
    # 非列表行（人写的说明）保留在列表之后；空行不保留，版式统一由本函数给
    tail = [ln for ln in section if ln.strip() and not _LIST_RE.match(ln)]

    want = [r for r in (note_rels or []) if r]
    kept = []
    seen = set()
    for ln in items:
        m = re.search(r"\[\[([^\]]+)\]\]", ln)
        key = (m.group(1) if m else ln).strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(ln)
    for rel in want:
        if rel in seen:
            continue
        seen.add(rel)
        kept.append(f"- [[{rel}]]")
    kept = kept[-MAX_RECENT:]

    # 版式固定：标题下一行就是列表（不留前导空行），列表后空一行接下一段。
    # 人写的说明行（非列表）留在列表之后，不被覆盖。
    new_section = kept + [""] + (tail + [""] if tail else [])
    return "\n".join(lines[:start + 1] + new_section + lines[end:])


def _refresh_doc(doc: Path, note_rels, now: datetime) -> bool:
    """只改 `updated` 与「最近笔记」；返回是否真的落盘。"""
    try:
        raw = doc.read_text(encoding="utf-8")
    except Exception:
        log.warning("project_doc: 读主文档失败 %s", doc, exc_info=True)
        return False
    m = _FM_RE.match(raw)
    if not m:
        log.warning("project_doc: 主文档无 frontmatter，跳过更新 %s", doc)
        return False
    fm_lines = m.group(1).split("\n")
    body = raw[m.end():]
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    hit = False
    for i, ln in enumerate(fm_lines):
        if ln.startswith("updated:"):
            fm_lines[i] = f"updated: {stamp}"
            hit = True
            break
    if not hit:
        fm_lines.append(f"updated: {stamp}")
    new_raw = "---\n" + "\n".join(fm_lines) + "\n---\n" + _append_recent(_touch_status(body, now), note_rels)
    if new_raw == raw:
        return False
    try:
        doc.write_text(new_raw, encoding="utf-8")
    except Exception:
        log.warning("project_doc: 写主文档失败 %s", doc, exc_info=True)
        return False
    return True


def _invalidate_caches() -> None:
    """让 /api/notes/tree 立刻看到新文件；失败不影响主流程。"""
    try:
        from ..routes.routes_notes import _invalidate_notes_cache
        _invalidate_notes_cache()
    except Exception:
        log.warning("project_doc: 笔记缓存失效失败", exc_info=True)


def on_project_note_written(notes_dir, note_path, note_rel: str, note_type: str) -> Optional[str]:
    """POST /notes 落盘后的钩子。返回生成/更新的主文档相对路径（未动则 None）。

    调用方必须再包一层 try/except：这里已经兜底，但写入链路不能有任何
    依赖本函数的假设。
    """
    if not enabled():
        return None
    if (note_type or "") != "project":
        return None
    try:
        notes_dir = Path(notes_dir)
        note_path = Path(note_path)
        project_dir = note_path.parent
        # 落点必须是项目目录本身，不能是 10-项目 根（否则会生成「10-项目项目.md」）
        root = notes_dir / "10-项目"
        if project_dir.resolve() == root.resolve() or root.resolve() not in project_dir.resolve().parents:
            return None
        if _is_main_doc(note_path):
            return None

        now = datetime.now()
        existing = _find_main_doc(project_dir)
        # 父目录已有主文档 = 存量项目，只在「本次写入的正主文档就是它」时才刷。
        # 旧实现直接 return None，导致平铺在 `10-项目/` 根的存量主文档永远不更新，
        # 「最近笔记」段永远是空的 —— 这也是存量不回补与自动更新必须共存的地方。
        if existing is None and _parent_has_main_doc(project_dir):
            return None
        if existing is not None:
            key = str(project_dir.resolve())
            last = _LAST_REFRESH.get(key, 0.0)
            if time.time() - last < REFRESH_INTERVAL:
                # 窗口内：只记账不落盘，链接留到下次刷新一起补
                _PENDING.setdefault(key, []).append(note_rel)
                return None
            rels = _PENDING.pop(key, []) + [note_rel]
            changed = _refresh_doc(existing, rels, now)
            if changed:
                # 只有真落了盘才记时间：没改动（链接已存在）就不该吃掉 60 秒窗口
                _LAST_REFRESH[key] = time.time()
                _invalidate_caches()
                log.info("project_doc: 已更新主文档 %s", existing)
                return str(existing.relative_to(notes_dir))
            return None

        raw = _read_template(notes_dir)
        if raw is None:
            return None
        if not _template_sections_ok(raw):
            # 磁盘模板存在但骨架不符（旧格式/写坏了）：回落到内置骨架，
            # 不能让一个坏模板把功能整体静默关掉。
            log.warning("project_doc: 磁盘模板五段骨架不符，改用内置骨架")
            raw = BUILTIN_TEMPLATE
        doc = project_dir / doc_name(project_dir.name)
        # 同名文件已存在但不是本模块生成的（用户手写的同名普通笔记）：
        # 让位给 `<目录名>项目.md`，绝不能覆盖用户笔记，也不能因为这层撞名就
        # 让「目录名以项目结尾」的项目永远拿不到主文档。
        if doc.exists() and not _looks_generated(doc):
            alt = project_dir / (doc.stem + DOC_SUFFIX)
            if alt.exists():
                return None
            doc = alt
        if doc.exists():
            return None
        doc.write_text(_build_doc(project_dir.name, note_rel, now), encoding="utf-8")
        # 首次生成不记账：紧接着的第二篇要能立刻触发刷新（否则被 60 秒窗口吞掉）
        _PENDING.pop(str(project_dir.resolve()), None)
        _invalidate_caches()
        log.info("project_doc: 已生成主文档 %s", doc)
        return str(doc.relative_to(notes_dir))
    except Exception:
        log.warning("project_doc: 项目主文档钩子异常，已忽略（不阻断写入）", exc_info=True)
        return None
