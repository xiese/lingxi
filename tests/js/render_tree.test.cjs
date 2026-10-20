/**
 * 前端笔记树与移动端抽屉的常驻回归测试（零依赖：node:test + 轻量 DOM 打桩）
 *
 * 为什么需要它：这两块行为（树折叠/懒渲染、移动端抽屉关闭时机）此前只靠一次性
 * 手写 harness 验证，用完即弃。实证教训——`rn()` 的 `.catch` 会静默吞掉 `sn()`
 * 内部的异常，使验证得出与事实完全相反的结论。所以必须有可重复执行的检查。
 *
 * 跑法（注意：目录形式 `node --test tests/js/` 会报 MODULE_NOT_FOUND，别用）：
 *     node --test                           # 自动发现所有 .test.cjs
 *     node --test tests/js/render_tree.test.cjs   # 只跑前端测试
 *
 * 注意：glob 模式（如 tests/js/加通配符）在 subprocess 里不会展开（不经 shell），
 * 所以 pytest 桥与 CI 都应传显式文件路径或依赖 --test 的自动发现。
 *
 * 设计要点（都踩过坑，勿简化）：
 *   1. app.js 的顶层 `let A/T` 是 script 级词法声明，跨 script 不共享 —— 必须把
 *      「打桩 + app.js 源码 + 测试代码」拼成同一个 script 一次执行。
 *   2. `marked` 必须打桩，否则 renderNoteHtml → m2 → marked.setOptions 抛异常，
 *      被 rn() 的 .catch 吞掉，表现为「改动没生效」。
 *   3. getElementById 必须返回**缓存对象**，否则写进去的状态读不回来。
 *   4. querySelectorAll 必须复刻真实选择器语义：'.note-dir-panel,#tocPanel' 会
 *      同时命中两个元素。
 *   5. `lt()` 内部是 ap().then(...)，本身不返回 Promise —— 必须手动冲刷微任务队列。
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

/** 构造一个隔离的 vm 环境，把 app.js 与其依赖一起装进去。 */
function makeSandbox() {
  const stub = `
var __filter = '';
var __requested = [];
var __fetchCount = 0;
var __treeItems = [];
var __elCache = {};
function mkEl(id){
  return {
    _h:'', _cls:{}, _attrs:{},
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    get value(){ return id==='noteFilter' ? __filter : (this._v||''); }, set value(v){ this._v=v; },
    textContent:'', className:'', style:{}, dataset:{}, id:id, tagName:'DIV',
    classList:{
      add:function(c){ var e=__elCache[id]; if(e) e._cls[c]=1; },
      remove:function(c){ var e=__elCache[id]; if(e) delete e._cls[c]; },
      contains:function(c){ var e=__elCache[id]; return !!(e && e._cls[c]); },
      toggle:function(c){ var e=__elCache[id]; if(e && e._cls[c]){ delete e._cls[c]; return false; } if(e) e._cls[c]=1; return true; }
    },
    appendChild:function(){}, removeChild:function(){}, remove:function(){}, addEventListener:function(){},
    setAttribute:function(k,v){ this._attrs[k]=v; }, getAttribute:function(k){ return this._attrs[k]!==undefined?this._attrs[k]:null; },
    removeAttribute:function(k){ delete this._attrs[k]; },
    querySelector:function(){ return null; }, querySelectorAll:function(){ return []; },
    insertAdjacentHTML:function(){}, focus:function(){}, click:function(){}
  };
}
var document = {
  getElementById: function(id){ if(!__elCache[id]) __elCache[id]=mkEl(id); return __elCache[id]; },
  createElement: function(){ return mkEl('__tmp__'); },
  addEventListener: function(){},
  querySelectorAll: function(sel){
    // 复刻真实选择器语义：'.note-dir-panel,#tocPanel' 同时命中两个元素
    if(sel.indexOf('note-dir-panel')>=0 && sel.indexOf('tocPanel')>=0){
      return [document.getElementById('noteDirPanel'), document.getElementById('tocPanel')];
    }
    if(sel.indexOf('note-dir-panel')>=0) return [document.getElementById('noteDirPanel')];
    if(sel.indexOf('tocPanel')>=0) return [document.getElementById('tocPanel')];
    return [];
  },
  querySelector: function(){ return null; },
  body:{appendChild:function(){}}, documentElement:{classList:{add:function(){},remove:function(){}}}
};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}},clipboard:{}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var fetch=function(url){
  __requested.push(url); __fetchCount++;
  var body;
  if(url.indexOf('/api/notes/tree')>=0) body={count:__treeItems.length,total:__treeItems.length,items:__treeItems};
  else body={path:'20-知识/x.md',meta:{created:'2026-01-01',updated:'2026-01-01',tags:['a'],type:'knowledge'},content:'# 标题'};
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
  return { ctx, sandbox };
}

/** 在沙箱里执行一段异步测试代码，返回其结果（自动冲刷微任务）。 */
function runInSandbox(ctx, code) {
  return vm.runInContext(
    `(async () => { const flush = async function(n){ n=n||40; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx,
    { filename: 'test-body.js' }
  );
}

function fakeItems(n, prefix) {
  const items = [];
  for (let i = 0; i < n; i++) {
    items.push({
      path: `${prefix || '10-项目'}/笔记${i}.md`,
      title: `笔记${i}`,
      type: 'project', tags: ['测试'], status: '',
      modified: '2026-10-07T00:00:00', size: 100 + i, is_dir: false
    });
  }
  return items;
}

// ─────────────────────────── 笔记树 ───────────────────────────

test('笔记树：根层目录展开、深层目录默认折叠', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [
      {path:'20-知识/根层.md', title:'根层', type:'knowledge', tags:[], size:1, is_dir:false},
      {path:'20-知识/子目录/深层.md', title:'深层', type:'knowledge', tags:[], size:1, is_dir:false}
    ];
    A='http://127.0.0.1:18900'; T='tok';
    lt(); await flush();
    const h = document.getElementById('nt').innerHTML;
    return {
      html: h,
      expandedHeads: (h.match(/▾/g)||[]).length,
      collapsedHeads: (h.match(/▸/g)||[]).length,
      hiddenBodies: (h.match(/nt-body hidden/g)||[]).length,
      lazyRegs: (h.match(/data-lz="/g)||[]).length
    };
  `);
  assert.ok(res.expandedHeads > 0, '根层目录应有展开标记 ▾');
  assert.ok(res.collapsedHeads > 0, '深层目录应有折叠标记 ▸');
  assert.strictEqual(res.hiddenBodies, res.lazyRegs, '折叠的目录体应同时带 hidden 与 data-lz 登记');
});

test('笔记树：点开折叠目录后补渲染子树并注销懒渲染登记', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [{path:'20-知识/子目录/深层.md', title:'深层', type:'knowledge', tags:[], size:1, is_dir:false}];
    A='http://127.0.0.1:18900'; T='tok';
    lt(); await flush();
    const before = Object.keys(_ntLazy).length;
    const lzKey = Object.keys(_ntLazy)[0];
    const fakeEl = {
      nextElementSibling: {
        classList:{ contains:function(c){return c==='nt-body'}, toggle:function(){return false} },
        getAttribute:function(n){ return n==='data-lz' ? lzKey : null; },
        set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h||''; },
        removeAttribute:function(){}
      },
      querySelector:function(){ return { set textContent(v){this._t=v}, get textContent(){return this._t} }; }
    };
    ntToggle(fakeEl);
    return { before, after: Object.keys(_ntLazy).length, rendered: fakeEl.nextElementSibling.innerHTML.length };
  `);
  assert.ok(res.before > 0, '应有折叠目录登记懒渲染');
  assert.strictEqual(res.after, res.before - 1, '展开后应注销该目录的懒渲染登记');
  assert.ok(res.rendered > 0, '展开后应补渲染出子树内容');
});

test('笔记树：过滤态整树展开、无折叠残留', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [
      {path:'20-知识/甲.md', title:'甲', type:'knowledge', tags:[], size:1, is_dir:false},
      {path:'20-知识/子目录/乙.md', title:'乙', type:'knowledge', tags:[], size:1, is_dir:false}
    ];
    A='http://127.0.0.1:18900'; T='tok';
    lt(); await flush();
    __filter = '乙';
    ltFilter(); await flush();
    const h = document.getElementById('nt').innerHTML;
    return { hasHidden: h.indexOf('nt-body hidden')>=0, hasLazy: h.indexOf('data-lz=')>=0, len: h.length };
  `);
  assert.strictEqual(res.hasHidden, false, '过滤态不应有折叠残留');
  assert.strictEqual(res.hasLazy, false, '过滤态不应有懒渲染残留');
});

// ─────────────────── 第 1 组修复：过滤不重拉全量 ───────────────────

test('过滤输入不再重发全量请求（走内存缓存）', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [{path:'20-知识/甲.md', title:'甲', type:'knowledge', tags:[], size:1, is_dir:false}];
    A='http://127.0.0.1:18900'; T='tok';
    lt(); await flush();
    const afterFirst = __fetchCount;
    __filter = '甲'; ltD();
    __filter = '甲乙'; ltD();
    __filter = ''; ltD();
    await flush();
    return { afterFirst, afterFilters: __fetchCount, treeUrl: __requested[0] };
  `);
  assert.strictEqual(res.afterFilters, res.afterFirst,
    '过滤输入不应触发新的全量请求（期望只拉一次）');
  assert.ok(String(res.treeUrl).indexOf('/api/notes/tree') >= 0, '数据源应为 /api/notes/tree');
});

