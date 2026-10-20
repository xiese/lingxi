"""知识库 API — 路由包（版本见 config.py VERSION）"""

import sys, re, subprocess, threading, time as _time, secrets
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app, g

from ..config import KB_ROOT, NOTES_DIR, get_setting, is_excluded_path, RATE_LIMIT, TYPE_PREFIX
from ..store import is_master
from ..search import search_index, _tokenize
from ..services.text_clean import clean_for_match
from ..llm import llm_chat_with_retry, LLMUnavailable
from ..auth import safe, safe_note, parse_fm, get_token

bp = Blueprint("kb", __name__)

# ── 限流（before_request 全局钩子）──

_rate_store = {}
_rate_lock = threading.Lock()
_RATE_CLEANUP_INTERVAL = 120  # 每120秒清理过期记录


log = logging.getLogger("kb.__init__")

def _rate_cleanup_loop():
    """后台线程：每120秒清理过期限流记录"""
    while True:
        _time.sleep(_RATE_CLEANUP_INTERVAL)
        try:
            now = _time.time()
            with _rate_lock:
                for k in list(_rate_store.keys()):
                    for t in list(_rate_store[k].keys()):
                        _rate_store[k][t] = [ts for ts in _rate_store[k][t] if now - ts < RATE_LIMIT.get(t, (500, 60))[1]]
                        if not _rate_store[k][t]:
                            del _rate_store[k][t]
                    if not _rate_store[k]:
                        del _rate_store[k]
        except Exception:
            log.warning("__init__.py 异常", exc_info=True)


_rate_cleanup_started = False


def start_rate_cleanup():
    """启动限流记录清理线程（幂等，由 kb_lib.start_background() 调用）。

    2026-10-07 修复：原实现在模块级直接起线程——任何 import kb_lib.routes
    （含测试 collect，以及 kb_auth_watch / kb_eval / kb_note_hygiene 等脚本
    触发的包初始化）都会拉起这个 120 秒循环。现收编为显式启动，import 只做声明。
    """
    global _rate_cleanup_started
    if _rate_cleanup_started:
        return
    _rate_cleanup_started = True
    threading.Thread(target=_rate_cleanup_loop, daemon=True, name="kb-rate-cleanup").start()


def _rate_tier():
    """根据请求方法和路径归类限流层级"""
    ep = request.path
    if ep in ("/health", "/favicon.ico") or ep.startswith(("/web/", "/auth/")):
        return None  # 免限流
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if ep.startswith("/upload"):
            return "upload"
        return "write"
    return "read"


@bp.before_request
def rate_limit():
    tier = _rate_tier()
    if tier is None:
        return
    # 提取 token
    tk = get_token()
    if not tk:
        return  # 无 token 的请求后面会被 check_auth 拒绝
    if is_master(tk):
        return  # 主 Token 不限流
    lim, win = RATE_LIMIT.get(tier, (500, 60))
    prefix = tk[:16]
    now = _time.time()
    with _rate_lock:
        bucket = _rate_store.setdefault(prefix, {})
        times = bucket.setdefault(tier, [])
        cutoff = now - win
        times[:] = [t for t in times if t > cutoff]
        if len(times) >= lim:
            return jsonify({"error": "请求过于频繁，请稍后再试"}), 429
        times.append(now)

# ── 共享工具函数 ──

def json_body_dict():
    """取 JSON 请求体，并保证拿到的是 dict。

    安全加固：原写法 `request.get_json(force=True) or {}` 对
    list / str / number / bool 这类**真值**原样返回，随后调用方 `.get()`
    抛 `AttributeError` → 500。实测匿名端点 `POST /auth/request` 的 body
    传 `[1,2]` / `"str"` / `123` / `true` 全部 500，而 `null` / `[]` 却返
    200 —— 行为不一致且匿名可触发。这里统一收敛：非 dict 一律当空 dict，
    调用方随即按「缺少参数」返回 400。
    """
    d = request.get_json(force=True, silent=True)
    return d if isinstance(d, dict) else {}


