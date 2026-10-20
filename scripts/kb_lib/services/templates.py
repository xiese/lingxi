"""笔记模板：从 NOTES_DIR/_模板/ 加载 Markdown 模板。

每个 .md 文件即一个模板，文件名（去 .md）为模板名，正文里 `## ` 开头的
标题行按原顺序作为章节要求。目录不存在或为空时回退到内置「默认模板」，
其章节与 write_policy.REQUIRED_SECTIONS 完全一致，保证旧行为不破。

T9 起另有一套「模板集合」（见文件末尾）：4 套内置骨架 + `_模板/` 里的模板 +
用户自定义（`KB_ROOT/.kb/templates.json`），供新建笔记时选骨架。两套并存：

  · `list_templates()` / `get_template()` 保持原口径，老前端与写策略照旧；
  · `list_catalog()` / `save_custom()` / `delete_custom()` 是集合的增删改查。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import NOTES_DIR
from .write_policy import REQUIRED_SECTIONS

log = logging.getLogger("kb.templates")

TEMPLATES_DIR = NOTES_DIR / "_模板"
BUILTIN_NAME = "默认模板"

__all__ = [
    "TEMPLATES_DIR",
    "BUILTIN_NAME",
    "BUILTIN_TEMPLATES",
    "BuiltinTemplateError",
    "list_templates",
    "get_template",
    "list_catalog",
    "get_catalog_item",
    "save_custom",
    "delete_custom",
    "render_body",
]


def _builtin_template() -> dict:
    """内置兜底模板：章节严格等于 REQUIRED_SECTIONS。"""
    sections = list(REQUIRED_SECTIONS)
    return {
        "name": BUILTIN_NAME,
        "file": "",
        "title": BUILTIN_NAME,
        "content": "\n\n".join(sections),
        "frontmatter": "",
        "body": "\n\n".join(sections),
        "sections": sections,
        "builtin": True,
    }


def _split_frontmatter(content: str) -> Tuple[str, str]:
    """把模板拆成 (frontmatter, body)。

    模板文件普遍带 YAML frontmatter（`---` 开头）。前端新建表单要把模板正文
    填进 textarea，但 textarea 里塞 frontmatter 会有两个问题：
      ① 首字符是 `-` 不是 `#`，`ltgOrCreate()` 判据失效，选模板反而不创建；
      ② 后端 `_build_frontmatter()` 会自己重建一份，模板那份就成了正文里的
         第二份 frontmatter。
    所以单独给 body，`content` 仍保留原文（调用方要原样渲染时用）。
    """
    text = content or ""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not m:
        return "", text
    # 去掉 frontmatter 收尾 `---` 之后的空行：textarea 里前置空行会让
    # `startsWith('#')` 判据同样失效（模板文件普遍写成 `---\n\n# 标题`）。
    return m.group(0), text[m.end():].lstrip("\n")


def _extract_sections(content: str) -> List[str]:
    """抽取正文里 `## ` 开头的标题行，保持原顺序、保留前缀。"""
    sections: List[str] = []
    for line in (content or "").splitlines():
        line = line.strip()
        if line.startswith("## "):
            sections.append(line)
    return sections


def _template_path(name: str) -> Optional[Path]:
    """把模板名解析成 _模板/ 下的 .md 路径；越界或非法名返回 None。"""
    if not isinstance(name, str):
        return None
    stem = name.strip()
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem or stem in (".", ".."):
        return None
    if "/" in stem or "\\" in stem or "\x00" in stem:
        return None
    base = TEMPLATES_DIR.resolve()
    try:
        candidate = (base / (stem + ".md")).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    return candidate


def _load_one(path: Path) -> Optional[dict]:
    """读单个模板；任何失败只记 warning 并返回 None。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        log.warning("读取模板失败，已跳过：%s", path, exc_info=True)
        return None
    name = path.stem
    fm, body = _split_frontmatter(content)
    return {
        "name": name,
        "file": path.name,
        "title": name,
        "content": content,
        "frontmatter": fm,
        "body": body,
        "sections": _extract_sections(body),
        "builtin": False,
    }


