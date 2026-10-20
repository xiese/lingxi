"""功能插件机制：扫描 `web/plugins/<名字>/manifest.json`，提供列表与启停。

约定：一个插件一个目录，目录里必须有 `manifest.json`：

    {
      "name": "hello",              // 插件 id，必须与目录名一致
      "title": "示例插件",           // 界面显示名
      "version": "1.0.0",
      "description": "……",
      "mount": "note-detail",       // 挂载点，见 MOUNTS
      "entry": "plugin.js",         // 入口 JS，相对插件目录
      "permissions": []             // 预留，当前只做前端插件，不申请任何后端能力
    }

**只做前端插件**（维护者 2026-10-18 定）：插件只能改界面，不能执行 Python 逻辑。
所以这里只负责「有哪些插件、哪个开着」，绝不 import 插件里的任何代码。

启停状态落在 `.kb_plugins.json`（与 `.kb_settings.json` 同风格）：

    {"disabled": ["hello"]}

默认全部启用：新装一个插件目录就该直接出现，不用先去点开关。只有显式关掉的
才写进 `disabled`。这样删掉状态文件 = 恢复默认，不会留下「明明装了却看不见」
的悬案。

任何异常都不许炸：目录不存在、manifest 坏、entry 不存在 → 跳过该插件并 warning，
其余插件照常返回。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("kb.plugins")

# 插件目录固定在 web 资源下，与 index.html / ui.js / themes/ 同级
PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "plugins"

# 启停状态文件跟其它点文件放一起（KB_ROOT 下）
STATE_FILE_NAME = ".kb_plugins.json"

# 允许的挂载点：前端在对应容器里留了位置，插件只能挂到这几处
#   dash        总览页底部
#   note-detail 笔记详情底部（反向链接区块下面）
#   settings    设置页底部
MOUNTS = ("dash", "note-detail", "settings")

# 插件 id 白名单：目录名会拼进 URL（./plugins/<id>/plugin.js），必须挡住斜杠、点、空格
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 单个 manifest 上限，防止误把大文件放进来
_MAX_MANIFEST = 64 * 1024

_lock = threading.Lock()

__all__ = [
    "PLUGINS_DIR", "MOUNTS", "STATE_FILE_NAME",
    "list_plugins", "load_enabled", "set_enabled", "is_enabled",
    "parse_manifest", "state_file",
]


def state_file() -> Path:
    """启停状态文件路径。延迟取 KB_ROOT，方便测试时改环境变量/改目录。"""
    from ..config import KB_ROOT
    return Path(KB_ROOT) / STATE_FILE_NAME


def _read_state() -> Dict[str, List[str]]:
    """读状态文件；坏文件当空状态（= 全启用），不抛错。"""
    path = state_file()
    try:
        if not path.exists():
            return {"disabled": []}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        log.warning("插件状态文件读取失败，按全部启用处理：%s", path, exc_info=True)
        return {"disabled": []}
    if not isinstance(data, dict):
        return {"disabled": []}
    disabled = data.get("disabled")
    if not isinstance(disabled, list):
        return {"disabled": []}
    # 只留合法 id，脏值直接丢弃
    clean = [x for x in disabled if isinstance(x, str) and _ID_RE.match(x)]
    return {"disabled": clean}


def _write_state(disabled: List[str]) -> None:
    """写状态文件。先写临时文件再替换，避免写一半断电留下坏 JSON。"""
    path = state_file()
    payload = json.dumps({"disabled": sorted(set(disabled))}, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.warning("插件状态文件写入失败：%s", path, exc_info=True)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def parse_manifest(raw: str, dir_name: str) -> Optional[dict]:
    """解析 manifest 文本；不合法返回 None（调用方跳过该插件）。"""
    if not raw or len(raw) > _MAX_MANIFEST:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    name = data.get("name")
    # name 必须与目录名一致：否则前端拼 ./plugins/<dir>/<entry> 和后端启停用的
    # id 会对不上，出现「列表里有、开关关不掉」的怪状态。
    if not isinstance(name, str) or name != dir_name or not _ID_RE.match(name):
        return None

    mount = data.get("mount")
    if not isinstance(mount, str) or mount not in MOUNTS:
        return None

    entry = data.get("entry") or "plugin.js"
    if not isinstance(entry, str) or not entry or entry.startswith("/") or ".." in entry:
        return None

    title = data.get("title")
    version = data.get("version")
    desc = data.get("description")
    perms = data.get("permissions")

    return {
        "name": name,
        "title": title if isinstance(title, str) and title.strip() else name,
        "version": version if isinstance(version, str) else "",
        "description": desc if isinstance(desc, str) else "",
        "mount": mount,
        "entry": entry,
        "permissions": [p for p in perms if isinstance(p, str)] if isinstance(perms, list) else [],
    }


def _scan() -> List[dict]:
    """扫描插件目录，返回全部「合法」插件（不管启停）。

    容错口径：**一个插件坏掉不许拖垮整份清单**。所以异常必须逐个插件兜，
    绝不能在循环外面兜一层 `return []` —— 那样任一插件抛个怪异常
    （入口名带 `\0`、权限错、符号链接断掉）就会让所有插件一起消失。
    """
    found: List[dict] = []
    if not PLUGINS_DIR.is_dir():
        return found
    try:
        entries = sorted(PLUGINS_DIR.iterdir(), key=lambda p: p.name)
    except Exception:
        log.warning("无法列出插件目录：%s", PLUGINS_DIR, exc_info=True)
        return found

    for d in entries:
        # 逐个插件兜异常：坏一个跳过它，其余照常
        try:
            if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
                continue
            if not _ID_RE.match(d.name):
                log.warning("插件目录名不合法，已跳过：%s", d.name)
                continue
            mf = d / "manifest.json"
            if not mf.is_file():
                continue
            try:
                raw = mf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                log.warning("读取 manifest 失败，已跳过：%s", mf, exc_info=True)
                continue
            meta = parse_manifest(raw, d.name)
            if not meta:
                log.warning("manifest 不合法，已跳过：%s", mf)
                continue
            # 入口文件必须真实存在，否则前端注入 404 脚本会静默失效
            if not (d / meta["entry"]).is_file():
                log.warning("插件入口不存在，已跳过：%s/%s", d.name, meta["entry"])
                continue
            meta["url"] = "./plugins/%s/%s" % (d.name, meta["entry"])
            found.append(meta)
        except Exception:
            log.warning("扫描单个插件失败，已跳过：%s", d, exc_info=True)
            continue
    return found


def list_plugins() -> List[dict]:
    """全部合法插件，带 enabled 标记。设置页用。"""
    disabled = set(_read_state()["disabled"])
    out = []
    for meta in _scan():
        item = dict(meta)
        item["enabled"] = meta["name"] not in disabled
        out.append(item)
    return out


def load_enabled() -> List[dict]:
    """只返回已启用的插件。前端启动时用，拿到的就是要挂载的清单。"""
    return [p for p in list_plugins() if p["enabled"]]


def is_enabled(name: str) -> bool:
    return name not in set(_read_state()["disabled"])


def set_enabled(name: str, on: bool) -> dict:
    """开/关一个插件，返回最新状态。

    不存在的插件名直接抛 ValueError——让路由回 404，而不是静默写进状态文件
    留下一个「关了一个不存在的插件」的脏记录。
    """
    if not isinstance(name, str) or not _ID_RE.match(name):
        raise ValueError("插件名不合法")
    known = {p["name"] for p in _scan()}
    if name not in known:
        raise ValueError("插件不存在: %s" % name)

    with _lock:
        disabled = _read_state()["disabled"]
        if on:
            disabled = [x for x in disabled if x != name]
        else:
            if name not in disabled:
                disabled.append(name)
        _write_state(disabled)

    return {"name": name, "enabled": bool(on)}