def smart_chunk(text, chunk_size=1500, overlap=300):
    chunks = []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    current = ""
    for para in paragraphs:
        if len(current) + len(para) < chunk_size:
            current += para + "\n\n" if current else para
        else:
            if current:
                chunks.append(current.strip())
            current = para
    if current:
        chunks.append(current.strip())
    overlapped = []
    for i, ch in enumerate(chunks):
        if i > 0:
            prev_end = chunks[i - 1][-overlap:]
            ch = prev_end + "\n[重叠衔接]\n" + ch
        overlapped.append(ch)
    return overlapped if overlapped else [text]


def _chunk_lines(body, chunk_size=1500, overlap=300):
    """与 smart_chunk 相同的分块口径，但额外记录每块在原文中的起止行号。

    返回 [(chunk_text, line_start, line_end), ...]，行号 1-based、闭区间。
    用途：让 kb_ask 的回答可溯源到行（citations），而非只给一个文件路径。
    分块算法与 smart_chunk 保持一致（段落聚合 + 块间 overlap 衔接），
    唯一区别是不丢弃位置信息 —— 调用方无需改动原有 smart_chunk 语义。
    """
    if not body:
        return []
    lines = body.split("\n")
    # 记录每个段落（以空行分隔）的起止行
    paras = []          # [(text, start_line, end_line)]
    cur, cur_start = [], None
    for i, ln in enumerate(lines, start=1):
        if ln.strip() == "":
            if cur:
                paras.append(("\n".join(cur).strip(), cur_start, i - 1))
                cur, cur_start = [], None
        else:
            if cur_start is None:
                cur_start = i
            cur.append(ln)
    if cur:
        paras.append(("\n".join(cur).strip(), cur_start, len(lines)))

    # 按 chunk_size 聚合段落（与 smart_chunk 同口径）
    groups = []         # [(text, start_line, end_line)]
    cur_text, cur_s, cur_e = "", None, None
    for text, s, e in paras:
        if not text:
            continue
        if len(cur_text) + len(text) < chunk_size:
            cur_text = (cur_text + "\n\n" + text) if cur_text else text
            cur_s = s if cur_s is None else cur_s
            cur_e = e
        else:
            if cur_text:
                groups.append((cur_text.strip(), cur_s, cur_e))
            cur_text, cur_s, cur_e = text, s, e
    if cur_text:
        groups.append((cur_text.strip(), cur_s, cur_e))

    out = []
    for i, (text, s, e) in enumerate(groups):
        prefix = ""
        if i > 0:
            prefix = groups[i - 1][0][-overlap:] + "\n[重叠衔接]\n"
        out.append((prefix + text, s, e, len(prefix)))
    return out if out else [(body, 1, len(lines), 0)]