def list_templates() -> List[dict]:
    """列出 _模板/ 下的全部模板；目录缺失/为空时返回内置默认模板。

    老接口，口径不动：只扫 `_模板/*.md`，空则回退「默认模板」。
    模板集合（含内置骨架与自定义）走 `list_catalog()`。
    """
    templates: List[dict] = []
    try:
        if TEMPLATES_DIR.is_dir():
            for path in sorted(TEMPLATES_DIR.iterdir(), key=lambda p: p.name):
                if not path.is_file() or path.suffix.lower() != ".md":
                    continue
                item = _load_one(path)
                if item is not None:
                    templates.append(item)
    except Exception:
        log.warning("扫描模板目录失败，回退默认模板：%s", TEMPLATES_DIR, exc_info=True)
        templates = []
    if not templates:
        templates.append(_builtin_template())
    return templates


def get_template(name: str) -> Optional[dict]:
    """按模板名取完整模板；找不到返回 None。"""
    if isinstance(name, str) and name.strip() == BUILTIN_NAME:
        path = _template_path(name)
        if path is None or not path.is_file():
            return _builtin_template()
    path = _template_path(name)
    if path is None or not path.is_file():
        return None
    return _load_one(path)


# ── T9 模板集合：内置骨架 + _模板/ + 自定义（KB_ROOT/.kb/templates.json）──
#
# 与上面那套的区别：这里是「新建笔记时可选的一份 markdown 骨架」，字段固定为
# id / name / description / body，body 里可含 {{date}}、{{title}} 占位符。
# 老接口（list_templates/get_template）继续按模板名服务写策略与老前端，
# 新集合用 id 做键——两套 id 空间不重叠，互不影响。

CATALOG_FILE_NAME = os.path.join(".kb", "templates.json")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_BODY = 200 * 1024
_MAX_NAME = 80
_MAX_DESC = 200


class BuiltinTemplateError(ValueError):
    """试图修改/删除内置模板时抛出（路由层转 400）。"""


# 内置 5 套骨架（默认/项目/会议/读书/日记）。body 一律以 `# {{title}}` 开头：老前端 ltgOrCreate() 用
# `startsWith('#')` 判断「这条要不要直接建笔记」，首字符不是 # 就建不出来。
BUILTIN_TEMPLATES: List[dict] = [
    {
        "id": "builtin-meeting",
        "name": "会议记录",
        "description": "一次会议的结论与待办：议题、结论、待办、后续",
        "body": (
            "# {{title}}\n\n"
            "日期：{{date}}\n\n"
            "## 会议信息\n"
            "- 时间：{{date}}\n"
            "- 参与人：\n"
            "- 议题：\n\n"
            "## 结论\n"
            "- \n\n"
            "## 待办\n"
            "- [ ] 事项 —— 负责人 / 截止日期\n\n"
            "## 后续\n"
            "- \n"
        ),
    },
    {
        "id": "builtin-project",
        "name": "项目笔记",
        "description": "项目目标、当前状态与日志，适合长期跟踪一个项目",
        "body": (
            "# {{title}}\n\n"
            "## 目标\n"
            "一句话描述项目目标。\n\n"
            "## 当前状态\n"
            "- [ ] 里程碑 1\n"
            "- [ ] 里程碑 2\n\n"
            "## 关键决定\n"
            "- \n\n"
            "## 日志\n"
            "- {{date}}: 做了什么\n"
        ),
    },
    {
        "id": "builtin-reading",
        "name": "读书笔记",
        "description": "一本书的要点、摘录与行动：来源、要点、摘录、我的想法",
        "body": (
            "# {{title}}\n\n"
            "## 书籍信息\n"
            "- 书名：\n"
            "- 作者：\n"
            "- 读完日期：{{date}}\n\n"
            "## 核心要点\n"
            "- \n\n"
            "## 摘录\n"
            "> \n\n"
            "## 我的想法\n"
            "- \n\n"
            "## 行动\n"
            "- [ ] \n"
        ),
    },
    {
        "id": "builtin-diary",
        "name": "日记",
        "description": "一天的经历、想法与明日计划",
        "body": (
            "# {{title}}\n\n"
            "日期：{{date}}\n\n"
            "## 今天做了什么\n"
            "- \n\n"
            "## 想法 / 灵感\n"
            "- \n\n"
            "## 明日计划\n"
            "- [ ] \n"
        ),
    },
]

