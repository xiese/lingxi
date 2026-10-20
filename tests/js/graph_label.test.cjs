#!/usr/bin/env node
/**
 * 第 2 项改造（2026-10-18）：图谱标签。
 *
 * 真浏览器实测抓到过的问题：390px 屏上满屏「示例项目 组」。
 * 根因是 app.js 里 ctx.fillText((dn.title||'').slice(0,8), ...) 硬截前 8 字，
 * 而库里 140 篇标题以「示例项目」开头、其中 11 篇以「示例项目 组合」开头，
 * 前 8 字完全相同 → 屏幕上出现一堆一模一样的标签。
 *
 * 本文件锁三件事：
 *   ① labText 对同前缀组必须给出彼此不同的文本（分歧点开始截）
 *   ② labPick 只挑大节点（degree≥3）且默认不贴全图
 *   ③ 默认视图节点上限 = 150
 *
 * 反向验证：把 labText 改回 slice(0,8)、把 lgOpts 的 150 改回 300、
 * 把 labPick 的 degree>=3 去掉，本文件必须各报红。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const APP = path.join(__dirname, '../../scripts/web/app.js');
const src = fs.readFileSync(APP, 'utf8');

// 从源码里抠出纯函数（app.js 是浏览器脚本，直接 require 会报 window 未定义）。
// 注意：'use strict' 下 eval 的声明不外泄，必须用 new Function 显式返回。
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '在 app.js 里找不到 ' + name);
  let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
  throw new Error(name + ' 大括号不配对');
}
const labText = new Function(grab('labText') + '; return labText;')();
// labPick 内部会调 labText，必须把 labText 注入进它的作用域，
// 否则 new Function 里 labText 未定义——这是测试沙箱的事，不是产品代码的事。
const labPick = new Function('labText', grab('labPick') + '; return labPick;')(labText);
const lgOpts = new Function(grab('lgOpts') + '; return lgOpts;')();

let pass = 0, fail = 0;
function t(name, fn) {
  try { fn(); pass++; console.log('ok   ' + name); }
  catch (e) { fail++; console.log('FAIL ' + name + ' -> ' + e.message); }
}

// 生产库里真实存在的前缀（140 篇「示例项目」，其中 11 篇「示例项目 组合」）
const GROUP = [
  '示例项目 组合×手数复算_2026-09-30',
  '示例项目 组合×手数复算_2026-10-05',
  '示例项目 组合深度分析_2026-10-02',
  '示例项目 房间走势_2026-10-01',
  '示例项目 跳变_2026-09-28',
];

t('① 同前缀组内，标签必须两两不同（旧实现全截成「示例项目 组」）', () => {
  const outs = GROUP.map(s => labText(s, GROUP));
  const uniq = new Set(outs);
  assert.strictEqual(uniq.size, GROUP.length,
    '标签重复了：' + JSON.stringify(outs));
});

t('①-b 同前缀组内，标签里不能再以「示例项目 组」这种公共前缀开头', () => {
  GROUP.forEach(s => {
    const o = labText(s, GROUP);
    assert.ok(o.indexOf('示例项目') < 0,
      '标签还带着公共前缀「示例项目」：' + o + '（原文 ' + s + '）');
  });
});

t('①-c 同前缀组内，标签必须能区分到具体那一篇（含日期或差异段）', () => {
  const a = labText('示例项目 组合×手数复算_2026-09-30', GROUP);
  const b = labText('示例项目 组合×手数复算_2026-10-05', GROUP);
  assert.notStrictEqual(a, b, '两篇只差日期的笔记标签仍相同：' + a);
  // 至少要带上日期尾部的区分位
  assert.ok(/_2026-0/.test(a) || /2026-0/.test(a), '标签丢了日期段：' + a);
});

t('①-d 独占标题（没有同前缀伙伴）走普通省略，不参与去重', () => {
  const o = labText('知识库迭代记录 第 5 次', ['知识库迭代记录 第 5 次']);
  assert.ok(o.length > 0 && o.length <= 12, '独占标题不该被改坏：' + o);
});

t('② labPick 只给有边的大节点贴标签（孤立点不贴）', () => {
  // 口径（2026-10-18 修正）：原来写死 degree>=3，但生产实测全库 2015 节点里
  // 1899 个是 degree=0 的孤立点，degree>=3 只有 19 个 —— 屏幕上几乎不出现标签，
  // 等于把标签功能关掉。改成「degree>=1 且按度数取前 cap 个」。
  const nodes = [
    { id: 'a', title: '甲', degree: 10 },
    { id: 'b', title: '乙', degree: 2 },
    { id: 'c', title: '丙', degree: 3 },
    { id: 'd', title: '丁', degree: 0 },
  ];
  const out = labPick(nodes, 40);
  assert.ok(out.a, '大节点 a 没拿到标签');
  assert.ok(out.c, 'degree=3 的 c 应拿到标签');
  assert.ok(out.b, 'degree=2 有边，也该贴标签（旧口径漏掉太多）');
  assert.ok(!out.d, 'degree=0 的孤立点不该贴标签');
});

t('②-b labPick 默认不是全贴：150 节点里最多给 cap 个', () => {
  const nodes = [];
  for (let i = 0; i < 150; i++) nodes.push({ id: 'n' + i, title: '标题' + i, degree: 50 - (i % 50) });
  const out = labPick(nodes, 30);
  const n = Object.keys(out).length;
  assert.ok(n <= 30, '贴了 ' + n + ' 个标签，超过 cap=30');
});

t('②-c labPick 的标签也做了同前缀去重', () => {
  const nodes = GROUP.map((s, i) => ({ id: 'g' + i, title: s, degree: 20 - i }));
  const out = labPick(nodes, 40);
  const vals = Object.keys(out).map(k => out[k]);
  assert.strictEqual(new Set(vals).size, vals.length,
    'labPick 输出仍有重复标签：' + JSON.stringify(vals));
});

t('②-d 标题完全同名时靠路径区分，不能贴出两个一样的标签', () => {
  // 生产实测：chatgpt2api-服务.md 在 10-项目 和 50-档案 各有一份，标题一字不差
  const nodes = [
    { id: '10-项目/chatgpt2api/chatgpt2api-服务.md', title: 'chatgpt2api — OpenAI 兼容 API 代理池', degree: 2 },
    { id: '50-档案/空壳待定/chatgpt2api/chatgpt2api-服务.md', title: 'chatgpt2api — OpenAI 兼容 API 代理池', degree: 2 },
  ];
  const out = labPick(nodes, 40);
  const vals = Object.keys(out).map(k => out[k]);
  assert.strictEqual(new Set(vals).size, vals.length,
    '同名笔记标签仍然重复：' + JSON.stringify(vals));
  vals.forEach(v => assert.ok(v && v.length > 0, '标签不能为空'));
});

t('③ 默认视图上限必须是 150（维护者 2026-10-18 从 300 降下来）', () => {
  assert.strictEqual(lgOpts({}).cap, 150, '默认 cap 被改成 ' + lgOpts({}).cap);
});

t('③-b 勾「显示全部」时仍然不裁', () => {
  assert.strictEqual(lgOpts({ all: true }).cap, Infinity);
});

console.log('# pass ' + pass + ' / # fail ' + fail);
process.exit(fail ? 1 : 0);
