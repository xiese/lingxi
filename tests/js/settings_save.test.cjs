/**
 * 设置页保存的行为测试（2026-10-18 第 1 项）。
 *
 * 为什么必须跑真代码：`test_settings_groups.py` 里对 `<REDACTED>` 只做了源码 grep，
 * 双模型审查（DS 路）判为假绿——字符串存在不等于跳过逻辑生效。
 * 这里真调 sv()，拦截 fetch 看实际发出去的 body。
 *
 * 锁死的不变量：
 *   ① 回显的 `<REDACTED>` 占位符不许进 body（否则保存一次就把所有密钥清成占位符）
 *   ② 真实值必须照常提交
 *   ③ 未改动的项也要提交（后端是整表覆盖语义）
 *
 * 跑法：node --test tests/js/settings_save.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

function makeSandbox(inputs) {
  const stub = `
var __elCache = {};
function mkEl(id){
  return {
    _h:'', id:id, textContent:'', className:'', style:{}, dataset:{}, options:{length:1},
    set innerHTML(v){ this._h=v; }, get innerHTML(){ return this._h; },
    value:'', checked:false, tagName:'INPUT',
    classList:{add:function(){},remove:function(){},contains:function(){return false},toggle:function(){}},
    appendChild:function(){}, removeChild:function(){}, remove:function(){},
    addEventListener:function(){}, setAttribute:function(){}, getAttribute:function(){return null},
    removeAttribute:function(){}, focus:function(){}, click:function(){},
    insertAdjacentHTML:function(pos,html){ this._h += html; },
    querySelector:function(){return null}, querySelectorAll:function(){return []}
  };
}
var __inputs = ${JSON.stringify(inputs)}.map(function(o){ var e=mkEl('s_'+o.k); e.value=o.v; e.tagName=o.tag||'INPUT'; return e; });
var document = {
  getElementById: function(id){ if(!__elCache[id]) __elCache[id]=mkEl(id); return __elCache[id]; },
  createElement: function(){ return mkEl('__tmp__'); },
  addEventListener: function(){},
  querySelectorAll: function(sel){ return sel==='#sf [id^="s_"]' ? __inputs : []; },
  querySelector: function(){ return null; },
  body:{appendChild:function(){}},
  documentElement:{classList:{add:function(){},remove:function(){}}}
};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}},clipboard:{}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var __sent=[];
var fetch=function(url,opt){
  if(String(url).indexOf('/admin/settings')>=0 && opt && opt.body){ __sent.push(JSON.parse(opt.body)); }
  return Promise.resolve({status:200,ok:true,json:function(){ return Promise.resolve({ok:true}); }});
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

function run(ctx, code) {
  return vm.runInContext(
    `(async () => { const flush = async function(n){ n=n||80; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx,
    { filename: 'test-body.js' }
  );
}

test('回显的 <REDACTED> 占位符不会进提交 body', async () => {
  const ctx = makeSandbox([
    { k: 'llm_key', v: '<REDACTED>' },
    { k: 'llm_base', v: 'https://x/v1' },
    { k: 'embed_key', v: '<REDACTED>' },
  ]);
  const out = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    sv(); await flush();
    return __sent;
  `);

  assert.strictEqual(out.length, 1, '应发一次保存请求，实际 ' + out.length);
  const body = out[0];
  assert.ok(!('llm_key' in body), '占位符被当真实值提交了：' + JSON.stringify(body));
  assert.ok(!('embed_key' in body), '占位符被当真实值提交了：' + JSON.stringify(body));
  assert.strictEqual(body.llm_base, 'https://x/v1', '真实值必须照常提交：' + JSON.stringify(body));
});

test('全部是占位符时也不提交任何密钥字段', async () => {
  const ctx = makeSandbox([
    { k: 'llm_key', v: '<REDACTED>' },
    { k: 'embed_key', v: '<REDACTED>' },
  ]);
  const out = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    sv(); await flush();
    return __sent;
  `);

  assert.strictEqual(out.length, 1, out.length);
  assert.deepStrictEqual(Object.keys(out[0]), [], '全是占位符时不该提交任何字段：' + JSON.stringify(out[0]));
});

test('下拉框的值按 value 取，不受 tagName 影响', async () => {
  const ctx = makeSandbox([
    { k: 'llm_key', v: 'sk-real', tag: 'INPUT' },
    { k: 'rerank_enabled', v: 'false', tag: 'SELECT' },
  ]);
  const out = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    sv(); await flush();
    return __sent;
  `);

  assert.strictEqual(out.length, 1, out.length);
  assert.strictEqual(out[0].llm_key, 'sk-real', JSON.stringify(out[0]));
  assert.strictEqual(out[0].rerank_enabled, 'false', '布尔项应原样传字符串给后端归一：' + JSON.stringify(out[0]));
});
