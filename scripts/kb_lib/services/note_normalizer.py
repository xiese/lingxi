#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note_normalizer.py — 知识库统一写入入口（规范化路径 + front matter，无落盘副作用）。

规则唯一来源: kb_migrate_lib.py（《知识库整理方案_v1》），
经 kb_rules_export.py 导出为 rules.yaml 后由本模块读取，
保证「存量迁移」与「增量写入」是同一套口径。

用法::

    n = NoteNormalizer(kb_root="/path/to/知识库/笔记", rules_path="/path/to/知识库/笔记/.kb/rules.yaml")
    rel_path, front_matter, warnings = n.normalize({
        "title": "...", "content": "...", "type": "knowledge",
        "subdir": "示例项目", "tags": ["示例"], "status": "active",
    })

normalize() 只做计算，不读写磁盘（唯一的磁盘读取是重名判定的 os.path.exists）。

与迁移工具链的两处有意差异（均为写入场景需要）:
  1. 体裁判定优先看目录末段与标题关键词，而不是先看调用方给的 type。
     写入时 type 常被随手填成默认值 knowledge，没有信息量；先用它会把
     「排障」「日志」这类体裁全部压成 knowledge，kind 字段就失去意义。
  2. 新增 old_path 入参：原路径在 50-档案 下时一律按归档处理。
     设计文档原版没有这个入参，导致归档笔记认不出自己，被扔进收件箱。
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ── 服务端类型（scripts/kb_lib/config.py TYPE_MAP 的值域）──
# 写入的 front matter `type` 只能是这 7 个；体裁信息放 `kind` 字段。
SERVER_TYPE_BY_TOP = {
    "00-索引": "index",
    "10-项目": "project",
    "20-知识": "knowledge",
    "30-记录": "record",
    "30-写作": "writing",
    "40-日记": "diary",
    "50-档案": "archive",
    "60-梦境": "dream",
    "70-经验": "knowledge",
    "99-待整理": "record",
}
SERVER_TYPES = {"knowledge", "project", "writing", "diary", "record", "archive", "dream"}

# 体裁值域（迁移工具链 TYPE_MAP 的值域）
KINDS = {"log", "troubleshoot", "conclusion", "config", "knowledge", "record", "readme"}

# 标题命中排障词 -> troubleshoot，否则命中日志词 -> log
TROUBLESHOOT_RE = re.compile(r"排障|修复|事故|根因|复核|报错|崩溃|bug|fix|incident", re.I)

TIME_TOKEN_RE = re.compile(r"(?<!\d)\d{1,2}[_\-.]\d{2}(?!\d)")
TIME_4DIGIT_RE = re.compile(r"(?<=[_\-.])([01]\d|2[0-3])[0-5]\d(?=[_\-.]|$)")
ILLEGAL_RE = re.compile(r'[<>:"\\|?*\x00-\x1f/]+')
CUT_SEPS = ("_", "：", "，", "。", "、", "-", "(", "（")


