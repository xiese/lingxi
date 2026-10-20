/**
 * 第 7-4 项前端回归：功能插件加载器（plugins-loader.js）。
 *
 * 这个加载器的风险全在「出错时会不会连累整页」：
 *   ① 插件脚本 404 / 抛错 → 只撤掉自己那个容器，别的插件照挂
 *   ② 插件没按约定导出 mount() → 容器撤掉，界面不留空壳
 *   ③ 同一个插件重复触发挂载（切 tab、重开笔记）→ 只挂一次
 *   ④ 后端清单里的坏条目（名字非法 / 缺 url / 缺 mount）→ 直接过滤掉
 *   ⑤ 接口挂了 → 返回空数组，不能让 Promise 一直 pending 卡住页面
 *
 * 跑法：node --test tests/js/plugin_loader.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const LOADER = path.join(__dirname, '..', '..', 'scripts', 'web', 'plugins-loader.js');
const loaderSrc = fs.readFileSync(LOADER, 'utf8');

const STUB = `
var __boxes = {};
var __scripts = [];
var __timers = [];
// 可控定时器：默认不自动触发，测试里手动调 __timers[i].fn()
function setTimeout(fn, ms){ __timers.push({ fn: fn, ms: ms }); return __timers.length; }
function clearTimeout(id){ if(id>0 && __timers[id-1]) __timers[id-1].fn = null; }
var __warn = [];
function __mkBox(id){
  __boxes[id] = { id:id, children:[], _parent:null,
    get parentNode(){ return this._parent },
    appendChild:function(c){ c._parent=this; this.children.push(c); return c },
    removeChild:function(c){ var i=this.children.indexOf(c); if(i>=0)this.children.splice(i,1); c._parent=null; return c }
  };
  return __boxes[id];
}
__mkBox('kbPluginDash'); __mkBox('kbPluginNote'); __mkBox('kbPluginSettings');

var document = {
  readyState:'complete',
  head:{ appendChild:function(s){ __scripts.push(s); } },
  getElementById:function(id){ return __boxes[id]||null },
  querySelector:function(sel){
    var m = { '#kbPluginDash':'kbPluginDash', '#kbPluginNote':'kbPluginNote', '#kbPluginSettings':'kbPluginSettings' };
    return m[sel] ? __boxes[m[sel]] : null;
  },
  createElement:function(tag){
    var el = { tagName:tag, className:'', children:[], _parent:null, _attrs:{},
      setAttribute:function(k,v){ this._attrs[k]=v }, getAttribute:function(k){ return this._attrs[k] },
      get parentNode(){ return this._parent },
      appendChild:function(c){ c._parent=this; this.children.push(c); return c },
      removeChild:function(c){ var i=this.children.indexOf(c); if(i>=0)this.children.splice(i,1); c._parent=null; return c } };
    if(tag==='script'){
      // 不自动触发 onload/onerror，由测试手动调 __fire
      el.__fire = function(ok){ if(ok){ el.onload && el.onload(); } else { el.onerror && el.onerror(); } };
    }
    return el;
  },
  addEventListener:function(){}
};
var console = { warn:function(){ __warn.push([].slice.call(arguments).join(' ')) }, log:function(){} };
// loader 里判断的是 window.console，不给它挂上告警分支永远走不进去
var window = { kbPlugins:{}, console:console };
var __apiResp = null;
var __apCalls = [];
function ap(p){
  __apCalls.push(p);
  if(__apiResp === 'throw') return Promise.reject(new Error('接口挂了'));
  return Promise.resolve(__apiResp);
}
`;

function makeSandbox() {
  const sandbox = { Promise };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(STUB, ctx, { filename: 'stub.js' });
  vm.runInContext(loaderSrc, ctx, { filename: 'plugins-loader.js' });
  // loader 把入口挂在 window 上，测试里用裸名调不到，补两个别名
  vm.runInContext('var kbPluginMount = window.kbPluginMount; var kbPluginInvalidate = window.kbPluginInvalidate;', ctx);
  return ctx;
}

function run(ctx, code) {
  return vm.runInContext(
    `(async () => { const flush = async function(n){ n=n||80; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx
  );
}

const OK = { plugins: [
  { name:'alpha', title:'A', version:'1', mount:'dash', url:'./plugins/alpha/plugin.js', enabled:true },
  { name:'beta', title:'B', version:'1', mount:'settings', url:'./plugins/beta/plugin.js', enabled:true },
]};

// ── ① 正常挂载 ──

test('挂载：清单里的插件被注入容器，并调到自己的 mount()', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify(OK)};
    var p = kbPluginMount('dash');
    await flush();
    // 脚本插入后手动触发 load，模拟插件自己注册 mount
    __scripts.forEach(function(s){ window.kbPlugins['alpha'] = { mount:function(el,ctx){ el.setAttribute('data-mount','ok'); } }; s.__fire(true); });
    await flush();
    var box = __boxes['kbPluginDash'];
    return { n:box.children.length, attr: box.children[0] && box.children[0].getAttribute('data-mount'), warn:__warn };
  `);
  assert.equal(res.n, 1, 'dash 上应只挂 alpha 一个（beta 是 settings 挂载点）');
  assert.equal(res.attr, 'ok', '插件的 mount() 应被调用');
});

test('拉清单打的是 /api/plugins/enabled（ap 不补前缀）', async () => {
  const ctx = makeSandbox();
  ctx.__apiResp = { plugins: [] };
  await ctx.kbPluginMount('dash');
  assert.ok(ctx.__apCalls.includes('/api/plugins/enabled'),
    '实际请求路径是 ' + JSON.stringify(ctx.__apCalls) + '，缺 /api 前缀会 404');
});

test('挂载：挂载点容器不存在时安静返回 0，不抛错', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify(OK)};
    delete __boxes['kbPluginDash'];
    var n = await kbPluginMount('dash');
    return { n:n, warn:__warn.length };
  `);
  assert.equal(res.n, 0);
});

test('挂载：不认识的挂载点返回 0', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `var n = await kbPluginMount('sidebar'); return { n:n };`);
  assert.equal(res.n, 0);
});

// ── ② 坏插件不连累别人 ──

test('容错：一个插件脚本加载失败，另一个照常挂上', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify(OK)};
    kbPluginMount('dash'); kbPluginMount('settings');
    await flush();
    var dashScript = __scripts[0], setScript = __scripts[1];
    window.kbPlugins['alpha'] = { mount:function(){} };
    window.kbPlugins['beta']  = { mount:function(el){ el.setAttribute('data-mount','ok'); } };
    dashScript.__fire(false);   // alpha 挂掉
    setScript.__fire(true);     // beta 正常
    await flush();
    return { dash:__boxes['kbPluginDash'].children.length, set:__boxes['kbPluginSettings'].children.length,
             betaAttr: __boxes['kbPluginSettings'].children[0] && __boxes['kbPluginSettings'].children[0].getAttribute('data-mount'),
             warn:__warn.join('|') };
  `);
  assert.equal(res.dash, 0, '加载失败的插件要撤掉容器');
  assert.equal(res.set, 1, '另一个插件必须照常挂上');
  assert.equal(res.betaAttr, 'ok');
  assert.ok(res.warn.includes('alpha'), '应留下告警，便于排查');
});

test('容错：插件没导出 mount() 时撤掉容器，界面不留空壳', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify({plugins:[OK.plugins[0]]})};
    kbPluginMount('dash');
    await flush();
    __scripts[0].__fire(true);   // 脚本加载成功，但没往 kbPlugins 里注册
    await flush();
    return { n:__boxes['kbPluginDash'].children.length, warn:__warn.join('|') };
  `);
  assert.equal(res.n, 0, '没导出 mount() 的插件不许留空 div');
  assert.ok(res.warn.includes('mount'), '应提示没导出 mount()');
});

// ── ③ 去重 ──

test('去重：同一插件重复触发挂载只挂一次', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify({plugins:[OK.plugins[0]]})};
    kbPluginMount('dash');
    await flush();
    window.kbPlugins['alpha'] = { mount:function(){} };
    __scripts[0].__fire(true);
    await flush();
    kbPluginMount('dash'); kbPluginMount('dash');
    await flush();
    return { n:__boxes['kbPluginDash'].children.length, scripts:__scripts.length };
  `);
  assert.equal(res.n, 1, '重复调用不许挂第二份');
  assert.equal(res.scripts, 1, '重复调用不许再插一次脚本');
});

test('去重：invalidate() 之后能重新挂（启停插件后要用）', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify({plugins:[OK.plugins[0]]})};
    kbPluginMount('dash');
    await flush();
    window.kbPlugins['alpha'] = { mount:function(){} };
    __scripts[0].__fire(true);
    await flush();
    var before = __boxes['kbPluginDash'].children.length;
    kbPluginInvalidate();
    __boxes['kbPluginDash'].children.length = 0;   // 模拟页面重建容器
    kbPluginMount('dash');
    await flush();
    return { before:before, after:__boxes['kbPluginDash'].children.length };
  `);
  assert.equal(res.before, 1);
  assert.equal(res.after, 1, 'invalidate 后应能重新挂上');
});

// ── ④ 清单过滤 ──

test('过滤：清单里的坏条目被剔掉，好条目不受影响', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = { plugins: [
      ${JSON.stringify(OK.plugins[0])},
      { name:'../evil', mount:'dash', url:'./x.js' },
      { name:'nourl',  mount:'dash' },
      { name:'nomount', url:'./y.js' },
      null, 'string', 42
    ]};
    kbPluginMount('dash');
    await flush();
    window.kbPlugins['alpha'] = { mount:function(){} };
    __scripts[0].__fire(true);
    await flush();
    return { n:__boxes['kbPluginDash'].children.length, scripts:__scripts.length };
  `);
  assert.equal(res.n, 1, '只挂合法的那一个');
  assert.equal(res.scripts, 1, '坏条目不许触发脚本注入');
});

// ── ⑤ 接口挂了 ──

test('容错：/plugins/enabled 请求失败时返回空、页面继续', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = 'throw';
    var n = await kbPluginMount('dash');
    return { n:n, n2:__boxes['kbPluginDash'].children.length };
  `);
  assert.equal(res.n, 0);
  assert.equal(res.n2, 0);
});

test('容错：接口返回非数组时按空处理', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = { plugins: 'not-an-array' };
    var n = await kbPluginMount('dash');
    return { n:n };
  `);
  assert.equal(res.n, 0);
});


// ── ⑥ 脚本卡死（既不 onload 也不 onerror）──

test('容错：脚本既不 onload 也不 onerror 时超时撤容器，不留永久空壳', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify({plugins:[OK.plugins[0]]})};
    kbPluginMount('dash');
    await flush();
    var duringLoad = __boxes['kbPluginDash'].children.length;
    // 不 fire 任何事件，模拟被扩展拦掉 / 网络半开
    __timers.forEach(function(t){ if(t.fn) t.fn(); });
    await flush();
    return { duringLoad: duringLoad, afterTimeout: __boxes['kbPluginDash'].children.length,
             warnings: __warn.length };
  `);
  assert.equal(res.duringLoad, 1, '加载中应该先占位');
  assert.equal(res.afterTimeout, 0, '超时后必须把空壳撤掉');
  assert.ok(res.warnings >= 1, '超时要留告警，别静默');
});

// ── ⑦ 脚本只注入一次 ──

test('去重：同一个插件脚本只注入一次，重挂复用已加载脚本', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    __apiResp = ${JSON.stringify({plugins:[OK.plugins[0]]})};
    kbPluginMount('dash');
    await flush();
    window.kbPlugins['alpha'] = { mount:function(){} };
    __scripts[0].__fire(true);
    await flush();
    var n1 = __scripts.length;
    kbPluginInvalidate();
    __boxes['kbPluginDash'].children.length = 0;
    kbPluginMount('dash');
    await flush();
    return { n1: n1, n2: __scripts.length };
  `);
  assert.equal(res.n1, 1);
  assert.equal(res.n2, 1, '重挂不许再插一个 script 标签');
});
