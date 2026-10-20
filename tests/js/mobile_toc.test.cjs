#!/usr/bin/env node
/**
 * 第 20 项（2026-10-18）：手机端点目录按钮，抽屉弹出来一屏空白。
 *
 * 唯一根因（读生产代码 + 核实 CSS 特异性后确认）：
 *   index.html 手机媒体查询里 `.toc-panel{display:none!important}` 本意是藏桌面右栏，
 *   但抽屉内层 `<div class="toc-panel" id="tocContent">` 的 class 也叫 toc-panel，
 *   被同一条规则一起 display:none 掉了 —— 抽屉弹出来是空白。
 *
 * 修复：只加一条更具体的放行规则，让抽屉展开时内层内容显示：
 *   #tocPanel.mobile-show #tocContent{display:block!important}
 *
 * 曾误判并已撤销的改动（记录在此，防止以后有人重新加回去）：
 *   曾以为「#tocPanel 的 .hidden 优先级压过 .mobile-show」是第二个根因，并改了
 *   toggleMobileToc / closeMobilePanels / sn 三处 JS 增删 hidden。核实后不成立：
 *   .hidden{display:none}（特异性 0,1,0，无 !important）压不过
 *   #tocPanel.mobile-show{display:block!important}（1,1,0，带 !important）。
 *   三处 JS 改动已全部撤回，不是修复点。
 *
 * 性质说明：本文件是对源码文本的断言，只保证「修复点还在」，不等于真机行为验证。
 * 手机端效果由维护者用手机 Edge 真机确认。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const HTML = path.join(__dirname, '../../scripts/web/index.html');
const APP = path.join(__dirname, '../../scripts/web/app.js');
const html = fs.readFileSync(HTML, 'utf8');
const app = fs.readFileSync(APP, 'utf8');

let pass = 0;
function ok(name, cond, detail) {
  assert.ok(cond, name + (detail ? ' — ' + detail : ''));
  pass++;
  console.log('  ✓ ' + name);
}

console.log('① 抽屉内层不再被 .toc-panel 的 !important 规则吞掉');
ok('存在放行规则 #tocPanel.mobile-show #tocContent{display:block!important}',
  /#tocPanel\.mobile-show\s+#tocContent\s*\{[^}]*display\s*:\s*block\s*!important/.test(html),
  '缺这条规则时，抽屉展开后内层仍是 display:none，弹出即空白');

ok('放行规则写在手机媒体查询块内（不是全局）',
  (() => {
    const i = html.indexOf('#tocPanel.mobile-show #tocContent');
    if (i < 0) return false;
    const before = html.slice(0, i);
    const openAt = before.lastIndexOf('@media');
    if (openAt < 0) return false;
    // ① 花括号必须仍是净开口（这条规则确实在媒体查询内部）
    let depth = 0;
    for (const ch of html.slice(openAt, i)) {
      if (ch === '{') depth++;
      else if (ch === '}') depth--;
    }
    if (depth <= 0) return false;
    // ② 该媒体查询的条件必须是手机端（宽度上限），不能是 print 或 min-width
    const head = html.slice(openAt, html.indexOf('{', openAt));
    return /max-width\s*:\s*(7[0-9]{2}|6[0-9]{2}|5[0-9]{2})px/.test(head)
        || /width\s*<=\s*7[0-9]{2}px/.test(head);
  })(),
  '放到全局会破坏桌面右栏的显示；放进 print 块则手机上根本不生效');

console.log('①b 选择器与真实 DOM 结构匹配（#tocContent 确实是 #tocPanel 的后代）');
ok('#tocContent 在 DOM 里位于 #tocPanel 内部',
  (() => {
    // 取 #tocPanel 这个元素的开标签，再配平到它的闭标签，检查里面有没有 id="tocContent"
    const m = html.match(/<div[^>]*id="tocPanel"[^>]*>/);
    if (!m) return false;
    const start = m.index + m[0].length;
    let depth = 1;
    let i = start;
    // 粗略配平 div（本项目这一层没有嵌套同名结构，够用）
    while (i < html.length && depth > 0) {
      const nextOpen = html.indexOf('<div', i);
      const nextClose = html.indexOf('</div>', i);
      if (nextClose < 0) break;
      if (nextOpen >= 0 && nextOpen < nextClose) { depth++; i = nextOpen + 4; }
      else { depth--; i = nextClose + 6; }
    }
    const inner = html.slice(start, i);
    return /id="tocContent"/.test(inner);
  })(),
  'CSS 选择器写 #tocPanel #tocContent，若 DOM 里两者不是父子关系，规则根本不生效');

ok('#tocContent 的 class 仍是 toc-panel（这正是被误伤的原因）',
  (() => {
    const m = html.match(/<div[^>]*id="tocContent"[^>]*>/);
    return !!m && /class="[^"]*\btoc-panel\b/.test(m[0]);
  })(),
  '如果以后有人改了 class，这条放行规则就失去意义，应重新评估');

console.log('② 桌面右栏隐藏规则仍在（没把原规则删掉）');
ok('仍有 .toc-panel{display:none!important} 用于隐藏桌面右栏',
  /\.toc-panel\s*\{[^}]*display\s*:\s*none\s*!important/.test(html),
  '删掉原规则会让桌面端右栏重复出现');

console.log('③ 已撤销的误判改动没有被重新加回');
ok('toggleMobileToc 里不再增删 hidden（原实现即可）',
  (() => {
    const m = app.match(/function toggleMobileToc\(\)\{[^\n]*\}/);
    return !!m && !/classList\.(add|remove)\('hidden'\)/.test(m[0]);
  })(),
  '曾误判 hidden 优先级问题而加的三处 JS 改动，已核实不成立并撤回');
ok('closeMobilePanels 里既不 add 也不 remove hidden',
  (() => {
    const m = app.match(/function closeMobilePanels\(\)\{[^\n]*\}/);
    return !!m && !/classList\.(add|remove)\('hidden'\)/.test(m[0]);
  })(),
  '两个方向都要锁：只查 add 的话，加回 remove 仍会绿');
ok('sn() 的无 TOC 分支保持原样（直接 add hidden）',
  /else\{document\.getElementById\('tocPanel'\)\.classList\.add\('hidden'\)\}/.test(app),
  '改成「仅非 mobile-show 才 hidden」属越权改动，已撤回');

console.log('\n全部通过：' + pass + ' 项断言');