class NoteNormalizer:
    """规范化笔记的落点路径与 front matter。规则全部来自 rules.yaml。"""

    def __init__(self, kb_root, rules_path=None):
        self.root = str(kb_root)
        rules_path = rules_path or os.environ.get("KB_RULES") or os.path.join(
            self.root, ".kb", "rules.yaml"
        )
        with open(rules_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}
        self.folders = self.cfg["folders"]
        self.target_dirs = list(self.cfg.get("target_dirs") or [])
        self.dir_flags = dict(self.cfg.get("dir_flags") or {})
        # 迁移工具链的有序映射表 [旧前缀, 新前缀, 标记]（长前缀在前）
        self.dir_map = [tuple(x) for x in (self.cfg.get("dir_map") or [])]
        self.unmatched_exact = dict(self.cfg.get("unmatched_exact") or {})
        self.unmatched_top = dict(self.cfg.get("unmatched_top") or {})
        self.top_order = list(self.cfg.get("top_order") or [])
        self._tops = set(self.top_order) | set(self.unmatched_top)
        self.archive_prefix = self.folders["archive"]
        self.archive_pending = str(self.cfg.get("archive_pending_batch") or "2026-10")
        self.inbox = self.folders["inbox"]
        self.caps = self.cfg.get("name_caps") or {}
        self.max_name = int(self.caps.get("max_name_len", 25))
        self.max_title = int(self.caps.get("max_title", 40))
        self.date_re = re.compile(self.cfg.get("date_re") or r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})")
        self.log_hint = re.compile(self.cfg.get("log_hint") or r"日志|排障|修复|复核|复盘")
        self.status_map = {k.lower(): v for k, v in (self.cfg.get("status_map") or {}).items()}
        self.kind_map = {k.lower(): v for k, v in (self.cfg.get("kind_map") or {}).items()}
        self.dirname_kind = dict(self.cfg.get("dirname_kind") or {})
        self.tag_map = {k.lower(): v for k, v in (self.cfg.get("tag_lower_map") or {}).items()}
        self.kind_subdirs = dict(self.cfg.get("kind_subdirs") or {})
        self.kind_subdir_order = list(self.cfg.get("kind_subdir_order") or [])
        self.log_subdir = self.cfg.get("log_subdir") or "日志"
        self.ts_subdir = self.cfg.get("troubleshoot_subdir") or "排障"
        self.troubleshoot_hint = self.cfg.get("troubleshoot_hint") or "排障"
        self.topfile_hint = [tuple(x) for x in (self.cfg.get("topfile_hint") or [])]
        self.log_skip = set(self.cfg.get("log_subdir_skip") or [])
        # 别名反向索引：别名 -> 目标目录。DIR_MAP 有序，冲突时保留先出现的。
        self.alias_index = {}
        for target, names in (self.cfg.get("aliases") or {}).items():
            for name in names:
                self.alias_index.setdefault(name.lower(), target)
        # 自动发现的现有项目目录缓存（按 10-项目 的 mtime 失效）
        self._disc_cache = None
        self._disc_mtime = None

    # ── 项目自动发现 ──────────────────────────────────────────────
    def _discover_projects(self):
        """读 10-项目 下的现有子目录名（设计文档第 25 行：新建目录即生效）。

        只发现一层：项目目录下的子目录是分类，不是项目本身。
        以 10-项目 的 mtime 为缓存键——新建项目目录会改父目录 mtime，
        因此新建的项目立即可见，无需重启或改配置。
        下划线/点开头的（_assets 等）与文件一律排除。
        """
        proj_root = os.path.join(self.root, self.folders["projects"])
        try:
            mt = os.path.getmtime(proj_root)
        except OSError:
            return set()
        if self._disc_cache is not None and self._disc_mtime == mt:
            return self._disc_cache
        found = set()
        try:
            for name in os.listdir(proj_root):
                if name.startswith(".") or name.startswith("_"):
                    continue
                if os.path.isdir(os.path.join(proj_root, name)):
                    found.add(name)
        except OSError:
            return set()
        self._disc_cache = found
        self._disc_mtime = mt
        return found

    # ── 目录识别 ──────────────────────────────────────────────────
    @staticmethod
    def _segment_match(d, prefix):
        """d 是否以 prefix 按路径段开头（不吃半个段名）。与 kb_migrate_lib 同名函数一致。"""
        return d == prefix or d.startswith(prefix + "/")

    def match_dir(self, d):
        """逐行对齐 kb_migrate_lib.match_dir。

        返回 (目标目录, 标记, 是否命中映射)。标记 m=项目 k=原位 w=待整理 z=归档。
        未命中映射时按一级段兜底，第三位为 False。
        """
        d = str(d or "").strip("/")
        if d in self.unmatched_exact:
            return d, self.unmatched_exact[d], True
        for a, b, f in self.dir_map:
            if not a or not self._segment_match(d, a):
                continue
            tail = d[len(a):]
            if not tail:
                return b, f, True
            # 保留未被子映射覆盖的深层路径（20-知识/示例项目/xxx → 10-项目/示例项目/xxx）
            cand = b + tail
            deeper = ""
            for a2, b2, f2 in self.dir_map:
                if a2 and a2 != a and self._segment_match(cand, a2):
                    deeper = b2 + cand[len(a2):]
                    f = f2
                    break
            if not deeper:
                # 恒等映射: 深层子目录原样保留；否则整体换前缀
                return (d if a == b else cand), f, True
            return deeper, f, True
        top = d.split("/", 1)[0] if d else ""
        if top in self.unmatched_top:
            flag = self.unmatched_top[top]
            if flag == "z":
                return f"{self.archive_prefix}/{self.archive_pending}/{d}", "z", False
            if flag == "w":
                return f"{self.inbox}/{d}", "w", False
            return d, "k", False
        return self.inbox, "w", False

    def _match_by_tail(self, candidate):
        """候选按路径段当尾部，去已知目标目录里找以其结尾的最长目录。

        例: "示例项目/子目录" -> 10-项目/示例项目/子目录；"示例项目" -> 10-项目/示例项目
        """
        segs = [s for s in str(candidate).strip("/").split("/") if s]
        if not segs:
            return ""
        best = ""
        for d in self.target_dirs:
            dsegs = d.split("/")
            if len(dsegs) >= len(segs) and dsegs[-len(segs):] == segs:
                if len(d) > len(best):
                    best = d
        return best

    def _match_by_alias(self, candidate):
        """整串优先，再逐段（深到浅）查别名表。"""
        c = str(candidate).strip().lower()
        if not c:
            return ""
        if c in self.alias_index:
            return self.alias_index[c]
        for seg in reversed([s for s in c.split("/") if s]):
            if seg in self.alias_index:
                return self.alias_index[seg]
        return ""

    def _candidates(self, project, subdir, tags):
        """写入方给的线索 → 候选原路径（长候选在前，先试更具体的）。"""
        out = []
        p = str(project or "").strip().strip("/")
        s = str(subdir or "").strip().strip("/")
        for c in (f"{p}/{s}" if p and s else (s or p), p, s):
            if c and c not in out:
                out.append(c)
        for seg in reversed([x for x in s.split("/") if x]):
            if seg not in out:
                out.append(seg)
        for t in tags or []:
            t = str(t).strip()
            if t and t not in out:
                out.append(t)
        return out

    def resolve_dir(self, project="", subdir="", tags=(), old_path=""):
        """返回 (目标目录, 标记)。认不出时返回 (99-待整理, "w")。

        三条路，按优先级：
        1. 有原路径（存量笔记）→ kb_migrate_lib.match_dir 全量规则；
        2. 候选自带已知顶层前缀（如 20-知识/运维）→ 同一套 match_dir；
        3. 只有项目名/子目录（写入场景）→ 已知目标目录的尾段匹配 + 别名表。
        候选不带顶层前缀时不硬拼前缀：恒等映射（70-经验→70-经验）会把
        任意未知名都「认下来」，产生假命中。
        """
        old_dir = os.path.dirname(str(old_path or "").strip("/"))
        if old_path and not old_dir:
            # 笔记根目录下的散件（如 README.md）: 工具链原位保留（dir 为空）
            return "", "k"
        if old_dir:
            d, f, _hit = self.match_dir(old_dir)
            # 一级根目录散件: 按文件名关键词猜归属（对齐 plan_note 的 TOPFILE_HINT）
            if old_dir in (self.folders["projects"], self.folders["knowledge"]):
                stem = os.path.splitext(os.path.basename(str(old_path).strip("/")))[0]
                if stem == "README":
                    f = "k"
                else:
                    for kw, target in self.topfile_hint:
                        if kw in stem:
                            return target, "m"
            return d, f
        for c in self._candidates(project, subdir, tags):
            head = c.split("/", 1)[0]
            if head in self._tops:
                d, f, hit = self.match_dir(c)
                if hit:
                    return d, f
            hit2 = self._match_by_tail(c) or self._match_by_alias(c)
            if hit2:
                return hit2, self.dir_flags.get(hit2, "k")
            # 自动发现兜底：候选就是 10-项目 下的现有目录名（含多级时的首段）
            if "/" not in c and c in self._discover_projects():
                return f"{self.folders['projects']}/{c}", "m"
        return self.inbox, "w"

    # ── 体裁识别 ──────────────────────────────────────────────────
    def resolve_kind(self, raw_type, folder, is_log, flag="k", leaf=""):
        """体裁: log/troubleshoot/conclusion/config/knowledge/record/readme。

        逐行对齐 kb_migrate_lib.plan_note 的 type 映射（508-520 行）:
        调用方 type 先查 TYPE_MAP，命中即用（knowledge 也算命中）；
        未命中才看目录末段；再未命中按标记兜底。

        注意 leaf 必须是**追加日志子目录之前**的目录末段：工具链在 461 行
        就固定了 leaf，487 行改的是 new_dir，leaf 不跟着变。用追加后的末段
        会把所有项目日志判成 log/troubleshoot，与工具链不一致。
        """
        tp = self.kind_map.get(str(raw_type or "").strip().lower(), "")
        if not tp:
            tp = self.dirname_kind.get(leaf or folder.rstrip("/").split("/")[-1], "")
        if not tp:
            tp = {
                "m": "log" if is_log else "record",
                "k": "knowledge",
                "w": "record",
                "z": "record",
            }.get(flag, "record")
        if flag == "z":
            tp = "record"
        return tp

    # ── 状态识别 ──────────────────────────────────────────────────
    def resolve_status(self, raw_status, is_log, flag, body_len):
        raw = str(raw_status or "").strip()
        st = self.status_map.get(raw.lower(), "draft")
        if not raw and body_len is not None and body_len < 300:
            st = "draft"
        elif is_log and st == "draft":
            st = "done"          # 方案五: 日志/排障/研究轮写入即 done
        if flag == "z":
            st = "archived"      # 50-档案 一律 archived
        return st

    # ── 命名 ──────────────────────────────────────────────────────
    def _sanitize(self, name, cap):
        name = ILLEGAL_RE.sub("_", name or "")
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"_{2,}", "_", name).strip("_")
        if len(name) <= cap:
            return name
        head = name[:cap]
        cut = max(head.rfind(sep) for sep in CUT_SEPS)
        if cut >= int(cap * 0.6):
            head = head[:cut]
        head = head.strip("_：，。、-(")
        return head or name[:cap]

    def _strip_project_name(self, short, new_dir, old_dir):
        """剥掉短标题里的项目名（目录已是项目名，不重复写）。

        剥掉短标题里的项目名（目录已是项目名，不重复写）。大小写敏感：
        只有与目录段逐字符相同的前缀才剥除，避免误伤大小写不同的词。
        """
        cands = set()
        for d in (new_dir, old_dir):
            for seg in str(d or "").split("/"):
                seg = seg.strip()
                if seg and not re.fullmatch(r"[\d\-_]+", seg):
                    cands.add(seg)
        out = short
        for name in sorted(cands, key=len, reverse=True):
            if len(name) < 2:
                continue
            for pat in (f"^{re.escape(name)}[_\\-]?", f"[_\\-]?{re.escape(name)}$"):
                cand = re.sub(pat, "", out)
                if cand and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cand):
                    out = cand
        return re.sub(r"_{2,}", "_", out).strip("_") or short

    def _strip_time_token(self, short):
        out = TIME_TOKEN_RE.sub("_", short)
        out = TIME_4DIGIT_RE.sub("", out)
        return re.sub(r"_{2,}", "_", out).strip("_") or short

    def extract_date(self, *texts):
        """从多个文本里取第一个日期，返回 YYYY-MM-DD。"""
        for t in texts:
            m = self.date_re.search(str(t or ""))
            if not m:
                continue
            g = m.groups()
            if g[0] is not None:
                y, mo, d = g[0], g[1], g[2]
            else:
                y, mo, d = g[3], g[4], g[5]
            try:
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            except (TypeError, ValueError):
                continue
        return ""

    def is_log_like(self, stem, title, folder, date):
        """事件型判定，逐行对齐 kb_migrate_lib.plan_note:

        (文件名或标题命中 LOG_HINT) 且有日期；目录末段是 日志/排障 时直接算。
        """
        leaf = folder.rstrip("/").split("/")[-1]
        hit = bool(
            self.log_hint.search(str(stem or "")) or self.log_hint.search(str(title or ""))
        ) and bool(date)
        if leaf in (self.log_subdir, self.ts_subdir):
            hit = True
        return hit

    def build_name(self, stem, folder, date, is_log, old_dir=""):
        """返回 (文件名, 短标题)。文件名含 .md，总长不超过 max_name。

        与 kb_migrate_lib.plan_note 一致：短标题取自**原文件名**（不是 front
        matter 的 title），日期一律压掉后只留开头前缀。
        """
        stem_clean = self.date_re.sub("_", str(stem or ""))
        short = self._sanitize(stem_clean, 999)
        short = self._strip_project_name(short, folder, old_dir)
        short = self._strip_time_token(short)
        # 2026-10-22 修（T-08）：原文件名以日期开头时，非事件型笔记的日期也会被
        # date_re.sub("_") 抹掉，文件名只剩标题（"2026-10-19 项目进展" → "项目进展.md"）。
        # 日期是原始信息，不能丢：日期开头的笔记一律保留 YYYY-MM-DD_ 前缀。
        keep_date = bool(date) and bool(self.date_re.match(str(stem or "")))
        if not is_log and keep_date:
            cap = min(int(self.caps.get("knowledge", 20)),
                      self.max_name - len(date) - 1 - 3)
            short = self._sanitize(short, max(1, cap))
            return f"{date}_{short}.md", short
        cap = int(self.caps.get("log" if is_log else "knowledge", 20))
        if is_log:
            # 事件型文件名 = YYYY-MM-DD(10) + "_" + 短标题 + ".md"(3)
            cap = min(cap, self.max_name - len(date) - 1 - 3)
        else:
            cap = min(cap, self.max_name - 3)
        short = self._sanitize(short, max(1, cap))
        return (f"{date}_{short}.md" if is_log else f"{short}.md"), short

    def unique_path(self, folder, filename):
        """重名加 _2/_3…；加序号后仍不超过 max_name（从短标题尾部压掉）。"""
        rel = f"{folder}/{filename}" if folder else filename
        if not os.path.exists(os.path.join(self.root, rel)):
            return rel
        stem, ext = os.path.splitext(filename)
        is_dated = bool(self.date_re.match(stem))
        date_prefix = f"{stem[:10]}_" if is_dated else ""
        body = stem[len(date_prefix):]
        n = 2
        while True:
            suffix = f"_{n}"
            over = max(0, len(stem) + len(suffix) - (self.max_name - len(ext)))
            room = max(1, len(body) - over)
            cand = f"{date_prefix}{body[:room]}{suffix}{ext}"
            rel = f"{folder}/{cand}" if folder else cand
            if not os.path.exists(os.path.join(self.root, rel)):
                return rel
            n += 1

    # ── 标签 ──────────────────────────────────────────────────────
    def normalize_tags(self, tags, folder):
        """小写化 + 别名合并。与 kb_migrate_lib.normalize_tags 逐条对齐。

        不过滤「与项目目录同名的标签」——迁移工具链的标签表里
        dsh/hermes/cpa 这类项目名本身就是保留标签，过滤掉会丢信息。
        """
        out = []
        for t in tags or []:
            k = self.tag_map.get(str(t).strip().lower(), str(t).strip().lower())
            if k and k not in out:
                out.append(k)
        return out

    # ── 主入口 ────────────────────────────────────────────────────
    def normalize(self, note, dedup=True):
        """note: dict(title, content, type, status, tags, subdir, project, created, old_path)

        返回 (相对路径, front_matter, warnings)。不落盘。

        dedup=False 时不做重名加后缀（服务端写入语义是覆盖更新，不是新建副本）。
        """
        warns = []
        title = str(note.get("title") or "").strip()
        content = str(note.get("content") or "")
        old_path = str(note.get("old_path") or "").strip("/")
        old_dir = os.path.dirname(old_path) if old_path else ""
        subdir = str(note.get("subdir") or "")
        # 短标题取自原文件名；没有原路径时退回 title
        stem = os.path.splitext(os.path.basename(old_path))[0] if old_path else title

        # 目录: 原路径优先（存量笔记认自己），否则用写入方给的线索
        folder, flag = self.resolve_dir(
            project=note.get("project") or "",
            subdir=subdir,
            tags=note.get("tags") or [],
            old_path=old_path,
        )
        if flag == "w" and folder == self.inbox:
            warns.append("未识别项目，已放入 %s 待人工分流" % self.inbox)
        archiving = flag == "z" or folder.startswith(self.archive_prefix)

        date = self.extract_date(stem, note.get("created") or "")
        # 归档笔记同样判事件型（对齐 plan_note 462 行：只看关键词+日期，不看 flag）
        is_log = self.is_log_like(stem, title, folder, date)
        if is_log and not date:
            warns.append("事件型但无日期，退回普通命名")
            is_log = False

        # leaf 与命名都固定在追加日志子目录之前（对齐 plan_note 461/475 行的时机）
        leaf = folder.rstrip("/").split("/")[-1]
        kind = self.resolve_kind(note.get("type"), folder, is_log, flag, leaf)
        # 命名先于追加：否则 日志/排障 会被 strip_project_name 当成项目名剥掉
        filename, short = self.build_name(stem, folder, date, is_log, old_dir)

        # 事件型落进项目目录 -> 进 日志/ 或 排障/（对齐 plan_note 484-487 行）
        if (
            is_log
            and flag == "m"
            and leaf not in self.log_skip
            and folder.startswith(self.folders["projects"] + "/")
        ):
            hit = self.troubleshoot_hint in stem or self.troubleshoot_hint in title
            folder = f"{folder}/{self.ts_subdir if hit else self.log_subdir}"

        # 空壳判定按字节数，与 kb_migrate_lib.plan_note 的 body_bytes 对齐
        body_bytes = len(content.encode("utf-8")) if content else 0
        status = self.resolve_status(note.get("status"), is_log, flag, body_bytes)

        rel = self.unique_path(folder, filename) if dedup else (
            f"{folder}/{filename}" if folder else filename
        )

        top = rel.split("/", 1)[0]
        server_type = SERVER_TYPE_BY_TOP.get(top, "record")
        if top == self.archive_prefix:
            server_type = "archive"

        summary = str(note.get("summary") or "")
        if not summary and len(title) > self.max_title:
            summary = title[:400]

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        fm = {
            "title": (title or short)[: self.max_title],
            "type": server_type,
            "kind": kind,
            "project": folder.split("/", 1)[1] if folder.startswith(
                self.folders["projects"] + "/") else "",
            "status": status,
            "created": note.get("created") or now,
            "updated": now,
            "tags": self.normalize_tags(note.get("tags"), folder),
            "summary": summary,
        }
        if server_type not in SERVER_TYPES:
            warns.append("服务端不认识 type=%s，已按目录推断" % server_type)
        return rel, fm, warns