# 老的「默认模板」也进集合：它按 REQUIRED_SECTIONS 生成，写策略默认就按这套
# 章节校验，把它列进选择器里，用户想按默认口径写时不用另找一套。
BUILTIN_TEMPLATES.append({
    "id": "builtin-default",
    "name": BUILTIN_NAME,
    "description": "按写策略默认五节（背景/关键发现/实施步骤/验证结果/后续建议）",
    "body": "# {{title}}\n\n" + "\n\n".join(REQUIRED_SECTIONS) + "\n",
})

_BUILTIN_BY_ID = {t["id"]: t for t in BUILTIN_TEMPLATES}


def catalog_file() -> Path:
    """自定义模板文件路径。延迟取 KB_ROOT，测试改环境变量即可隔离。"""
    from ..config import KB_ROOT

    return Path(KB_ROOT) / CATALOG_FILE_NAME


def _now_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def render_body(body: str, title: str = "", date: str = "") -> str:
    """把骨架里的 {{date}} / {{title}} 换掉；title 为空时留空标题。

    `{{timestamp}}` 一并处理：老模板习惯用它，留着会被后端当成字面量写进正文。
    只替换这三个已知占位符，其他 `{{...}}` 原样保留（可能是笔记正文里的模板语法）。
    """
    text = str(body or "")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    text = text.replace("{{date}}", date or _now_date())
    text = text.replace("{{title}}", str(title or ""))
    text = text.replace("{{timestamp}}", stamp)
    return text


def _normalize_custom(raw) -> Optional[dict]:
    """把一条自定义模板收敛成标准结构；不合法返回 None（坏条目跳过，不炸）。"""
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()
    body = raw.get("body")
    if not _ID_RE.match(tid) or not isinstance(body, str) or not body.strip():
        return None
    if len(body) > _MAX_BODY:
        return None
    name = str(raw.get("name") or "").strip() or tid
    return {
        "id": tid,
        "name": name[:_MAX_NAME],
        "description": str(raw.get("description") or "").strip()[:_MAX_DESC],
        "body": body,
        # sections 必须在这里补齐：老前端按 sections 校验章节骨架，
        # 缺字段会让自定义模板在老页面上无法使用（内置与 _模板/ 两条来源都带）。
        "sections": _extract_sections(body),
        "builtin": False,
    }


def _read_custom() -> List[dict]:
    """读自定义模板；文件缺失/坏掉一律当空表（读不到就用内置兜底）。"""
    path = catalog_file()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("读取自定义模板失败，按空表处理：%s", path, exc_info=True)
        return []
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: List[dict] = []
    seen = set(_BUILTIN_BY_ID)
    for raw in items:
        item = _normalize_custom(raw)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
    return out


