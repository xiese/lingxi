#!/usr/bin/env python3
"""知识库检索回归评估。

用途：每次改动检索/索引/问答后，一条命令出通过率，防止「抽样通过 ≠ 修好」。

两种用例来源：
  1. golden set（推荐）：tests/golden_search.json —— 手写「问题 → 期望命中的笔记路径」
  2. 自动生成（兜底）：从全库笔记标题生成 query，检验能否召回自己

用法:
    python3 scripts/kb_eval.py                 # 自动生成，K=5，n=40
    python3 scripts/kb_eval.py 10 60           # K=10，n=60
    python3 scripts/kb_eval.py --golden        # 只用 golden set
    python3 scripts/kb_eval.py --golden --json # 输出 JSON（给 CI / 对比用）

退出码: 0 = 全部命中；1 = 有未命中（便于 CI 卡口）
"""
import sys
import os
import json
import random
import re
import urllib.request
import urllib.parse

KB = os.environ.get("KB_URL", "http://127.0.0.1:8899")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 库根目录：状态文件（token）在这里，默认与代码同根
KB_ROOT = os.environ.get("KB_ROOT") or ROOT
# golden set 属于本地评测数据，可用 KB_GOLDEN 指定；缺失时自动回退到自动生成用例
GOLDEN = os.environ.get("KB_GOLDEN") or os.path.join(ROOT, "tests", "golden_search.json")


def _token():
    """从 .api_tokens.json 取 master token（只读，不外泄）。"""
    for name in (".api_tokens.json",):
        p = os.path.join(KB_ROOT, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["master"]["token"]
    raise SystemExit("找不到 .api_tokens.json")


def _norm(p):
    """路径归一化：API 返回的 path 不带 '笔记/' 前缀，golden set 里带，需统一。"""
    p = str(p).strip().lstrip("./")
    if p.startswith("笔记/"):
        p = p[len("笔记/"):]
    return p


def semantic(q, k=5):
    # 安全加固：服务端只读 limit（routes_search.py 的 _int_arg("limit", 5, 1, 20)），
    # 原来传 k，K>5 时永远只评到前 5 名，K<5 时又多算，CI 卡口基于错口径。
    u = KB + "/api/search/semantic?" + urllib.parse.urlencode({"q": q, "limit": k})
    r = urllib.request.Request(u, headers={"Authorization": "Bearer " + _token()})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read()).get("items", [])


def load_golden():
    if not os.path.exists(GOLDEN):
        return []
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f).get("cases", [])


def build_cases(n=40, seed=42):
    """从索引路径生成自动用例（避开 50-档案 / 60-梦境，与索引排除口径一致）。"""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from kb_lib.search import search_index as SI
    SI.load()
    paths = [x for x in SI.paths if "50-档案" not in x and "60-梦境" not in x]
    random.seed(seed)
    random.shuffle(paths)
    cases = []
    for p in paths[: n * 2]:
        stem = re.sub(r"\.md$", "", p.split("/")[-1])
        q = re.sub(r"[_\-]+", " ", stem)
        q = re.sub(r"\d{4}-\d{2}-\d{2}", " ", q).strip()
        q = re.sub(r"\s+", " ", q)
        if len(q) >= 4:
            cases.append({"q": q, "path": p})
        if len(cases) >= n:
            break
    return cases


def run(cases, K=5):
    details = []
    for c in cases:
        try:
            items = semantic(c["q"], k=K)
        except Exception as e:
            details.append({"path": c["path"], "q": c["q"], "rank": -1, "err": str(e)[:80]})
            continue
        ps = [_norm(it.get("path", "")) for it in items]
        want = _norm(c["path"])
        rank = next((i + 1 for i, x in enumerate(ps) if x == want), None)
        details.append({"path": c["path"], "q": c["q"], "rank": rank or -1})
    return details


def main():
    argv = sys.argv[1:]
    as_json = "--json" in argv
    only_golden = "--golden" in argv
    nums = [int(a) for a in argv if a.isdigit()]
    K = nums[0] if nums else 5
    n = nums[1] if len(nums) > 1 else 40

    golden = load_golden()
    cases = golden if only_golden else (golden + build_cases(n))
    if not cases:
        print("无可用样本")
        return 2

    details = run(cases, K)
    hit = sum(1 for d in details if d["rank"] > 0)
    ranks = sorted(d["rank"] for d in details if d["rank"] > 0)
    pct = hit * 100 // len(details)

    if as_json:
        print(json.dumps({
            "k": K, "n": len(details), "hit": hit, "rate": round(hit / len(details), 4),
            "ranks": {"min": ranks[0] if ranks else None,
                      "p50": ranks[len(ranks) // 2] if ranks else None,
                      "max": ranks[-1] if ranks else None},
            "miss": [{"q": d["q"], "path": d["path"]} for d in details if d["rank"] < 0],
        }, ensure_ascii=False, indent=2))
        return 0 if hit == len(details) else 1

    print(f"=== 检索回归评估 (K={K}, n={len(details)}) ===")
    if golden:
        print(f"  golden 用例: {len(golden)} 条")
    print(f"  top-{K} 命中率: {hit}/{len(details)} = {pct}%")
    miss = [d for d in details if d["rank"] < 0]
    if miss:
        print(f"  未命中 {len(miss)} 条:")
        for d in miss[:15]:
            print(f"    q=\"{d['q'][:48]}\"  目标={d['path'][:56]}")
    if ranks:
        print(f"  命中排名: min={ranks[0]} p50={ranks[len(ranks)//2]} max={ranks[-1]}")
    return 0 if hit == len(details) else 1


if __name__ == "__main__":
    sys.exit(main())
