#!/usr/bin/env python3
"""lingxi 仓库闸门：本地 pre-commit / pre-push 与 CI 共用同一份规则。

用法:
  python3 scripts/ci/guard.py history [REF]   # 历史闸：只允许一个根提交，且必须是本仓快照根
  python3 scripts/ci/guard.py paths           # 路径闸：受跟踪文件必须在白名单内
  python3 scripts/ci/guard.py content         # 内容闸：不许出现真实 IP 或内网机器简称；文档不许出现 /root/ 路径
  python3 scripts/ci/guard.py release [REF]   # 发布闸：release 提交必须有对应 tag，tag 指向的版本要对
  python3 scripts/ci/guard.py deps            # 依赖闸：代码 import 的第三方包必须在 requirements 里声明
  python3 scripts/ci/guard.py all [REF]

背景（2026-10-19）：两个旧仓都因为「开发库本地 .git 仍是旧历史，只换 remote 就 push」
把令牌、key、索引、日记带上了 GitHub。history 闸就是专门拦这件事的：旧历史无论
force-push 还是 merge 进来，根提交都对不上。content 闸另外禁止任何跟踪文件出现旧仓名。
"""
import ipaddress
import os
import re
import subprocess
import sys

# 旧仓的根提交（前缀匹配）。出现任意一个 = 旧历史混进来了。
FORBIDDEN_ROOTS = ("63ffbd6", "5773cf0")
ROOT_PIN = ".github/ROOT_COMMIT"

ALLOWED_TOP = {
    "scripts", "tests", "docs", "_spec", ".github", ".githooks",
    "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "SECURITY.md", "LICENSE", "AGENTS.md", ".gitignore", ".env.example",
    "requirements.txt", "requirements-optional.txt", "requirements-dev.txt",
}
# docs/ 只放通用文档；带日期的开发记录写进知识库笔记，不进代码仓。
ALLOWED_DOCS = {
    "API.md", "ARCHITECTURE.md", "CLI.md", "CONFIGURATION.md", "DEVELOPMENT.md",
    "INSTALL.md", "INDEXING.md", "MCP.md", "TROUBLESHOOTING.md", "UPGRADE.md",
}
BANNED_NAME = re.compile(
    r"(^|/)(\.api_tokens\.json|\.kb_settings\.json|\.audit\.db.*|\.audit_log\.jsonl.*|"
    r"\.search_index\.pkl|\.embeddings\.npy|\.faiss\.index.*|\.kb_plugins\.json|"
    r"\.kb_mcp_usage\.jsonl|kb-cli\.json|\.env|kb\.db|.*\.pkl|.*\.npy|.*\.sqlite3?)$")
BANNED_DIR = re.compile(r"(^|/)(笔记|00-索引|10-项目|20-知识|30-记录|40-日记|50-档案|70-经验|_assets|_bak[^/]*|_备份|\.doc_cleanup_backup[^/]*|_trash[^/]*)/")
DATED = re.compile(r"20\d\d-\d\d-\d\d")
# 旧仓名（拆开拼接，避免本文件自己命中）。任何跟踪文件出现即失败，无豁免。
OLD_REPO_NAMES = (
    "lingshu" + "-core",
    "lingshu" + "-kb",
    "local" + "-knowledge-base",
    "xiese/" + "knowledge-base",
    "xiese/" + "notes",
    "xiese/" + "123",
)

# 2026-10-20 加固：称呼、业务名、真机主机名。之前闸门一条都拦不住，泄露全靠人肉记。
# 一律拆开拼接，避免本文件自己命中自己的规则。
BANNED_WORDS = (
    "哥" + "哥",                 # 私人称呼
    "小" + "灵儿",               # 私人称呼
    "超" + "链",                 # 真实业务名
    "BTC" + "268",              # 真实业务名
    "加拿大" + "28",             # 真实业务名
    "熊猫" + "联盟",             # 真实业务名
    "新联" + "竞技",             # 真实业务名
    "镖" + "局",                 # 真实业务名
)
# 真机主机名检测用通用模式，不把具体主机名写进代码（否则规则自身就是泄露）。
HOSTNAME_LIKE = re.compile(r"\b[A-Z]\d{10,}\b")

