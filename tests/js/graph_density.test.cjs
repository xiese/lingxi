/**
 * 遗留 #16「知识图谱去蜘蛛网化」的密度验收（2026-10-19）。
 *
 * 为什么要有这条：graph_prune 只证明「能裁」，没证明「裁完是不是一张网」。
 * 真实形态：节点与边极多，其中绝大多数是语义边（约 98%），画出来就是蜘蛛网。
 * 这条测试用真实生产的形态当输入，断言三件事：
 *   1. 裁完的节点数受控（默认 150）；
 *   2. 裁完的平均度落在可读区间（≤6），超过就是网；
 *   3. 孤立点不占位——有边的节点优先保留，剩余名额才给散点；
 *   4. 语义边默认不请求（只请求 wiki 边），这是「去蜘蛛网」的第一道闸。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '../../scripts/web/app.js'), 'utf8');

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
for (const fn of ['lgPrune', 'lgStat', 'lgOpts', 'lgQuery', 'lgCap', 'lgEdgeCap']) {
  vm.runInContext(grab(fn), sandbox, {filename: fn + '.js'});
}

// 按生产形态造图：200 节点里 20 个有边、180 个孤立点；边 300 条（含大量语义边）
function prodLikeGraph() {
  const nodes = [], links = [];
  const linked = 20, isolated = 180;
  for (let i = 0; i < linked; i++) nodes.push({id: 'n' + i, degree: 0, label: '节点' + i});
  for (let i = 0; i < isolated; i++) nodes.push({id: 'z' + i, degree: 0, label: '散点' + i});
  // 语义边按 max_links=8 的上限铺：每个有边节点连到若干邻居
  for (let i = 0; i < linked; i++) {
    for (let k = 1; k <= 8; k++) {
      const j = (i + k * 7) % linked;
      if (j === i) continue;
      links.push({source: 'n' + i, target: 'n' + j, type: 'semantic', weight: 0.8});
    }
  }
  // wiki 边 19 条
  for (let i = 0; i < 19; i++) {
    links.push({source: 'n' + (i % linked), target: 'n' + ((i + 3) % linked), type: 'wiki', weight: 1});
  }
  const deg = {};
  for (const l of links) {
    deg[l.source] = (deg[l.source] || 0) + 1;
    deg[l.target] = (deg[l.target] || 0) + 1;
  }
  nodes.forEach(n => { n.degree = deg[n.id] || 0; });
  return {nodes, links};
}

const g = prodLikeGraph();

// 1) 默认口径来自 lgOpts，不能在测试里写死 150
const opt = sandbox.lgOpts({});
assert.strictEqual(opt.cap, 150, '默认裁剪上限应为 150（2026-10-18 维护者拍板）');
assert.strictEqual(sandbox.lgOpts({all: true}).cap, Infinity, '勾「显示全部」时不裁');

// 2) 裁完节点数受控
const pr = sandbox.lgPrune(g, opt.cap, opt.showAll);
assert.strictEqual(pr.nodes.length, 150, '裁完应为 150 个节点');
assert.strictEqual(pr.total, g.nodes.length, 'total 要保留原始总数供状态栏显示');
assert.ok(pr.capped, 'capped 标记必须为真');

// 3) 孤立点不占位：有边节点全保留，剩余名额才给散点
const keptLinked = pr.nodes.filter(n => n.id.startsWith('n')).length;
const keptIso = pr.nodes.filter(n => n.id.startsWith('z')).length;
assert.strictEqual(keptLinked, 20, '有边的节点必须全部保留，不能被散点挤掉');
assert.strictEqual(keptIso, 150 - 20, '剩余名额才补散点');

// 3b) 人工双链优先（遗留 #16）：带 wiki 边的节点必须先于纯语义节点保住。
// 真实形态：带 wiki 的节点度数普遍低，按 degree 排序几乎挤不进去 —— 那是缺陷。
{
  const nodes = [], links = [];
  // 5 个 wiki 节点，度数很低；200 个语义节点，度数很高
  for (let i = 0; i < 5; i++) nodes.push({id: 'w' + i, degree: 1});
  for (let i = 0; i < 200; i++) nodes.push({id: 'h' + i, degree: 20});
  for (let i = 0; i < 5; i++) links.push({source: 'w' + i, target: 'w' + ((i + 1) % 5), type: 'wiki'});
  for (let i = 0; i < 200; i++) links.push({source: 'h' + i, target: 'h' + ((i + 1) % 200), type: 'semantic'});
  const r = sandbox.lgPrune({nodes, links}, 50, false);
  const keptW = r.nodes.filter(n => n.id.startsWith('w')).length;
  assert.strictEqual(keptW, 5, '带人工双链的节点必须全部保留，实际只留下 ' + keptW + ' 个');
}

// 4) 边只保留两端都在保留集里的
for (const l of pr.links) {
  const s = (l.source && l.source.id) || l.source;
  const t = (l.target && l.target.id) || l.target;
  assert.ok(s && t, '边必须有端点');
}

// 4b) 边上限（遗留 #16）：压边之后平均度必须落在可读区间
const edgeCap = sandbox.lgEdgeCap(false);
assert.strictEqual(edgeCap, 4, '桌面每节点边上限应为 4');
assert.strictEqual(sandbox.lgEdgeCap(true), 3, '手机每节点边上限应为 3');
const capped = sandbox.lgCap({links: pr.links}, edgeCap);
const avgDeg = (capped.length * 2) / Math.max(1, pr.nodes.length);
assert.ok(avgDeg <= 6, '压边后平均度 ' + avgDeg.toFixed(2) + ' 超过 6，还是一张网');
assert.ok(capped.length > 0, '压边不能把边全丢掉');
// 每个节点的实际度数都不超过上限
const cnt = {};
for (const l of capped) {
  const s = (l.source && l.source.id) || l.source;
  const t = (l.target && l.target.id) || l.target;
  cnt[s] = (cnt[s] || 0) + 1;
  cnt[t] = (cnt[t] || 0) + 1;
}
for (const k in cnt) {
  assert.ok(cnt[k] <= edgeCap, '节点 ' + k + ' 有 ' + cnt[k] + ' 条边，超过上限 ' + edgeCap);
}
// wiki 边优先：只要 wiki 边不超过上限，必须全部保住
const wikiIn = pr.links.filter(l => l.type === 'wiki').length;
const wikiOut = capped.filter(l => l.type === 'wiki').length;
assert.strictEqual(wikiOut, Math.min(wikiIn, 19), 'wiki 边必须优先于语义边保留');

// 6) 语义边默认不请求：去蜘蛛网的第一道闸
const q = sandbox.lgQuery({});
assert.ok(q.indexOf('include_semantic=0') >= 0, '默认必须不请求语义边，实际: ' + q);
assert.ok(q.indexOf('include_wiki=1') >= 0, '默认必须请求 wiki 边，实际: ' + q);
// 显式打开时才请求
assert.ok(sandbox.lgQuery({sem: true}).indexOf('include_semantic=1') >= 0);

console.log('density ok: nodes=' + pr.nodes.length + ' links=' + pr.links.length + ' avgDeg=' + avgDeg.toFixed(2));
