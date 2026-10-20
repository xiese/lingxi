/**
 * 日志页「加载更多 / 按类筛选 / 清理弹窗」的常驻回归测试（2026-10-18 第 4 项）。
 *
 * 为什么必须跑真代码：后端键名改对了、前端仍读旧字段，是这类改造最常见的假绿。
 * 双模型审查（DS 路）明确点过「前端第 1/2/3/5/7 条无 diff 内测试」，
 * 所以这里直接执行 app.js 的 ll() / llCat() / logCleanPreview() 看真实产出。
 *
 * 锁死的不变量：
 *   ① 首屏只取 50 条，且只渲染「加载更多」，不再出现一屏分页按钮
 *   ② 本页没取满就收掉「加载更多」——读日志本身会写一条审计记录，
 *      用 logLoaded<logTotal 判会永远差一条，按钮永远在（审查点出的真 bug）
 *   ③ 翻页不再重复拉 /admin/audit/stats
 *   ④ 类别按钮带 category=xxx 打到后端
 *   ⑤ 清理弹窗 all 模式也显示具体条数
 *
 * 跑法：node --test tests/js/log_page.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

/** 构造隔离环境；pages 是「第 N 页返回几条」的函数，calls 记录所有请求 URL。 */
function makeSandbox(pages, stats, catButtons) {
  const stub = `
var __elCache = {};
var __calls = [];
function mkEl(id){
  return {
    _h:'', id:id, textContent:'', className:'', style:{}, dataset:{}, options:{length:1},
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    value:'', checked:false,
    classList:{add:function(){},remove:function(){},contains:function(){return false},toggle:function(){}},
    appendChild:function(){}, removeChild:function(){}, remove:function(){},
    addEventListener:function(){}, setAttribute:function(){}, getAttribute:function(){return null},
    removeAttribute:function(){}, focus:function(){}, click:function(){},
    insertAdjacentHTML:function(pos,html){ this._h += html; },
    // logLoaded = body.querySelectorAll('tr').length —— 必须真数插入的行
    querySelectorAll:function(sel){
      if(sel==='tr'){ return (String(this._h).match(/<tr/g)||[]).map(function(){return {}}); }
      return [];
    },
    querySelector:function(){return null}
  };
}
var document = {
  getElementById: function(id){ if(!__elCache[id]) __elCache[id]=mkEl(id); return __elCache[id]; },
  createElement: function(){ return mkEl('__tmp__'); },
  addEventListener: function(){},
  querySelectorAll: function(sel){
    // 类别 chips：真返回带 data-cat/data-label 的按钮，否则无法验证「计数不叠加」
    if(String(sel).indexOf('log-cat')>=0) return __catBtns;
    return [];
  },
  querySelector: function(){ return null; },
  body:{appendChild:function(){}},
  documentElement:{classList:{add:function(){},remove:function(){}}}
};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}},clipboard:{}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var __pages = ${JSON.stringify(pages)};
var __stats = ${JSON.stringify(stats)};
var __catBtns = ${JSON.stringify(catButtons || [])}.map(function(b){
  return { _h:b.label, _label:b.label, _cat:b.cat,
    getAttribute:function(k){ if(k==='data-cat') return this._cat; if(k==='data-label') return this._label; return null; },
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    get textContent(){ return String(this._h).replace(/<[^>]*>/g,''); } };
});
var fetch=function(url){
  var u=String(url); __calls.push(u);
  var body;
  if(u.indexOf('/admin/audit/stats')>=0){ body = __stats; }
  else if(u.indexOf('/admin/audit/cleanup')>=0){ body = {will_delete:7}; }
  else if(u.indexOf('/admin/audit?')>=0){
    var m=u.match(/page=(\\d+)/); var pg=m?parseInt(m[1],10):1;
    var n=(__pages[String(pg)]!==undefined)?__pages[String(pg)]:0;
    var entries=[]; for(var i=0;i<n;i++){ entries.push({time:'2026-10-18T05:00:00',action:'notes',path:'/notes/x',device:'dev',ip:'127.0.0.1',status:200}); }
    body = {entries:entries, total:__stats.total, total_pages:1};
  } else { body = {items:[],count:0,total:0}; }
  return Promise.resolve({status:200,ok:true,json:function(){ return Promise.resolve(body); }});
};
var confirm=function(){return true;};
var CSS={escape:function(s){return String(s)}};
var setTimeout=function(fn){ if(typeof fn==='function') fn(); return 0; }; var clearTimeout=function(){};
var requestAnimationFrame=function(fn){return 0;};
var marked={setOptions:function(){return marked},use:function(){return marked},
  parse:function(x){return '<p>'+String(x==null?'':x)+'</p>'},
  parseInline:function(x){return String(x==null?'':x)},
  Renderer:function(){this.html=function(){return ''};this.link=function(){return ''};
    this.image=function(){return ''};this.heading=function(){return ''};
    this.paragraph=function(t){return '<p>'+t+'</p>'};this.code=function(){return ''};}};
`;
  const sandbox = { console, setTimeout: () => 0, clearTimeout: () => {}, Promise };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(stub, ctx, { filename: 'stub.js' });
  vm.runInContext(appSrc, ctx, { filename: 'app.js' });
  return ctx;
}