def _select_chunks(query, doc_chunks, max_chars, min_per_doc=1):
    """按相关度把字符预算分配到各篇的块上（零 API 调用）。

    doc_chunks: [(path, [chunk, ...]), ...]，按检索排名顺序。

    背景：原实现固定取 smart_chunk(...)[:3]，即永远只取每篇开头 ~4500 字符。
    实测长度 top15 的笔记上下文覆盖率仅 10%~38%，问笔记后半部分必然答不出；
    且预算按笔记顺序先到先得，靠前的笔记吃满预算。

    现在：① 每篇保底 min_per_doc 块（保证 sources 有意义）
          ② 剩余预算按全局相关度从高到低填（长笔记可多占）
          ③ 输出保持原顺序、块内保持原文顺序
    """
    q_tokens = set(_tokenize(query))

    def _txt(x):
        return x[0] if isinstance(x, tuple) else x

    # 记忆化：同一块在「保底选优」与「剩余填充」两轮里各算一次，
    # 长笔记块多、_tokenize 是纯 CPU 开销，重复分词在这里是主要成本。
    _score_cache = {}

    def _score(ch):
        key = id(ch)
        v = _score_cache.get(key)
        if v is None:
            v = len(q_tokens & set(_tokenize(_txt(ch))))
            _score_cache[key] = v
        return v

    picked = {}
    total = 0
    # ① 每篇保底取最相关的一块
    for di, (_path, chunks) in enumerate(doc_chunks):
        if not chunks:
            continue
        best = max(range(len(chunks)), key=lambda i: (_score(chunks[i]), -i))
        if total + len(_txt(chunks[best])) <= max_chars:
            picked[(di, best)] = True
            total += len(_txt(chunks[best]))
    # ② 剩余块全局按相关度填充
    rest = []
    for di, (_path, chunks) in enumerate(doc_chunks):
        for ci, ch in enumerate(chunks):
            if (di, ci) not in picked:
                rest.append((_score(ch), -di, -ci, di, ci))
    rest.sort(reverse=True)
    for _sc, _a, _b, di, ci in rest:
        ch = doc_chunks[di][1][ci]
        if total + len(_txt(ch)) <= max_chars:
            picked[(di, ci)] = True
            total += len(_txt(ch))
    # ③ 按原顺序输出
    out = []
    for di, (path, chunks) in enumerate(doc_chunks):
        sel = [chunks[ci] for ci in range(len(chunks)) if (di, ci) in picked]
        if sel:
            out.append((path, sel))
    return out


def build_qa_context(q, top=5, max_chars=24000, with_citations=False):
    """搜索笔记并构建问答上下文。

    返回 (context_str, sources_list)；with_citations=True 时返回三元组
    (context_str, sources_list, citations_list)。

    citations 元素：{path, line_start, line_end, quote, rank, score}
    —— 让回答可溯源到行。sources 保持「纯路径字符串列表」不变，
    以免破坏 web/app.js（sources.join(', ')）与 kb-cli 的既有消费方式。
    """
    ctxs, degraded = search_index.search(q, top, with_status=True)
    if degraded:
        # 检索失败时不要说「无相关笔记」——那会让 LLM 以为库里真没有内容。
        log.warning("问答上下文检索不可用: %s", degraded)
        empty = "[检索不可用，以下内容不完整]"
        if with_citations:
            return empty, [], []
        return empty, []
    doc_chunks = []
    fm_offset = {}
    for c in ctxs:
        fp = NOTES_DIR / c["path"]
        try:
            # 一次读取、一次解析：原来 read_text(fp) 之后 parse_fm(fp)
            # 会再读一遍同一个文件，长笔记上是双倍 IO。
            raw = fp.read_text(encoding="utf-8", errors="ignore")
            _, body = parse_fm(raw)
            # parse_fm 会剥掉 YAML frontmatter，body 行号 ≠ 文件行号。
            # citations 面向的是「打开文件跳到第几行」，必须用文件行号，
            # 故把被剥掉的行数补回去（差值为 fm 行数）。
            fm_offset[c["path"]] = len(raw.split("\n")) - len(body.split("\n"))
            doc_chunks.append((c["path"], _chunk_lines(body, 1500, 300)))
        except Exception:
            log.warning("__init__.py 异常", exc_info=True)
            fm_offset[c["path"]] = 0
            doc_chunks.append((c["path"], []))

    score_map = {c["path"]: c.get("score", 0.0) for c in ctxs}
    rank_map = {c["path"]: i + 1 for i, c in enumerate(ctxs)}

    ctx_parts = []
    citations = []
    for path, chunks in _select_chunks(q, doc_chunks, max_chars):
        for item in chunks:
            if isinstance(item, tuple):
                if len(item) >= 4:
                    ch, ls, le, plen = item[0], item[1], item[2], item[3]
                else:
                    ch, ls, le, plen = item[0], item[1], item[2], 0
            else:
                ch, ls, le, plen = item, None, None, 0
            ctx_parts.append(f"[{path}]\n{ch}")
            if with_citations and ls is not None:
                # quote 必须从本块真实起始处取：ch 前面可能拼了上一块的 overlap 尾巴，
                # 若直接取 ch[:200]，引文会落在上一块的行上、与 line_start 错位。
                off = fm_offset.get(path, 0)
                citations.append({
                    "path": path,
                    "line_start": ls + off,
                    "line_end": le + off,
                    "quote": ch[plen:plen + 200],
                    "rank": rank_map.get(path),
                    "score": round(float(score_map.get(path, 0.0)), 4),
                })
    context = "\n\n".join(ctx_parts) if ctx_parts else "无相关笔记"
    sources = [c["path"] for c in ctxs]
    if with_citations:
        return context, sources, citations
    return context, sources


