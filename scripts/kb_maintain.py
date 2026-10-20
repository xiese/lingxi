#!/usr/bin/env python3
"""
知识库维护工具 (kb_maintain.py) — Phase 1 安全与稳定性检查
用法: python kb_maintain.py check
"""
import sys
import time, os, re, subprocess, ast, json
from pathlib import Path
import logging

log = logging.getLogger("kb_maintain")

# 项目根目录：优先环境变量，回退到脚本上两级
KB_ROOT = Path(os.environ.get("KB_ROOT", Path(__file__).resolve().parent.parent))
NOTES_DIR = KB_ROOT / "笔记"
SCRIPTS_DIR = KB_ROOT / "scripts"
WEB_DIR = SCRIPTS_DIR / "web"

# ── 私有文件黑名单 ──
PRIVATE_FILES = [
    ".api_tokens.json",
    ".audit_log.jsonl",
    ".kb_settings.json",
    ".kb_auth_watch_state.json",
    ".kb_mcp_usage.jsonl",
    ".search_index.pkl",
    ".embeddings.npy",
    ".faiss.index",
    ".audit.db",
    ".audit.db-wal",
    ".audit.db-shm",
    ".editorconfig",
    ".kb_version",
]

# ── 检查结果 ──
class CheckResult:
    def __init__(self):
        self.passed = 0
        self.warnings = 0
        self.errors = 0
        self.details = []

    def ok(self, msg):
        self.passed += 1
        self.details.append(("✅", msg))

    def warn(self, msg):
        self.warnings += 1
        self.details.append(("⚠️ ", msg))

    def fail(self, msg):
        self.errors += 1
        self.details.append(("❌", msg))

    def summary(self):
        total = self.passed + self.warnings + self.errors
        print(f"\n{'═' * 60}")
        print(f"检查结果: {total} 项 — ✅ {self.passed} 通过 / ⚠️  {self.warnings} 警告 / ❌ {self.errors} 错误")
        if self.errors:
            print("❌ 存在错误，请修复后再提交！")
            return 1
        elif self.warnings:
            print("⚠️  存在警告，建议修复。")
            return 0
        else:
            print("✅ 全部通过！")
            return 0


def run_git(*args, cwd=None):
    """安全执行 git 命令"""
    try:
        r = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True,
            cwd=str(cwd or KB_ROOT), timeout=15,
        )
        return r.stdout.strip(), r.returncode
    except Exception as e:
        log.warning("git 命令执行失败: %s", args, exc_info=True)
        return f"[git error: {e}]", 1


# ── 检查项 ──

def check_python_syntax(result):
    """1. Python 语法检查 (AST 编译)"""
    print("\n🔍 检查 Python 语法...")
    py_files = []
    for d in [SCRIPTS_DIR, SCRIPTS_DIR / "kb_lib", SCRIPTS_DIR / "kb_lib" / "routes"]:
        if d.exists():
            py_files.extend(d.glob("*.py"))
    for fp in py_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                source = f.read()
            ast.parse(source, filename=str(fp))
            result.ok(f"Python 语法: {fp.relative_to(KB_ROOT)}")
        except SyntaxError as e:
            result.fail(f"Python 语法错误: {fp.relative_to(KB_ROOT)} — {e}")


def check_gitignore_privacy(result):
    """2. .gitignore 是否包含私有文件黑名单"""
    print("\n🔒 检查 .gitignore 私有文件覆盖...")
    gitignore = KB_ROOT / ".gitignore"
    if not gitignore.exists():
        result.fail(".gitignore 文件不存在")
        return
    content = gitignore.read_text(encoding="utf-8")
    for f in PRIVATE_FILES:
        if f in content:
            result.ok(f".gitignore 包含: {f}")
        else:
            result.fail(f".gitignore 缺少: {f}")
    # 检查 笔记/ 是否被排除
    if "笔记/" in content or "notes/" in content:
        result.ok(".gitignore 包含: 笔记/")
    else:
        result.fail(".gitignore 缺少: 笔记/ (笔记不上传公开仓库)")


def check_git_tracked_privacy(result):
    """3. 私有文件是否被 Git 跟踪"""
    print("\n📋 检查 Git 跟踪的私有文件...")
    out, code = run_git("ls-files")
    if code != 0:
        result.warn(f"git ls-files 失败: {out}")
        return
    tracked = set(out.split("\n"))
    # 检查私有文件
    found_private = []
    for f in PRIVATE_FILES:
        for t in tracked:
            if t.endswith(f) or t == f:
                found_private.append(t)
    # 检查 笔记/ 目录
    notes_tracked = [t for t in tracked if t.startswith("笔记/")]
    if found_private:
        for f in found_private:
            result.fail(f"私有文件被 Git 跟踪: {f}")
    else:
        result.ok("无私有文件被 Git 跟踪")
    if notes_tracked:
        result.fail(f"笔记/ 目录有 {len(notes_tracked)} 个文件被 Git 跟踪")
        for t in notes_tracked[:5]:
            result.fail(f"  → {t}")
    else:
        result.ok("笔记/ 目录未被 Git 跟踪")