# ── 服务端接入层 ──────────────────────────────────────────────────
# 以下供 kb-server 的写入入口调用。核心约束：**任何异常都不得阻断写入**。
# 开关 KB_NORMALIZE_WRITES=0/off/false/no 时完全退回原行为。

_ENABLED_OFF = {"0", "off", "false", "no"}


def writes_enabled():
    """写入规范化总开关（默认开）。关闭后行为与接入前逐字节一致。"""
    return (os.environ.get("KB_NORMALIZE_WRITES", "1") or "").strip().lower() not in _ENABLED_OFF


_SINGLETON = {}


def get_normalizer(notes_dir):
    """按笔记根目录缓存单例；规则文件读不到时返回 None（调用方降级）。"""
    key = str(notes_dir)
    if key in _SINGLETON:
        return _SINGLETON[key]
    inst = None
    try:
        inst = NoteNormalizer(key)
    except Exception:
        log.warning("note_normalizer: 规则文件不可用，写入规范化已降级", exc_info=True)
    _SINGLETON[key] = inst
    return inst


def plan_server_write(notes_dir, *, title, content="", note_type="", subdir="",
                      tags=None, status="", created="", old_path=""):
    """把一次写入请求规范化。

    成功返回 dict：{rel, folder, type, kind, warnings}；返回 None 表示「不接管」，
    调用方保持原逻辑。触发 None 的条件：开关关闭、规则不可用、或规范化过程抛任何
    异常——这是最高优先级的失败模式，写入绝不能因此中断。

    与迁移场景的两处关键差异：
      1. 不加重名后缀：服务端同路径写入的既有语义是**覆盖更新**，
         若在此加 _2 会让「再次保存」变成新建副本，破坏既有契约。
      2. 不返回 front matter：服务端 front matter 由 _build_frontmatter 统一构建，
         本模块只接管落点、文件名，以及新增的 kind 字段。
    """
    if not writes_enabled():
        return None
    try:
        n = get_normalizer(notes_dir)
        if n is None:
            return None
        rel, fm, warns = n.normalize({
            "title": title, "content": content, "type": note_type,
            "subdir": subdir, "tags": tags or [], "status": status,
            "created": created, "old_path": old_path,
        }, dedup=False)
        if not rel:
            return None
        folder = os.path.dirname(rel)
        # 调用方**没给归属线索**（subdir 为空）时不接管，交回原逻辑按 type 落位。
        # 否则 diary 的 40-日记/年/月、以及不带线索的普通知识写入都会被无谓地
        # 打散进待整理。「未识别进待整理」只对**给了线索却认不出**的写入生效。
        if not (subdir or "").strip():
            return None
        # 类型一致性判据（必须）：规范化是从**目录**反推 type 的，若放任它覆盖
        # 调用方明确给出的 type，会出现 archive 被挪进 20-知识、writing 被改判
        # 成 record 这类错乱。因此调用方给了**有信息量的 type**时，要求规范化
        # 落点的顶层目录与之一致；不一致就不接管，交回原逻辑。
        # knowledge 是服务端默认值、无信息量（写入时常见），不参与该判据。
        top = rel.split("/", 1)[0]
        # 落点目录存在性判据（2026-10-16 拍板方案 2；2026-10-23 收窄，T-10）：
        # 原判据是「落点目录不存在就不接管」，意图是防止存量迁移没跑完时新笔记
        # 凭空新建目标态目录、与既有目录并存（目录分裂）。
        #
        # 但它过严：**父目录存在、只差一层子目录**的情况也被放弃，导致带 subdir
        # 的写入绝大多数根本没被规范化。实测 14 种写入场景只接管 5 种（T-09 后 6 种）。
        #
        # 收窄后分两档：
        #   · 落点目录已存在        → 接管（原行为）
        #   · 落点缺一层、父目录在  → 接管（新建这一层，不会造成同项目两份目录，
        #                             因为父目录已经是该项目的既有位置）
        #   · 落点缺两层及以上      → 不接管（这才真会造出目标态的整条新分支）
        # 待整理区（inbox）仍完全豁免，不受此判据约束。
        if top != n.inbox:
            landing = os.path.join(notes_dir, folder)
            if not os.path.isdir(landing):
                parent = os.path.dirname(folder)
                # 父目录为空 = 落点就是顶层目录本身（顶层都不存在说明库结构异常），不接管
                if not parent or not os.path.isdir(os.path.join(notes_dir, parent)):
                    return None
        want = (note_type or "").strip()
        if top == n.inbox:
            # 待整理区只服务一个场景：**项目归属未定**的写入（用户拍板：
            # 解析不出项目 -> 99-待整理，取代设计文档的 00-收件箱）。
            # 其他类型（日记/写作/档案/普通知识）落进这里属于目录识别失败，
            # 不能借此改写调用方的类型判断，一律不接管、交回原逻辑。
            # 该区同时豁免一致性判据——否则这条拍板路径永远不会生效。
            if want != "project":
                return None
            ftype = "project"
        else:
            if want and want != "knowledge" and want in SERVER_TYPES:
                if SERVER_TYPE_BY_TOP.get(top, "") != want:
                    return None
            ftype = fm.get("type", "")
        return {
            "rel": rel,
            "folder": folder,
            "type": ftype,
            "kind": fm.get("kind", ""),
            "warnings": warns,
        }
    except Exception:
        log.warning("note_normalizer: 规范化失败，已退回原写入逻辑", exc_info=True)
        return None
