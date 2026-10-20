/**
 * 第 7-1 项前端回归：断链标注 + 反向链接面板。
 *
 * 为什么必须有这个测试：这两块全是「渲染完再异步补」的逻辑，
 * 出错时被 rn() 的 .catch 静默吞掉，页面上只是「看起来没标红」，
 * 靠肉眼根本分不清是「没有断链」还是「代码没跑」。所以直接跑真函数。
 *
 * 覆盖：
 *   ① markBrokenLinks：解析到 null 的加 .wl-broken，解析到的去掉该类
 *   ② markBrokenLinks：解析结果里没这个 key（后端漏返回）时不误标断链
 *   ③ resolveLinksIn：N 个双链只发 1 次批量请求（不是 N 次逐个解析）
 *   ④ resolveLinksIn：请求体带 from 与去重后的 targets
 *   ⑤ renderBacklinks：无数据时清空面板，有数据时渲染条数与路径
 *
 * 跑法：node --test tests/js/wikilink_broken.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

/** 一个够用的假元素：只实现 markBrokenLinks 用到的 class/attribute。 */
function mkLink(target) {
  const cls = {};
  const attrs = { 'data-wl': target };
  return {
    _cls: cls,
    classList: {
      add: (c) => { cls[c] = 1; },
      remove: (c) => { delete cls[c]; },
      contains: (c) => !!cls[c],
    },
    getAttribute: (k) => (attrs[k] !== undefined ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = v; },
  };
}

function mkContainer(links) {
  return {
    innerHTML: '',
    querySelectorAll: (sel) => (sel === '[data-wl]' ? links : []),
  };
}

function makeSandbox() {
  const stub = `
var __requests = [];
var __resp = {};
// 沙箱内用的假双链元素（与文件外层 mkLink 同构，但必须在 vm 里可见）
function mkLink(target){
  var cls={}, attrs={'data-wl':target};
  return { classList:{add:function(c){cls[c]=1},remove:function(c){delete cls[c]},contains:function(c){return !!cls[c]}},
           getAttribute:function(k){return attrs[k]!==undefined?attrs[k]:null},
           setAttribute:function(k,v){attrs[k]=v} };
}
var document = {
  getElementById: function(){ return { innerHTML:'', classList:{add:function(){},remove:function(){},contains:function(){return false}} }; },
  createElement: function(){ return { style:{}, classList:{add:function(){},remove:function(){}} }; },
  addEventListener: function(){}, querySelectorAll: function(){ return []; }, querySelector: function(){ return null; },
  body:{appendChild:function(){},removeChild:function(){}}
};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var fetch=function(url,opt){
  __requests.push({url:url, body:(opt&&opt.body)||null});
  var body=__resp[url]!==undefined?__resp[url]:{};
  return Promise.resolve({status:200,ok:true,json:function(){return Promise.resolve(body)}});
};
var setTimeout=function(fn){return 0;}; var clearTimeout=function(){};
var CSS={escape:function(s){return String(s)}};
var marked={setOptions:function(){return marked},use:function(){return marked},parse:function(x){return String(x)},
  parseInline:function(x){return String(x)},Renderer:function(){}};
`;
  const sandbox = { console, setTimeout: () => 0, clearTimeout: () => {}, Promise };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(stub, ctx, { filename: 'stub.js' });
  vm.runInContext(appSrc, ctx, { filename: 'app.js' });
  return ctx;
}

function run(ctx, code) {
  return vm.runInContext(`(async () => { const flush = async function(n){ n=n||40; for(let i=0;i<n;i++) await null; }; ${code} })()`, ctx);
}

// ── ① 断链标红 / 正常链接不标 ──