# ── 查重工具函数（在 llm_analyze_doc 和 smart_upload_auto 中复用）──

def _find_existing_doc(query, title, body, top_n=5, score_threshold=None):
    """检查文档是否与已有笔记重复。
    返回 (match_path, matches) — match_path=None 表示新文档。

    2026-10-17：query 与 title 先过 `clean_for_match` 去模板套话。
    背景：调用方传进来的 body 已被插入 `# {title}` 且常带五节模板套话，
    query = title + body[:400] 于是被套话占满，与任何套同模板的笔记都高度相似。
    清洗规则集中在 `services/text_clean.py`，不在本函数里硬编码。
    """
    query = clean_for_match(query)
    title = clean_for_match(title)
    if score_threshold is None:
        score_threshold = get_setting("dedup_threshold", 0.3)
    results, degraded = search_index.search(query, top=top_n, with_status=True)
    # 2026-10-11：把「检索面降级」也回传出去。
    # 背景（实测）：写入 /notes 会触发索引刷新，刷新窗口内 paths 已换新、
    # embeddings 尚未更新 → is_vector_search_ready() 为 False，候选退化成
    # 纯 BM25，融合分从 0.99 掉到 0.056，低于阈值 0.3 → **静默判成「无重复」**。
    # 调用方看到的是「查过了、没重复」，实际是「向量面当时不可用，没查准」。
    if not degraded:
        try:
            _vready = search_index.is_vector_search_ready()
        except Exception:
            _vready = False
        if not _vready:
            degraded = "vector_not_ready:向量检索不可用（索引刷新窗口内），本次判定仅基于 BM25"
            log.warning("查重降级：向量检索不可用，本次判定仅基于 BM25")
        else:
            try:
                _dg = _rerank_degrade_take()
            except Exception:
                _dg = None
            if _dg:
                degraded = "rerank_degraded:%s" % _dg.get("mode")
    if degraded:
        # 检索不可用时**不能**判定为「新文档」，否则同一篇内容会被反复写入。
        # 显式跳过查重并提示，而不是静默当成新文档。
        log.warning("查重检索不可用，跳过查重: %s", degraded)
        return None, [{"degraded": True, "reason": degraded}], "index_degraded:%s" % str(degraded)[:80]
    if not results or results[0]["score"] < score_threshold:
        return None, (results[:3] if results else []), None

    # 2026-10-11：改为「编号制」。
    # 旧 prompt 让模型回答 `update_<完整路径>`，实测模型会直接把提示词里的
    # 占位符照抄回来（输出 `update_路径`），且候选一多就编造不存在的路径。
    # 现在只让它回 `update 2` 这样的编号，路径由本地按编号取，从根上消除
    # 「路径抄错/编造」这一类失败。
    cands = [r for r in results[:3] if (NOTES_DIR / r["path"]).exists()]
    if not cands:
        return None, results[:3], None

    ctx_list = []
    for i, r in enumerate(cands, 1):
        fp = NOTES_DIR / r["path"]
        try:
            ctx_list.append(
                f"候选{i} ({r['score']:.2f}): {r['path']}\n"
                f"{fp.read_text(encoding='utf-8')[:800]}"
            )
        except Exception:
            log.warning("__init__.py 异常", exc_info=True)
    if not ctx_list:
        return None, results[:3], None

    ctx = "\n\n".join(ctx_list)
    # 综合 title + body 前段+尾段，提高去重判断准确率
    dedup_new = f"标题: {title or '无标题'}\n{body[:800]}"
    if len(body) > 1300:
        dedup_new += f"\n...\n{body[-500:]}"
    prompt = (
        f"判断新文档是否与已有笔记重复。已有候选（编号从1开始）:\n{ctx[:3000]}\n\n"
        f"新文档:\n{dedup_new[:1500]}\n\n"
        f"若新文档与某个候选讲的是同一件事（只是更新/补充/重复记录），"
        f"回答 `update 编号`（例如 update 2）；\n"
        f"若新文档是独立主题，回答 `new`。\n"
        f"只回答这两个形式之一，不要解释，不要输出路径。"
    )
    try:
        r = llm_chat_with_retry([
            {"role": "system", "content": "你是知识库查重助手，严格按用户要求直接回答，不要解释。"},
            {"role": "user", "content": prompt},
        ], 50, 0.1)
        ans = r.strip()
        m = re.match(r"update\s*[#第]?\s*(\d+)", ans)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(cands):
                return cands[n - 1]["path"], results[:3], "llm"
            log.warning("查重 LLM 编号越界: %r (候选 %d 个)", ans, len(cands))
            return None, results[:3], "llm_bad_index:%s" % ans[:40]
        # 兼容：模型仍回 update_<路径> 时，只接受候选内的路径
        if ans.startswith("update_"):
            cand = ans[7:].strip()
            _norm = {str(x.get("path", "")).strip().replace("\\", "/") for x in cands}
            if cand.replace("\\", "/") in _norm:
                return cand, results[:3], "llm"
            log.warning("查重 LLM 返回候选外路径，忽略: %r", cand)
            return None, results[:3], "llm_out_of_candidate:%s" % cand[:80]
    except LLMUnavailable as e:
        # 查重是可选增强：LLM 不可用时退回「按相似度判定」，不算失败。
        # 2026-10-11：但降级状态必须回传，否则调用方以为「已判过、没重复」。
        log.info("查重 LLM 不可用，退回相似度判定")
        return None, results[:3], "llm_unavailable:%s" % str(e)[:80]
    except Exception as e:
        log.warning("__init__.py 异常", exc_info=True)
        return None, results[:3], "llm_error:%s" % str(e)[:80]
    return None, results[:3], "llm_judged_new"