IP_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
IP_ALLOW_EXACT = {"127.0.0.1", "0.0.0.0", "1.2.3.4", "5.6.7.8", "255.255.255.255", "255.255.255.0"}
# 2026-10-29 加固：内网机器简称（两段式地址写法，后面跟端口或「生产/主库」这类上下文词）。
# IP_RE 要求四段，这种简称拦不住，但对外仍是「哪台机器在跑」的线索，统一改示例段。
# 判据取「第二段 >= 2 位数字」并排除后跟量词，避免把「生产 3.9 万条」这类数量写法误判成主机。
SHORT_HOST_PORT = re.compile(r"(?<![\d.])(\d{1,3}\.\d{2,3})\s*[:：]\s*\d{2,5}(?![\d.])")
PROD_SHORT_HOST = re.compile(
    r"(生产|内网|线上|主库)\s+(\d{1,3}\.\d{2,3})(?![\d.])"
    r"(?!\s*(?:万|亿|%|％|元|倍|秒|条|页|次|天|台|个|款|人))")
DOC_IPS = ("192.0.2", "198.51.100", "203.0.113")
# 发布闸：写了 release 提交却没有对应 tag = 这个版本对外装不到；tag 指错提交同样要拦。
VERSION_PATH = "scripts/kb_lib/config.py"
VERSION_RE = re.compile(r'^VERSION\s*=\s*["\'](\d+\.\d+\.\d+)["\']', re.M)
RELEASE_SUBJ = re.compile(r"release:\s*v?(\d+\.\d+\.\d+)", re.I)
# 依赖闸：import 名与发行包名不同名的常见映射（漏声明的表现是「没装就静默回落内置默认」）。
REQ_FILES = ("requirements.txt", "requirements-optional.txt", "requirements-dev.txt")
IMPORT_ALIAS = {
    "yaml": "pyyaml", "sklearn": "scikit_learn", "cv2": "opencv_python",
    "pil": "pillow", "bs4": "beautifulsoup4", "dateutil": "python_dateutil",
    "fitz": "pymupdf", "dotenv": "python_dotenv", "docx": "python_docx",
    "faiss": "faiss_cpu",
}
IMPORT_TOP = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
# 闸门自身与 CI 配置必须含 /root/ 字面量才能表达规则，单独豁免。
SELF_EXEMPT = re.compile(r"(^scripts/ci/guard\.py$|^\.github/)")
TEXT_EXT = (".py", ".js", ".cjs", ".mjs", ".ts", ".sh", ".md", ".yml", ".yaml", ".json",
            ".toml", ".txt", ".html", ".css", ".ini", ".cfg", ".example", ".service", "")


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=False).stdout


