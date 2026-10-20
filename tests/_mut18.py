#!/usr/bin/env python3
"""第 18 项反向验证的变异器：把修复逐条改坏。

用法：python3 _mut18.py <case> <file>
每个 case 只做一处最小改动，改完由 test_health_revert.sh 跑测试看是否报红。
"""
import sys

CASE, PATH = sys.argv[1], sys.argv[2]
src = open(PATH, encoding="utf-8").read()


def sub(old, new, required=True):
    global src
    if old not in src:
        if required:
            sys.exit(f"[{CASE}] 变异锚点未命中：{old[:60]!r}")
        return
    src = src.replace(old, new, 1)


if CASE == "rg_sync":
    # /health 改回同步扫描（原缺陷）
    sub("stale, cached = search_index.peek_stale()",
        "stale = search_index.count_stale(); cached = True")
    sub("                search_index.prime_stale_async()", "                pass")

elif CASE == "peek_zero":
    # 冷启动返回 0，冒充「索引完全跟得上」
    sub("        return None, False", "        return 0, False")

elif CASE == "prime_sync":
    # 预热改成同步执行，阻塞请求
    # 注意：不能把 _run() 放在锁内，那会和 count_stale 抢同一把非重入锁而死锁
    sub("            t = threading.Thread(target=_run, name=\"stale-prime\", daemon=True)\n"
        "            _stale_prime_thread = t\n"
        "            t.start()\n"
        "        return True",
        "            _stale_prime_thread = None\n"
        "        _run()\n"
        "        return True")

elif CASE == "no_dedup":
    # 去掉「已有线程在跑则不重复起」
    sub("            if t is not None and t.is_alive():", "            if False:")

elif CASE == "us_null_zero":
    # update_stats 改回把 null 当 0
    sub('"stale": _stale_from_health(health),',
        '"stale": int(health.get("stale", 0) or 0),')

elif CASE == "us_dirty_zero":
    # 脏值改回 0
    sub("    except (TypeError, ValueError):\n        return None",
        "    except (TypeError, ValueError):\n        return 0")

elif CASE == "no_cached_field":
    # 不返回 stale_cached，调用方无法区分「真 0」与「未知」
    sub('        "stale_cached": cached,\n', "")

elif CASE == "count_no_cache":
    # 真实 count_stale 不写缓存 → 预热永远无效
    sub('            _stale_cache["count"] = count\n'
        '            _stale_cache["ts"] = time.time()',
        "            pass")

elif CASE == "prime_race":
    # 竞态改回：锁内判断、锁外赋值+start
    sub("            t = threading.Thread(target=_run, name=\"stale-prime\", daemon=True)\n"
        "            _stale_prime_thread = t\n"
        "            t.start()\n"
        "        return True",
        "        t = threading.Thread(target=_run, name=\"stale-prime\", daemon=True)\n"
        "        _stale_prime_thread = t\n"
        "        t.start()\n"
        "        return True")

elif CASE == "us_none_literal":
    # 渲染改回直接插值 → 会输出字面 None
    sub('f"（stale={_stale_text(stats[\'stale\'])}）\\n"',
        'f"（stale={stats[\'stale\']}）\\n"')

else:
    sys.exit(f"未知 case: {CASE}")

open(PATH, "w", encoding="utf-8").write(src)
