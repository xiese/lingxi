"""写路径单一真相：卫生 + 质量 + 写后索引出口。

REST / MCP 共用本模块；MCP 不再本地重复门禁。

规则来源：``rules.yaml``（单一真相源，2026-10-27 收敛）。启动时加载，
读不到或字段缺失时回落下方内置默认值——行为不退化，只是规则可配置化。
"""
from __future__ import annotations

import os
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 规则常量：优先读 rules.yaml。
# 本文件在 kb_lib/services/ 下，故取 parent.parent（即 kb_lib/rules.yaml）——
# 不是「与 write_policy.py 同目录」，旧注释写错过（2026-10-31 审查轮1 两路都点了）。
_RULES_YAML = Path(__file__).resolve().parent.parent / "rules.yaml"


def _load_rules() -> dict:
    """加载 rules.yaml。坏文件当空 dict（回落内置默认），不抛错但**留下日志**。

    静默回落很危险：rules.yaml 语法一坏，全部规则悄悄失效、服务看起来一切正常。
    """
    _lg = logging.getLogger("kb.write_policy")
    try:
        if not _RULES_YAML.exists():
            _lg.warning("rules.yaml 不存在（%s），全部规则回落内置默认", _RULES_YAML)
            return {}
        import yaml
        data = yaml.safe_load(_RULES_YAML.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            _lg.warning("rules.yaml 顶层不是映射（%s），全部规则回落内置默认", _RULES_YAML)
            return {}
        return data
    except Exception:
        _lg.warning("rules.yaml 解析失败（%s），全部规则回落内置默认", _RULES_YAML,
                    exc_info=True)
        return {}


_RULES = _load_rules()

# 与历史 MCP 默认对齐，可用环境变量覆盖
MIN_CONTENT_LEN = int(os.environ.get(
    "KB_MCP_MIN_CONTENT_LEN",
    str((_RULES.get("quality") or {}).get("min_content_len", "800"))))
REQUIRED_SECTIONS = [
    s for s in ((_RULES.get("quality") or {}).get("required_sections")
                or ["## 背景", "## 关键发现", "## 实施步骤", "## 验证结果", "## 后续建议"])
    if isinstance(s, str) and s
]

def _rules_str_set(value, default):
    """从 rules.yaml 取值构造字符串集合；类型不对就回落默认并告警。

    直接 ``frozenset(yaml值)`` 有坑：若 yaml 里误写成字符串 ``"deleted, delete"``，
    会被拆成**单字符集合**，删除后「强制刷索引」静默失效（2026-10-31 审查轮1 指出）。
    """
    if isinstance(value, (list, tuple)) and value and all(
            isinstance(x, str) and x for x in value):
        return frozenset(value)
    if value is not None:
        logging.getLogger("kb.write_policy").warning(
            "rules.yaml 的 force_refresh_actions 类型不对（%s），已回落内置默认",
            type(value).__name__)
    return frozenset(default)


# 写后需要 force 刷搜索的动作（删除/归档会改变可检索集合）
FORCE_REFRESH_ACTIONS = _rules_str_set(
    _RULES.get("force_refresh_actions"),
    ["deleted", "delete", "archived", "archive", "batch_deleted", "batch_archived"])

# ── 2026-10-31 新增：知识库规则反哺（第 1/2/4 条）──
# 第 2 条「写入必带的 frontmatter 字段」：
# 本模块**不**消费 rules.yaml 的 quality.required_fields —— 服务端在写路径上必然会
# 生成 title/type/status（不传就是 draft），所以「写入时校验」天然无效；
# 它真正的用武之地是**扫存量笔记**的巡查作业（尚未实现）。
# 为免留下「定义了没人用」的死代码，这里不定义对应常量；清单仍留在 rules.yaml
# 作规则声明，注释已标消费方待实现。
# 兜底默认取「不改变现状」那一侧：规则键缺失时不产生新提示。
# （rules.yaml 里现值是 false 时更是如此；用户 2026-10-31 明确「第 2 条主力交给巡查作业扫库」）
STATUS_DEFAULT_NOTICE = bool(
    (_RULES.get("quality") or {}).get("status_default_notice", False))

# 第 1 条：总览页必须带的检索字段（缺则 warning，不硬拦）
# 兜底为空列表 = 规则键缺失时不检查（不改变现状）；真正的清单在 rules.yaml
OVERVIEW_REQUIRED_FIELDS = [
    s for s in ((_RULES.get("project") or {}).get("overview_required_fields") or [])
    if isinstance(s, str) and s
]

# 第 4 条：写笔记 → 标记「该项目总览待刷新」（增量刷新队列）
# 兜底 false = 规则键缺失时不产生磁盘副作用；rules.yaml 里显式开了才是开
OVERVIEW_REFRESH_ENABLED = bool(
    (_RULES.get("project") or {}).get("overview_refresh_enabled", False))
try:
    OVERVIEW_REFRESH_INTERVAL_MIN = int(
        (_RULES.get("project") or {}).get("overview_refresh_interval_min", 60))
except (TypeError, ValueError):
    OVERVIEW_REFRESH_INTERVAL_MIN = 60
OVERVIEW_PENDING_FILE = str(
    (_RULES.get("project") or {}).get("overview_pending_file") or ".kb/overview_pending.json")

# 队列文件的进程内互斥。
# waitress 是多线程（threads=8），mark_overview_pending 是 read-modify-write：
# 不加锁会丢更新；再叠加固定临时名还会让两个 writer 互踩写出半截 JSON。
_OVERVIEW_QUEUE_LOCK = threading.Lock()

__all__ = [
    "MIN_CONTENT_LEN",
    "REQUIRED_SECTIONS",
    "FORCE_REFRESH_ACTIONS",
    "STATUS_DEFAULT_NOTICE",
    "OVERVIEW_REQUIRED_FIELDS",
    "OVERVIEW_REFRESH_ENABLED",
    "OVERVIEW_REFRESH_INTERVAL_MIN",
    "OVERVIEW_PENDING_FILE",
    "required_sections_for",
    "validate_note_quality",
    "validate_write_hygiene",
    "on_note_changed",
    "mark_overview_pending",
]


log = logging.getLogger("kb.write_policy")


def required_sections_for(template_name: Optional[str] = None) -> List[str]:
    """按模板名取章节要求；None / 找不到 / 模板无 `## ` 标题时回退默认五节。"""
    if not template_name:
        return list(REQUIRED_SECTIONS)
    try:
        from .templates import get_template

        tpl = get_template(template_name)
    except Exception:
        log.warning("读取模板失败，回退默认章节：%s", template_name, exc_info=True)
        return list(REQUIRED_SECTIONS)
    if not tpl:
        return list(REQUIRED_SECTIONS)
    sections = [s for s in (tpl.get("sections") or []) if isinstance(s, str) and s.strip()]
    if not sections:
        return list(REQUIRED_SECTIONS)
    return sections


def validate_note_quality(
    content: str,
    note_type: str = "knowledge",
    quality_check: bool = True,
    template: Optional[str] = None,
) -> Optional[dict]:
    """质量门禁。通过返回 None；失败返回 error dict（HTTP 400 体）。

    - quality_check=False 时跳过
    - diary / writing 不强制章节与长度
    - project / knowledge 默认：≥ MIN_CONTENT_LEN + 5 必备章节
    - template 指定模板时，章节要求改用该模板的 `## ` 标题
    """
    if not quality_check:
        return None
    if note_type not in ("project", "knowledge"):
        return None

    required = required_sections_for(template)
    problems: List[str] = []
    text = (content or "").strip()
    if len(text) < MIN_CONTENT_LEN:
        problems.append(
            f"正文过短：{len(text)} 字符，最低建议 {MIN_CONTENT_LEN} 字符"
        )
    missing = [s for s in required if s not in text]
    if missing:
        problems.append("缺少必备章节：" + ", ".join(missing))
    if not problems:
        return None
    return {
        "error": "quality_check_failed",
        "problems": problems,
        "required_sections": list(required),
        "min_content_len": MIN_CONTENT_LEN,
        "hint": (
            "建议包含背景、关键发现、实施步骤、验证结果、后续建议；"
            "小型临时笔记可设置 quality_check=false（卫生硬拦仍生效）。"
        ),
    }


def ensure_inside(path, root, label: str = "写入路径"):
    """落盘前的最后一道围栏：确认 path 在 root 之内。

    返回 (error_dict|None, resolved_path|None)。调用方应在 mkdir/write 前调用，
    把「用户输入拼路径」的失误挡成 400 而不是写坏文件或 500。
    """
    try:
        rp = Path(path).resolve()
        rr = Path(root).resolve()
        rp.relative_to(rr)
        return None, rp
    except ValueError:
        log.warning("%s 逃出根目录: %s（root=%s）", label, path, root)
        return {
            "error": "path_forbidden",
            "detail": f"{label}超出笔记库范围",
            "hint": "标题/子目录不得包含 .. 或绝对路径",
        }, None
    except OSError as e:
        log.warning("%s 解析失败: %s（%s）", label, path, e)
        return {
            "error": "path_forbidden",
            "detail": f"{label}无法解析",
            "hint": "路径含非法字符",
        }, None


def _path_forbidden(detail: str, hint: str) -> dict:
    """统一的路径类硬拦响应体（error/detail/hint 键序固定）。"""
    return {
        "error": "path_forbidden",
        "detail": detail,
        "hint": hint,
    }


def _norm_write_inputs(
    subdir: str, title: str, content: str
) -> Tuple[str, str, str]:
    """归一化写入三项输入：反斜杠压平、去首尾空白与斜杠。"""
    sd_norm = (subdir or "").replace("\\", "/").strip().strip("/")
    title_s = (title or "").strip()
    content_s = content or ""
    return sd_norm, title_s, content_s


def _guard_path_traversal(title_s: str, sd_norm: str) -> Optional[dict]:
    """路径穿越硬拦（2026-09-18 修复）。

    背景：标题里的 `/` 曾被当作目录分隔符，`../` 更能逃出笔记库
    （实测可写到 KB_ROOT 外任意 .md，服务以 root 运行）。
    这里做**与落盘无关**的独立校验：只要含路径分隔符或 .. 段就拒。
    title 先于 subdir 检查，顺序即优先级。
    """
    for field_name, raw in (("title", title_s), ("subdir", sd_norm)):
        if not raw:
            continue
        if "\0" in raw:
            return _path_forbidden(
                f"{field_name} 含空字节",
                "标题/子目录不得含控制字符",
            )
        norm = raw.replace("\\", "/")
        if any(seg == ".." for seg in norm.split("/")):
            return _path_forbidden(
                f"{field_name} 含上级目录引用（..）",
                "标题只能是单段名字；子目录不得用 .. 回溯",
            )
        if field_name == "title" and re.match(r"^[/\\\uFF0F\uFF3C]", raw):
            return _path_forbidden(
                "title 不得以路径分隔符开头（绝对路径）",
                "标题是单段文件名；归类请用 subdir",
            )
        if field_name == "subdir" and re.match(r"^[/\\\uFF0F\uFF3C]", raw):
            return _path_forbidden(
                "subdir 不得以路径分隔符开头（绝对路径）",
                "subdir 用相对路径，如 项目名 或 项目名/子目录",
            )
    return None


def _guard_title_nonempty(title_s: str) -> Optional[dict]:
    """标题净化后不得为空或纯点。"""
    if not title_s.strip().strip(".").strip():
        return _path_forbidden("title 净化后为空", "请给出有效标题")
    return None


_FORBIDDEN_PATH_SUBSTRINGS = (
    "10-项目_知识库项目",
    "10-项目/10-项目_知识库项目",
)


def _guard_deprecated_path(sd_norm: str, title_s: str) -> Optional[dict]:
    """废弃路径 10-项目_知识库项目 硬拦。"""
    for bad in _FORBIDDEN_PATH_SUBSTRINGS:
        if bad in sd_norm or bad in f"{sd_norm}/{title_s}":
            return _path_forbidden(
                f"禁止写入废弃路径：{bad}",
                "请写 10-项目/知识库项目.md 或 10-项目/知识库项目/ 下；见 skill personal-knowledge-base §9",
            )
    return None


def _guard_nested_top_dir(
    note_type: str, sd_norm: str, type_map: Optional[dict]
) -> Optional[dict]:
    """嵌套撞名硬拦（2026-09-26 新增）。

    背景：2026-09-19、2026-09-26 两次出现 20-知识/10-项目/ 这类错位——
    项目笔记被写进知识区里一个叫「10-项目」的子目录。旧校验只拦废弃路径，
    不看子目录名是否撞顶层目录名，所以清完照样复发。
    判据按**最终落点**：剥掉与当前 type 同名的首段后，subdir 里不应再出现
    任何顶层目录名。70-经验 例外——那是用户私有目录，交给 owner_ack 闸门。
    archive 例外：50-档案 下按原目录结构镜像归档（如 空壳待定_20260913/20-知识/工具研究），
    那是设计如此，不是错位；archive 落根另有 archive_root_forbidden 管着。
    """
    if note_type == "archive":
        return None
    top_dirs = set((type_map or {}).values()) | {"00-索引", "_模板", "70-经验"}
    type_prefix = str((type_map or {}).get(note_type, "") or "")
    segs = [x for x in sd_norm.split("/") if x]
    if type_prefix and segs and segs[0] == type_prefix:
        segs = segs[1:]
    for seg in segs:
        if seg in top_dirs and seg != "70-经验":
            return {
                "error": "nested_top_dir_forbidden",
                "detail": f"subdir 里不能再出现顶层目录名：{seg}",
                "hint": "subdir 只填分类名（如 运维、研究）；顶层目录由 type 决定，不要手写 10-项目/ 这类前缀",
            }
    return None


def _guard_project_dir_fork(note_type: str, sd_norm: str) -> Optional[dict]:
    """项目 subdir 末段部署/deploy 分叉硬拦。"""
    if note_type != "project" or not sd_norm:
        return None
    last = sd_norm.split("/")[-1]
    if re.search(r"(部署|deploy|deployment)\s*$", last, re.I):
        return {
            "error": "project_dir_fork_forbidden",
            "detail": f"禁止新建带部署后缀的项目子目录：{last}",
            "hint": "并入同一项目目录（如 grokcli-2api/），不要 foo/ + foo 部署/ 双开",
        }
    return None


def _guard_archive_root(note_type: str, sd_norm: str) -> Optional[dict]:
    """archive 禁止落到 50-档案 根目录。"""
    if note_type != "archive":
        return None
    parts = [p for p in sd_norm.split("/") if p and p != "50-档案"]
    if len(parts) == 0:
        return {
            "error": "archive_root_forbidden",
            "detail": "禁止直接写到 50-档案 根目录",
            "hint": "请 subdir 指定 文档收敛_YYYY-MM-DD/子目录 或其它归档子路径",
        }
    return None


def _project_subdir_segments(note_type: str, sd_norm: str) -> Optional[List[str]]:
    """把 project 的 subdir 归一成「项目名 / 可选子目录」两段语义。

    create_note 允许 subdir 带顶层目录前缀（如 "10-项目/某某项目"），
    这段前缀不是层级，计数前必须剥掉；非 project 类型直接返回 None（不适用）。
    """
    if note_type != "project" or not sd_norm:
        return None
    segs = [p for p in sd_norm.split("/") if p]
    if segs and re.match(r"^\d{2}-", segs[0]):
        segs = segs[1:]
    return segs


def _guard_project_dir_action_word(note_type: str, sd_norm: str) -> Optional[dict]:
    """项目目录名以「动作词」结尾硬拦 —— 2026-10-26 新增。

    背景：实测 10-项目/ 下 69 个目录里，21 个是「一篇笔记一个目录」，
    目录名就是那次任务的标题（<项目>-整合服务 / <项目>-算法池总览UI /
    <长任务名>与动态结算 …）。根因是调用方把任务标题当 subdir 传进来，
    而 plan_server_write 一旦拿到 subdir 且落点父目录存在就会接管并建目录。

    项目目录应当是**名词**（一个真实存在的东西），不是**动作**（一次任务）。
    「整合服务」不是一个东西，是某个项目的一次动作 → 应归该项目目录。

    判定用两种模式，覆盖实测全部 21 个坏目录：
      A. 分隔符 + 动作词结尾：<项目>-整合服务 / dsh-会话与插件治理
      B. 无分隔符但整名以动作词结尾：<长任务名>以动作词收尾
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if not segs:
        return None
    first = segs[0]
    ACTION_TAIL = (
        r"整合服务|总览UI|开发记录|回测|采集|接入|移植|修复|审查|审计|"
        r"整改|排障|定稿|上线|落地|实现|验证|复盘|调研|方案|设计|"
        r"结论|锁定|账本|成绩单|服务器运维|治理|升级|改造|重构|清理|收敛|"
        r"统计走势图|新架构|数据源|结算|部署|迁移|运维|维护|排查|诊断|"
        r"测试库|测试机"
    )
    pat_a = re.compile(r"[-—_·](" + ACTION_TAIL + r")\s*$", re.I)
    pat_b = re.compile(r"(" + ACTION_TAIL + r")\s*$", re.I)
    hit = pat_a.search(first) or (pat_b.search(first) if len(first) >= 5 else None)
    if hit:
        return {
            "error": "project_dir_action_word_forbidden",
            "detail": f"项目目录名不能以动作词结尾：{first}",
            "hint": (
                "项目目录应是名词（一个真实存在的东西），不是一次任务。"
                "把这篇文档放进所属项目目录，例如 subdir=\"算法库\" 而不是 "
                "subdir=\"算法库-整合服务\"。新建项目请先在 "
                "10-项目/README.md 项目表登记。"
            ),
        }
    return None


def _guard_project_dir_depth(note_type: str, sd_norm: str) -> Optional[dict]:
    """project 的 subdir 有效层级 > 2 段硬拦 —— 2026-10-26 新增。

    合法形态只有三种：""、"<项目名>"、"<项目名>/<配置|更新|专题>"。
    出现 3 段说明调用方在造嵌套层级，长期会把库结构搞成迷宫。

    只约束 project：archive 的 50-档案 下镜像原结构（20-知识/工具研究）是设计如此，
    knowledge 的分类层级同理，都不该被这条拦掉。
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if segs is None:
        return None
    if len(segs) > 2:
        return {
            "error": "project_dir_depth_forbidden",
            "detail": f"项目 subdir 层级过深（{len(segs)} 段）：{sd_norm}",
            "hint": "只允许 <项目名> 或 <项目名>/<配置|更新|专题>，最多两段",
        }
    return None


def _guard_skill_dump(
    note_type: str, title_s: str, content_s: str
) -> Optional[dict]:
    """knowledge 下 skill 整包 dump 硬拦（≥20k 且无指针关键词）。"""
    if note_type != "knowledge":
        return None
    title_l = title_s.lower()
    if "skill" not in title_l or len(content_s) < 20000:
        return None
    pointer_ok = any(
        x in content_s[:1500]
        for x in ("权威来源", "指针", "skill_view", "~/.kb-skills")
    )
    if not pointer_ok:
        return {
            "error": "skill_dump_forbidden",
            "detail": "禁止把 skill 整包 dump 写入 20-知识（正文过长）",
            "hint": "skill 只放 ~/.kb-skills/；笔记侧最多写 ~600B 指针",
        }
    return None


def _same_basename_warnings(
    note_type: str,
    title_s: str,
    notes_dir: Optional[Path],
    sanitize_fn: Optional[Callable[[str], str]],
) -> List[dict]:
    """同名 basename 已存在 → warning（可选，依赖 notes_dir；不硬拦）。

    任何异常只降级为日志，不影响写入判定。
    """
    warnings: List[dict] = []
    try:
        if notes_dir is None:
            return warnings
        if sanitize_fn is None:
            from ..auth import sanitize as sanitize_fn  # type: ignore
        safe_title = sanitize_fn(title_s).replace(" ", "_")
        if safe_title and not (note_type == "project" and title_s == "README"):
            matches = []
            for f in notes_dir.rglob(f"{safe_title}.md"):
                rel = str(f.relative_to(notes_dir)).replace("\\", "/")
                if rel.startswith("50-档案/"):
                    continue
                matches.append(rel)
            if matches:
                warnings.append(
                    {
                        "kind": "same_basename_exists",
                        "paths": matches[:8],
                        "hint": "若 10-项目已有主文，20-知识只写指针/摘要；勿双主文",
                    }
                )
    except Exception:
        log.warning("write_policy.py 异常", exc_info=True)
    return warnings


_PROJECT_LEDGER_FILE = str((_RULES.get("project") or {}).get("ledger_file") or "README.md")
_PROJECT_ROOT_DIR = str((_RULES.get("project") or {}).get("root_dir") or "10-项目")
_PROJECT_OVERVIEW_NAME = str((_RULES.get("project") or {}).get("overview_name") or "项目总览.md")
# 存量主档的后缀：`10-项目/<名>项目.md`
DOC_SUFFIX_FOR_LEDGER = str((_RULES.get("project") or {}).get("doc_suffix_for_ledger") or "项目.md")
# 项目内唯一允许的分类子目录（规则 3 补丁，2026-10-26 第二轮）。
# 前三个是规则表写明的「配置/更新/专题」；后两个是 note_normalizer 自动追加的
# 事件型落点（log_subdir / troubleshoot_subdir 的默认值）。这五个名字必须与
# note_normalizer 的默认配置保持一致，否则工具链自己的落点会被自己的守卫拦掉。
ALLOWED_PROJECT_SUBDIRS = tuple(
    (_RULES.get("project") or {}).get("allowed_subdirs")
    or ("配置", "更新", "专题", "日志", "排障"))
# {readme 绝对路径: (mtime, 已登记项目名集合)}，进程内缓存，README 改动即失效
_LEDGER_CACHE: dict = {}


def _registered_project_names(notes_dir) -> Optional[set]:
    """读 `10-项目/README.md` 项目表 + `10-项目/*项目.md`，返回已登记项目名集合。

    三种来源合并（任一命中即算登记，宁可放宽不可误拦）：
      ① 表格首列（`| 项目 | 主档 | 子目录 |` 的「项目」列）
      ② markdown 链接文字与 `./xxx` 目标名
      ③ `10-项目/<名>项目.md` 这类平铺主档（存量项目的主档在根层，不在目录内）

    返回 None 表示**台账不可用**（README 缺失/读不了/解析异常）。调用方必须
    把 None 当成「放行」—— 守卫失效要退化成不拦，不能把全库写入锁死。
    """
    if notes_dir is None:
        return None
    root = Path(notes_dir) / _PROJECT_ROOT_DIR
    readme = root / _PROJECT_LEDGER_FILE
    try:
        if not readme.is_file():
            return None
        key = str(readme)
        mtime = readme.stat().st_mtime
        cached = _LEDGER_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        text = readme.read_text(encoding="utf-8")
        names: set = set()
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells:
                continue
            cell = cells[0]
            # 跳过表头分隔行（|---|）与空单元格
            if not cell or set(cell) <= set("-: "):
                continue
            names.add(cell)
        for m in re.finditer(r"\[([^\]]+)\]\(\./([^)/]+)", text):
            names.add(m.group(1).strip())
            names.add(m.group(2).strip())
        try:
            for f in root.glob(f"*{DOC_SUFFIX_FOR_LEDGER}"):
                names.add(f.name[: -len(DOC_SUFFIX_FOR_LEDGER)].strip())
        except OSError:
            pass
        _LEDGER_CACHE[key] = (mtime, names)
        return names
    except Exception:
        log.warning("write_policy.py 读项目台账失败，本次跳过规则 3", exc_info=True)
        return None


def _guard_project_new_dir_unregistered(
    note_type: str, sd_norm: str, notes_dir
) -> Optional[dict]:
    """新建项目目录必须在 `10-项目/README.md` 项目表登记（规则 3）—— 2026-10-26 新增。

    **只拦「目录尚不存在」的情形**：存量目录一律放行，所以对现有 70 个项目零影响，
    也允许把新文档写进任何已有项目目录。台账不可读时同样放行（见
    `_registered_project_names` 的 None 语义）。

    为什么只拦新建：老目录没登记是历史欠账，靠一次性清洗补；新建目录没登记才是
    「一篇一目录」复发的入口，必须在写入端堵死。
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if not segs:
        return None
    if notes_dir is None:
        return None
    name = segs[0]
    root = Path(notes_dir) / _PROJECT_ROOT_DIR
    try:
        if (root / name).is_dir():
            return None  # 存量项目目录，放行
    except OSError:
        return None
    names = _registered_project_names(notes_dir)
    if names is None or name in names:
        return None
    return {
        "error": "project_dir_unregistered",
        "detail": f"新建项目目录未在 10-项目/README.md 项目表登记：{name}",
        "hint": (
            "先在 10-项目/README.md 的项目表加一行（名称 + 一句话 + 状态），再写入；"
            "已有项目请直接 subdir=<已有项目名>，不要为新任务开新目录"
        ),
    }


def _guard_project_subdir_unregistered(
    note_type: str, sd_norm: str, notes_dir
) -> Optional[dict]:
    """项目**内部**新开子目录也必须受控（规则 3 补丁）—— 2026-10-26 第二轮新增。

    为什么规则 3 不够：规则 3 只拦「10-项目 下新项目目录」，而生产上真正的
    复发口是项目内部 —— `note_normalizer` 判定事件型后自动追加 `/日志`、`/排障`，
    再叠加 `project_doc` 把 `note_path.parent` 当项目目录，就在
    `10-项目/<项目>/日志/` 里生成了 `日志项目.md`，凭空造出一个「假项目」。
    2026-10-26 审查时生产上这种残留有 5 处。

    三条边界（与规则 3 同一套口径）：
      1. 只认白名单 `ALLOWED_PROJECT_SUBDIRS`（配置/更新/专题/日志/排障）；
      2. 目录**已经存在**就放行 —— 存量欠账不能把正常写入锁死；
      3. 台账读不到（None）也放行 —— 守卫失效必须退化成不拦。
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if not segs or len(segs) < 2:
        return None            # 只有项目名、没有子目录，不归本守卫管
    if notes_dir is None:
        return None
    sub = segs[1]
    if sub in ALLOWED_PROJECT_SUBDIRS:
        return None
    root = Path(notes_dir) / _PROJECT_ROOT_DIR
    try:
        if (root / segs[0] / sub).is_dir():
            return None        # 存量子目录，放行
    except OSError:
        return None
    names = _registered_project_names(notes_dir)
    if names is None:
        return None
    return {
        "error": "project_subdir_unregistered",
        "detail": f"项目内新开子目录不在白名单：{segs[0]}/{sub}",
        "hint": (
            "项目内只允许 " + "/".join(ALLOWED_PROJECT_SUBDIRS) +
            " 这五个分类子目录。这篇文档请直接放进项目目录（subdir=<项目名>），"
            "不要为一次任务在项目内部再开目录。"
        ),
    }


def _project_overview_warnings(
    note_type: str,
    sd_norm: str,
    title_s: str,
    notes_dir,
) -> List[dict]:
    """同一项目出现「总览 + 主档」双入口 → warning（规则 4，不硬拦）。

    规则 4 的口径是「一个项目有且只有一个总览入口」。存量里已经有
    两个项目同时存在 `<项目名>项目.md` 和 `项目总览.md`（历史产物），
    硬拦会让正常写入失败，所以这里只提示，让调用方自己决定合并哪一份。
    """
    warnings: List[dict] = []
    try:
        if notes_dir is None:
            return warnings
        segs = _project_subdir_segments(note_type, sd_norm)
        if not segs:
            return warnings
        name = segs[0]
        d = Path(notes_dir) / _PROJECT_ROOT_DIR / name
        if not d.is_dir():
            return warnings
        overview = d / _PROJECT_OVERVIEW_NAME
        mains = [d / f"{name}{DOC_SUFFIX_FOR_LEDGER}", d / f"{name}.md"]
        main = next((m for m in mains if m.is_file()), None)
        stem = (title_s or "").strip()
        main_stems = {name, f"{name}项目"}
        if stem == _PROJECT_OVERVIEW_NAME[: -len(".md")] and main is not None:
            warnings.append(
                {
                    "kind": "project_dual_entry",
                    "paths": [str(main.relative_to(notes_dir))],
                    "hint": (
                        "本项目已有主档，再写「项目总览」会形成双入口；"
                        "规则 4 要求一个项目只有一个总览入口，请并入主档或删掉旧的那份"
                    ),
                }
            )
        elif stem in main_stems and overview.is_file():
            # 写主档时主档文件可能还不存在（本次才创建），所以这里只看总览在不在，
            # 不能要求 main 已存在 —— 否则「先有总览、后写主档」这条永远不告警。
            warnings.append(
                {
                    "kind": "project_dual_entry",
                    "paths": [str(overview.relative_to(notes_dir))],
                    "hint": (
                        "本项目已有「项目总览」，再写主档会形成双入口；"
                        "规则 4 要求一个项目只有一个总览入口"
                    ),
                }
            )
    except Exception:
        log.warning("write_policy.py 双入口检查异常，已忽略", exc_info=True)
    return warnings


def _content_has_fm_key(content: str, key: str) -> bool:
    """content 的 **frontmatter 顶层键**里有没有 key。

    不能用 ``key in content`` 子串判断：正文里出现同名字样（或代码块里写示例）就会
    被误判成「已有该字段」，真正缺失时反而不报警（审查轮3 三路都点了这一条）。
    """
    txt = content or ""
    if not txt.startswith("---"):
        return False
    end = txt.find("\n---", 3)
    if end < 0:
        return False
    for line in txt[3:end].splitlines():
        if not line.strip() or line[0] in " \t-#":   # 空行、续行、列表项、注释
            continue
        if re.match(r"^" + re.escape(key) + r"\s*:", line):
            return True
    return False


def _project_overview_field_warnings(
    note_type: str,
    title_s: str,
    tags=None,
    aliases=None,
    content: str = "",
) -> List[dict]:
    """写「项目总览」时检查检索字段（第 1 条，2026-10-31 新增）。

    检索得分是四层乘法，后两层吃的是 frontmatter 的
    importance / title / tags / canonical_tags / aliases。
    实测：只写 importance、不写 tags/aliases 的总览，搜项目名进不了前 8。

    检查项**由 rules.yaml 的 ``project.overview_required_fields`` 驱动**，本函数不写死
    清单；清单里出现本函数不认识的字段名时**跳过而不误报**（避免配置与实现漂移）。
    ``title`` 不在此列：总览的 title 必须恰好是「项目总览」才会走到这里，
    检查它恒为真、没有意义（要搜得到项目名靠的是 tags/aliases）。

    只 warning 不硬拦 —— 存量 3 份总览都缺这些字段，硬拦会让它们无法更新。
    """
    warnings: List[dict] = []
    try:
        if note_type != "project":
            return warnings
        stem = (title_s or "").strip()
        if stem != _PROJECT_OVERVIEW_NAME[: -len(".md")]:
            return warnings
        checks = {
            "tags": lambda: not (tags or []),
            "aliases": lambda: not (aliases or []),
            "importance": lambda: not _content_has_fm_key(content, "importance"),
        }
        known = [k for k in OVERVIEW_REQUIRED_FIELDS if k in checks]
        missing: List[str] = [k for k in known if checks[k]()]
        if missing:
            warnings.append({
                "kind": "overview_missing_retrieval_fields",
                "missing": missing,
                "hint": (
                    "总览页缺 %s。检索得分是四层乘法："
                    "rerank × 时间衰减 × (1+importance×0.5) × (1+0.25×标题/标签命中数，上限2.0)；"
                    "实测缺这些字段的总览，搜项目名时进不了前 8。"
                    "修复：写入时带 tags/aliases，正文 frontmatter 加 importance: 1.0。"
                    % "、".join(missing)
                ),
            })
    except Exception:
        log.warning("write_policy.py 总览字段检查异常，已忽略", exc_info=True)
    return warnings


def _status_default_warning(note_type: str, status=None) -> List[dict]:
    """payload 未显式传 status → 提示会落成 draft（第 2 条，2026-10-31 新增）。"""
    try:
        if not STATUS_DEFAULT_NOTICE or note_type not in ("project", "knowledge"):
            return []
        if (status or "").strip():
            return []
        return [{
            "kind": "status_defaulted_to_draft",
            "hint": (
                "payload 未指定 status，本条会落成 draft。"
                "正式的方案/结论类笔记建议显式传 status=active（或 done/archived），"
                "否则在设置页的状态视图里会被归入草稿。"
            ),
        }]
    except Exception:
        return []


def validate_write_hygiene(
    note_type: str,
    subdir: str,
    title: str,
    content: str,
    notes_dir: Optional[Path] = None,
    type_map: Optional[dict] = None,
    sanitize_fn: Optional[Callable[[str], str]] = None,
    tags=None,
    aliases=None,
    status=None,
) -> Tuple[Optional[dict], List[dict]]:
    """写时卫生硬拦。返回 (error_dict|None, warnings)。

    规则与历史 routes_notes._hygiene_path_guards / MCP validate_write_hygiene 对齐：
    - 废弃路径 10-项目_知识库项目
    - 项目 subdir 末段部署/deploy 分叉
    - 项目目录名以动作词结尾（2026-10-26 新增，止「一篇一目录」）
    - 项目 subdir 深度 > 2 段（2026-10-26 新增）
    - 新建项目目录未在 10-项目/README.md 项目表登记（2026-10-26 新增，规则 3）
    - archive 禁止落到 50-档案根
    - knowledge 下 skill 整包 dump（≥20k 且无指针关键词）
    - 同名 basename 已存在 → warning（不硬拦）
    - 同一项目「总览 + 主档」双入口 → warning（规则 4，不硬拦）

    本函数只做薄编排：校验规则拆到各 _guard_* / _same_basename_warnings，
    下面的调用顺序即校验优先级，与拆分前逐行一致；任何改动顺序前先看各函数 docstring。
    """
    warnings: List[dict] = []
    sd_norm, title_s, content_s = _norm_write_inputs(subdir, title, content)

    err = _guard_path_traversal(title_s, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_title_nonempty(title_s)
    if err is not None:
        return err, warnings

    err = _guard_deprecated_path(sd_norm, title_s)
    if err is not None:
        return err, warnings

    err = _guard_nested_top_dir(note_type, sd_norm, type_map)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_fork(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_action_word(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_depth(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_project_new_dir_unregistered(note_type, sd_norm, notes_dir)
    if err is not None:
        return err, warnings

    err = _guard_project_subdir_unregistered(note_type, sd_norm, notes_dir)
    if err is not None:
        return err, warnings

    err = _guard_archive_root(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_skill_dump(note_type, title_s, content_s)
    if err is not None:
        return err, warnings

    warnings.extend(
        _same_basename_warnings(note_type, title_s, notes_dir, sanitize_fn)
    )
    warnings.extend(
        _project_overview_warnings(note_type, sd_norm, title_s, notes_dir)
    )
    # ── 2026-10-31 新增（知识库规则反哺 · 第 1、2 条）──
    warnings.extend(
        _project_overview_field_warnings(note_type, title_s, tags, aliases, content_s)
    )
    warnings.extend(_status_default_warning(note_type, status))
    return None, warnings


def _overview_queue_path(notes_dir):
    """解析队列文件绝对路径，并确保它仍在 notes_dir 内（防规则配置路径逃逸）。

    队列路径来自 rules.yaml 的 ``project.overview_pending_file``；
    若该值被写成绝对路径或含 ``..``，``base / value`` 会跳出笔记根 —— 必须先校验再写。
    返回 Path；越界或无法解析时返回 None（调用方安静跳过）。
    """
    try:
        if notes_dir is None:
            return None
        base = Path(notes_dir).resolve()
        qp = (base / OVERVIEW_PENDING_FILE).resolve()
        if qp != base and base not in qp.parents:
            log.warning("overview 队列路径越界，已跳过：%s 不在 %s 内", qp, base)
            return None
        return qp
    except Exception:
        log.warning("write_policy.py 队列路径解析异常", exc_info=True)
        return None


def mark_overview_pending(rel_path: str, action: str = "updated", notes_dir=None):
    """写笔记后把所属项目加入「总览待刷新」队列（第 4 条，2026-10-31 新增）。

    - 只处理 ``10-项目/<项目>/<文件>``（**至少三段**；``10-项目/x.md`` 这种直接放在
      项目根下的文件不是项目笔记，不入队 —— 否则会把 ``x.md`` 当项目名写进队列）
    - 总览自身不入队
    - 队列文件 = ``<notes_dir>/.kb/overview_pending.json``（``.kb`` 不入库），
      路径经 containment 校验，越界即跳过
    - 并发：进程内 ``_OVERVIEW_QUEUE_LOCK`` + **唯一临时名**。
      固定临时名会让 waitress 多线程的两个 writer 互踩，写出半截 JSON
    - 文件损坏：**先备份成 ``.corrupt-<时间>`` 再重建**，不再静默清空（丢了能查）
    - 任何异常只 warning，**绝不影响写入主流程**
    - 返回入队的项目名；没入队返回 None
    """
    if not OVERVIEW_REFRESH_ENABLED:
        return None
    try:
        import json as _json
        from datetime import datetime as _dt

        rel = str(rel_path or "").replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p]
        if len(parts) < 3 or parts[0] != _PROJECT_ROOT_DIR:
            return None
        proj = parts[1]
        if not proj or proj in (".", "..") or parts[-1] == _PROJECT_OVERVIEW_NAME:
            return None

        qp = _overview_queue_path(notes_dir)
        if qp is None:
            return None

        with _OVERVIEW_QUEUE_LOCK:
            qp.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if qp.is_file():
                try:
                    data = _json.loads(qp.read_text(encoding="utf-8")) or {}
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    try:
                        bad = qp.with_name(
                            qp.name + ".corrupt-" + _dt.now().strftime("%Y%m%d-%H%M%S"))
                        os.replace(qp, bad)
                        log.warning("overview 队列文件损坏，已备份到 %s 并重建", bad)
                    except Exception:
                        log.warning("write_policy.py 队列损坏备份失败", exc_info=True)
                    data = {}
            pending = dict(data.get("pending") or {})
            now = _dt.now().isoformat(timespec="seconds")
            pending[proj] = {"last": now, "action": action}
            data["pending"] = pending
            data["interval_min"] = OVERVIEW_REFRESH_INTERVAL_MIN
            data["updated"] = now
            tmp = qp.with_name(f"{qp.name}.tmp-{os.getpid()}-{threading.get_ident()}")
            try:
                tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(tmp, qp)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
        return proj
    except Exception:
        log.warning("write_policy.py mark_overview_pending 异常，已忽略", exc_info=True)
        return None


def on_note_changed(
    action: str = "updated",
    force_search_refresh: bool = False,
    _hooks: Optional[Dict[str, Any]] = None,
    *,
    rel_path: Optional[str] = None,
    notes_dir=None,
) -> None:
    """统一写后出口：缓存失效 + 索引刷新 +（可选）mark dirty。

    - create / save / upload / smart_upload / batch_tag：默认 update_indexes（冷却）
    - delete / archive / batch_delete/archive / 显式 force：trigger_index_refresh(force=True)
    - 测试可注入 _hooks，避免碰生产索引
    """
    hooks = _hooks
    if hooks is None:
        # 延迟导入，避免循环依赖
        from ..routes import trigger_index_refresh, _update_indexes
        from ..routes.routes_notes import _invalidate_notes_cache
        from ..routes.routes_general import _invalidate_graph_cache

        hooks = {
            "invalidate_notes_cache": _invalidate_notes_cache,
            "invalidate_graph_cache": _invalidate_graph_cache,
            "update_indexes": _update_indexes,
            "trigger_index_refresh": trigger_index_refresh,
        }
        # 生产路径：尽量挂上 SearchIndex.mark_dirty（A2）
        try:
            from ..search import search_index

            hooks["mark_index_dirty"] = lambda: search_index.mark_dirty(reason=action)
        except Exception:
            log.warning("write_policy.py 异常", exc_info=True)

    inv_notes = hooks.get("invalidate_notes_cache")
    if callable(inv_notes):
        inv_notes()

    inv_graph = hooks.get("invalidate_graph_cache")
    if callable(inv_graph):
        inv_graph()

    mark_dirty = hooks.get("mark_index_dirty")
    if callable(mark_dirty):
        mark_dirty()

    # ── 第 4 条（2026-10-31）：写笔记 → 标记「该项目总览待刷新」──
    # ⚠️ 必须放在下面的 force 早退**之前**：删除/归档/移动都在 FORCE_REFRESH_ACTIONS 里，
    #    函数会在 force 分支直接 return，排在后面等于对这些动作永不生效（自审发现）。
    # 只入队，不在这里刷新（刷新由 kb-pipeline watch 按设置里的间隔消费）；
    # notes_dir 必须一并传，否则 mark_overview_pending 拿不到笔记根会**安静跳过**。
    if rel_path:
        mark_overview_pending(rel_path, action=action, notes_dir=notes_dir)

    force = bool(force_search_refresh) or action in FORCE_REFRESH_ACTIONS
    if force:
        # 与旧 delete 行为对齐：关闭 index_auto_rebuild 时不刷搜索
        auto = True
        try:
            from ..config import get_setting

            auto = bool(get_setting("index_auto_rebuild"))
        except Exception:
            log.warning("write_policy.py 异常: on_note_changed", exc_info=True)
            auto = True
        if auto or _hooks is not None:
            # 测试注入 hooks 时不读配置，直接 force 触发以便断言
            trig = hooks.get("trigger_index_refresh")
            if callable(trig):
                trig(force=True)
        return

    upd = hooks.get("update_indexes")
    if callable(upd):
        upd()
