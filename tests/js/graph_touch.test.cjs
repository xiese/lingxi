#!/usr/bin/env node
/**
 * 第 12 项（2026-10-18）：手机端图谱「划不动 / 不显示标签 / 卡顿」。
 *
 * 真机（手机 Edge）现象：
 *   ① 单指拖不动、双指不能缩放
 *   ② 图谱页往下滑会触发页面刷新
 *   ③ 一个标签都不显示
 *   ④ 拖动时掉帧
 *
 * 读代码定位到 5 处修复点 + 1 处加固，本文件逐条锁死：
 *   ① d3.zoom().filter() 对 touchstart/touchmove 直接 return false
 *   ② canvas 的 touch-action 是 pan-y，竖直手势被浏览器判给页面滚动
 *   ③ 标签门限写死 tf.k>0.45，手机上 fitView 后 k 只有 0.2~0.4
 *   ④ 仿真每个 tick 都整屏重绘
 *   ⑤ 下拉刷新的 touchstart 没排除图谱页
 *   加固：_mob 从 lgRefresh 尾部提到开头（原代码时序上没有真 bug）
 *
 * 注意：本文件是「源码级锁回归」，不是行为验证。手机端真实手感必须由维护者
 * 用手机 Edge 真机确认，测试全绿不代表真机通过。
 */
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '../../scripts/web/app.js');
const src = fs.readFileSync(APP, 'utf8');

// 剥掉 // 行注释与 /* */ 块注释，避免注释里的描述被当成代码命中
function noComments(t){
  return t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

let pass = 0, fail = 0;
function ok(name, cond, why) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (why ? '  ← ' + why : '')); }
}
// 取某个函数体的源码（从 `filter(function` 起，按花括号配平截断）
function blockAfter(marker) {
  const i = src.indexOf(marker);
  if (i < 0) return '';
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  return src.slice(i);
}


// 求某个下标所属的「最内层函数」区间，用于判断两处代码是否在同一函数作用域。
// 只靠 lastIndexOf('function') 会被嵌套函数骗过（doFit 声明在 tick 之前）。
function allFnRanges(s) {
  const out = [];
  const re = /function\s*[\w$]*\s*\(/g;
  let m;
  while ((m = re.exec(s))) {
    let i = s.indexOf('{', m.index + m[0].length);
    if (i < 0) continue;
    let depth = 0, end = -1;
    for (let j = i; j < s.length; j++) {
      if (s[j] === '{') depth++;
      else if (s[j] === '}') { depth--; if (depth === 0) { end = j; break; } }
    }
    if (end > 0) out.push([m.index, end]);
  }
  return out;
}
function innermostFn(ranges, pos) {
  let best = null;
  for (const r of ranges) {
    if (pos >= r[0] && pos <= r[1]) {
      if (!best || (r[1] - r[0]) < (best[1] - best[0])) best = r;
    }
  }
  return best;
}

// ── ① d3.zoom 手势过滤 ─────────────────────────────────────
console.log('① d3.zoom 手势过滤');
const zf = blockAfter('.filter(function(ev)');
ok('找到 zoom 的 filter 函数体', zf.length > 0);
// 三个触摸事件必须各自落在 return true 的那一行上，不能被改成 return false
['touchstart', 'touchmove', 'touchend'].forEach(t => {
  const re = new RegExp("if\\([^)]*" + t + "[^)]*\\)\\s*return\\s+true\\s*;");
  ok(t + ' 明确 return true（放行给 d3）', re.test(zf),
    '手机上这条路被关掉就完全拖不动');
});
// 否定断言必须同时覆盖 `if(...)return false;` 和 `if(...){return false;}` 两种写法，
// 否则只加一对花括号就能绕过。
const touchFalseRe = /if\s*\([^)]*touch[a-z]*[^)]*\)\s*\{?\s*return\s+false\s*;?\s*\}?/;
ok('不再出现对触摸事件的 return false（含花括号写法）',
  !touchFalseRe.test(zf),
  '原缺陷就是这一句；写成 if(...){return false;} 同样是缺陷');