test('缓存为空时过滤会回退到一次真实拉取', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [{path:'20-知识/甲.md', title:'甲', type:'knowledge', tags:[], size:1, is_dir:false}];
    A='http://127.0.0.1:18900'; T='tok';
    __filter = '甲';
    ltD(); await flush();          // 缓存为空，应触发拉取
    return { count: __fetchCount, hasContent: document.getElementById('nt').innerHTML.length > 20 };
  `);
  assert.strictEqual(res.count, 1, '缓存为空时过滤应回退到一次真实拉取');
  assert.ok(res.hasContent, '应渲染出内容');
});

test('写入笔记后缓存失效，下次切页签会重新拉取', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    __treeItems = [{path:'20-知识/甲.md', title:'甲', type:'knowledge', tags:[], size:1, is_dir:false}];
    A='http://127.0.0.1:18900'; T='tok';
    lt(); await flush();
    const before = __fetchCount;
    ltInvalidate();               // 模拟新建/保存/智能上传后失效
    lt(); await flush();
    return { before, after: __fetchCount };
  `);
  assert.strictEqual(res.after, res.before + 1, '失效后应重新拉取一次全量');
});

// ─────────────────────── 移动端抽屉 ───────────────────────

test('移动端：点笔记后目录抽屉与遮罩都关闭', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    const panel = document.getElementById('noteDirPanel');
    const ov = document.getElementById('mobileOverlay');
    panel.classList.add('mobile-show'); ov.classList.add('show');
    rn('20-知识/测试笔记');
    await flush();
    return {
      panelOpen: panel.classList.contains('mobile-show'),
      overlayShown: ov.classList.contains('show'),
      rendered: document.getElementById('nv').innerHTML.length
    };
  `);
  assert.strictEqual(res.panelOpen, false, '点笔记后抽屉应关闭');
  assert.strictEqual(res.overlayShown, false, '点笔记后遮罩应关闭');
  assert.ok(res.rendered > 100, '笔记内容应已渲染（否则说明 sn 内部抛异常被吞）');
});

test('移动端：切页签后目录抽屉与目录面板都关闭', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    const panel = document.getElementById('noteDirPanel');
    const toc = document.getElementById('tocPanel');
    const ov = document.getElementById('mobileOverlay');
    panel.classList.add('mobile-show'); toc.classList.add('mobile-show'); ov.classList.add('show');
    st('dash');
    return {
      panelOpen: panel.classList.contains('mobile-show'),
      tocOpen: toc.classList.contains('mobile-show'),
      overlayShown: ov.classList.contains('show')
    };
  `);
  assert.strictEqual(res.panelOpen, false, '切页签后目录抽屉应关闭');
  assert.strictEqual(res.tocOpen, false, '切页签后目录面板应关闭');
  assert.strictEqual(res.overlayShown, false, '切页签后遮罩应关闭');
});

test('桌面端：无抽屉态时调用关闭逻辑不产生副作用', async () => {
  const { ctx } = makeSandbox();
  const res = await runInSandbox(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    const panel = document.getElementById('noteDirPanel');
    const ov = document.getElementById('mobileOverlay');
    closeMobilePanels();
    return { panelOpen: panel.classList.contains('mobile-show'), overlayShown: ov.classList.contains('show') };
  `);
  assert.strictEqual(res.panelOpen, false, '桌面端不应残留 mobile-show');
  assert.strictEqual(res.overlayShown, false, '桌面端不应残留遮罩');
});