# ── 索引刷新辅助 ──

_last_rebuild_time = 0
REBUILD_COOLDOWN = 300  # 5分钟冷却，防止频繁重建
_refresh_lock = threading.Lock()  # 并发保护：同一时间只执行一次 refresh

# ── Shell 索引脚本异步执行辅助（避免阻塞 API 响应）──

_last_shell_index_time = 0
_SHELL_INDEX_COOLDOWN = 60  # 60 秒冷却，频繁写入时不反复跑 shell 脚本
_index_shell_lock = threading.Lock()


def _run_index_shell_async():
    """后台异步执行 索引更新.sh（带冷却 + 锁，避免并发执行和 API 阻塞）"""
    global _last_shell_index_time
    now = _time.time()
    if now - _last_shell_index_time < _SHELL_INDEX_COOLDOWN:
        return
    if not _index_shell_lock.acquire(blocking=False):
        return  # 已有 shell 脚本在执行，跳过
    _last_shell_index_time = now

    def _run():
        try:
            script_path = KB_ROOT / "scripts" / "索引更新.sh"
            if sys.platform == "win32":
                return
            subprocess.run(
                ["bash", str(script_path)],
                capture_output=True, cwd=str(KB_ROOT), timeout=30,
            )
        except subprocess.TimeoutExpired:
            try:
                current_app.logger.warning("索引更新脚本超时(30s)")
            except Exception:
                log.warning("__init__.py 异常", exc_info=True)
        except subprocess.CalledProcessError as e:
            try:
                current_app.logger.error(f"索引更新脚本失败: {e.stderr[:200] if e.stderr else 'no stderr'}")
            except Exception:
                log.warning("__init__.py 异常", exc_info=True)
        except FileNotFoundError:
            log.debug("__init__.py 异常: _run", exc_info=True)
            pass  # bash 不存在，跳过
        finally:
            _index_shell_lock.release()

    threading.Thread(target=_run, daemon=True).start()

