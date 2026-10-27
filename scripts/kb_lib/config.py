"""知识库 API — 配置与常量"""

import os, json, threading
from pathlib import Path
import logging

log = logging.getLogger("kb.config")

# 分词
try:
    import jieba
    jieba.setLogLevel(20)
    HAS_JIEBA = True
except ImportError:
    log.warning("config.py 异常: 模块级", exc_info=True)
    HAS_JIEBA = False

# 模块级分词函数（始终定义符号，防止导入失败）
if HAS_JIEBA:
    def _jieba_tokenizer(text):
        return [w for w in jieba.cut(text) if w.strip()]
else:
    _jieba_tokenizer = None

# 路径
# 库根目录：优先读 KB_ROOT 环境变量；未设置时自动取本仓库根目录
# （本文件位于 <仓库根>/scripts/kb_lib/config.py，故上溯三级）。
# 部署时建议显式指定，例如 export KB_ROOT=/srv/knowledge-base
KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parents[2]).resolve()
_temp_file_lock = threading.Lock()
NOTES_DIR = KB_ROOT / "笔记"
VERSION = "1.0.16"
# ── 环境变量新旧名兼容（2026-10-07 KB_HOST 拆名）──
#
# KB_HOST 历史上同名双义：本文件用它表示「监听绑定地址」，kb_mcp_server.py
# 用它表示「上游 REST API 地址」——运维设错就出事（把 URL 设成绑定地址会直接
# 起不来，把 0.0.0.0 设成上游地址会打不通）。现已拆名：绑定地址用 KB_BIND，
# 上游地址用 KB_API_BASE。旧名 KB_HOST 只保留一个版本的兼容：读旧名、映射到
# 新名、记一次 deprecation warning，不炸。
def _env_with_legacy(new_name, old_name, default):
    """优先读新名环境变量；仅设置旧名时取旧值并记一次 deprecation warning。"""
    val = os.environ.get(new_name)
    if val:
        return val
    legacy = os.environ.get(old_name)
    if legacy:
        log.warning(
            "配置弃用警告: 环境变量 %s 已更名为 %s（旧名 %s 仅保留一个版本的"
            "兼容，之后将只认新名）。当前按旧名取值 %r，请尽快在 systemd unit "
            "与启动脚本中改用 %s。",
            old_name, new_name, old_name, legacy, new_name,
        )
        return legacy
    return default


# 默认只监听本机，避免误把服务暴露到公网。
# 需要对外提供服务时显式设置 KB_BIND=0.0.0.0，并自行配置鉴权与反向代理。
# 旧名 KB_HOST（与上游 API 地址的旧名相撞，见上方 _env_with_legacy 注释）兼容一版。
HOST = _env_with_legacy("KB_BIND", "KB_HOST", "127.0.0.1")
KB_BIND = HOST
PORT = int(os.environ.get("KB_PORT", "8899"))
TOKEN_FILE = KB_ROOT / ".api_tokens.json"
# 主索引：JSON（结构化）+ `<INDEX_FILE>.arrays.npz`（数值数组），读取一律 allow_pickle=False。
# 旧格式是整包 pickle（`.search_index.pkl`），加载即执行任意代码；加载路径保留一次性
# 迁移（读旧文件、立刻写新格式、旧文件改名 .legacy），之后不再碰 pickle。
INDEX_FILE = KB_ROOT / ".search_index.json"
EMBEDDINGS_FILE = KB_ROOT / ".embeddings.npy"
FAISS_FILE = KB_ROOT / ".faiss.index"
# A0: 索引三产物一致性清单（paths/contents/embeddings/faiss 计数 + version）
INDEX_MANIFEST_FILE = KB_ROOT / ".index_manifest.json"
SETTINGS_FILE = KB_ROOT / ".kb_settings.json"
LOG_FILE = KB_ROOT / ".audit_log.jsonl"
MCP_USAGE_LOG = KB_ROOT / ".kb_mcp_usage.jsonl"
# Web 静态资源目录：跟随代码位置，不随 KB_ROOT 变化
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# 技能目录：存放可被 /api/skills 读取的 SKILL.md 集合。
# 未设置 KB_SKILLS_DIR 时默认取用户主目录下的隐藏目录，避免依赖任何外部工具。
SKILLS_DIR = Path(
    os.environ.get("KB_SKILLS_DIR") or (Path.home() / ".kb-skills")
).resolve()

# 限流
RATE_LIMIT = {"read": (500, 60), "write": (60, 60), "upload": (20, 60)}

# 受信反向代理地址（逗号分隔）。只有请求来自这些地址时，才采信
# X-Forwarded-For / X-Real-IP；默认为空 = 不信任任何转发头。
TRUSTED_PROXIES = {
    p.strip() for p in os.environ.get("KB_TRUSTED_PROXIES", "").split(",") if p.strip()
}