test('断链标注：解析为 null 的加 wl-broken，解析到的去掉', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    var ok = mkLink('甲'), bad = mkLink('不存在');
    bad.classList.add('wl-broken');           // 模拟上一次渲染残留
    var box = { innerHTML:'', querySelectorAll:function(s){ return s==='[data-wl]'?[ok,bad]:[] } };
    var n = markBrokenLinks(box, {'甲':'20-知识/甲.md','不存在':null});
    return { n:n, okBroken:ok.classList.contains('wl-broken'), badBroken:bad.classList.contains('wl-broken') };
  `);
  assert.strictEqual(res.n, 1, '只有 1 个断链');
  assert.strictEqual(res.okBroken, false, '能解析的不该标红');
  assert.strictEqual(res.badBroken, true, '解析不到的要标红');
});

// ── ② 后端漏返回的 key 不误标 ──

test('断链标注：解析结果缺该目标时不误判为断链', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    var a = mkLink('未返回的目标');
    var box = { querySelectorAll:function(s){ return s==='[data-wl]'?[a]:[] } };
    var n = markBrokenLinks(box, {});
    return { n:n, broken:a.classList.contains('wl-broken') };
  `);
  assert.strictEqual(res.n, 0, '没返回 ≠ 断链，不能标红');
  assert.strictEqual(res.broken, false);
});

// ── ③ 只发一次批量请求 ──

test('批量解析：多个双链只发 1 次请求', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    var ls=[mkLink('甲'),mkLink('乙'),mkLink('丙'),mkLink('甲')];
    var box={ querySelectorAll:function(s){ return s==='[data-wl]'?ls:[] } };
    __resp['http://127.0.0.1:18900/api/links/resolve-batch']={resolved:{'甲':'a.md','乙':'b.md','丙':null}};
    var n = await resolveLinksIn(box,'20-知识/来源.md');
    return { reqs:__requests.length, n:n,
             body:JSON.parse(__requests[0].body),
             broken:ls.filter(function(e){return e.classList.contains('wl-broken')}).length };
  `);
  assert.strictEqual(res.reqs, 1, '4 个双链必须只发 1 次批量请求，不能逐个解析');
  assert.strictEqual(res.n, 1);
  assert.strictEqual(res.broken, 1);
  // 沙箱里 Array.prototype.sort 与宿主 realm 不同，用 join 比较（顺序无关）
  assert.strictEqual(res.body.targets.slice().sort().join(','), ['甲', '乙', '丙'].sort().join(','), 'targets 应去重');
  assert.strictEqual(res.body.from, '20-知识/来源.md', '必须带上来源路径（同目录优先解析要用）');
});

// ── ④ 没有双链时不发请求 ──

test('批量解析：正文无双链时零请求', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    var box={ querySelectorAll:function(){ return [] } };
    var n = await resolveLinksIn(box,'x.md');
    return { reqs:__requests.length, n:n };
  `);
  assert.strictEqual(res.reqs, 0);
  assert.strictEqual(res.n, 0);
});

// ── ⑤ 反链面板渲染 ──

test('反链面板：有数据显示条数与路径，无数据清空', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    var p={ innerHTML:'' };
    renderBacklinks(p,[{path:'10-项目/A.md',title:'A'},{path:'20-知识/B.md',title:'B'}],'20-知识/目标.md');
    var withRows=p.innerHTML;
    renderBacklinks(p,[],'20-知识/目标.md');
    return { withRows:withRows, empty:p.innerHTML };
  `);
  assert.ok(res.withRows.includes('反向链接 (2)'), '应显示条数：' + res.withRows.slice(0, 120));
  assert.ok(res.withRows.includes('10-项目/A.md'), '应显示来源路径');
  assert.strictEqual(res.empty, '', '无反链时面板应清空');
});

// ── ⑥ 代码段里的 [[...]] 不得变成双链 ──

test('代码段内的 [[...]] 保持字面量，不生成 data-wl', () => {
  const ctx = makeSandbox();
  const md = [
    '正文示例：[[真链接]]',
    '行内代码：`[[示例甲]]`',
    '```',
    '[[示例乙]]',
    '```',
    '结尾：[[真链接二]]',
  ].join('\n');
  ctx.__md = md;
  const res = vm.runInContext('({ html: m2(__md) })', ctx);
  assert.ok(res.html.includes('[[示例甲]]'), '行内代码里的示例不该被替换成链接：' + res.html.slice(0, 300));
  assert.ok(res.html.includes('[[示例乙]]'), '代码块里的示例不该被替换成链接');
  assert.ok(res.html.includes('data-wl="真链接"'), '正文双链应生成 data-wl');
  assert.ok(res.html.includes('data-wl="真链接二"'), '正文双链应生成 data-wl');
});