ok('桌面鼠标路径是最后一条 return，且真可达',
  (() => {
    // 只查「存在 return !ev.button」不够：前面若塞一句无条件 return true，
    // 这行就是死代码，鼠标反而不能用。
    // 做法：把 filter 体按行拆开，收集所有 return 所在行；
    //   a) 最后一条 return 必须是 `return !ev.button;`
    //   b) 除最后一条外，每条 return 所在行必须含 if( —— 即都是有条件的，
    //      单独一行 `return true;` 会被抓到。
    const lines = zf.split('\n');
    const retIdx = [];
    lines.forEach((ln, i) => { if (/\breturn\b/.test(ln)) retIdx.push(i); });
    if (retIdx.length < 2) return false;
    const last = lines[retIdx[retIdx.length - 1]];
    if (!/^\s*return\s*!\s*ev\.button\s*;\s*$/.test(last)) return false;
    return retIdx.slice(0, -1).every(i => /\bif\s*\(/.test(lines[i]));
  })(),
  '鼠标路径若被无条件 return 挡在前面就是死代码，桌面反而拖不动');

// ── ② canvas touch-action ──────────────────────────────────
console.log('② canvas touch-action');
// 必须锁到图谱 canvas 那个变量上。只查「某处 .style.touchAction='none'」不够：
// 换到别的元素上设，图谱 canvas 仍然是 pan-y，手机上照样只收到半截手势。
const cvTouch = (() => {
  // 图谱 canvas 是动态创建的：var cv=document.createElement('canvas')。
  // 全文件可能有不止一个 canvas，必须在 lgRefresh（图谱渲染入口）函数体内找，
  // 否则取到别处的 canvas 会让断言失去意义。
  const fnStart = src.indexOf('function lgRefresh(');
  if (fnStart < 0) return null;
  let depth = 0, started = false, end = -1;
  for (let j = fnStart; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) { end = j; break; } }
  }
  if (end < 0) return null;
  const body = src.slice(fnStart, end + 1);
  const m = body.match(/var\s+(\w+)\s*=\s*document\.createElement\(['"]canvas['"]\)/);
  if (!m) return null;
  const v = m[1];
  const re = new RegExp('\\b' + v + "\\.style\\.touchAction\\s*=\\s*['\"]([^'\"]+)['\"]");
  const hit = body.match(re);
  return { v, val: hit ? hit[1] : null };
})();
ok("图谱 canvas（cv）的 touchAction 是 'none'",
  !!cvTouch && cvTouch.val === 'none',
  "pan-y 会让浏览器把竖直手势判给页面滚动，d3 只收到半截；设到别的元素上不算");
ok("图谱 canvas 不再残留 touchAction='pan-y'",
  !(cvTouch && cvTouch.val === 'pan-y'));

// ── ③ 标签显示门限 ──────────────────────────────────────────
console.log('③ 标签显示门限');
ok('门限按设备分档（_mob ? 0.18 : 0.45）',
  /tf\.k\s*>\s*\(\s*_mob\s*\?\s*0\.18\s*:\s*0\.45\s*\)/.test(src),
  '手机上 fitView 后 k 常在 0.2~0.4，单档 0.45 等于把标签全关掉');
ok('不再有写死的 tf.k>0.45 单档门限（排除注释）',
  (() => {
    // ① 去掉 // 行注释和 /* */ 块注释，避免注释里的描述被当成代码
    const code = src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '');
    // ② 再把分档写法整体挖掉，剩下的若还有写死门限就是漏改
    const stripped = code.replace(/tf\.k\s*>\s*\(\s*_mob\s*\?\s*0\.18\s*:\s*0\.45\s*\)/g, 'REMOVED');
    return !/tf\.k\s*>\s*0\.45/.test(stripped);
  })(),
  '分档之外还留着单档门限，等于又关掉手机标签');

// ── ③b 手机标签数上限 ───────────────────────────────────────
console.log('③b 手机标签数上限');
// ③b2 使用点：labPick 必须拿到 _cap，不能改回写死的 30
ok('labPick 实际使用 _cap（使用点改回 30 会漏）',
  /labPick\s*\(\s*gd\.nodes\s*,\s*_cap\s*\)/.test(noComments(src)),
  'label 上限必须真的作用到贴标签处，只改声明不改使用点等于没生效');
ok('labPick 调用点不再出现写死的 30/40',
  !/labPick\s*\(\s*gd\.nodes\s*,\s*(30|40)\s*\)/.test(noComments(src)),
  '使用点写死数字会让 _cap 的修改失效');

ok('_cap 全文件只有一处赋值，且是字面量 40',
  (() => {
    // 只认「最后一次赋值」会被死代码骗过（if(false)_cap=40 也能排在最后）。
    // 改成：整个文件里 _cap 只能被赋值一次，值必须是字面量 40。
    // ① 任何形式的再赋值都算：= / += / -= / *= / ++ / --（只匹配 `=` 会漏掉 _cap+=1）
    const assigns = [...src.matchAll(/\b_cap\s*(?:\+\+|--|[+\-*/]?=(?!=))/g)];
    if (assigns.length !== 1) return false;
    // ② 唯一那一处必须是 var _cap=40;
    const m = src.match(/var\s+_cap\s*=\s*([^;,}\n]+)/);
    return !!m && m[1].trim() === '40';
  })(),
  '待办正文写 30，但 17:49 维护者拍板为 40；多处赋值会让死代码或覆盖值蒙混过关');

// ── ③c _mob 声明位置（加固项）──────────────────────────────
console.log('③c _mob 声明（加固，非缺陷修复）');
const drawDef = src.indexOf('function draw(');
const mobDef = src.indexOf('var _mob=window.innerWidth<768');
ok('_mob 在 draw() 定义之前声明', mobDef > 0 && drawDef > 0 && mobDef < drawDef,
  '提前只是让作用域更明确，原代码时序上没有真 bug');