# 允许扩展名
ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml",
    ".py", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", ".pdf", ".html", ".css",
    ".js", ".ts",
}
TYPE_MAP = {
    "knowledge": "20-知识",
    "project": "10-项目",
    "writing": "30-写作",
    "diary": "40-日记",
    "record": "30-记录",
    "archive": "50-档案",
    "dream": "60-梦境",
}

# 反向映射：目录名 → 类型名（由 TYPE_MAP 自动推导）
DIR_TO_TYPE = {v: k for k, v in TYPE_MAP.items()}

# 用户私有目录：只由用户本人写入，AI 不得自行写入。
# 场景：多个并行 AI 会话批量把项目教训写进该目录，会挤占注入预算、
# 把用户自己的内容挤掉，因此加此闸门。
# 写入这些目录必须在请求里显式带 owner_ack=user；缺省一律 403 拒绝。
USER_OWNED_DIRS = ("70-经验",)
USER_OWNED_ACK = "owner_ack"

# 扫描/统计目录
SCAN_DIRS = ["10-项目", "20-知识", "30-写作", "30-记录", "40-日记", "50-档案", "60-梦境", "99-待整理"]

# ID 前缀映射
TYPE_PREFIX = {"knowledge": "kb", "project": "proj", "writing": "write", "diary": "day", "record": "rec", "archive": "arc"}

# ── 路径排除 ──
# 60-梦境 不在排除名单里：它是 TYPE_MAP 里的正式类型（dream），
# 排除它会导致 27 篇梦境笔记既不进目录树、也搜不到。
PROTECTED = {"00-索引", "scripts", "_spec", "_模板", ".git", "_assets"}


def is_excluded_path(rel, *, exclude_archived=False, exclude_assets=True):
    """检查笔记相对路径是否应被排除在索引/统计/操作之外

    Args:
        rel: 笔记相对路径（相对 NOTES_DIR）
        exclude_archived: 是否排除 50-档案（仪表盘统计时用）
        exclude_assets: 是否排除 _assets（笔记列表/编辑时用）
    """
    excluded = set(PROTECTED)
    if not exclude_assets:
        excluded.discard("_assets")
    if exclude_archived:
        excluded.add("50-档案")
    return any(rel.startswith(p + "/") or rel == p for p in excluded)


# ── Settings 文件级缓存（mtime 检测，文件不变不读盘）──

_settings_cache = None
_settings_cache_mtime = 0
_settings_key_cache = {}
_settings_lock = threading.Lock()
# 缓存未命中的哨兵标记，用于区分"key 不存在"和"key 的值是 None"
_sentinel = object()


def load_settings():
    global _settings_cache, _settings_cache_mtime
    try:
        mtime = SETTINGS_FILE.stat().st_mtime if SETTINGS_FILE.exists() else 0
    except OSError:
        log.warning("config.py 异常: load_settings", exc_info=True)
        mtime = 0
    if _settings_cache is not None and mtime == _settings_cache_mtime:
        return _settings_cache
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(s, dict):
                raise ValueError("settings 顶层必须是 JSON 对象")
        except (ValueError, OSError) as e:
            log.warning(f"settings 文件解析失败，回落 DEFAULT_SETTINGS: {e}", exc_info=True)
            s = dict(DEFAULT_SETTINGS)
        for k, v in DEFAULT_SETTINGS.items():
            s.setdefault(k, v)
        # 未知键告警（拼错不报=配置静默失效的经典坑；只 warning 不删除，保持宽进）
        # 要先减去「文件级键」：它们由代码直接读取、设计上不进设置页，不是拼错的键。
        unknown = set(s) - set(DEFAULT_SETTINGS) - FILE_ONLY_SETTINGS
        if unknown:
            log.warning(f"settings 含未知键（拼错将静默无效，请核对）: {sorted(unknown)}")
        for k in ("index_auto_rebuild", "rerank_enabled"):
            if isinstance(s.get(k), str):
                s[k] = s[k].lower() in ("true", "1", "yes")
        for k in ("max_log_days", "token_ttl", "decay_tau_days"):
            if isinstance(s.get(k), str):
                try:
                    s[k] = int(s[k])
                except (ValueError, TypeError):
                    log.debug("config.py 异常: load_settings", exc_info=True)
                    s[k] = DEFAULT_SETTINGS.get(k, 30)
        for k in ("dedup_threshold", "hybrid_alpha"):
            if isinstance(s.get(k), str):
                try:
                    s[k] = float(s[k])
                except (ValueError, TypeError):
                    log.debug("config.py 异常: load_settings", exc_info=True)
                    s[k] = DEFAULT_SETTINGS.get(k, 0.3)
    else:
        s = dict(DEFAULT_SETTINGS)
    # 敏感密钥环境变量兜底（不硬编码，不在 DEFAULT_SETTINGS 写真实值）
    for key_name in ("embedding_key", "llm_key"):
        if not s.get(key_name):
            env_val = os.environ.get(key_name.upper(), "")
            if env_val:
                s[key_name] = env_val
    _settings_cache = s
    _settings_cache_mtime = mtime
    # 清空单 key 缓存（旧值可能受 settings 影响）
    _settings_key_cache.clear()
    return s


