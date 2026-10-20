#!/usr/bin/env python3
"""知识库版本一致性检查。

原则：包版本号只有一个来源 —— scripts/kb_lib/config.py 的 VERSION。
其余文件不得再出现与包版本联动的版本字面量；网页上显示的版本一律由
/api/version 在运行时回填（app.js checkVersion 写 #verBadge），
Service Worker 缓存名固定化（入口文件本来就是网络优先，不依赖改名失效）。

白名单（不随包版本联动的字样，允许保留）：
- kb_lib/graph.py 与 tests/test_graph_compute_contract.py 的「v1.0.0 冻结」
  —— 对外图计算契约的协议版本标识，与包版本无关；
- kb_lib/routes/routes_integrations.py 的「v1.0.0 起」
  —— 面向客户端的历史变更说明（该接口自 v1.0.0 起的行为）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 随包版本联动、不允许出现版本字面量的文件（前端回填 + 缓存名固定化）
VERSION_FREE_FILES = [
    "scripts/kb-server.py",
    "scripts/kb_lib/__init__.py",
    "scripts/kb_lib/routes/__init__.py",
    "scripts/web/app.js",
    "scripts/web/index.html",
    "scripts/web/manifest.json",
    "scripts/web/sw.js",
]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def current_version() -> str:
    m = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"', read("scripts/kb_lib/config.py"), re.M)
    assert m, "config.py 未定义 VERSION"
    return m.group(1)


def test_config_is_single_source() -> None:
    """VERSION 只允许在 config.py 定义一次。"""
    config = read("scripts/kb_lib/config.py")
    assert len(re.findall(r'^VERSION = "', config, re.M)) == 1, "config.py 应且仅应定义一次 VERSION"


def test_no_hardcoded_package_version() -> None:
    """随版本走的文件不得写死包版本字面量。"""
    v = current_version()
    for rel in VERSION_FREE_FILES:
        text = read(rel)
        assert v not in text, f"{rel} 仍硬编码包版本 {v}（应由 config.py / /api/version 提供）"


def test_share_manifest_version_uses_config() -> None:
    """分享包等接口的 version 字段应引用 VERSION，而非字面量。"""
    src = read("scripts/kb_lib/routes/routes_integrations.py")
    assert not re.search(r'"version":\s*"\d+\.\d+\.\d+"', src), \
        "routes_integrations.py 存在硬编码版本字面量，应引用 config.VERSION"


def test_no_cache_busting_param() -> None:
    """index.html 不用 ?v= 版本参数做缓存失效（入口文件网络优先 + no-store）。"""
    html = read("scripts/web/index.html")
    assert "app.js?v=" not in html, "index.html 不应携带 app.js 版本查询参数"


def test_sw_cache_name_fixed() -> None:
    """sw.js 缓存名固定，不随版本改名。"""
    sw = read("scripts/web/sw.js")
    m = re.search(r'const CACHE = "([^"]+)"', sw)
    assert m, "sw.js 未定义 CACHE"
    assert m.group(1) == "kb-static", f"sw.js 缓存名应固定为 kb-static，当前为 {m.group(1)!r}"


if __name__ == "__main__":
    tests = [
        test_config_is_single_source,
        test_no_hardcoded_package_version,
        test_share_manifest_version_uses_config,
        test_no_cache_busting_param,
        test_sw_cache_name_fixed,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    raise SystemExit(1 if failures else 0)