function runInSandbox(ctx, code) {
  return vm.runInContext(
    `(async () => { const flush = async function(n){ n=n||80; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx,
    { filename: 'test-body.js' }
  );
}

test('首屏只取 50 条，只给「加载更多」，不再出现分页按钮', async () => {
  // 第 1 页满 50 条 → 还应能继续加载
  const ctx = makeSandbox({ 1: 50 }, { total: 39468, today: 12, devices: { a: 1, b: 2 }, actions: { notes: 5 }, categories: {} });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ll(1); await flush();
    return {
      count: document.getElementById('logCount').textContent,
      pg: document.getElementById('logPg').innerHTML,
      rows: (document.getElementById('logBody').innerHTML.match(/<tr/g)||[]).length,
      urls: __calls.slice()
    };
  `);

  assert.ok(out.rows === 50, '首屏应渲染 50 行，实际 ' + out.rows);
  assert.ok(/加载更多/.test(out.pg), '满页时应给「加载更多」：' + out.pg);
  assert.ok(!/1<\/button>/.test(out.pg), '不应再出现数字分页按钮：' + out.pg);
  assert.ok(/limit=50/.test(out.urls[0]), '首屏 limit 应为 50：' + out.urls[0]);
  assert.ok(/共 39468 条/.test(out.count), '计数应显示总数：' + out.count);
});

test('本页没取满就收掉「加载更多」——不会出现点不完的按钮', async () => {
  // 第 1 页满 50，第 2 页只有 3 条：total 是 99999（模拟每读一次就涨一条）
  const ctx = makeSandbox({ 1: 50, 2: 3 }, { total: 99999, today: 0, devices: {}, actions: {}, categories: {} });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ll(1); await flush();
    ll(2); await flush();
    return {
      pg: document.getElementById('logPg').innerHTML,
      rows: (document.getElementById('logBody').innerHTML.match(/<tr/g)||[]).length
    };
  `);

  assert.ok(out.rows === 53, '两页应累加 53 行，实际 ' + out.rows);
  assert.ok(!/加载更多/.test(out.pg), '本页没取满就不该再给按钮（否则永远差一条）：' + out.pg);
  assert.ok(/已全部加载/.test(out.pg), '应提示已全部加载：' + out.pg);
});

test('翻页不再重复拉 /admin/audit/stats', async () => {
  const ctx = makeSandbox({ 1: 50, 2: 50 }, { total: 500, today: 0, devices: {}, actions: {}, categories: {} });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ll(1); await flush();
    ll(2); await flush();
    return { stats: __calls.filter(function(u){return u.indexOf('/admin/audit/stats')>=0}).length };
  `);

  assert.strictEqual(out.stats, 1, '统计接口只在首屏拉一次，翻页不该再打，实际 ' + out.stats);
});

test('类别筛选把 category 带给后端', async () => {
  const ctx = makeSandbox({ 1: 4 }, { total: 10, today: 0, devices: {}, actions: {}, categories: { auth: 4 } });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    llCat('auth'); await flush();
    return { urls: __calls.slice() };
  `);

  const hit = out.urls.filter(function(u){ return u.indexOf('/admin/audit?') >= 0; });
  assert.ok(hit.length > 0, '应发出日志查询请求');
  assert.ok(/category=auth/.test(hit[0]), '类别应作为 category 参数传给后端：' + hit[0]);
});

test('清理弹窗 all 模式也显示具体条数', async () => {
  const ctx = makeSandbox({ 1: 0 }, { total: 39468, today: 0, devices: {}, actions: {}, categories: {} });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    var sel=document.getElementById('logCleanDays'); sel.value='all';
    logCleanPreview(); await flush();
    return { txt: document.getElementById('logCleanWill').textContent };
  `);

  assert.ok(/39468/.test(out.txt), 'all 模式应显示具体条数，不能只说「全部记录」：' + out.txt);
});

test('类别标签计数不会越刷越长（glm 审查点出的真 bug）', async () => {
  // 复现原 bug 的条件：按钮首次 textContent = 「全部」，渲染一次后变成「全部39468」，
  // 第二次从 textContent 反取标签名就会把数字并进标签，每刷一次长一截。
  const ctx = makeSandbox(
    { 1: 0 },
    { total: 39468, today: 0, devices: {}, actions: {}, categories: { read: 100, write: 20, auth: 7, system: 3 } },
    [{ cat: '', label: '全部' }, { cat: 'read', label: '读操作' },
     { cat: 'write', label: '写操作' }, { cat: 'auth', label: '认证' },
     { cat: 'system', label: '系统' }]
  );
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ll(1); await flush();
    ll(1); await flush();
    ll(1); await flush();
    return { texts: __catBtns.map(function(b){ return b.textContent; }) };
  `);

  // 刷三次后，标签文字仍应恰好是「全部39468」这种形态，不能出现两个数字
  const all = out.texts[0];
  assert.strictEqual(all, '全部39468', '「全部」标签刷三次后应仍是 全部39468，实际 ' + all);
  assert.strictEqual(out.texts[3], '认证7', '「认证」标签刷三次后应仍是 认证7，实际 ' + out.texts[3]);
  out.texts.forEach(function(t){
    const nums = String(t).match(/\d+/g) || [];
    assert.strictEqual(nums.length, 1, '每个标签只应有一个数字，实际「' + t + '」');
  });
});
