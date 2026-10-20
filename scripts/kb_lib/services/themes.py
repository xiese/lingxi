"""主题插件：从 `scripts/web/themes/*.css` 扫出可用主题列表。

约定：一个 `.css` 文件就是一套主题，文件名（去 `.css`）即主题 id；
文件首行注释写名称与描述，格式：

    /* name: 极光 | desc: 蓝紫渐变 · 明亮卡片 */

读不到注释时用 id 兜底（名称=id、描述留空），保证「加一个 CSS 文件就多一套
主题」，不用改代码。`classic`（经典）是原版界面、没有 CSS 文件，但必须始终
出现在列表里，否则用户切不回原版。

目录不存在、读文件失败、注释残缺一律不抛异常：最差情况只返回 classic 一条，
前端设置页照样能用。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

log = logging.getLogger("kb.themes")

# 主题目录固定在 web 资源下，与 index.html / ui.js 同级
THEMES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "themes"

CLASSIC = {"id": "classic", "name": "经典", "desc": "原版界面"}

# 首行注释里的 name / desc，允许顺序颠倒、允许只写一个
_META_RE = re.compile(r"(name|desc)\s*[:：]\s*([^|*]+)", re.IGNORECASE)
# 主题 id 白名单：文件名直接拼进 URL（./themes/<id>.css），必须挡住空格、斜杠等
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# base.css 是两套主题共用的外壳（侧栏、子 Tab、底栏、弹层…），永远加载，
# 不是可选主题；列进设置页会让用户切到一个只有外壳没有配色的半成品。
SHARED_CSS = "base"

__all__ = ["THEMES_DIR", "CLASSIC", "SHARED_CSS", "parse_meta", "list_themes"]


def parse_meta(text: str, theme_id: str) -> dict:
    """从 CSS 文本里解析 name / desc；读不到就用 id 兜底。"""
    meta = {"id": theme_id, "name": theme_id, "desc": ""}
    if not text:
        return meta
    # 只看文件开头 512 字符，避免整文件扫注释里的干扰内容
    head = text[:512]
    m = re.search(r"/\*(.*?)\*/", head, re.S)
    if not m:
        return meta
    for key, val in _META_RE.findall(m.group(1)):
        val = val.strip()
        if not val:
            continue
        if key.lower() == "name":
            meta["name"] = val
        else:
            meta["desc"] = val
    return meta


def list_themes() -> List[dict]:
    """列出 themes/ 下的全部主题，末尾固定追加 classic。"""
    themes: List[dict] = []
    try:
        if THEMES_DIR.is_dir():
            for path in sorted(THEMES_DIR.iterdir(), key=lambda p: p.name):
                if not path.is_file() or path.suffix.lower() != ".css":
                    continue
                theme_id = path.stem
                # 跳过隐藏文件、classic 同名文件（classic 由代码兜底，不能有 CSS）、
                # 以及不能安全拼进 URL 的文件名
                if not theme_id or theme_id.startswith(".") or theme_id == "classic":
                    continue
                if theme_id == SHARED_CSS:
                    continue
                if not _ID_RE.match(theme_id):
                    log.warning("主题文件名不合法，已跳过：%s", path.name)
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    log.warning("读取主题文件失败，已跳过：%s", path, exc_info=True)
                    continue
                themes.append(parse_meta(text, theme_id))
    except Exception:
        log.warning("扫描主题目录失败，仅返回经典主题：%s", THEMES_DIR, exc_info=True)
        themes = []
    themes.append(dict(CLASSIC))
    return themes
