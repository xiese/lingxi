#!/usr/bin/env python3
"""第 13 项（设置页分组式）反向验证。

把每处修复逐个改坏，确认 tests/js/settings_ios.test.cjs 报红；最后还原并复查全绿。

用法：python3 tests/js/revert_13.py
退出码：0 = 8 条变异全部有约束力；1 = 有漏检
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "scripts/web/app.js"
HTML = ROOT / "scripts/web/index.html"
TESTS = [ROOT / "tests/js/settings_ios.test.cjs",
         ROOT / "tests/js/settings_home_behavior.test.cjs"]


def run_test() -> bool:
    """两个测试都必须绿：静态锁结构 + 行为测试跑真代码。"""
    for t in TESTS:
        r = subprocess.run(["node", str(t)], capture_output=True, text=True)
        if r.returncode != 0:
            return False
    return True


# 每条变异从原文件里按正则取锚点，避免手写转义出错


def sub_once(text: str, old: str, new: str):
    """精确替换一次；返回 None 表示锚点没匹配上。"""
    if old not in text:
        return None
    return text.replace(old, new, 1)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    app_bak = tmp / "app.js"
    html_bak = tmp / "index.html"
    shutil.copy2(APP, app_bak)
    shutil.copy2(HTML, html_bak)

    def restore():
        shutil.copy2(app_bak, APP)
        shutil.copy2(html_bak, HTML)

    if not run_test():
        print("基线就是红的，先修好再跑反向验证")
        restore()
        return 1

    app_src = APP.read_text(encoding="utf-8")
    html_src = HTML.read_text(encoding="utf-8")

    # 从原文件里按正则取锚点，避免手写转义出错
    m_row = re.search(r"h\+='(<div class=\"set-row set-row-link\")", app_src)
    m_arrow = re.search(r'(<span class="set-arrow">›</span>)', app_src)
    m_search = re.search(r'(placeholder="搜索设置项")', app_src)
    m_item = re.search(r"(\{k:'token_ttl'[^}]*\},)", app_src)
    m_sel = re.search(r"(inp='<select id=\"s_'\+\w+\([\w.]+\)\+'\")", app_src)
    m_txt = re.search(r"(inp='<textarea id=\"s_'\+\w+\([\w.]+\)\+'\")", app_src)
    m_inp = re.search(r"(inp='<input id=\"s_'\+\w+\([\w.]+\)\+'\")", app_src)
    m_card = re.search(r"(\.set-card\{background:var\(--card-bg\))", html_src)

    cases = []

    if m_row:
        # 一级页里本来只拼一行链接；改成先铺一个输入框再拼链接
        old = "h+='" + m_row.group(1)
        new = "h+=_setField(f,_SET_CACHE||{});h+='" + m_row.group(1)
        cases.append(("一级页直接铺输入框（退回一页全摊）", APP, old, new))
    if m_arrow:
        cases.append(("去掉右侧箭头", APP, m_arrow.group(1), ""))
    if m_search:
        cases.append(("去掉顶部搜索", APP, m_search.group(1), 'placeholder="搜索"'))
    if m_item:
        cases.append(("删掉一个设置项 token_ttl", APP, m_item.group(1), ""))
    for name, rx in (("input", m_inp), ("select", m_sel), ("textarea", m_txt)):
        if rx:
            cases.append((f"改坏 {name} 的 s_ id", APP, rx.group(1), rx.group(1).replace('id="s_', 'id="x_')))
    if m_card:
        cases.append(("破坏原有 .set-card 规则", HTML, m_card.group(1), ".set-card{background:#fff"))

    # DS 第 1 轮指出的几个真问题，各一条变异
    m_write = re.search(r"(document\.getElementById\('sf'\)\.innerHTML=h;)", app_src)
    if m_write:
        cases.append(("干掉了写回 #sf 那一行（渲染等于没做）", APP, m_write.group(1), ""))
    m_icon = re.search(r"(<span class=\"set-icon\">)", app_src)
    if m_icon:
        cases.append(("去掉一级列表图标（12:48 口径要求）", APP, m_icon.group(1), '<span>'))
    m_list = re.search(r"(<div id=\"setList\">)", app_src)
    if m_list:
        cases.append(("去掉 #setList 容器（搜索会整页重绘打断输入法）", APP, m_list.group(1), '<div>'))
    m_sum = re.search(r"(if\(f\.t==='password'\)return v\?'••••••••':'未设置';)", app_src)
    if m_sum:
        cases.append(("密码类直接显示原值/占位符", APP, m_sum.group(1), "if(f.t==='password')return String(v||'');"))

    red = 0
    fail = 0
    for name, target, old, new in cases:
        src = target.read_text(encoding="utf-8")
        out = sub_once(src, old, new)
        if out is None:
            print(f"❌ 变异失败（锚点未匹配）：{name}")
            fail += 1
            continue
        target.write_text(out, encoding="utf-8")
        if run_test():
            print(f"❌ 漏检：{name}")
            fail += 1
        else:
            print(f"✅ 报红：{name}")
            red += 1
        restore()

    # 无破坏自检：什么都不改，必须判为「绿」，否则说明测试本身乱红
    if run_test():
        print("✅ 无破坏自检通过（判定不是假绿）")
    else:
        print("❌ 无破坏自检失败：没改坏却报红")
        fail += 1

    # 还原后必须全绿
    if run_test():
        print("✅ 还原后全绿")
    else:
        print("❌ 还原后仍红，还原失败")
        fail += 1

    total = len(cases)
    print("-----------------------------------------")
    print(f"第 13 项反向验证：{total} 条变异 → 报红 {red} 条，漏检 {total - red} 条")
    shutil.rmtree(tmp, ignore_errors=True)
    if red != total or fail:
        print("有漏检或无约束力断言，退出码 1")
        return 1
    print(f"{red}/{total} 全部有约束力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
