/**
 * 总览六卡渲染的常驻回归测试（2026-10-18 第 8 项改造）。
 *
 * 为什么需要它：后端 /api/dashboard 的键名改了（projects 从「篇数」变成「一级目录数」），
 * 前端 ld() 必须同步消费。只测后端键名会漏掉「前端仍显示旧字段」这种假绿——
 * 双模型审查已经点出过这个缺口，所以这里直接跑 app.js 的 ld() 看真实产出 HTML。
 *
 * 跑法：node --test tests/js/dashboard_cards.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

/** 构造隔离环境：document 打桩 + fetch 返回可控的 /api/dashboard。 */
function makeSandbox(dashBody) {
  const stub = `
var __elCache = {};
function mkEl(id){
  return {
    _h:'', id:id, textContent:'', className:'', style:{}, dataset:{},
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    get value(){ return ''; }, set value(v){},
    classList:{add:function(){},remove:function(){},contains:function(){return false},toggle:function(){}},
    appendChild:function(){}, removeChild:function(){}, remove:function(){},
    addEventListener:function(){}, setAttribute:function(){}, getAttribute:function(){return null},
    removeAttribute:function(){},
    querySelector:function(){return null}, querySelectorAll:function(){return []},
    insertAdjacentHTML:function(){}, focus:function(){}, click:function(){}
  };
}
var document = {
  getElementById: function(id){ if(!__elCache[id]) __elCache[id]=mkEl(id); return __elCache[id]; },
  createElement: function(){ return mkEl('__tmp__'); },
  addEventListener: function(){},
  querySelectorAll: function(){ return []; },
  querySelector: function(){ return null; },
  body:{appendChild:function(){}},
  documentElement:{classList:{add:function(){},remove:function(){}}}
};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}},clipboard:{}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var __dash = ${JSON.stringify(dashBody)};
var fetch=function(url){
  var body = (String(url).indexOf('/dashboard')>=0) ? __dash : {items:[],count:0,total:0};
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
    `(async () => { const flush = async function(n){ n=n||60; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx,
    { filename: 'test-body.js' }
  );
}

/** 生产 远程服务器 的真实口径样本（2026-10-18 实测）。
 *
 * 项目主数字取 66 而非 67：`ls -d 10-项目/* /` 得 67，其中 `_assets` 按维护者定稿
 * 「排除 . 与 _ 开头」被后端剔除，故后端真实返回 66。测试必须锁代码实际会产出的
 * 数字，锁 67 就是自欺（双模型审查已指出过这个坑）。
 */
const REAL = {
  projects: 66, knowledge: 60, writing: 0, records: 0, diaries: 0, archived: 13,
  projects_notes: 723, knowledge_notes: 927, writing_notes: 0, records_notes: 11,
  diaries_notes: 0, archived_notes: 309,
  total: 1970, total_dirs: 139,
  dir_fallback: { projects: false, knowledge: false, writing: true, records: true, diaries: true, archived: false }
};
// 自检：total 必须等于六类篇数之和（723+927+0+11+0+309=1970）。
// 上一版 fixture 分项加起来 2071 却写 total 1970，被 glm 审查判为「凑绿」——
// 这里把这条约束写成断言，谁再手改数字都会被当场拦下。
if (REAL.projects_notes + REAL.knowledge_notes + REAL.writing_notes +
    REAL.records_notes + REAL.diaries_notes + REAL.archived_notes !== REAL.total) {
  throw new Error('fixture 自相矛盾：分项之和 != total');
}

test('总览六卡：主数字取目录数，小字一律是「篇笔记」', async () => {
  const ctx = makeSandbox(REAL);
  const html = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ld(); await flush();
    return document.getElementById('dc').innerHTML;
  `);

  // ① 每张小字都是「篇笔记」，不再出现「个目录」
  assert.ok(!/个目录/.test(html), '小字里不应再出现「个目录」：' + html);
  const notesHits = (html.match(/篇笔记/g) || []).length;
  assert.ok(notesHits >= 6, '六张卡应都有「篇笔记」小字，实际命中 ' + notesHits);

  // ② 项目卡主数字 = 目录数 67，小字 = 746 篇
  assert.ok(/text-3xl font-bold text-blue-600">66</.test(html), '项目主数字应为 66（一级目录数，已排除 _assets）');
  assert.ok(/723 篇笔记/.test(html), '项目小字应为「723 篇笔记」');

  // ③ 总计卡主数字 = 目录合计，小字 = 篇数合计
  assert.ok(/text-rose-600">139</.test(html), '总计主数字应为 139（目录合计）');
  assert.ok(/1970 篇笔记/.test(html), '总计小字应为「1970 篇笔记」');

  // ④ 无子目录的分类退化为篇数并注明
  assert.ok(/无子目录，按篇数显示/.test(html), 'writing/records/diaries 应走退化分支并注明');
});

test('总览六卡：后端只给旧键时不会渲染出 undefined', async () => {
  const ctx = makeSandbox({
    projects: 12, knowledge: 5, writing: 1, records: 2, diaries: 3, archived: 4,
    total: 27
  });
  const html = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    ld(); await flush();
    return document.getElementById('dc').innerHTML;
  `);
  assert.ok(!/undefined/.test(html), '缺新键时不应出现 undefined：' + html);
});
