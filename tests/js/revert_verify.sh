#!/bin/bash
# 第 12、20 项反向验证（变异测试）
#
# 作用：逐条把「修复」改回原来的错误写法，确认对应测试**报红**。
# 测试全绿只说明「当前代码能过」；只有改坏也报红，才说明断言真的在锁这个修复点。
#
# 用法：bash tests/js/revert_verify.sh
# 依赖：node、python3。脚本会临时改 scripts/web/app.js 与 index.html，
#       每条用例跑完立刻从备份还原，结束时再核对一次。
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APP="$ROOT/scripts/web/app.js"
HTML="$ROOT/scripts/web/index.html"
T12="$ROOT/tests/js/graph_touch.test.cjs"
T20="$ROOT/tests/js/mobile_toc.test.cjs"
TMPD="$(mktemp -d)"
BAK_APP="$TMPD/app.bak"
BAK_HTML="$TMPD/html.bak"
cp "$APP" "$BAK_APP"; cp "$HTML" "$BAK_HTML"
OUT="$TMPD/out.txt"
trap 'cp "$BAK_APP" "$APP"; cp "$BAK_HTML" "$HTML"; rm -rf "$TMPD"' EXIT
# 不用进程替换 tee（统计时会与子进程竞态）。先把原始 stdout 存到 fd3，
# 整段输出落文件，结束时恢复 fd1 再 cat，避免 cat 把自己写回文件。
exec 3>&1
exec >"$OUT" 2>&1

run_case () {
  local name="$1" test="$2"
  if node "$test" >/dev/null 2>&1; then
    echo "❌ [$name] 改回原状后测试仍通过 → 该断言无约束力"
  else
    echo "✅ [$name] 改回原状后测试报红"
  fi
  cp "$BAK_APP" "$APP"; cp "$BAK_HTML" "$HTML"
}

py () { python3 - "$1" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding='utf-8').read()
PY
}
edit () { # edit <file> <old> <new>
  python3 - "$1" "$2" "$3" <<'PY'
import sys
p,old,new=sys.argv[1],sys.argv[2],sys.argv[3]
s=open(p,encoding='utf-8').read()
if old not in s:
    print("!! 未找到待替换片段:", old[:40]); sys.exit(3)
open(p,'w',encoding='utf-8').write(s.replace(old,new,1))
PY
}

# ① zoom filter 拒绝触屏
edit "$APP" "if(ev.type==='touchstart'||ev.type==='touchmove'||ev.type==='touchend')return true;" \
            "if(ev.type==='touchstart'||ev.type==='touchmove')return false;"
run_case "① zoom filter 拒绝触屏" "$T12"

# ①b 只把 touchend 改成 return false
edit "$APP" "if(ev.type==='touchstart'||ev.type==='touchmove'||ev.type==='touchend')return true;" \
            "if(ev.type==='touchstart'||ev.type==='touchmove')return true;
        if(ev.type==='touchend')return false;"
run_case "①b touchend 单独改成 return false" "$T12"

# ② touchAction 改回 pan-y
edit "$APP" "cv.style.touchAction='none';" "cv.style.touchAction='pan-y';"
run_case "② touch-action 改回 pan-y" "$T12"

# ③ 标签门限改回单档 0.45
edit "$APP" "if(tf.k>(_mob?0.18:0.45)){" "if(tf.k>0.45){"
run_case "③ 标签门限改回 0.45" "$T12"

# ③b _cap 改成分端三元
edit "$APP" "var _cap=40;" "var _cap=_mob?30:40;"
run_case "③b _cap 改成分端三元（30/40）" "$T12"

# ③c _mob 重复声明
edit "$APP" "var _cap=40;" "var _cap=40;
    var _mob=window.innerWidth<768;"
run_case "③c _mob 重复声明" "$T12"

# ④ 去掉 tick 节流（真删门限两行）
edit "$APP" "      var _now=Date.now();
      if(_now-_lastDraw<33)return;
      _lastDraw=_now;
      draw();" "      draw();"
run_case "④ 去掉重绘节流" "$T12"

# ⑤ 去掉图谱页跳过
edit "$APP" "    if(onGraphTab())return;
" ""
run_case "⑤ 下拉刷新不跳过图谱页" "$T12"