def check_token_in_url(result):
    """4. 前端代码中是否有 token 在 URL query 参数中"""
    print("\n🔐 检查 Token URL 暴露...")
    token_pattern = re.compile(r'[?&]token=[a-zA-Z0-9]{10,}', re.IGNORECASE)
    js_files = list(WEB_DIR.glob("*.js")) + list(WEB_DIR.glob("*.html")) if WEB_DIR.exists() else []
    if not js_files:
        result.warn("Web 目录无 JS/HTML 文件")
        return
    found = False
    for fp in js_files:
        try:
            content = fp.read_text(encoding="utf-8")
            matches = token_pattern.findall(content)
            if matches:
                found = True
                result.fail(f"Token URL 暴露: {fp.name} — {len(matches)} 处")
        except Exception:
            log.debug("读取前端文件失败，跳过: %s", fp, exc_info=True)
    if not found:
        result.ok("前端代码无 Token URL 暴露")


def check_hardcoded_token(result):
    """5. Python 脚本中是否有硬编码的长 Token"""
    print("\n🔑 检查硬编码 Token...")
    # 匹配 40 字符以上的十六进制/字母数字串（可能是 Token）
    token_pattern = re.compile(r'["\']([a-zA-Z0-9]{40,})["\']')
    py_files = []
    for d in [SCRIPTS_DIR, SCRIPTS_DIR / "kb_lib", SCRIPTS_DIR / "kb_lib" / "routes", SCRIPTS_DIR / "kb-cli"]:
        if d.exists():
            py_files.extend(d.glob("*.py"))
    found = False
    for fp in py_files:
        try:
            content = fp.read_text(encoding="utf-8")
            # 跳过 kb-cli.py 中的 DEFAULT_API 常量
            for m in token_pattern.finditer(content):
                token = m.group(1)
                # 排除明显的非 token（如 UUID、hash 等）
                if token.startswith("0000") or len(token) > 100:
                    continue
                found = True
                result.fail(f"疑似硬编码 Token: {fp.relative_to(KB_ROOT)} — {token[:8]}...")
        except Exception:
            log.debug("读取脚本文件失败，跳过: %s", fp, exc_info=True)
    if not found:
        result.ok("Python 脚本无硬编码 Token")


def check_index_stability(result):
    """6. 索引脚本连续运行稳定性：实际连续运行两次并比较输出文件。"""
    print("\n🔄 检查索引脚本稳定性...")
    index_script = SCRIPTS_DIR / "索引更新.sh"
    if not index_script.exists():
        result.warn("索引更新.sh 不存在（可能运行在 Windows 环境）")
        return
    watched = [
        KB_ROOT / "00-索引" / "README.md",
        KB_ROOT / "00-索引" / "最近更新.md",
        KB_ROOT / "00-索引" / "标签索引.md",
    ]

    def digest(fp):
        import hashlib
        return hashlib.sha256(fp.read_bytes()).hexdigest() if fp.exists() else None

    try:
        first = subprocess.run(["bash", str(index_script)], cwd=str(KB_ROOT), capture_output=True, text=True, timeout=240)
        if first.returncode != 0:
            detail = (first.stderr or first.stdout or f"returncode={first.returncode}").strip()[:500]
            result.fail(f"索引脚本首次运行失败: {detail}")
            return
        mid = {str(p.relative_to(KB_ROOT)): digest(p) for p in watched}
        second = subprocess.run(["bash", str(index_script)], cwd=str(KB_ROOT), capture_output=True, text=True, timeout=240)
        if second.returncode != 0:
            detail = (second.stderr or second.stdout or f"returncode={second.returncode}").strip()[:500]
            result.fail(f"索引脚本第二次运行失败: {detail}")
            return
        after = {str(p.relative_to(KB_ROOT)): digest(p) for p in watched}
        changed = [name for name in sorted(mid) if mid[name] != after[name]]
        if changed:
            result.fail("索引脚本第二次运行仍产生漂移: " + ", ".join(changed))
        else:
            result.ok("索引脚本连续两次运行无漂移")
    except Exception as e:
        result.warn(f"无法检查索引稳定性: {e}")


def check_git_status(result):
    """7. Git 工作区状态"""
    print("\n📊 检查 Git 工作区状态...")
    out, code = run_git("status", "--short", "--branch")
    if code != 0:
        result.warn(f"git status 失败: {out}")
        return
    lines = out.split("\n") if out else []
    # 第一行是分支信息
    branch_line = lines[0] if lines else ""
    print(f"   分支: {branch_line}")
    # 检查是否有 staged 的敏感文件
    staged_sensitive = []
    for line in lines[1:]:
        if not line.strip():
            continue
        status = line[:2]
        filepath = line[3:].strip()
        for sf in PRIVATE_FILES:
            if filepath.endswith(sf) or filepath == sf:
                staged_sensitive.append((status, filepath))
        if filepath.startswith("笔记/"):
            staged_sensitive.append((status, filepath))
    if staged_sensitive:
        for status, fp in staged_sensitive:
            result.fail(f"Git 暂存敏感文件: [{status}] {fp}")
    else:
        result.ok("Git 工作区无私有文件变更")
    # 未跟踪文件统计
    untracked = [l for l in lines[1:] if l.startswith("??")]
    if untracked:
        result.ok(f"未跟踪文件: {len(untracked)} 个（正常）")