def _write_custom(items: List[dict]) -> None:
    """原子写自定义模板表（先写临时文件再 replace，避免写一半留坏文件）。"""
    path = catalog_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"templates": items}, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _dir_templates() -> List[dict]:
    """`_模板/*.md` 也进集合，id 用 `dir-<名字哈希>`，与内置/自定义不撞。"""
    out: List[dict] = []
    try:
        if not TEMPLATES_DIR.is_dir():
            return out
        for path in sorted(TEMPLATES_DIR.iterdir(), key=lambda p: p.name):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            item = _load_one(path)
            if item is None:
                continue
            body = item.get("body") or ""
            if not body.strip():
                continue
            digest = hashlib.sha1(item["name"].encode("utf-8")).hexdigest()[:10]
            out.append({
                "id": f"dir-{digest}",
                "name": item["name"],
                "description": "来自 笔记/_模板/ 目录",
                "body": body,
                "sections": item.get("sections") or _extract_sections(body),
                "builtin": False,
                "source": "dir",
            })
    except Exception:
        log.warning("扫描模板目录失败，集合只返回内置与自定义：%s", TEMPLATES_DIR, exc_info=True)
    return out


def list_catalog(rendered: bool = False) -> List[dict]:
    """模板集合全量：内置 5 套 → `_模板/` → 自定义，按此顺序返回。

    rendered=False 时 body 原样带占位符；rendered=True 时 {{date}}/{{title}}/
    {{timestamp}} 已替换（{{title}} 留空），供「选中即插入」的客户端直接用。
    """
    items: List[dict] = []
    for t in BUILTIN_TEMPLATES:
        items.append({
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "body": t["body"],
            "sections": _extract_sections(t["body"]),
            "builtin": True,
            "source": "builtin",
        })
    items.extend(_dir_templates())
    items.extend(_read_custom())
    if rendered:
        for item in items:
            item["body"] = render_body(item["body"])
    return items


def get_catalog_item(tid: str) -> Optional[dict]:
    """按 id 取集合里的一条；找不到返回 None。"""
    key = str(tid or "").strip()
    if not key:
        return None
    for item in list_catalog():
        if item["id"] == key:
            return item
    return None


def save_custom(payload: dict) -> dict:
    """新建或覆盖一条自定义模板（同 id 覆盖）。

    body 必须非空——空骨架等于没模板，还会让写策略拿不到章节，直接拒绝。
    内置 id 一律不许覆盖（要改内置只能改代码），抛 BuiltinTemplateError。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    tid = str(payload.get("id") or "").strip()
    if not tid:
        # 没给 id 就按名字派生一个，方便前端「只填名字和正文」直接存
        seed = str(payload.get("name") or "").strip()
        if not seed:
            raise ValueError("缺少 id 或 name")
        tid = "custom-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    if not _ID_RE.match(tid):
        raise ValueError("id 只能包含字母、数字、下划线和短横线，长度 1-64")
    if tid in _BUILTIN_BY_ID:
        raise BuiltinTemplateError("内置模板不可覆盖")
    # `_模板/` 目录里的模板也用 dir-<hash> 形式的 id 出现在集合中，
    # 不拦的话存一份同 id 自定义模板会让 list_catalog 出现两条同 id，
    # 之后 get_catalog_item 永远命中 dir 那条，用户存的取不到、改不动。
    if tid.startswith("dir-") and any(x.get("id") == tid for x in _dir_templates()):
        raise ValueError("该 id 与 _模板/ 目录中的模板冲突，请换一个")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body 不能为空")
    if len(body) > _MAX_BODY:
        raise ValueError(f"body 过长（上限 {_MAX_BODY} 字节）")
    item = _normalize_custom({
        "id": tid,
        "name": payload.get("name") or tid,
        "description": payload.get("description") or "",
        "body": body,
    })
    if item is None:
        raise ValueError("模板内容不合法")
    items = [x for x in _read_custom() if x["id"] != tid]
    items.append(item)
    items.sort(key=lambda x: x["id"])
    _write_custom(items)
    return item


def delete_custom(tid: str) -> bool:
    """删一条自定义模板；内置的抛 BuiltinTemplateError，不存在返回 False。"""
    key = str(tid or "").strip()
    if key in _BUILTIN_BY_ID:
        raise BuiltinTemplateError("内置模板不可删除")
    items = _read_custom()
    kept = [x for x in items if x["id"] != key]
    if len(kept) == len(items):
        return False
    _write_custom(kept)
    return True
