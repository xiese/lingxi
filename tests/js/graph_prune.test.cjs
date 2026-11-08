/**
 * 第 2 项知识图谱 —— 默认裁剪与请求参数（2026-10-18）。
 *
 * 这条测试是针对审查点出的「假绿」写的。原来 graph_prune.test.cjs 直接
 * 调 prune(g,300,false)，300 是测试自己写死的字面量——把 lgRefresh 里
 * 的默认值从 300 改成 3000，测试照样全绿。所以这里必须验证的是
 * 「默认值本身」和「请求参数本身」，不是「传 300 能裁」。
 *
 * 同样，语义边默认关（验收第 2 条）和 wiki 边默认开（第 3 条）也在这里
 * 用 lgQuery 的真实输出验证，不再靠读 index.html 的 checked 属性。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '../../scripts/web/app.js'), 'utf8');

// 从真实源码里截出函数体再执行——不是 grep 字符串断言。
// 抽不到就直接抛，避免"没找到就当通过"的静默假绿。
function grab(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(SRC);
  assert.ok(m, '在 app.js 里找不到函数 ' + name + '，测试无法验证真实代码');
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
for (const fn of ['lgPrune', 'lgStat', 'lgOpts', 'lgQuery']) {
  vm.runInContext(grab(fn), sandbox, {filename: fn + '.js'});
}

// 造一个与生产同形态的图：300 节点 / 120 边（wiki 21 + 语义 99）
function makeGraph() {
  const nodes = [];
  for (let i = 0; i < 300; i++) nodes.push({id: 'n' + i, degree: i % 37});
  const links = [];
  for (let i = 0; i < 120; i++) {
    links.push({
      source: 'n' + (i % 300),
      target: 'n' + ((i * 7 + 3) % 300),
      type: i < 21 ? 'wiki' : 'semantic'
    });
  }
  return {nodes, links, meta: {node_count: 300, link_count: 120}};
}

let pass = 0, fail = 0;
function t(name, fn) {
  try { fn(); pass++; console.log('ok   ' + name); }
  catch (e) { fail++; console.log('FAIL ' + name + ' -> ' + e.message); }
}

t('裁剪优先保留有边节点：孤立点不能挤掉有连线的节点', () => {
  // 真实形态：绝大多数节点 degree=0。若纯按 degree 排序取前 150，
  // 会混进 34 个孤立散点；应先把有边节点全留下，再用孤立点补足。
  const nodes = [];
  for (let i = 0; i < 50; i++) nodes.push({ id: 'iso' + i, title: '孤' + i, degree: 0 });
  for (let i = 0; i < 10; i++) nodes.push({ id: 'lnk' + i, title: '连' + i, degree: 2 });
  const p = sandbox.lgPrune({ nodes, links: [], meta: {} }, 20, false);
  const linked = p.nodes.filter(n => (n.degree || 0) >= 1).length;
  assert.strictEqual(linked, 10, '有边节点被孤立点挤掉了：只剩 ' + linked + ' 个');
  assert.strictEqual(p.nodes.length, 20, '裁剪后数量不对：' + p.nodes.length);
});

t('默认 cap 必须是 150（维护者 2026-10-18 从 300 降下来）', () => {
  const o = sandbox.lgOpts({});
  assert.strictEqual(o.cap, 150, '默认裁剪上限被改了：' + o.cap);
  assert.strictEqual(o.showAll, false);
});

t('勾「显示全部」时不裁剪', () => {
  const o = sandbox.lgOpts({all: true});
  assert.strictEqual(o.cap, Infinity);
  assert.strictEqual(o.showAll, true);
});

t('默认参数真的能裁到 150 个节点', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  assert.strictEqual(p.nodes.length, 150, '实际裁到 ' + p.nodes.length);
  assert.strictEqual(p.total, 300);
  assert.strictEqual(p.capped, true);
});

t('裁剪后每条边的两端都还在保留集里', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  const keep = new Set(p.nodes.map(n => n.id));
  for (const l of p.links) {
    assert.ok(keep.has(l.source), '边的一端被裁掉了: ' + l.source);
    assert.ok(keep.has(l.target), '边的一端被裁掉了: ' + l.target);
  }
});

t('显示全部时原样返回全部 300 个节点', () => {
  const g = makeGraph();
  const o = sandbox.lgOpts({all: true});
  const p = sandbox.lgPrune(g, o.cap, o.showAll);
  assert.strictEqual(p.nodes.length, 300);
  assert.strictEqual(p.capped, false);
});

t('默认请求里语义边必须是关的（验收第 2 条）', () => {
  const q = sandbox.lgQuery({minSim: 0.8, maxLinks: 8, sem: false, wiki: true});
  assert.ok(q.indexOf('include_semantic=0') >= 0, '语义边默认没关: ' + q);
});

t('默认请求里 wiki 边必须是开的（验收第 3 条）', () => {
  const q = sandbox.lgQuery({minSim: 0.8, maxLinks: 8, sem: false, wiki: true});
  assert.ok(q.indexOf('include_wiki=1') >= 0, 'wiki 边默认没开: ' + q);
});

t('勾上语义边后请求参数才变 1', () => {
  const q = sandbox.lgQuery({minSim: 0.8, maxLinks: 8, sem: true, wiki: true});
  assert.ok(q.indexOf('include_semantic=1') >= 0, q);
});

t('min_sim / max_links 原样带进请求', () => {
  const q = sandbox.lgQuery({minSim: 0.88, maxLinks: 4, sem: false, wiki: true});
  assert.ok(q.indexOf('min_sim=0.88') >= 0, q);
  assert.ok(q.indexOf('max_links=4') >= 0, q);
});

console.log('# pass ' + pass + ' / # fail ' + fail);
process.exit(fail ? 1 : 0);
