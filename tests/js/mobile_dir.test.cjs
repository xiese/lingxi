/**
 * 笔记页左侧目录按钮的常驻回归测试（2026-10-18 第 6 项改造）。
 *
 * 为什么需要它：`ltInvalidate()` 只把 `_ntItems` 置 null 不重拉，
 * 保存笔记后直接点「📂」会打开一个内容空白的面板。这个 bug 纯前端、
 * 只在真机上手点才看得见，后端测试永远抓不到——所以这里直接跑 app.js
 * 的 `toggleMobileDir()`，断言打开面板时真的把目录树拉下来渲染了。
 *
 * 跑法：node --test tests/js/mobile_dir.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

/**
 * 构造隔离环境。
 * @param {object} opts
 *   treeBody   /api/notes/tree 返回体（null 表示模拟接口报错）
 *   treeFails  true 时 fetch 直接 reject
 */
function makeSandbox(opts) {
  const treeBody = opts.treeBody === undefined ? { items: [] } : opts.treeBody;
  const stub = `
var __elCache = {};
var __calls = [];
function mkEl(id){
  return {
    _h:'', id:id, textContent:'', className:'', style:{}, dataset:{},
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    get value(){ return ''; }, set value(v){},
    classList:{add:function(){},remove:function(){},contains:function(){return false},toggle:function(){return false}},
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
var __tree = ${JSON.stringify(treeBody)};
var __treeFails = ${opts.treeFails ? 'true' : 'false'};
var fetch=function(url){
  __calls.push(String(url));
  if(__treeFails) return Promise.reject(new Error('network down'));
  var body = (String(url).indexOf('/notes/tree')>=0) ? __tree : {items:[],count:0,total:0};
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

const TREE = {
  items: [
    { path: '10-项目/知识库项目/方案.md', title: '方案' },
    { path: '20-知识/算法/笔记.md', title: '笔记' }
  ],
  count: 2
};

test('目录按钮：缓存为空时打开面板会重新拉树并渲染，不留空白', async () => {
  const ctx = makeSandbox({ treeBody: TREE });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    _ntItems = null;                     // 模拟刚保存完笔记：缓存被置空
    document.getElementById('nt').innerHTML = '';   // 面板内容为空
    toggleMobileDir();                   // 点「📂」
    await flush();
    return {
      fetched: __calls.filter(function(u){ return u.indexOf('/notes/tree')>=0; }).length,
      cached: Array.isArray(_ntItems) ? _ntItems.length : -1,
      html: document.getElementById('nt').innerHTML
    };
  `);

  assert.strictEqual(out.fetched, 1, '打开面板时应主动拉一次 /api/notes/tree，实际拉取次数 ' + out.fetched);
  assert.strictEqual(out.cached, 2, '_ntItems 应被填充为 2 条');
  assert.ok(out.html.length > 0, '面板 innerHTML 不能为空（空白就是原 bug）');
  assert.ok(/方案|笔记|知识/.test(out.html), '面板里应能看到目录内容：' + out.html.slice(0, 200));
  assert.ok(!/加载中/.test(out.html), '拉取成功后不应停留在「加载中」');
});

test('目录按钮：缓存已有数据时不重复拉接口', async () => {
  const ctx = makeSandbox({ treeBody: TREE });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    _ntItems = [{path:'20-知识/a.md',title:'a'}];
    toggleMobileDir();
    await flush();
    return { fetched: __calls.filter(function(u){ return u.indexOf('/notes/tree')>=0; }).length };
  `);
  assert.strictEqual(out.fetched, 0, '缓存有效时不该重复请求，实际 ' + out.fetched);
});

test('目录按钮：接口失败时显示可点重试，不静默空白', async () => {
  const ctx = makeSandbox({ treeFails: true });
  const out = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    _ntItems = null;
    document.getElementById('nt').innerHTML = '';
    toggleMobileDir();
    await flush();
    return { html: document.getElementById('nt').innerHTML, cached: _ntItems };
  `);
  assert.ok(/重试/.test(out.html), '失败时应提示可重试：' + out.html);
  assert.strictEqual(out.cached, null, '失败后缓存应保持为空，下次打开会重试');
});