ok('_mob 只在 lgRefresh 函数体内声明一次',
  (() => {
    const fnStart = src.indexOf('function lgRefresh(');
    if (fnStart < 0) return false;
    // 用花括号配平取 lgRefresh 整个函数体
    let depth = 0, started = false, end = -1;
    for (let j = fnStart; j < src.length; j++) {
      if (src[j] === '{') { depth++; started = true; }
      else if (src[j] === '}') { depth--; if (started && depth === 0) { end = j; break; } }
    }
    if (end < 0) return false;
    const body = src.slice(fnStart, end + 1);
    // ① lgRefresh 内声明恰好一次
    if ((body.match(/var\s+_mob\s*=/g) || []).length !== 1) return false;
    // ② 函数外不许再出现任何形式的 _mob 赋值（含隐式全局 `_mob=...`、`_mob+=`、`_mob++`），
    //    只查 `var _mob=` 会漏掉不带 var 的写法。
    const outside = src.slice(0, fnStart) + src.slice(end + 1);
    return !/\b_mob\s*(?:\+\+|--|[+\-*/]?=(?!=))/.test(outside);
  })(),
  '重复声明或声明跑到函数外，都会让门限判定拿到另一份值');

// ── ④ 仿真重绘节流 ──────────────────────────────────────────
console.log('④ 仿真降帧');
const tickBlock = blockAfter("sim.on('tick'");
ok('tick 里有 33ms 门限（约 30fps）',
  /if\(\s*\w+\s*-\s*_lastDraw\s*<\s*33\s*\)\s*return\s*;/.test(tickBlock),
  '没有门限时每个 tick 都整屏重绘，手机掉帧');
ok('门限之后才刷新 _lastDraw 并 draw()',
  (() => {
    const guard = tickBlock.search(/if\(\s*\w+\s*-\s*_lastDraw\s*<\s*33\s*\)\s*return\s*;/);
    const set = tickBlock.indexOf('_lastDraw=');
    const drawCall = tickBlock.indexOf('draw()');
    return guard >= 0 && set > guard && drawCall > set;
  })(),
  '顺序错了会变成永远不画或每次都画');
ok('_lastDraw 与 sim.on(\'tick\') 属同一函数作用域，且在它之前初始化',
  (() => {
    const initAt = src.search(/var\s+_lastDraw\s*=\s*-?\d+/);
    const tickAt = src.indexOf("sim.on('tick'");
    if (initAt < 0 || tickAt < 0 || initAt > tickAt) return false;
    // 必须是比较「最内层所属函数」，不能只看最近一个 function 关键字：
    // doFit 声明在 tick 之前，把初始化塞进 doFit 时最近 function 起点会相同而误判为同作用域。
    const ranges = allFnRanges(src);
    const a = innermostFn(ranges, initAt);
    const b = innermostFn(ranges, tickAt);
    return !!a && !!b && a[0] === b[0] && a[1] === b[1];
  })(),
  '初始化被塞进嵌套函数（如 doFit）时 tick 读到 undefined，门限行为不确定');

// ── ⑤ 图谱页不触发下拉刷新 ──────────────────────────────────
console.log('⑤ 下拉刷新与图谱页冲突');
ok('onGraphTab 函数体只有一句 return curTab()===\'graph\'',
  (() => {
    const m = src.match(/function\s+onGraphTab\s*\(\)\s*\{([^{}]*)\}/);
    if (!m) return false;
    // 整段函数体做「去空白后完全等于」，前面塞一句 return true 也会被抓到
    const body = m[1].replace(/\s+/g, '');
    return body === "returncurTab()==='graph'" || body === 'returncurTab()==="graph"';
  })(),
  '函数体多一句 return、恒真或判错 tab，图谱页要么永不刷新、要么每页都不刷新');
ok('下拉刷新的 touchstart 处理器里跳过图谱页',
  (() => {
    // 必须落在 document.addEventListener('touchstart', ...) 的回调体内，
    // 只写在别的函数里等于没生效
    const i = src.indexOf("document.addEventListener('touchstart',function(e){");
    if (i < 0) return false;
    let depth = 0, started = false, end = -1;
    for (let j = i; j < src.length; j++) {
      if (src[j] === '{') { depth++; started = true; }
      else if (src[j] === '}') { depth--; if (started && depth === 0) { end = j; break; } }
    }
    if (end < 0) return false;
    const handler = src.slice(i, end + 1);
    // 下拉刷新这个 handler 的特征：里面有 pulling 状态赋值
    if (!/pulling\s*=\s*true/.test(handler)) return false;
    // 位置也要对：必须排在 pulling=true 之前。
    // 放在后面的话 pulling 已经被置真，图谱上往下拖仍会进入下拉刷新流程。
    const g = handler.search(/if\(\s*onGraphTab\(\)\s*\)\s*return\s*;/);
    const p = handler.search(/pulling\s*=\s*true/);
    return g >= 0 && p >= 0 && g < p;
  })(),
  '写在别的函数里不生效；手指往下拖是在平移图谱，不该刷新页面');

console.log('');
if (fail) { console.log(`失败：${fail} 项，通过：${pass} 项`); process.exit(1); }
console.log(`全部通过：${pass} 项断言`);
