#!/usr/bin/env python3
"""lingxi 仓库闸门：本地 pre-commit / pre-push 与 CI 共用同一份规则。

用法:
  python3 scripts/ci/guard.py history [REF]   # 历史闸：只允许一个根提交，且必须是本仓快照根
  python3 scripts/ci/guard.py paths           # 路径闸：受跟踪文件必须在白名单内
  python3 scripts/ci/guard.py content         # 内容闸：不许出现真实 IP；文档里不许出现 /root/ 路径
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
    "INSTALL.md", "MCP.md", "TROUBLESHOOTING.md", "UPGRADE.md",
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

IP_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
IP_ALLOW_EXACT = {"127.0.0.1", "0.0.0.0", "1.2.3.4", "5.6.7.8", "255.255.255.255", "255.255.255.0"}
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
            if "/root/" in line and not SELF_EXEMPT.search(p):
                errs.append(f"{p}:{n} 出现 /root/ 绝对路径（请写 $KB_ROOT）")
            for name in OLD_REPO_NAMES:
                if name in line:
                    errs.append(f"{p}:{n} 出现旧仓名 {name}（请改用新仓 xiese/lingxi）")
    return errs


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    ref = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
    errs = []
    if mode in ("history", "all"):
        errs += check_history(ref)
    files = tracked() if mode in ("paths", "content", "all") else []
    if mode in ("paths", "all"):
        errs += check_paths(files)
    if mode in ("content", "all"):
        errs += check_content(files)
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