# ⑥ 第 20 项：CSS 放行规则改回原状（删掉）
edit "$HTML" "  #tocPanel.mobile-show #tocContent{display:block!important}
" ""
run_case "⑥ 第20项 CSS 放行规则被删" "$T20"

# ⑦ 第 20 项：放行规则挪进 print 块
python3 - "$HTML" <<'PY'
import sys
p=sys.argv[1]; s=open(p,encoding='utf-8').read()
rule="  #tocPanel.mobile-show #tocContent{display:block!important}\n"
# 必须先确认 print 块真的存在，否则「挪进 print」退化成「直接删除」，
# 用例 ⑦ 就变成和 ⑥ 同义，测不出「放错媒体查询」这个真实风险。
if "@media print{" not in s:
    print("!! 未找到 @media print{，用例 ⑦ 无法构造"); sys.exit(3)
s=s.replace(rule,"",1)
s=s.replace("@media print{","@media print{\n"+rule,1)
open(p,'w',encoding='utf-8').write(s)
PY
run_case "⑦ 放行规则挪进 print 块" "$T20"

# ⑧ 第 20 项：closeMobilePanels 加回 remove('hidden')
edit "$APP" "function closeMobilePanels(){document.querySelectorAll('.note-dir-panel,#tocPanel').forEach(function(e){e.classList.remove('mobile-show')});" \
            "function closeMobilePanels(){document.querySelectorAll('.note-dir-panel,#tocPanel').forEach(function(e){e.classList.remove('mobile-show');e.classList.remove('hidden')});"
run_case "⑧ closeMobilePanels 加回 remove('hidden')" "$T20"


# ③d _cap 前面是 40、后面又被覆盖成分端三元
edit "$APP" "var _cap=40;" "var _cap=40;
    var _cap2=0; if(_cap2)_cap=_mob?30:40;"
run_case "③d _cap 被后续赋值覆盖" "$T12"

# ⑤b onGraphTab 判定挪到 refresh() 里（不在 touchstart 处理器内）
edit "$APP" "    if(onGraphTab())return;
" ""
edit "$APP" "  function refresh(){" "  function refresh(){
    if(onGraphTab())return;"
run_case "⑤b onGraphTab 判定挪出 touchstart" "$T12"

# ⑨ 第 20 项：把 #tocContent 挪出 #tocPanel（DOM 结构不再匹配）
edit "$HTML" '<div class="w-48 flex-shrink-0 hidden" id="tocPanel"><div class="toc-panel" id="tocContent">' \
             '<div class="toc-panel" id="tocContent"></div><div class="w-48 flex-shrink-0 hidden" id="tocPanel">'
run_case "⑨ #tocContent 挪出 #tocPanel" "$T20"