def get_setting(key, default=None):
    """获取设置项，支持默认值（default 优先于 DEFAULT_SETTINGS）。线程安全。"""
    # 1) 锁外读缓存（Python dict 读在 CPython 中是原子的）
    cached = _settings_key_cache.get(key, _sentinel)
    if cached is not _sentinel:
        return cached
    # 2) 锁内 double-check：未命中时加载配置并填充
    with _settings_lock:
        if key in _settings_key_cache:
            return _settings_key_cache[key]
        val = load_settings().get(key, default if default is not None else DEFAULT_SETTINGS.get(key))
        _settings_key_cache[key] = val
    return val


DEFAULT_SETTINGS = {
    # ── 外部服务连接 ──
    # 默认全部留空：本系统不绑定任何具体的模型服务商。
    # 需要向量检索与问答时，在 Web 界面「设置」或 .kb_settings.json 中填写
    # 任意 OpenAI 兼容服务的地址、密钥与模型名（见 README 与 docs/CONFIGURATION.md）。
    # 严禁把真实密钥写进本文件——本文件会进入版本库。
    "llm_base": "",
    "llm_key": "",
    "llm_model": "",
    "embedding_base": "",
    "embedding_key": "",
    "embedding_model": "",
    # 检索模式：bm25 = 仅关键词检索（默认，无需外部服务）；
    #           llm  = BM25 + 向量混合检索（需先配置上方嵌入服务）。
    "embedding_type": "bm25",
    "rerank_enabled": False,   # 精排需外部服务，默认关闭；配置后可开启
    "rerank_model": "",
    # ── 运行参数 ──
    "token_ttl": 43200,
    "index_auto_rebuild": True,
    "max_log_days": 30,
    "theme": "light",
    "hybrid_alpha": 0.6,
    "qq_push_enabled": False,
    "qq_push_webhook_url": "",
    "qq_push_webhook_secret": "",
    "qq_push_target": "",
    "qq_push_payload_mode": "generic",
    # ── 消息推送：按事件勾选（第 16 项，2026-10-18）──
    # 全部默认关闭，只推维护者勾上的事件。事件清单由 push_qq.PUSH_EVENTS 定义，
    # 这里只存「哪些被勾上」的键列表（逗号分隔），不存事件定义本身。
    "qq_push_events": "",
    "auth_watch_interval_seconds": 300,
    "dedup_threshold": 0.3,
    # ── 检索时间衰减（T4，2026-10-16）──
    # 让较新的笔记在检索结果里适度靠前。0 = 关闭。
    # 实测（40 query / 512 样本）TAU=90 时新笔记(<30天) Top3 命中 22%→29%。
    # 数值越小衰减越猛（新笔记更靠前、老笔记被压得越狠）；建议 30~365。
    "decay_tau_days": 90,
    # ── 正文路径徽标（第 5 项，2026-10-18）──
    # 一行一个「路径前缀|服务器名」，例如：
    #     /srv/notes/示例/|远程服务器
    # 只影响徽标上显示的名字，不影响可点性（可点只看本机 stat 结果）。
    # 留空则统一显示「其他服务器」。
    "other_server_prefixes": "",
}

# 「文件级键」白名单：由代码直接读取、但**设计上不进设置页**的调优项。
#
# 为什么单独列一张表、而不塞进 DEFAULT_SETTINGS：
#   DEFAULT_SETTINGS 是「设置页契约」——每加一个键，前端 _SET_GROUPS 必须同步渲染，
#   否则 tests/test_settings_groups.py 的「前端后端键集合一致」会红。
#   下面这三个是 embed.py 的限速/代理调优项（见 docs/CONFIGURATION.md「可选调优项」），
#   只在 .kb_settings.json 里手工填，设置页里没有对应输入框，
#   所以它们既不该进 DEFAULT_SETTINGS、也不该被当成「拼错的键」告警。
#
# 判据：加进来之前先确认「有代码真的读它」。没人读的残留键（例如换服务商时留下的
# 备份块）不属于这里，应当从 settings 文件里清掉，而不是登记成合法键。
FILE_ONLY_SETTINGS = {
    "embedding_proxy",
    "embedding_request_gap",
    "embedding_tokens_per_min",
}

def save_settings(s):
    """原子写入 settings 文件（tempfile + os.replace），防止写入中断导致文件损坏"""
    data = json.dumps(s, indent=2, ensure_ascii=False)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(SETTINGS_FILE))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                log.warning("config.py 异常: save_settings", exc_info=True)
                pass
    # 使 settings 缓存失效
    global _settings_cache, _settings_cache_mtime
    _settings_cache = None
    _settings_cache_mtime = 0
    _settings_key_cache.clear()
