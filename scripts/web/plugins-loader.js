/* ── v1.4 功能插件加载器 ──
 *
 * 契约（见 scripts/kb_lib/services/plugins.py）：
 *   一个插件 = web/plugins/<name>/ 目录 + manifest.json + 入口 JS
 *   manifest 里声明 mount（挂载点）与 entry（入口文件）
 *
 * 挂载点容器（由 index.html / app.js 预留）：
 *   dash        -> #kbPluginDash       总览页底部
 *   note-detail -> #kbPluginNote       笔记详情底部（每次打开笔记重建，需重新挂）
 *   settings    -> #kbPluginSettings   设置页底部
 *
 * 插件入口 JS 的约定：导出 `mount(el, ctx)`，el 是挂载点容器，ctx 带
 * {name, title, version, api}。不调用 mount 的插件什么也不会发生（不报错），
 * 这样写坏一个插件不会拖垮整页。
 *
 * 只做前端插件：这里只注入 <script>，不执行任何后端逻辑。
 */
(function(){
'use strict';

var MOUNT_SEL = {
  'dash': '#kbPluginDash',
  'note-detail': '#kbPluginNote',
  'settings': '#kbPluginSettings'
};

// 已挂载的插件名，避免重复注入（切换 tab / 重开笔记都会重新触发挂载）
var mounted = {};
var plugins = null;

// 已加载过的插件脚本 url，避免重复注入（重复注入会让插件副作用累积）
var scriptLoaded = {};

function loadScript(url, timeoutMs){
  // 已经注入过的脚本直接复用，不重复 createElement
  if(scriptLoaded[url]) return Promise.resolve(url);
  var ms = timeoutMs || 8000;
  return new Promise(function(res, rej){
    var done = false;
    var timer = null;
    function finish(ok, val){
      if(done) return;
      done = true;
      if(timer) clearTimeout(timer);
      if(ok){ scriptLoaded[url] = true; res(val) } else { rej(val) }
    }
    var s = document.createElement('script');
    s.src = url;
    s.async = false;
    s.onload = function(){ finish(true, url) };
    s.onerror = function(){ finish(false, new Error('插件脚本加载失败: ' + url)) };
    // 脚本既不 onload 也不 onerror（被扩展拦、网络半开）时兜底，别留永久空壳
    timer = setTimeout(function(){
      finish(false, new Error('插件脚本加载超时: ' + url));
    }, ms);
    document.head.appendChild(s);
  });
}

// 插件 id 只认字母数字下划线短横线（后端也校验，这里再挡一层）
function safeId(n){ return typeof n === 'string' && /^[A-Za-z0-9_-]+$/.test(n) }

function containerFor(mount){
  var sel = MOUNT_SEL[mount];
  return sel ? document.querySelector(sel) : null;
}

// 挂载一批插件到指定挂载点
function mountAll(list, mount){
  var box = containerFor(mount);
  if(!box) return Promise.resolve(0);
  var todo = [];
  (list || []).forEach(function(p){
    if(!p || !safeId(p.name) || p.mount !== mount) return;
    if(mounted[p.name]) return;
    mounted[p.name] = true;
    todo.push(p);
  });
  if(!todo.length) return Promise.resolve(0);

  return Promise.all(todo.map(function(p){
    var el = document.createElement('div');
    el.className = 'kb-plugin';
    el.setAttribute('data-plugin', p.name);
    box.appendChild(el);
    return loadScript(p.url).then(function(){
      var mod = window.kbPlugins && window.kbPlugins[p.name];
      if(mod && typeof mod.mount === 'function'){
        mod.mount(el, {name: p.name, title: p.title, version: p.version, api: (typeof ap === 'function' ? ap : null)});
      }else{
        // 脚本没按约定导出 mount：把容器撤掉，别在界面上留个空壳
        if(el.parentNode) el.parentNode.removeChild(el);
        mounted[p.name] = false;
        if(window.console && console.warn) console.warn('[plugin] ' + p.name + ' 没有导出 mount()，已跳过');
      }
    }).catch(function(e){
      if(el.parentNode) el.parentNode.removeChild(el);
      mounted[p.name] = false;
      if(window.console && console.warn) console.warn('[plugin]', e && e.message || e);
    });
  })).then(function(){ return todo.length });
}

function fetchEnabled(){
  if(plugins) return Promise.resolve(plugins);
  if(typeof ap !== 'function') return Promise.resolve([]);
  // ap() 不补 /api 前缀，路径要写全
  return ap('/api/plugins/enabled').then(function(d){
    var list = d && d.plugins;
    if(!Array.isArray(list)) return [];
    plugins = list.filter(function(p){ return p && safeId(p.name) && p.mount && p.url });
    return plugins;
  }).catch(function(){ return [] });
}

// 对外：挂到某个挂载点。页面各处在合适时机调用它。
function mountPoint(mount){
  if(!MOUNT_SEL[mount]) return Promise.resolve(0);
  return fetchEnabled().then(function(list){ return mountAll(list, mount) });
}

// 启停插件后要重新拉清单（缓存作废 + 已挂记录清空）
function invalidate(){
  plugins = null;
  mounted = {};
}

window.kbPlugins = window.kbPlugins || {};
window.kbPluginMount = mountPoint;
window.kbPluginInvalidate = invalidate;

// 启动即挂总览页与设置页；笔记详情由 app.js 每次打开笔记时调 kbPluginMount('note-detail')
function boot(){
  mountPoint('dash');
  mountPoint('settings');
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();

})();
