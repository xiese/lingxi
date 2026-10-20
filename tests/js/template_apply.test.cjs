/**
 * 第 7-2 项前端回归：套用模板时 textarea 里只许出现模板正文（body）。
 *
 * 为什么必须有这个测试：模板文件普遍以 YAML frontmatter 开头。一旦
 * `applyTemplate()` 在 `body` 为空时回退到 `content`（原文），就会出两个问题：
 *   ① textarea 首字符变成 `-`，`ltgOrCreate()` 的 `startsWith('#')` 判据失效；
 *   ② 后端 `_build_frontmatter()` 会重建一份，模板那份成为正文里第二份 frontmatter。
 * 这两个后果在页面上都不报错，只是笔记建不出来 / 建出来是坏的，肉眼排查极慢。
 *
 * 覆盖：
 *   ① 缓存命中 + body 正常 → 填入 body
 *   ② 缓存命中 + body 为空 + content 有 frontmatter → 必须留空，不许填 content
 *   ③ 缓存未命中走接口拉取 + body 为空 → 同样不许填 content
 *   ④ textarea 已有用户输入 → 不覆盖
 *
 * 跑法：node --test tests/js/template_apply.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

const STUB = `
var __els = {};
function __mkEl(id){ __els[id] = { id:id, value:'', innerHTML:'', textContent:'', style:{},
  classList:{add:function(){},remove:function(){},contains:function(){return false}},
  addEventListener:function(){}, setAttribute:function(){}, getAttribute:function(){return null},
  querySelector:function(){return null}, querySelectorAll:function(){return []} };
  return __els[id]; }
__mkEl('lt'); __mkEl('noteTemplate'); __mkEl('ltr');
var document = {
  getElementById: function(id){ return __els[id] || null; },
  createElement: function(){ return { style:{}, classList:{add:function(){},remove:function(){}}, appendChild:function(){} }; },
  addEventListener: function(){}, querySelectorAll: function(){ return []; }, querySelector: function(){ return null; },
  body:{appendChild:function(){},removeChild:function(){}}
};
var localStorage={_d:{},getItem:function(k){return this._d[k]||null},setItem:function(k,v){this._d[k]=v},removeItem:function(k){delete this._d[k]}};
var location={origin:'http://127.0.0.1:18900',href:'',search:''};
var navigator={userAgent:'node',serviceWorker:{register:function(){return{then:function(){return{catch:function(){}}}}}}};
var window={addEventListener:function(){},location:location,navigator:navigator,matchMedia:function(){return{matches:false,addEventListener:function(){}}},scrollTo:function(){}};
var __apiResp = {};
var fetch=function(url,opt){
  var body = __apiResp[url]!==undefined ? __apiResp[url] : {};
  return Promise.resolve({status:200,ok:true,json:function(){return Promise.resolve(body)}});
};
var setTimeout=function(fn){return 0;}; var clearTimeout=function(){};
var CSS={escape:function(s){return String(s)}};
var marked={setOptions:function(){return marked},use:function(){return marked},parse:function(x){return String(x)},
  parseInline:function(x){return String(x)},Renderer:function(){}};
`;

function makeSandbox() {
  const sandbox = { console, setTimeout: () => 0, clearTimeout: () => {}, Promise };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(STUB, ctx, { filename: 'stub.js' });
  vm.runInContext(appSrc, ctx, { filename: 'app.js' });
  return ctx;
}

function run(ctx, code) {
  return vm.runInContext(
    `(async () => { const flush = async function(n){ n=n||60; for(let i=0;i<n;i++) await null; }; ${code} })()`,
    ctx
  );
}

const FM = '---\nid: "proj-{{timestamp}}"\ntitle: ""\ntype: project\n---\n\n# {{title}}\n\n## 目标\n';

// ── ① 正常 body 能填进去 ──

test('套用模板：body 正常时填入 textarea', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    _tplCache = { '项目模板': { name:'项目模板', body:'# {{title}}\\n\\n## 目标\\n', content:'${FM.replace(/\n/g, '\\n')}' } };
    __els['noteTemplate'].value = '项目模板';
    __els['lt'].value = '';
    applyTemplate();
    return { val: __els['lt'].value };
  `);
  assert.ok(res.val.startsWith('#'), 'body 应以 # 开头，实际：' + JSON.stringify(res.val.slice(0, 20)));
  assert.ok(!res.val.includes('---'), '不许把 frontmatter 带进去');
});

// ── ② body 为空时绝不回退 content ──

test('套用模板：body 为空时留空，不许回退到含 frontmatter 的 content', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    _tplCache = { '坏模板': { name:'坏模板', body:'', content:'${FM.replace(/\n/g, '\\n')}' } };
    __els['noteTemplate'].value = '坏模板';
    __els['lt'].value = '';
    applyTemplate();
    return { val: __els['lt'].value };
  `);
  assert.strictEqual(res.val, '', 'body 为空就必须留空，回退 content 会让首字符变 - 导致建不出笔记');
});

// ── ③ 缓存未命中走接口，body 为空同样不回退 ──

test('套用模板：接口返回 body 为空时不回退 content', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    _tplCache = {};
    A = 'http://127.0.0.1:18900';   // app.js 里 A 默认是空串，不设 mock key 对不上
    __els['noteTemplate'].value = '坏模板2';
    __els['lt'].value = '';
    __apiResp['http://127.0.0.1:18900/api/templates'] = { templates: [
      { name:'坏模板2', body:'', content:'${FM.replace(/\n/g, '\\n')}' }
    ] };
    applyTemplate();
    await flush();
    return { val: __els['lt'].value };
  `);
  assert.strictEqual(res.val, '', '接口路径同样不许回退 content');
});

// ── ④ 已有用户输入不覆盖 ──

test('套用模板：textarea 已有内容时不覆盖', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    _tplCache = { '项目模板': { name:'项目模板', body:'# 模板标题\\n', content:'# 模板标题\\n' } };
    __els['noteTemplate'].value = '项目模板';
    __els['lt'].value = '我自己写的草稿';
    applyTemplate();
    return { val: __els['lt'].value };
  `);
  assert.strictEqual(res.val, '我自己写的草稿', '不能覆盖用户已经打的字');
});
