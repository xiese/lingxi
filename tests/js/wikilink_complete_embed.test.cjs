/**
 * 双链输入补全（K3）+ `![[嵌入]]` 渲染的前端回归。
 *
 * 为什么必须有：补全的「光标在不在未闭合的 [[ 里」和「候选怎么排」都是纯逻辑，
 * 错了页面只是「没反应」；嵌入是渲染完再异步拉正文，出错了也只会停在「加载中…」。
 * 靠肉眼分不清「没这功能」和「代码没跑」，所以直接跑真函数。
 *
 * 覆盖：
 *   ① wlCompleteContext：未闭合 / 已闭合 / 换行 / 别名锚点 / 无双链
 *   ② wlCandidates：过滤、前缀优先、limit
 *   ③ wlInsert：替换光标前那段 query 并补 ]]
 *   ④ m2：![[x]] 生成 data-wl-embed；代码段内不动；embed:false 时退化成普通双链
 *   ⑤ embedWikilinksIn：解析到的填入正文、解析不到的标断链、自引用跳过
 *
 * 跑法：node --test tests/js/wikilink_complete_embed.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', '..', 'scripts', 'web', 'app.js');
const appSrc = fs.readFileSync(APP_JS, 'utf8');

function makeSandbox() {
  const stub = `
var __requests = [];
var __resp = {};
// 假嵌入节点：只实现 embedWikilinksIn 用到的那几样
function mkEmbed(t){
  var cls={}, attrs={'data-wl-embed':t}, body={innerHTML:''};
  return { classList:{add:function(c){cls[c]=1},remove:function(c){delete cls[c]},contains:function(c){return !!cls[c]}},
           getAttribute:function(k){return attrs[k]!==undefined?attrs[k]:null},
           setAttribute:function(k,v){attrs[k]=v},
           querySelector:function(s){return s==='.wl-embed-body'?body:null},
           _body:body };
}
var document = {
  getElementById: function(){ return { innerHTML:'', style:{}, classList:{add:function(){},remove:function(){},contains:function(){return false}}, querySelectorAll:function(){return []} }; },
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
var escapeHtml=function(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); };
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

// ── ① 光标上下文判定 ──

test('wlCompleteContext：只在未闭合的 [[ 里给候选', () => {
  const ctx = makeSandbox();
  const res = vm.runInContext(`JSON.stringify({
    open: wlCompleteContext('前 [[abc', 7),
    closed: wlCompleteContext('前 [[abc]] 后', 12),
    newline: wlCompleteContext('前 [[ab\\nc', 8),
    none: wlCompleteContext('没有双链', 4),
    alias: wlCompleteContext('[[x|别', 5),
    fresh: wlCompleteContext('[[', 2)
  })`, ctx);
  const d = JSON.parse(res);
  assert.deepStrictEqual(d.open, { start: 2, query: 'abc' });
  assert.strictEqual(d.closed, null, '已闭合的 [[...]] 不该再弹候选');
  assert.strictEqual(d.newline, null, '换行后不在补全中');
  assert.strictEqual(d.none, null);
  assert.strictEqual(d.alias, null, '在写别名时不该继续补全');
  assert.deepStrictEqual(d.fresh, { start: 0, query: '' }, '刚打 [[ 应列出候选');
});

// ── ② 候选过滤与排序 ──

test('wlCandidates：过滤 + 前缀优先 + limit', () => {
  const ctx = makeSandbox();
  const items = [
    { path: '20-知识/索引.md', title: '索引系统' },
    { path: '10-项目/索引服务.md', title: '索引服务' },
    { path: 'x/甲的索引.md', title: '甲的索引' },
    { path: 'y/不相关.md', title: '不相关' },
  ];
  ctx.__items = items;
  const res = vm.runInContext(`JSON.stringify({
    hit: wlCandidates(__items,'索引',10),
    none: wlCandidates(__items,'zzz',10),
    lim: wlCandidates(__items,'索引',1)
  })`, ctx);
  const d = JSON.parse(res);
  assert.strictEqual(d.hit.length, 3, '只保留命中的 3 条');
  assert.strictEqual(d.hit[0].title, '索引服务', '前缀命中且路径靠前');
  assert.strictEqual(d.hit[1].title, '索引系统');
  assert.strictEqual(d.hit[2].title, '甲的索引', '包含命中排最后');
  assert.strictEqual(d.none.length, 0);
  assert.strictEqual(d.lim.length, 1, 'limit 生效');
});

// ── ③ 插入写回 ──

test('wlInsert：替换 query 并补上 ]]', () => {
  const ctx = makeSandbox();
  const res = vm.runInContext(`JSON.stringify((function(){
    var ta={ value:'前 [[索', selectionStart:5,
             setSelectionRange:function(a,b){this._sel=[a,b]}, focus:function(){} };
    wlInsert(ta,{start:2,query:'索'},{path:'20-知识/索引系统.md',title:'索引系统'});
    return { v:ta.value, sel:ta._sel };
  })())`, ctx);
  const d = JSON.parse(res);
  assert.strictEqual(d.v, '前 [[20-知识/索引系统]]', '插入去掉 .md 的相对路径（路径解析最稳，不靠标题撞名）');
  assert.strictEqual(d.sel[0], d.v.length, '光标应落在 ]] 之后');
});

// ── ④ m2 的嵌入占位 ──

test('m2：![[x]] 生成嵌入占位，代码段内不动，embed:false 退化成双链', () => {
  const ctx = makeSandbox();
  const md = ['正文', '', '![[甲笔记]]', '', '行内：`![[示例甲]]`', '', '```', '![[示例乙]]', '```'].join('\n');
  ctx.__md = md;
  const res = vm.runInContext(`JSON.stringify({
    on: m2(__md),
    off: m2('看看 ![[甲笔记]] 的效果', {embed:false})
  })`, ctx);
  const d = JSON.parse(res);
  assert.ok(d.on.includes('data-wl-embed="甲笔记"'), '正文里的 ![[x]] 应生成嵌入占位：' + d.on.slice(0, 200));
  assert.ok(d.on.includes('wl-embed-refresh'), '嵌入块标题行要带「↻ 重新加载」按钮');
  assert.ok(d.on.includes('![[示例甲]]'), '行内代码里的示例不该被处理');
  assert.ok(d.on.includes('![[示例乙]]'), '代码块里的示例不该被处理');
  assert.ok(!d.off.includes('data-wl-embed'), 'embed:false 时不再生成嵌入');
  assert.ok(d.off.includes('data-wl="甲笔记"'), 'embed:false 时退化成普通双链');
});

// ── ⑤ 异步填入 ──

test('embedWikilinksIn：填入正文 / 断链标红 / 自引用跳过', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    var good=mkEmbed('甲'), bad=mkEmbed('不存在'), self=mkEmbed('自己');
    var box={ querySelectorAll:function(s){ return s==='[data-wl-embed]'?[good,bad,self]:[] } };
    __resp['http://127.0.0.1:18900/api/links/resolve-batch']={resolved:{'甲':'20-知识/甲.md','不存在':null,'自己':'20-知识/自己.md'}};
    __resp['http://127.0.0.1:18900/notes/'+encodeURIComponent('20-知识/甲.md')]={content:'# 甲\\n\\n正文甲'};
    await embedWikilinksIn(box,'20-知识/自己.md');
    return { reqs:__requests.length,
             goodHtml:good._body.innerHTML,
             badBroken:bad.classList.contains('wl-broken'),
             selfHtml:self._body.innerHTML };
  `);
  assert.strictEqual(res.reqs, 2, '一次批量解析 + 只对解析到的目标拉正文（自引用/断链不拉）');
  assert.ok(res.goodHtml.includes('正文甲'), '嵌入正文应被渲染填入：' + res.goodHtml.slice(0, 120));
  assert.strictEqual(res.badBroken, true, '解析不到的嵌入应标断链');
  assert.ok(res.selfHtml.includes('自引用'), '自引用不发请求，就地提示');
});

// ── ⑥ 嵌入块「↻ 重新加载」 ──

test('wlEmbedRefresh：重解析 + 重拉这一篇，只刷这一块', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    A='http://127.0.0.1:18900'; T='tok';
    var el=mkEmbed('甲'), other=mkEmbed('乙');
    other._body.innerHTML='不该被动的块';
    __resp['http://127.0.0.1:18900/api/links/resolve-batch']={resolved:{'甲':'20-知识/甲.md'}};
    __resp['http://127.0.0.1:18900/notes/'+encodeURIComponent('20-知识/甲.md')]={content:'最新正文'};
    currentEditPath='20-知识/引用者.md';
    var btn={ closest:function(s){ return s==='.wl-embed'?el:null } };
    var before=__requests.length;
    await wlEmbedRefresh(btn);
    return { html:el._body.innerHTML, reqs:__requests.length-before, other:other._body.innerHTML };
  `);
  assert.ok(res.html.includes('最新正文'), '刷新后应换成最新正文：' + res.html.slice(0, 120));
  assert.strictEqual(res.reqs, 2, '一次重解析 + 一次重拉正文');
  assert.strictEqual(res.other, '不该被动的块', '只刷被点的那一块');
});

// ── ⑦ 目录树未加载时的预取（每条一条，不是每次按键都发） ──

test('补全：目录树未加载时预取一次，拿到后自动弹候选', async () => {
  const ctx = makeSandbox();
  const res = await run(ctx, `
    var calls=0;
    var realLt=lt;
    lt=function(){ calls++; _ntItems=[{path:'20-知识/甲.md',title:'甲'}]; return Promise.resolve(_ntItems); };
    var handlers={};
    var ta={ value:'[[', selectionStart:2,
             getBoundingClientRect:function(){return {left:0,top:0,bottom:0,width:100}},
             focus:function(){}, setSelectionRange:function(){},
             addEventListener:function(k,f){(handlers[k]=handlers[k]||[]).push(f)} };
    wlBindComplete(ta);
    _ntItems=null;
    handlers['input'][0]();      // 第一次输入：触发预取
    await flush(30);
    var afterFirst=calls;
    handlers['input'][0]();      // 第二次输入：已有数据，不该再拉
    await flush(30);
    lt=realLt;
    return { afterFirst:afterFirst, total:calls };
  `);
  assert.strictEqual(res.afterFirst, 1, '未加载时应预取一次');
  assert.strictEqual(res.total, 1, '拿到目录树后不该再重复拉取');
});
