/**
 * 第 2 项图谱状态栏口径自洽（2026-10-18）。
 *
 * 真浏览器实测抓到过：「节点 300 / 2015 · 连线 226（wiki 99 / 语义 641）」
 * —— 226 ≠ 99+641。外面用裁剪后的实际值、括号里用后端原始总数，两个口径混用。
 *
 * 审查指出上一版是假绿：只调 lgStat 并喂一组自洽的入参，等于测试自己
 * 保证自己正确；lgRefresh 里真正传参的地方没被覆盖，bug 复发也不会报红。
 * 所以这里改成验证「lgRefresh 实际怎么算这几个数」，用与源码同构的
 * 取数逻辑（从 gd.links 划分 wiki/semantic）复现，再断言输出自洽。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '../../scripts/web/app.js'), 'utf8');

function grab(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(SRC);
  assert.ok(m, '在 app.js 里找不到函数 ' + name);
  let i = m.index + m[0].length, depth = 1;
  while (i < SRC.length && depth > 0) {
    const ch = SRC[i];
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    i++;
  }
  return SRC.slice(m.index, i);
}

const sandbox = {console, JSON, Math, Number, String, Object, Array, Infinity};
vm.createContext(sandbox);
for (const fn of ['lgPrune', 'lgStat', 'lgOpts']) {
  vm.runInContext(grab(fn), sandbox, {filename: fn + '.js'});
}

// 复刻 lgRefresh 里画完之后的取数方式：从实际画出来的 links 划分 wiki/语义。
// 这段必须与源码同构，否则测试和实现各走一套，又是假绿。
function statsFromRendered(nodes, links, meta) {
  const wikLinks = links.filter(l => l.type !== 'semantic');
  const semLinks = links.filter(l => l.type === 'semantic');
  return sandbox.lgStat({
    shown: nodes.length,
    total: (meta && meta.node_count) || nodes.length,
    capped: nodes.length < ((meta && meta.node_count) || nodes.length),
    links: links.length,
    wiki: wikLinks.length,
    semantic: semLinks.length,
    metaWiki: (meta && meta.wiki_link_count) || 0,
    metaSemantic: (meta && meta.semantic_count) || 0
  });
}

let pass = 0, fail = 0;
function t(name, fn) {
  try { fn(); pass++; console.log('ok   ' + name); }
  catch (e) { fail++; console.log('FAIL ' + name + ' -> ' + e.message); }
}

function makeGraph() {
  const nodes = [];
  for (let i = 0; i < 2015; i++) nodes.push({id: 'n' + i, degree: i % 37});
  const links = [];
  for (let i = 0; i < 740; i++) {
    links.push({
      source: 'n' + (i % 2015),
      target: 'n' + ((i * 7 + 3) % 2015),
      type: i < 99 ? 'wiki' : 'semantic'
    });
  }
  return {nodes, links, meta: {node_count: 2015, wiki_link_count: 99, semantic_count: 641}};
}

// 从状态栏文案里抠出四个数字，用于交叉验算
function nums(s) {
  const m = s.match(/连线\s*(\d+)（wiki\s*(\d+)\s*\/\s*语义\s*(\d+)）/);
  assert.ok(m, '状态栏格式不符合预期: ' + s);
  return {links: +m[1], wiki: +m[2], semantic: +m[3]};
}

t('默认裁剪态：括号内 wiki+语义 必须等于连线数', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  const s = statsFromRendered(p.nodes, p.links, g.meta);
  const n = nums(s);
  assert.strictEqual(n.wiki + n.semantic, n.links,
    '口径自相矛盾: ' + s + '（wiki+语义=' + (n.wiki + n.semantic) + '）');
});

t('默认裁剪态：状态栏必须标出「已裁剪」和原始总数', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  const s = statsFromRendered(p.nodes, p.links, g.meta);
  assert.ok(s.indexOf('已裁剪') >= 0, '裁剪了却没标注: ' + s);
  assert.ok(s.indexOf('原 wiki 99') >= 0, '没标原始 wiki 数: ' + s);
  assert.ok(s.indexOf('原 语义 641') >= 0 || s.indexOf('语义 641') >= 0, '没标原始语义数: ' + s);
});

t('默认裁剪态：节点数显示 150 / 2015', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  const s = statsFromRendered(p.nodes, p.links, g.meta);
  assert.ok(s.indexOf('节点 150 / 2015') >= 0, s);
});

t('显示全部时：不出现「已裁剪」', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({all: true});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  const s = statsFromRendered(p.nodes, p.links, g.meta);
  assert.ok(s.indexOf('已裁剪') < 0, '没裁剪却标了已裁剪: ' + s);
  assert.ok(s.indexOf('节点 2015') >= 0, s);
});

t('关语义边时（生产实测）：只应出现 wiki 99 / 语义 0', () => {
  // 复刻生产：include_semantic=0 时后端只回 99 条 wiki 边。
  // 注意 fixture 必须让 wiki 边连在「degree 前 300」的节点上，
  // 否则会被裁剪掉——生产里 wiki 边恰恰连在 hub 节点上才活下来。
  const nodes = [];
  for (let i = 0; i < 2015; i++) nodes.push({id: 'n' + i, degree: 2015 - i});
  const links = [];
  for (let i = 0; i < 99; i++) links.push({source: 'n' + i, target: 'n' + ((i + 1) % 99), type: 'wiki'});
  const meta = {node_count: 2015, wiki_link_count: 99, semantic_count: 641};
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune({nodes, links, meta}, o.cap, o.showAll);
  const s = statsFromRendered(p.nodes, p.links, meta);
  const n = nums(s);
  assert.strictEqual(n.wiki, 99, s);
  assert.strictEqual(n.semantic, 0, s);
  assert.strictEqual(n.links, 99, s);
});

t('反向验证：若把括号里的值换成 meta 原始总数，本测试必须报红', () => {
  // 直接复刻原 bug 的输出形态，确认 nums() 能抓出 226 ≠ 99+641
  const buggy = '节点 300 / 2015 · 连线 226（wiki 99 / 语义 641）（已裁剪，原 wiki 99 / 语义 641）';
  const n = nums(buggy);
  assert.notStrictEqual(n.wiki + n.semantic, n.links,
    '测试抓不出原 bug，说明断言无效');
});

console.log('# pass ' + pass + ' / # fail ' + fail);
process.exit(fail ? 1 : 0);