def trigger_index_refresh(force=False):
    """后台异步刷新搜索索引（8个路由共享此调用）——带 5 分钟冷却，force=True 可绕过"""
    global _last_rebuild_time
    now = _time.time()
    if not force and now - _last_rebuild_time < REBUILD_COOLDOWN:
        return  # 冷却中，跳过
    # 并发守卫：已有 refresh 线程在跑则跳过，避免多条线程同时操作索引
    if not _refresh_lock.acquire(blocking=False):
        return  # 已有 refresh 正在执行
    _last_rebuild_time = now

    def _run_refresh():
        try:
            search_index.incremental_refresh()
        finally:
            _refresh_lock.release()

    threading.Thread(target=_run_refresh, daemon=True).start()


def parse_tags_response(raw):
    """解析 LLM 标签响应（支持中文逗号/顿号/英文逗号分隔），返回去重非空标签列表"""
    return [t.strip() for t in re.split(r"[,，、；]", raw) if t.strip()]


def _update_indexes():
    """刷新索引页（静态标签/最近更新）+ 搜索索引（增量 TF-IDF/Embedding）

    create_note / save_note / smart_upload_auto 三个写操作共享此模式。
    Shell 脚本异步执行（带冷却+锁），搜索索引增量刷新亦有冷却机制。
    """
    _run_index_shell_async()  # 异步执行，不阻塞 API 响应
    if get_setting("index_auto_rebuild"):
        trigger_index_refresh()  # 不加 force，让冷却机制生效，避免每次写操作都重建搜索索引


# ── 共享文件名去重 ──

def _dedup_path(dest_dir, stem, suffix):
    """返回不冲突的文件路径（自动追加 _1, _2 等后缀）"""
    dest = dest_dir / f"{stem}{suffix}"
    c = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{c}{suffix}"
        c += 1
    return dest


# ── 共享笔记路径解析 ──

def _resolve_note(np_path):
    """安全解析笔记路径，返回 (fp, None, 200) 或 (None, error_response, status_code)"""
    try:
        fp = safe_note(np_path)
    except Exception:
        log.warning("__init__.py 异常: _resolve_note", exc_info=True)
        return None, jsonify({"error": "路径非法"}), 400
    if not fp.exists():
        return None, jsonify({"error": "不存在"}), 404
    if not fp.is_file():
        # 安全加固：原先目录也能通过解析，随后 read_text / unlink /
        # shutil.move 直接 IsADirectoryError → 500；DELETE 一个目录更会把
        # 整棵目录搬进 50-档案。这里统一按「不是文件」挡成 400。
        return None, jsonify({"error": "不是文件"}), 400
    return fp, None, 200


# ── 共享来源身份 / Frontmatter 构建 ──

def _split_tags(value):
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,，、]", str(value))
    return [str(x).strip() for x in raw if str(x).strip()]


def _merge_tags(*tag_lists):
    """合并多组标签，并做同义/大小写规范化。"""
    try:
        from ..services.tags import normalize_tags
    except Exception:
        log.warning("__init__.py 异常: _merge_tags", exc_info=True)
        normalize_tags = None
    out, seen = [], set()
    for tags in tag_lists:
        for tag in _split_tags(tags):
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    if normalize_tags is not None:
        return normalize_tags(out)
    return out


def _quote_yaml(value):
    # 安全加固：把 \r \n \t 转成字面量，避免值里的真实换行
    # 把「一行 key: value」撑成多行，破坏 frontmatter 结构。
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return (s.replace("\r\n", "\\n").replace("\r", "\\n")
             .replace("\n", "\\n").replace("\t", "\\t"))