def ip_ok(s):
    if s in IP_ALLOW_EXACT or s.endswith(".0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return True  # 不是合法 IP（版本号之类）
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return True
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"):  # 文档保留段
        if ip in ipaddress.ip_network(net):
            return True
    return False


def check_history(ref="HEAD"):
    errs = []
    roots = [r for r in git("rev-list", "--max-parents=0", ref).split() if r]
    if len(roots) != 1:
        errs.append(f"根提交应当只有 1 个，实际 {len(roots)} 个：{[r[:7] for r in roots]}")
    for r in roots:
        if r.startswith(FORBIDDEN_ROOTS):
            errs.append(f"检出旧仓根提交 {r[:7]}：旧历史混进来了")
    pin = git("show", f"{ref}:{ROOT_PIN}").strip()
    if pin and roots and roots[0] != pin:
        errs.append(f"根提交 {roots[0][:7]} 与 {ROOT_PIN} 钉住的 {pin[:7]} 不一致")
    bad = [l for l in git("log", "--format=%h %s", ref).splitlines() if re.match(r"^\w+ auto: update", l)]
    if bad:
        errs.append(f"出现生产自动提交（auto: update）{len(bad)} 条，例如 {bad[0]}")
    return errs


def tracked():
    out = git("-c", "core.quotePath=false", "ls-files", "-z")
    return [p for p in out.split("\0") if p]


def check_paths(files):
    errs = []
    for p in files:
        top = p.split("/", 1)[0]
        if top not in ALLOWED_TOP:
            errs.append(f"不在白名单的顶层路径：{p}")
        elif BANNED_DIR.search(p) or BANNED_NAME.search(p):
            errs.append(f"个人数据/运行期状态文件：{p}")
        elif top == "docs" and (p.count("/") != 1 or p.split("/", 1)[1] not in ALLOWED_DOCS):
            errs.append(f"docs/ 只放通用文档（开发记录写知识库）：{p}")
    return errs


def check_content(files):
    errs = []
    for p in files:
        if not os.path.isfile(p) or not p.endswith(TEXT_EXT) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            text = open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in IP_RE.finditer(line):
                if not ip_ok(m.group(1)):
                    masked = ".".join(m.group(1).split(".")[:2]) + ".x.x"
                    errs.append(f"{p}:{n} 出现真实 IP {masked}（请用 192.0.2.x 文档段或配置项）")
            m = SHORT_HOST_PORT.search(line)
            if m and not any(m.group(1).startswith(d) for d in DOC_IPS):
                errs.append(f"{p}:{n} 出现内网机器简称 {m.group(1).split('.')[0]}.x.x:端口"
                            f"（请写 192.0.2.x 示例段或「生产实例」）")
            m = PROD_SHORT_HOST.search(line)
            if m and not any(m.group(2).startswith(d) for d in DOC_IPS):
                errs.append(f"{p}:{n} 出现内网机器简称 {m.group(1)} {m.group(2).split('.')[0]}.x.x"
                            f"（请写「生产实例」）")
            if "/root/" in line and not SELF_EXEMPT.search(p):
                errs.append(f"{p}:{n} 出现 /root/ 绝对路径（请写 $KB_ROOT）")
            for name in OLD_REPO_NAMES:
                if name in line:
                    errs.append(f"{p}:{n} 出现旧仓名 {name}（请改用新仓 xiese/lingxi）")
            for w in BANNED_WORDS:
                if w in line:
                    errs.append(f"{p}:{n} 出现禁词 {w}（称呼/业务名/主机名不许进仓库）")
            m = HOSTNAME_LIKE.search(line)
            if m:
                errs.append(f"{p}:{n} 出现疑似真机主机名（大写字母+长数字）")
    return errs


def check_release_tags(ref="HEAD"):
    """发布闸：历史里每个 `release: X.Y.Z` 提交都要有同名 tag，且 tag 指向的提交版本要对得上。

    实证背景（2026-10-29）：CHANGELOG 记到 1.0.16，实际 tag 只到 v1.0.9 ——
    1.0.10 之后的版本在 GitHub 上没有锚点，按 tag 取代码的人拿不到对应版本。
    发布流程（.github/workflows/release.yml）只在推 main 时给「当前 VERSION」打 tag，
    漏掉的歷史版本不会自己补回来，所以这条单独设闸。
    """
    errs = []
    seen = []
    for line in git("log", "--format=%H%x09%s", ref).splitlines():
        h, _, subj = line.partition("\t")
        m = RELEASE_SUBJ.search(subj)
        if m:
            seen.append((m.group(1), h, subj.strip()))
    for ver, h, subj in seen:
        tag = f"v{ver}"
        if not git("tag", "-l", tag).strip():
            errs.append(f"{tag} 缺失：发布提交 {h[:7]}「{subj[:40]}」已合入，但仓库没有这个 tag")
            continue
        mv = VERSION_RE.search(git("show", f"{tag}:{VERSION_PATH}"))
        if not mv:
            errs.append(f"{tag} 指向的提交读不到 {VERSION_PATH} 的 VERSION，无法核对")
        elif mv.group(1) != ver:
            errs.append(f"{tag} 指向的提交里 VERSION 是 {mv.group(1)}，tag 打错了")
    return errs


def _pkg_norm(name):
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def declared_requires():
    """requirements 三个文件里声明过的包名（按 PEP 503 归一化）。"""
    out = set()
    for f in REQ_FILES:
        try:
            text = open(f, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~;\[\s]", line, 1)[0].strip()
            if name:
                out.add(_pkg_norm(name))
    return out


def check_deps(files=None):
    """依赖闸：代码 import 的第三方包必须在 requirements 里声明过。

    实证背景（2026-10-27）：write_policy 用 yaml 读 rules.yaml，但 PyYAML 当时没声明，
    表现是没装就静默回落内置默认规则 —— 环境不同结果不同，最难查的一类坑。
    """
    files = tracked() if files is None else files
    py = [p for p in files if p.endswith(".py") and os.path.isfile(p)]
    declared = declared_requires()
    local = {"scripts", "tests"}
    for p in py:
        parts = p.split("/")
        local.update(seg.replace("-", "_") for seg in parts[:-1])
        local.add(parts[-1][:-3].replace("-", "_"))
    std = set(getattr(sys, "stdlib_module_names", set()))
    missing = {}
    for p in py:
        try:
            text = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for m in IMPORT_TOP.finditer(text):
            top = m.group(1).split(".")[0]
            if not top or top.startswith("_") or top in std or top in local:
                continue
            # 别名键是小写（"pil"→"pillow"），而 import 名可能带大写（PIL）：
            # 不做大小写归一化会让 `import PIL` 漏匹配、误报「依赖没写进 requirements」
            # （2026-10-31 审查轮2 指出）
            name = _pkg_norm(IMPORT_ALIAS.get(top) or IMPORT_ALIAS.get(top.lower()) or top)
            if name not in declared:
                missing.setdefault(name, p)
    return [f"{name} 被代码 import（{p}）却没写进 {', '.join(REQ_FILES)}"
            for name, p in sorted(missing.items())]


def check_authors(ref="HEAD"):
    """作者闸：真机主机名会随 commit 作者字段一起推上 GitHub，必须单独拦。"""
    errs = []
    out = git("log", "--format=%an|%ae|%cn|%ce", ref)
    for line in out.splitlines():
        if not line.strip():
            continue
        for field in line.split("|"):
            for w in BANNED_WORDS:
                if w in field:
                    errs.append(f"提交作者字段含禁词 {w}：{field}")
            if HOSTNAME_LIKE.search(field):
                errs.append(f"提交作者字段含疑似真机主机名：{field}")
            m = re.search(r"@([\w.-]+)", field)
            if m and m.group(1).endswith(".local"):
                errs.append(f"提交作者邮箱是主机名后缀 .local：{field}")
    return errs


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    ref = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
    errs = []
    if mode in ("history", "all"):
        errs += check_history(ref)
    files = tracked() if mode in ("paths", "content", "all", "deps") else []
    if mode in ("paths", "all"):
        errs += check_paths(files)
    if mode in ("content", "all"):
        errs += check_content(files)
    if mode in ("release", "all"):
        errs += check_release_tags(ref)
    if mode in ("deps", "all"):
        errs += check_deps(files)
    if mode in ("authors", "history", "all"):
        errs += check_authors(ref)
    if errs:
        print("❌ 仓库闸门未通过：")
        for e in errs[:80]:
            print("   - " + e)
        if len(errs) > 80:
            print(f"   ……共 {len(errs)} 条")
        return 1
    print(f"✅ 仓库闸门通过（{mode}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