# ⑩ _mob 声明跑到 lgRefresh 外面
edit "$APP" "    var _mob=window.innerWidth<768;
" ""
edit "$APP" "function lgRefresh(" "var _mob=window.innerWidth<768;
function lgRefresh("
run_case "⑩ _mob 声明跑到 lgRefresh 外" "$T12"


# ⑪ onGraphTab 函数体改错（恒真）
edit "$APP" "function onGraphTab(){return curTab()==='graph'}" "function onGraphTab(){return true}"
run_case "⑪ onGraphTab 恒真" "$T12"

# ⑫ _lastDraw 初始化挪到 tick 之后
edit "$APP" "var _lastDraw=0;" ""
edit "$APP" "    draw();
  }).catch(" "    var _lastDraw=0;
    draw();
  }).catch("
run_case "⑫ _lastDraw 初始化挪到 tick 之后" "$T12"


# ⑬ onGraphTab 前置 return true（DS 第 8 轮）
edit "$APP" "function onGraphTab(){return curTab()==='graph'}" "function onGraphTab(){return true;return curTab()==='graph'}"
run_case "⑬ onGraphTab 前置 return true" "$T12"

# ⑭ onGraphTab 判错 tab
edit "$APP" "function onGraphTab(){return curTab()==='graph'}" "function onGraphTab(){return curTab()==='list'}"
run_case "⑭ onGraphTab 判错 tab" "$T12"

# ⑮ _lastDraw 塞进嵌套函数 doFit（同一 lgRefresh 内，但不同作用域）
edit "$APP" "    var _lastDraw=0;" ""
edit "$APP" "function doFit(){ if(_fitDone) return;" "function doFit(){ var _lastDraw=0; if(_fitDone) return;"
run_case "⑮ _lastDraw 塞进 doFit" "$T12"

# ⑯ _cap 死代码蒙混（分端 + if(false)_cap=40）
edit "$APP" "    var _cap=40;" "    var _cap=_mob?30:40;
    if(false)_cap=40;"
run_case "⑯ _cap 死代码蒙混" "$T12"


# ⑰ _cap 自增（DS 第 9 轮）
edit "$APP" "    var _cap=40;" "    var _cap=40;
    _cap++;"
run_case "⑰ _cap 自增" "$T12"

# ⑱ _cap 复合赋值
edit "$APP" "    var _cap=40;" "    var _cap=40;
    _cap+=1;"
run_case "⑱ _cap 复合赋值" "$T12"

# ⑲ 触摸改花括号 return false
edit "$APP" "if(ev.type==='touchstart'||ev.type==='touchmove'||ev.type==='touchend')return true;" "if(ev.type==='touchstart'){return false;}
        if(ev.type==='touchmove'||ev.type==='touchend')return true;"
run_case "⑲ 触摸花括号 return false" "$T12"

# ⑳ 鼠标路径前加无条件 return true
edit "$APP" "        return !ev.button;" "        return true;
        return !ev.button;"
run_case "⑳ 鼠标路径变死代码" "$T12"

# ㉑ _mob 函数外隐式全局
edit "$APP" "function lgRefresh(){" "_mob=window.innerWidth<768;
function lgRefresh(){"
run_case "㉑ _mob 隐式全局" "$T12"


# ㉒ touch-action 设到别的元素（DS 第 10 轮）
edit "$APP" "cv.style.touchAction='none';" "cv.style.touchAction='pan-y';
    c.style.touchAction='none';"
run_case "㉒ touch-action 设到别的元素" "$T12"

# ㉓ onGraphTab 挪到 pulling=true 之后
edit "$APP" "    if(onGraphTab())return;
    if(topMost()>0)return;
    startY=e.touches[0].clientY;dist=0;pulling=true;" "    if(topMost()>0)return;
    startY=e.touches[0].clientY;dist=0;pulling=true;
    if(onGraphTab())return;"
run_case "㉓ onGraphTab 挪到 pulling 之后" "$T12"


# ㉔ lgRefresh 外新增一个 canvas 设 none（原 cv 仍 pan-y）
edit "$APP" "cv.style.touchAction='none';" "cv.style.touchAction='pan-y';"
edit "$APP" "function lgRefresh(){" "var _x=document.createElement('canvas');_x.style.touchAction='none';
function lgRefresh(){"
run_case "㉔ 别的 canvas 设 none 蒙混" "$T12"


# ㉕ labPick 使用点改回写死 30（DS 第 12 轮）
edit "$APP" "var lab=labPick(gd.nodes, _cap);" "var lab=labPick(gd.nodes, 30);"
run_case "㉕ labPick 使用点改回 30" "$T12"

# ㉖ labPick 使用点改成写死 40
edit "$APP" "var lab=labPick(gd.nodes, _cap);" "var lab=labPick(gd.nodes, 40);"
run_case "㉖ labPick 使用点改成 40" "$T12"

cp "$BAK_APP" "$APP"; cp "$BAK_HTML" "$HTML"
echo "--- 还原后复跑 ---"
node "$T12" >/dev/null 2>&1 && echo "✅ 第12项 还原后重新通过" || echo "❌ 第12项 还原失败"
node "$T20" >/dev/null 2>&1 && echo "✅ 第20项 还原后重新通过" || echo "❌ 第20项 还原失败"

# 汇总：任何一条「改坏后仍绿」都算失败
FAILS=$(grep -c '^❌' "$OUT" || true)
OKS=$(grep -c '^✅ \[' "$OUT" || true)
exec 1>&3 3>&-
cat "$OUT"
echo "合计：有约束力 $OKS 条，无约束力 ${FAILS:-0} 条"
if [ "${FAILS:-0}" -ne 0 ]; then
  echo "存在无约束力断言，反向验证失败"
  exit 1
fi
exit 0