def _yaml_list(values):
    return ", ".join(_quote_yaml(v) for v in (values or []) if str(v).strip())


def _default_content_layer(note_type):
    return "curated" if note_type in ("project", "knowledge") else "raw"


def _source_identity():
    info = getattr(g, "ti", {}) or {}
    name = info.get("identity_label") or info.get("name") or info.get("device") or "unknown"
    source = {
        "source_client": name,
        "uploaded_by": name,
        "token_name": info.get("name") or name,
        "platform": info.get("platform", ""),
        "project_scope": info.get("project_scope", ""),
        "source_type": info.get("source") or ("master" if info.get("permission") == "master" else "token"),
    }
    return {k: v for k, v in source.items() if v}


def _token_defaults():
    info = getattr(g, "ti", {}) or {}
    return {
        "default_tags": _split_tags(info.get("default_tags", [])),
        "default_dir": str(info.get("default_dir", "")).strip().strip("/"),
        "project_scope": str(info.get("project_scope", "")).strip(),
    }


def _safe_notes_subdir(rel):
    rel = str(rel or "").strip().strip("/")
    if not rel:
        return None
    try:
        target = (NOTES_DIR / rel).resolve()
        target.relative_to(NOTES_DIR)
    except Exception:
        log.warning("__init__.py 异常: _safe_notes_subdir", exc_info=True)
        return None
    return target


def _build_frontmatter(title, note_type, tags, relations=None, status="draft", priority=None, source=None,
                       content_layer=None, canonical_tags=None, aliases=None, compiled_from=None, kind=None):
    """构建笔记 YAML frontmatter（被 create_note / smart_upload_auto 共享）

    kind（2026-10-16 新增）：体裁，取值 log/troubleshoot/conclusion/config/
    knowledge/record/readme。与 type 分离——type 是服务端的 7 个枚举值，
    体裁信息过去无处存放，只能挤进 tags。缺省不写该行，历史行为不变。
    """
    now = datetime.now()
    prefix = TYPE_PREFIX.get(note_type, "kb")
    safe_title = _quote_yaml(title)
    date_str = now.strftime("%Y-%m-%d")
    datetime_str = now.strftime("%Y-%m-%d %H:%M")
    source = source or {}
    editor = source.get("token_name") or source.get("uploaded_by") or source.get("source_client") or "unknown"
    edit_source_type = source.get("source_type") or "manual"
    lines = [
        "---",
        f'id: "{prefix}-{now.strftime("%Y%m%d%H%M%S")}{secrets.token_hex(2)}"',
        f'title: "{safe_title}"',
        f"type: {note_type}",
        "schema_version: 1",
    ]
    if kind:
        lines.append(f"kind: {kind}")
    lines += [
        f'content_layer: "{_quote_yaml(content_layer or _default_content_layer(note_type))}"',
        f"tags: [{_yaml_list(tags)}]",
        f"canonical_tags: [{_yaml_list(canonical_tags)}]",
        f"aliases: [{_yaml_list(aliases)}]",
        f"relations: [{_yaml_list(relations)}]",
        f"compiled_from: [{_yaml_list(compiled_from)}]",
        f"created: {date_str}",
        f"updated: {datetime_str}",
        f"status: {status}",
        f'last_edited_by: "{_quote_yaml(editor)}"',
        f'last_edit_source_type: "{_quote_yaml(edit_source_type)}"',
    ]
    if priority:
        lines.append(f"priority: {priority}")
    for k in ("source_client", "uploaded_by", "token_name", "platform", "project_scope", "source_type"):
        v = source.get(k)
        if v:
            lines.append(f'{k}: "{_quote_yaml(v)}"')
    lines.append("---")
    return "\n".join(lines)


# 导入子模块（触发路由注册）
from . import routes_admin
from . import routes_auth
from . import routes_search
from . import routes_llm
from . import routes_notes
from . import routes_files
from . import routes_general
from . import routes_integrations