/* 知识库 UI 外壳 v1.1.0：主题（极光 / 控制台 / 经典）+ 分类导航 + 子 Tab + 总览功能区
 * 只做「外壳」：不改 app.js 的业务逻辑，通过包装全局 st()/ld() 同步导航状态。
 * 经典主题 = 原版界面（不注入新导航），可随时切回。
 */
(function(){
'use strict';
var SPRITE='<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs><symbol id="ki-home" viewBox="0 0 24 24"><path d="M3 10l9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></symbol><symbol id="ki-book" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/></symbol><symbol id="ki-file" viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></symbol><symbol id="ki-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></symbol><symbol id="ki-graph" viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="8" r="2.5"/><circle cx="9" cy="18" r="2.5"/><path d="M8.2 7.2l7.5.6M7 8.3l1.4 7.4M16.4 10l-5.6 6.3"/></symbol><symbol id="ki-spark" viewBox="0 0 24 24"><path d="M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z"/><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/></symbol><symbol id="ki-chat" viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></symbol><symbol id="ki-puzzle" viewBox="0 0 24 24"><path d="M10 3h4v3a2 2 0 1 0 4 0V5h3v5h-1a2 2 0 1 0 0 4h1v6h-6v-1a2 2 0 1 0-4 0v1H4v-6h1a2 2 0 1 0 0-4H4V5h6z"/></symbol><symbol id="ki-up" viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M5 20h14"/></symbol><symbol id="ki-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol><symbol id="ki-key" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/></symbol><symbol id="ki-log" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></symbol><symbol id="ki-gear" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5.9z"/></symbol><symbol id="ki-moon" viewBox="0 0 24 24"><path d="M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z"/></symbol><symbol id="ki-bell" viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10 21a2 2 0 0 0 4 0"/></symbol><symbol id="ki-filter" viewBox="0 0 24 24"><path d="M3 5h18l-7 8v6l-4 2v-8z"/></symbol><symbol id="ki-cpu" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></symbol><symbol id="ki-db" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></symbol><symbol id="ki-pal" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="8" cy="10" r="1"/><circle cx="12" cy="7.5" r="1"/><circle cx="16" cy="10" r="1"/></symbol><symbol id="ki-send" viewBox="0 0 24 24"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></symbol><symbol id="ki-code" viewBox="0 0 24 24"><path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/></symbol><symbol id="ki-out" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></symbol><symbol id="ki-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol><symbol id="ki-x" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></symbol></defs></svg>';
function ic(n,c){return '<svg class="ki '+(c||'')+'" aria-hidden="true"><use href="#ki-'+n+'"/></svg>'}

// ── 功能分类（一处定义，侧栏 / 底栏 / 子 Tab / 总览 共用） ──
var GROUPS=[
  {id:'know',name:'知识',icon:'book',tabs:[
    {t:'notes',name:'笔记',icon:'file',desc:'目录浏览'},
    {t:'sem',name:'搜索',icon:'search',desc:'语义检索'},
    {t:'graph',name:'图谱',icon:'graph',desc:'关系网络'},
    {t:'upload',name:'上传',icon:'up',desc:'文件入库'}]},
  {id:'ai',name:'AI',icon:'spark',tabs:[
    {t:'llm',name:'问答 / 新建',icon:'chat',desc:'问答·智能新建'},
    {t:'market',name:'插件商城',icon:'puzzle',desc:'插件·主题·模板·技能·MCP'},
    // 2026-10-19 T10：技能/MCP 已并入插件商城。此项保留（不真删结构），由 hidden 标记在渲染处跳过
    {t:'skills',name:'技能 / MCP',icon:'puzzle',desc:'技能与 MCP',hidden:1}]},
  {id:'sys',name:'系统',icon:'gear',tabs:[
    {t:'set',name:'设置',icon:'gear',desc:'模型与参数'},
    {t:'auth',name:'授权',icon:'key',desc:'Token 管理'},
    {t:'log',name:'日志',icon:'log',desc:'审计记录'},
    {t:'apidoc',name:'API',icon:'code',desc:'接口文档'}]}
];
var TAB2GROUP={};GROUPS.forEach(function(g){g.tabs.forEach(function(x){TAB2GROUP[x.t]=g})});
var lastInGroup={};
// 主题列表：先给一份内置兜底（接口挂了界面照样能用），启动后由 /api/themes 覆盖
var FALLBACK_THEMES=[
  {id:'aurora',name:'极光',desc:'蓝紫渐变 · 明亮卡片'},
  {id:'console',name:'控制台',desc:'暗色 · 等宽字体 · 热力图'},
  {id:'classic',name:'经典',desc:'原版界面'}
];
var THEMES=FALLBACK_THEMES.slice();
var cur='dash';

// 主题 id 只认字母数字下划线短横线：既挡住脏值，也保证拼进 ./themes/<id>.css 不会跑出目录
function theme(){
  var t=localStorage.getItem('kb_theme');
  // 不能只按内置列表校验：新加的主题插件第一次刷新时列表还没拉回来，
  // 按列表校验会把用户刚选的主题打回极光。这里只校验 id 形态。
  return (t&&/^[A-Za-z0-9_-]+$/.test(t))?t:'aurora';
}
function shellOn(){return theme()!=='classic'}

// 主题样式：一套主题一个 CSS 文件，classic 不加载任何主题样式（= 原版界面）
function loadThemeCss(id){
  var l=document.getElementById('themeCss');
  if(!l)return;
  if(!id||id==='classic'){l.setAttribute('href','about:blank');return}
  l.setAttribute('href','./themes/'+id+'.css');
}

// 拉运行时主题列表；失败就保留内置三条，不抛错
function loadThemes(){
  if(typeof ap!=='function')return Promise.resolve(THEMES);
  return ap('/themes').then(function(d){
    var list=d&&d.themes;
    if(!Array.isArray(list)||!list.length)return THEMES;
    var clean=[];
    list.forEach(function(x){
      if(!x||typeof x.id!=='string'||!x.id)return;
      // id 只允许字母数字下划线短横线，防止拼进 href 时跑出 themes/ 目录
      if(!/^[A-Za-z0-9_-]+$/.test(x.id))return;
      clean.push({id:x.id,name:x.name||x.id,desc:x.desc||''});
    });
    if(clean.length)THEMES=clean;
    return THEMES;
  }).catch(function(){return THEMES});
}

function applyTheme(t){
  var h=document.documentElement;
  h.setAttribute('data-theme',t);
  if(t==='console'){h.classList.add('dark')}
  else{var s=localStorage.getItem('kb_dark');h.classList.toggle('dark',s==='1'||(!s&&window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches))}
  var dmt=document.getElementById('dmt');if(dmt)dmt.textContent=h.classList.contains('dark')?'☀️':'🌙';
  var meta=document.querySelector('meta[name="theme-color"]');
  if(meta)meta.setAttribute('content',t==='console'?'#0d1117':(t==='aurora'?'#5b6cf9':'#2563eb'));
}
function setTheme(t){localStorage.setItem('kb_theme',t);location.reload()}
window.kbSetTheme=setTheme;

// ── 构建外壳 ──
function buildHeader(){
  var hd=document.querySelector('#app>header');if(!hd)return;
  var h1=hd.querySelector('h1');
  if(h1)h1.innerHTML='<span class="kb-logo">'+ic('db')+'</span><span>知识库</span>';
  var left=h1&&h1.parentElement;
  if(left&&!document.getElementById('kbPrompt')){
    var p=document.createElement('div');p.id='kbPrompt';p.className='kb-prompt';
    left.parentElement.insertBefore(p,left);
  }
  var right=hd.querySelector('#dmt');
  if(right&&!document.getElementById('kbThemeBtn')){
    var b=document.createElement('button');b.id='kbThemeBtn';b.className='kb-hbtn';b.title='切换主题';b.innerHTML=ic('pal');
    b.onclick=openThemeSheet;right.parentElement.insertBefore(b,right);
  }
  var out=Array.prototype.find.call(hd.querySelectorAll('button'),function(x){return x.textContent.trim()==='退出'});
  if(out){out.classList.add('kb-hbtn');out.title='退出';out.innerHTML=ic('out')}
}
function buildSidebar(){
  var sb=document.querySelector('#app nav.sidebar');if(!sb)return;
  var h='<button onclick="st(\'dash\')" class="sidebar-item kb-sb" data-sb="dash">'+ic('home')+'<span>总览</span></button>';
  GROUPS.forEach(function(g){
    h+='<div class="kb-sb-h">'+g.name+'</div>';
    g.tabs.forEach(function(x){if(x.hidden)return;h+='<button onclick="st(\''+x.t+'\')" class="sidebar-item kb-sb" data-sb="'+x.t+'">'+ic(x.icon)+'<span>'+x.name+'</span></button>'});
  });
  sb.innerHTML=h;
}
function buildMobileNav(){
  var nav=document.getElementById('mobileNav');if(!nav)return;
  nav.classList.add('kb-nav');
  nav.innerHTML='<div class="mn-item" data-grp="dash" onclick="st(\'dash\')">'+ic('home')+'<em>总览</em></div>'+
    '<div class="mn-item" data-grp="know" onclick="kbGo(\'know\')">'+ic('book')+'<em>知识</em></div>'+
    '<div class="kb-fab-wrap"><button class="kb-fab" onclick="kbQuick()" aria-label="新建">'+ic('plus')+'</button></div>'+
    '<div class="mn-item" data-grp="ai" onclick="kbGo(\'ai\')">'+ic('spark')+'<em>AI</em></div>'+
    '<div class="mn-item" data-grp="sys" onclick="kbGo(\'sys\')">'+ic('gear')+'<em>系统</em></div>';
}
function buildSubtabs(){
  var main=document.querySelector('#app main');if(!main||document.getElementById('kbSub'))return;
  var d=document.createElement('div');d.id='kbSub';d.className='kb-sub hidden';main.insertBefore(d,main.firstChild);
}
function buildDash(){
  var dash=document.getElementById('tab-dash');if(!dash||document.getElementById('kbHero'))return;
  var hero=document.createElement('div');hero.id='kbHero';hero.className='kb-hero';
  hero.innerHTML='<div class="kb-hero-pr"><b>kb@server</b>:<i>~</i>$ status</div>'+
    '<div class="kb-hero-hi" id="kbHi"></div>'+
    '<div class="kb-hero-sub"><span class="kb-dot"></span><span id="kbHeroSub">正在读取知识库…</span></div>'+
    '<div class="kb-hero-search" onclick="st(\'sem\');setTimeout(function(){var q=document.getElementById(\'sq\');if(q)q.focus()},120)">'+ic('search')+'<span>搜索笔记、标签、项目…</span></div>';
  dash.insertBefore(hero,dash.firstChild);
  var hub=document.createElement('div');hub.id='kbHub';hub.className='kb-hub';
  var h='';
  GROUPS.forEach(function(g){
    var vis=g.tabs.filter(function(x){return !x.hidden});
    h+='<div class="kb-hub-g kb-g-'+g.id+'"><div class="kb-hub-t"><span>'+g.name+'</span><small>'+vis.length+' 项</small></div><div class="kb-hub-grid">';
    vis.forEach(function(x){h+='<button class="kb-tile" onclick="st(\''+x.t+'\')"><span class="kb-tile-ic">'+ic(x.icon)+'</span><span class="kb-tile-n">'+x.name+'</span><span class="kb-tile-d">'+x.desc+'</span></button>'});
    h+='</div></div>';
  });
  hub.innerHTML=h;
  dash.insertBefore(hub,hero.nextSibling); // 功能区紧跟 hero，不被统计卡挤到下面
  var heat=document.createElement('div');heat.id='kbHeatCard';heat.className='card p-4 kb-heat-card';
  heat.innerHTML='<h3 class="text-sm font-semibold text-gray-500 mb-3">写入热力图 <small>近 26 周</small></h3><div id="kbHeat" class="kb-heat"></div><div class="kb-heat-lg">少<i class="h0"></i><i class="h1"></i><i class="h2"></i><i class="h3"></i><i class="h4"></i>多</div>';
  var dc=document.getElementById('dc');dash.insertBefore(heat,dc?dc.nextSibling:null);
}
function buildSheet(){
  if(document.getElementById('kbSheet'))return;
  var o=document.createElement('div');o.id='kbSheetOv';o.className='kb-sheet-ov';o.onclick=closeSheet;
  var s=document.createElement('div');s.id='kbSheet';s.className='kb-sheet';
  document.body.appendChild(o);document.body.appendChild(s);
}
function openSheet(title,html){buildSheet();var s=document.getElementById('kbSheet');s.innerHTML='<div class="kb-sheet-h"><b>'+title+'</b><button class="kb-hbtn" onclick="kbCloseSheet()">'+ic('x')+'</button></div>'+html;document.getElementById('kbSheetOv').classList.add('on');s.classList.add('on')}
function closeSheet(){var s=document.getElementById('kbSheet');if(!s)return;s.classList.remove('on');document.getElementById('kbSheetOv').classList.remove('on')}
window.kbCloseSheet=closeSheet;
function openThemeSheet(){
  var t=theme(),h='<div class="kb-themes">';
  THEMES.forEach(function(x){h+='<button class="kb-theme'+(x.id===t?' on':'')+'" onclick="kbSetTheme(\''+x.id+'\')"><span class="kb-sw kb-sw-'+x.id+'"></span><span><b>'+x.name+'</b><small>'+x.desc+'</small></span></button>'});
  openSheet('主题',h+'</div>');
}window.kbThemeSheet=openThemeSheet;
window.kbQuick=function(){
  openSheet('新建',
    '<button class="kb-act" onclick="kbCloseSheet();st(\'llm\');setTimeout(function(){var e=document.getElementById(\'lt\');if(e){e.scrollIntoView({block:\'center\'});e.focus()}},150)">'+ic('file')+'<span><b>随手记</b><small>开头写 #标题 直接新建笔记</small></span></button>'+
    '<button class="kb-act" onclick="kbCloseSheet();st(\'llm\');setTimeout(function(){var e=document.getElementById(\'smartDoc\');if(e){e.scrollIntoView({block:\'center\'});e.focus()}},150)">'+ic('spark')+'<span><b>智能上传</b><small>粘贴 Markdown，自动分析 + 去重</small></span></button>'+
    '<button class="kb-act" onclick="kbCloseSheet();st(\'upload\')">'+ic('up')+'<span><b>上传文件</b><small>图片 / 附件存入 _assets</small></span></button>');
};
window.kbGo=function(gid){
  var g=GROUPS.filter(function(x){return x.id===gid})[0];if(!g)return;
  st(lastInGroup[gid]||g.tabs[0].t);
};

// ── 状态同步 ──
var PATH={dash:'~',notes:'~/知识/笔记',sem:'~/知识',graph:'~/知识/图谱',upload:'~/知识/上传',llm:'~/ai',market:'~/ai/商城',skills:'~/ai/技能',set:'~/系统',auth:'~/系统/授权',log:'~/系统/日志',apidoc:'~/系统/api'};
var CMD={dash:'status',notes:'ls',sem:'grep',graph:'graph',upload:'put',llm:'ask',skills:'mcp ls',set:'config',auth:'token ls',log:'tail -f',apidoc:'man api'};
function sync(n){
  cur=n;var g=TAB2GROUP[n];
  if(g)lastInGroup[g.id]=n;
  document.querySelectorAll('#mobileNav .mn-item').forEach(function(b){b.classList.toggle('active',n==='dash'?b.dataset.grp==='dash':(g&&b.dataset.grp===g.id))});
  var sub=document.getElementById('kbSub');
  if(sub){
    if(!g){sub.classList.add('hidden');sub.innerHTML=''}
    else{
      sub.classList.remove('hidden');
      sub.innerHTML='<div class="kb-sub-t">'+g.name+'</div><div class="kb-seg">'+g.tabs.map(function(x){return '<button class="'+(x.t===n?'on':'')+'" onclick="st(\''+x.t+'\')">'+x.name+'</button>'}).join('')+'</div>';
    }
  }
  var p=document.getElementById('kbPrompt');
  if(p)p.innerHTML='<b>kb@server</b>:<i>'+(PATH[n]||'~')+'</i>$ '+(CMD[n]||'');
  try{window.scrollTo(0,0)}catch(e){}
}
function greet(){var h=new Date().getHours();return h<5?'夜深了，早点休息':h<11?'早上好':h<13?'中午好':h<18?'下午好':'晚上好'}
function refreshDash(){
  var hi=document.getElementById('kbHi');if(hi)hi.textContent=greet();
  if(typeof ap!=='function')return;
  ap('/dashboard').then(function(d){
    if(!d||d.error)return;
    var el=document.getElementById('kbHeroSub');
    if(el)el.innerHTML='库里共 <b>'+esc(d.total)+'</b> 篇笔记 · 项目 '+esc(d.projects)+' · 知识 '+esc(d.knowledge);
  }).catch(function(){var el=document.getElementById('kbHeroSub');if(el)el.textContent='仪表盘暂不可用'});
  if(theme()==='console')renderHeat();
}
function renderHeat(){
  var box=document.getElementById('kbHeat');if(!box)return;
  ap('/notes?limit=500').then(function(d){
    var cnt={};(d.items||[]).forEach(function(i){var k=(i.modified||'').slice(0,10);if(k)cnt[k]=(cnt[k]||0)+1});
    var today=new Date();today.setHours(0,0,0,0);
    var start=new Date(today);start.setDate(start.getDate()-(25*7+today.getDay()));
    var max=1;Object.keys(cnt).forEach(function(k){if(cnt[k]>max)max=cnt[k]});
    var h='';
    for(var w=0;w<26;w++)for(var dd=0;dd<7;dd++){
      var dt=new Date(start);dt.setDate(start.getDate()+w*7+dd);
      if(dt>today){h+='<i class="hx" style="grid-column:'+(w+1)+';grid-row:'+(dd+1)+'"></i>';continue}
      var k=dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0')+'-'+String(dt.getDate()).padStart(2,'0');
      var c=cnt[k]||0,lv=c===0?0:Math.min(4,Math.ceil(c/max*4));
      h+='<i class="h'+lv+'" style="grid-column:'+(w+1)+';grid-row:'+(dd+1)+'" title="'+k+'：'+c+' 篇"></i>';
    }
    box.innerHTML=h;
  }).catch(function(){box.innerHTML='<span class="text-gray-400 text-xs">加载失败</span>'});
}

function init(){
  var t=theme();applyTheme(t);loadThemeCss(t);
  // 主题列表来自后端：先按内置列表把界面搭起来，再异步换成运行时列表
  loadThemes();
  if(!shellOn()){ // 经典主题：只加一个主题按钮，界面保持原样
    var dmt=document.getElementById('dmt');
    if(dmt&&!document.getElementById('kbThemeBtn')){document.body.insertAdjacentHTML('afterbegin',SPRITE);var b=document.createElement('button');b.id='kbThemeBtn';b.className='dark-toggle';b.title='切换主题';b.textContent='🎨';b.onclick=openThemeSheet;dmt.parentElement.insertBefore(b,dmt)}
    return;
  }
  document.body.insertAdjacentHTML('afterbegin',SPRITE);
  document.documentElement.classList.add('kb-shell');
  buildHeader();buildSidebar();buildMobileNav();buildSubtabs();buildDash();
  if(typeof window.st==='function'&&!window.st.__kb){
    var o=window.st;window.st=function(n){o(n);sync(n)};window.st.__kb=true;
  }
  if(typeof window.ld==='function'&&!window.ld.__kb){
    var l=window.ld;window.ld=function(){l();refreshDash()};window.ld.__kb=true;
  }
  if(typeof window.toggleDark==='function'&&t==='console'){window.toggleDark=function(){if(window.toast)toast.show('控制台主题固定为暗色','info')}}
  sync('dash');
  // 已登录时 app.js 可能在本脚本之前就调了 ld()，补一次
  var app=document.getElementById('app');if(app&&!app.classList.contains('hidden'))refreshDash();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

/* ── v1.3 图标统一：把界面里的 emoji 换成同一套线性 SVG 图标（不碰笔记正文 / 输入框） ── */
(function(){
'use strict';
var P={
 upload:'<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>',
 download:'<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>',
 check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
 tick:'<path d="m5 12 5 5 9-10"/>',
 xc:'<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
 x:'<path d="M6 6l12 12M18 6 6 18"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 hourglass:'<path d="M6 3h12M6 21h12"/><path d="M7 3c0 5 10 5 10 9s-10 4-10 9M17 3c0 5-10 5-10 9s10 4 10 9"/>',
 bot:'<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13v1M15 13v1"/><circle cx="12" cy="3.5" r="1"/>',
 toc:'<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
 file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
 folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
 folderopen:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v1"/><path d="M3 19 5.5 11h16L19 19z"/>',
 layers:'<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
 clipboard:'<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4h6v3H9zM9 12h6M9 16h4"/>',
 tag:'<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
 puzzle:'<path d="M10 3h4v3a2 2 0 1 0 4 0V5h1a2 2 0 0 1 2 2v3h-2a2 2 0 1 0 0 4h2v4a2 2 0 0 1-2 2h-4v-2a2 2 0 1 0-4 0v2H7a2 2 0 0 1-2-2v-4h1a2 2 0 1 0 0-4H5V7a2 2 0 0 1 2-2h3z"/>',
 link:'<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
 key:'<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
 search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
 refresh:'<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 3v5h5"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 21v-5h-5"/>',
 books:'<path d="M4 4h4v16H4zM10 4h4v16h-4z"/><path d="m16 5 3.5-1 3 15.5-3.5 1z"/>',
 chart:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
 trend:'<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
 save:'<path d="M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v5h8V3M7 21v-7h10v7"/>',
 gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
 brain:'<path d="M12 5a3 3 0 0 0-5.8-1A3 3 0 0 0 4 9a3 3 0 0 0 .5 5.5A3 3 0 0 0 9 19a3 3 0 0 0 3 1z"/><path d="M12 5a3 3 0 0 1 5.8-1A3 3 0 0 1 20 9a3 3 0 0 1-.5 5.5A3 3 0 0 1 15 19a3 3 0 0 1-3 1z"/>',
 box:'<path d="M3 7h18v4H3zM5 11v9h14v-9M10 15h4"/>',
 history:'<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
 bookopen:'<path d="M2 5h6a4 4 0 0 1 4 4v11a3 3 0 0 0-3-3H2zM22 5h-6a4 4 0 0 0-4 4v11a3 3 0 0 1 3-3h7z"/>',
 clip:'<path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/>',
 warn:'<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
 dot:'<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>',
 phone:'<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/>',
 inbox:'<path d="M3 13h5l1.5 3h5l1.5-3h5"/><path d="M5 5h14l2 8v6H3v-6z"/>',
 edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
 note:'<path d="M5 3h10l4 4v14H5z"/><path d="M9 11h6M9 15h6M9 7h3"/>',
 cal:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
 chat:'<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z"/>',
 home:'<path d="M3 10l9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
 pal:'<path d="M12 3a9 9 0 1 0 0 18c1 0 1.5-.7 1.5-1.5 0-1.2-1-1.5-1-2.5s.8-1.5 1.8-1.5H17a4 4 0 0 0 4-4c0-4.7-4-8.5-9-8.5z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="7" r="1"/><circle cx="15" cy="7.5" r="1"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 scale:'<path d="M12 4v16M7 20h10M5 8h14M5 8l-3 6a3 3 0 0 0 6 0zM19 8l-3 6a3 3 0 0 0 6 0z"/>',
 timer:'<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M10 2h4"/>',
 moon:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
};
// emoji → 图标名（箭头 → ← ↓ 保留为排版符号）
var M={'📤':'upload','📥':'inbox','✅':'check','✓':'tick','✔':'tick','❌':'xc','✕':'x','✖':'x','⏳':'hourglass','⌛':'hourglass','🕐':'clock','⏱':'timer','🤖':'bot','📑':'toc','📄':'file','📃':'file','📁':'folder','📂':'folderopen','🧬':'layers','📋':'clipboard','🏷':'tag','🧩':'puzzle','🔗':'link','🔑':'key','🔍':'search','🔎':'search','🔄':'refresh','📚':'books','📊':'chart','📈':'trend','💾':'save','⚙':'gear','🧠':'brain','📦':'box','📜':'history','📖':'bookopen','📎':'clip','⚠':'warn','🟢':'dot','📱':'phone','📝':'note','✏':'edit','📅':'cal','💬':'chat','🏠':'home','🎨':'pal','➕':'plus','⚖':'scale','🌙':'moon','☀':'sun','💡':'spark','🔒':'key','🗑':'x'};
var keys=Object.keys(M).sort(function(a,b){return b.length-a.length});
var RE=new RegExp('('+keys.map(function(k){return k.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}).join('|')+')\\uFE0F?\\s?','g');
var TEST=new RegExp(RE.source);
var SKIP={SCRIPT:1,STYLE:1,TEXTAREA:1,INPUT:1,CODE:1,PRE:1,svg:1,SVG:1};
function on(){return document.documentElement.classList.contains('kb-shell')}
function sprite(){
  if(document.getElementById('kbEmojiSprite'))return;
  var s='<svg id="kbEmojiSprite" width="0" height="0" style="position:absolute" aria-hidden="true"><defs>';
  for(var k in P)s+='<symbol id="ke-'+k+'" viewBox="0 0 24 24">'+P[k]+'</symbol>';
  document.body.insertAdjacentHTML('afterbegin',s+'</defs></svg>');
}
function svg(n){var ns='http://www.w3.org/2000/svg',e=document.createElementNS(ns,'svg');e.setAttribute('class','ki ki-e ki-e-'+n);e.setAttribute('aria-hidden','true');var u=document.createElementNS(ns,'use');u.setAttribute('href','#ke-'+n);e.appendChild(u);return e}
function skip(el){
  for(var p=el;p&&p!==document.body;p=p.parentNode){
    if(p.nodeType!==1)continue;
    if(SKIP[p.tagName]||p.isContentEditable)return true;
    var c=p.classList;if(c&&(c.contains('note-content')||c.contains('kb-noico')))return true;
    if(p.id==='laa'||p.id==='smartResult')return true;
  }
  return false;
}
function fixText(t){
  var v=t.nodeValue;if(!v||!TEST.test(v))return;
  var par=t.parentNode;if(!par||skip(par))return;
  if(par.tagName==='OPTION'||par.tagName==='TITLE'){t.nodeValue=v.replace(RE,'');return}
  var f=document.createDocumentFragment(),last=0;RE.lastIndex=0;var m;
  while((m=RE.exec(v))){if(m.index>last)f.appendChild(document.createTextNode(v.slice(last,m.index)));f.appendChild(svg(M[m[1]]));last=RE.lastIndex}
  if(last<v.length)f.appendChild(document.createTextNode(v.slice(last)));
  par.replaceChild(f,t);
}
function walk(root){
  if(!root)return;
  if(root.nodeType===3){fixText(root);return}
  if(root.nodeType!==1||SKIP[root.tagName])return;
  var w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null),n,list=[];
  while((n=w.nextNode()))if(TEST.test(n.nodeValue))list.push(n);
  list.forEach(fixText);
  // placeholder 里的 emoji 直接去掉
  if(root.querySelectorAll)root.querySelectorAll('[placeholder]').forEach(function(i){var p=i.getAttribute('placeholder');if(TEST.test(p))i.setAttribute('placeholder',p.replace(RE,''))});
}
var queue=[],sched=false;
function flush(){sched=false;var q=queue;queue=[];q.forEach(walk)}
function start(){
  if(!on())return;
  sprite();walk(document.body);
  new MutationObserver(function(ms){
    ms.forEach(function(m){
      if(m.type==='characterData')queue.push(m.target);
      else m.addedNodes.forEach(function(n){queue.push(n)});
    });
    if(!sched&&queue.length){sched=true;requestAnimationFrame(flush)}
  }).observe(document.body,{childList:true,subtree:true,characterData:true});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(start,0)});else start();
})();