def check_server_health(result):
    """8. 本地 /health 和 /api/version 检查（可选，服务端可能未运行）"""
    print("\n🏥 检查服务端健康状态...")
    import urllib.request, urllib.error
    api_url = os.environ.get("KB_API", "http://127.0.0.1:8899")
    try:
        req = urllib.request.Request(f"{api_url}/health", method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        result.ok(f"服务端健康: {data.get('status', '?')} (v{data.get('version', '?')}, 索引 {data.get('indexed', 0)} 篇)")
    except urllib.error.HTTPError as e:
        result.warn(f"服务端返回 HTTP {e.code}")
    except Exception as e:
        result.warn(f"服务端不可达: {e}")


def check_mcp_usage(result):
    """10. MCP usage 日志停更检测（区分「无客户端调用」与「logger 故障」两种停更）"""
    print("\n📡 检查 MCP usage 日志...")
    usage = KB_ROOT / ".kb_mcp_usage.jsonl"
    if not usage.exists():
        result.warn("MCP usage 日志不存在——从未有过调用记录，或路径被 KB_MCP_USAGE_LOG 改指向别处")
        return
    age_days = (time.time() - usage.stat().st_mtime) / 86400
    try:
        n_rows = sum(1 for ln in usage.open(encoding="utf-8") if ln.strip())
    except Exception:
        n_rows = -1
    if age_days <= 30:
        result.ok(f"MCP usage 日志 {age_days:.1f} 天前有写入（共 {n_rows} 行）")
    else:
        result.warn(
            f"MCP usage 日志已 {age_days:.0f} 天无新写入（{n_rows} 行）——两种可能，需区分："
            f"① 无客户端调用（闲置，正常）判别：journalctl -u kb-mcp-sse --since '30 days ago' | grep -c POST；"
            f"② logger 故障（异常）判别：systemctl show kb-mcp-sse -p ActiveEnterTimestamp 确认服务重启时间后追问调用记录。"
            f"交叉验证可跑 scripts/kb_mcp_stats.py"
        )


def check_web_assets(result):
    """9. 检查 Web 前端关键文件是否存在"""
    print("\n🌐 检查 Web 前端文件...")
    required = ["index.html", "sw.js", "manifest.json"]
    for f in required:
        fp = WEB_DIR / f
        if fp.exists():
            result.ok(f"前端文件存在: {f}")
        else:
            result.warn(f"前端文件缺失: {f}")


def check_shell_scripts(result):
    """10. Shell 脚本语法检查 (bash -n)"""
    print("\n📜 检查 Shell 脚本语法...")
    sh_files = list(SCRIPTS_DIR.glob("*.sh"))
    if not sh_files:
        result.ok("无 Shell 脚本需要检查")
        return
    # 检查 bash 是否可用
    try:
        r = subprocess.run(["bash", "--version"], capture_output=True, timeout=5)
        if r.returncode != 0:
            result.warn("bash 不可用，跳过 Shell 脚本检查")
            return
    except Exception:
        result.warn("bash 不可用（Windows 环境），跳过 Shell 脚本检查")
        return
    for fp in sh_files:
        try:
            r = subprocess.run(["bash", "-n", str(fp)], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                result.ok(f"Shell 语法: {fp.name}")
            else:
                result.fail(f"Shell 语法错误: {fp.name} — {r.stderr.strip()}")
        except Exception as e:
            result.warn(f"无法检查: {fp.name} — {e}")


# ── 主入口 ──

def cmd_check():
    print("🔍 知识库健康检查 (kb_maintain.py check)")
    print(f"   项目根目录: {KB_ROOT}")
    print(f"{'═' * 60}")

    result = CheckResult()

    check_python_syntax(result)
    check_gitignore_privacy(result)
    check_git_tracked_privacy(result)
    check_token_in_url(result)
    check_hardcoded_token(result)
    check_index_stability(result)
    check_git_status(result)
    check_web_assets(result)
    check_shell_scripts(result)
    check_server_health(result)
    check_mcp_usage(result)

    # 输出详情
    print(f"\n{'─' * 60}")
    print("📋 检查详情:")
    for icon, msg in result.details:
        print(f"  {icon} {msg}")

    sys.exit(result.summary())


def main():
    if len(sys.argv) < 2:
        print("用法: python kb_maintain.py <命令>\n")
        print("命令:")
        print("  check    运行健康检查（语法、隐私、安全、Git、服务端）")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "check":
        cmd_check()
    else:
        print(f"❌ 未知命令: {cmd}")
        print("可用命令: check")
        sys.exit(1)

if __name__ == "__main__":
    main()
