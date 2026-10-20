/* 使用增强（usage-boost）
 *
 * 两条独立待办，共用一份全量笔记清单：
 *   T11 趋势图：取数从 /notes?limit=500 换成 /api/notes/tree（后端 limit 硬卡 200，
 *       500 只回 200 条，12 个月趋势只用了 10% 样本）。再加快照粒度「日/月」切换
 *       与「点柱子看该时间点入库数」。
 *   T13 目录：平铺列表改成两层——默认只列顶层文件夹，点进去列该文件夹内笔记，
 *       面包屑可返回上一级；空文件夹保留（0 篇也显示）。
 *
 * 挂载点用现有的 dash：加载器启动即挂 dash/settings，与当前 tab 无关；
 * 目录在另一个 tab 里，没有专属容器，所以这里通过包装全局 lt()/ltFilter() 接管
 * 目录面板的渲染，不新增 DOM 容器、不改 index.html。
 *
 * 开关口径：插件停用时后端 /api/plugins/enabled 不返回本插件，plugin.js 根本不会
 * 被注入，页面走 app.js 原逻辑（趋势图用旧接口、目录平铺）。mount 里再查一次
 * 启用清单做兜底，避免脚本被缓存/手工注入时绕过开关。
 */
(function(){
'use strict';
window.kbPlugins = window.kbPlugins || {};

var NAME = 'usage-boost';
// 标准顶层目录：空目录在 /api/notes/tree 里没有任何条目，前端无从枚举，
// 用这份清单兜底，保证「空文件夹也显示」。口径 = config.SCAN_DIRS
// （10-项目/20-知识/30-写作/30-记录/40-日记/50-档案/99-待整理）
// 再并上 TYPE_MAP 里的 60-梦境 与用户私有的 70-经验。
var CANON = ['10-项目', '20-知识', '30-写作', '30-记录', '40-日记', '50-档案',
             '60-梦境', '70-经验', '99-待整理'];
var MONTHS = 12;
var DAYS = 30;

// items 缓存 15 秒：切回总览/目录时能拿到新笔记，又不会每切一次 tab 就拉全量
var ITEMS_TTL = 15000;
var S = { items: null, at: 0, pending: null, grain: 'month', sel: null, folder: null, api: null };

// ── 小工具 ──
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function jss(s){
  return esc(String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}
function pad(n){ return (n < 10 ? '0' : '') + n }
function ym(d){ return d.getFullYear() + '-' + pad(d.getMonth() + 1) }
function ymd(d){ return ym(d) + '-' + pad(d.getDate()) }

// ── 分桶（纯函数，便于单测）──
// 月：最近 12 个自然月，升序；日：最近 30 天（含今天），升序。
// 键一律用本地时间拼，不用 toISOString——东八区下 toISOString 会把 1 号零点
// 折回上个月，原实现 `new Date(y,m-i,1).toISOString().slice(0,7)` 就有这个坑。
function monthKeys(now, n){
  now = now || new Date(); n = n || MONTHS;
  var out = [];
  for (var i = n - 1; i >= 0; i--) out.push(ym(new Date(now.getFullYear(), now.getMonth() - i, 1)));
  return out;
}
function dayKeys(now, n){
  now = now || new Date(); n = n || DAYS;
  var out = [];
  for (var i = n - 1; i >= 0; i--) out.push(ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - i)));
  return out;
}
// mode: 'month' 取 modified 前 7 位，'day' 取前 10 位
function countBy(items, keys, mode){
  var idx = {}, out = {};
  keys.forEach(function(k){ idx[k] = 1; out[k] = 0 });
  (items || []).forEach(function(it){
    var m = String((it && it.modified) || '');
    if (!m) return;
    var k = mode === 'day' ? m.slice(0, 10) : m.slice(0, 7);
    if (idx[k]) out[k]++;
  });
  return out;
}

// ── 目录分组（纯函数）──
function groupTop(items){
  var folders = {}, rootNotes = [], names = {};
  (items || []).forEach(function(it){
    var p = String((it && it.path) || '');
    if (!p) return;
    var seg = p.split('/');
    var base = seg[seg.length - 1];
    // 与 renderTree 同口径：模板占位文件、隐藏文件不进目录
    if (base.charAt(0) === '.' || base.indexOf('{{') === 0) return;
    if (seg.length < 2) { rootNotes.push(it); return }
    var top = seg[0];
    if (!folders[top]) { folders[top] = []; names[top] = 1 }
    folders[top].push(it);
  });
  // 空文件夹保留：标准顶层目录即使一条笔记都没有也列出来
  CANON.forEach(function(n){ if (!folders[n]) folders[n] = [] });
  var keys = Object.keys(folders).sort(function(a, b){
    var na = parseInt(a, 10), nb = parseInt(b, 10);
    if (isFinite(na) && isFinite(nb) && na !== nb) return na - nb;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  return { folders: folders, names: keys, rootNotes: rootNotes };
}

// ── 取数：全量清单，一次请求多处复用 ──
function loadItems(api){
  var fresh = S.items && (Date.now() - S.at < ITEMS_TTL);
  if (fresh) return Promise.resolve(S.items);
  if (S.pending) return S.pending;
  S.pending = api('/api/notes/tree').then(function(d){
    if (!d || d.error || !Array.isArray(d.items)) throw new Error((d && d.error) || '目录数据异常');
    S.items = d.items;
    S.at = Date.now();
    S.pending = null;
    return S.items;
  }).catch(function(e){
    S.pending = null;
    throw e;
  });
  return S.pending;
}
function invalidate(){ S.items = null; S.at = 0; S.pending = null }

// ── T11 趋势图 ──
function trendHtml(items){
  var day = S.grain === 'day';
  var keys = day ? dayKeys(new Date(), DAYS) : monthKeys(new Date(), MONTHS);
  var counts = countBy(items, keys, day ? 'day' : 'month');
  var max = 1;
  keys.forEach(function(k){ if (counts[k] > max) max = counts[k] });
  var btn = function(g, label){
    var on = S.grain === g;
    return '<button onclick="kbUsageBoost.grain(\'' + g + '\')" style="font-size:11px;padding:2px 10px;border-radius:12px;cursor:pointer;'
      + (on ? 'background:var(--blue,#3b82f6);color:#fff;border:1px solid transparent'
            : 'background:transparent;color:var(--text3,#6b7280);border:1px solid var(--card-border,#e5e7eb)')
      + '">' + label + '</button>';
  };
  var h = '<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">'
    + btn('day', '日') + btn('month', '月')
    + '<span style="font-size:10px;color:var(--text4,#9ca3af);margin-left:4px">'
    + (day ? '近 30 天' : '近 12 个月') + ' · 全量 ' + items.length + ' 篇</span></div>';

  h += '<div style="display:flex;align-items:flex-end;gap:3px;height:150px">';
  keys.forEach(function(k, i){
    var n = counts[k];
    var pct = Math.max(n / max * 100, 2);
    var on = S.sel === k;
    // 日粒度 30 根柱子，标签只标每 5 天，避免糊成一团；完整日期在 title 里
    var label = day ? (i % 5 === 0 ? String(Number(k.slice(8, 10))) : '') : String(Number(k.slice(5, 7)));
    h += '<div onclick="kbUsageBoost.pick(\'' + k + '\')" title="' + k + '：' + n + ' 篇"'
      + ' style="flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;cursor:pointer">'
      + '<div style="height:' + pct + '%;background:var(--blue,#3b82f6);opacity:' + (on ? '1' : '.72') + ';border-radius:3px 3px 0 0;min-height:2px' + (on ? ';outline:1px solid var(--text2,#374151)' : '') + '"></div>'
      + '<div style="font-size:9px;line-height:12px;height:12px;text-align:center;color:var(--text4,#9ca3af)">' + label + '</div>'
      + '</div>';
  });
  h += '</div>';
  h += '<div id="kbUbDetail" style="font-size:11px;margin-top:6px;color:var(--text3,#6b7280)">'
    + (S.sel ? esc(S.sel) + '：<b>' + counts[S.sel] + '</b> 篇'
             : '点柱子看该时间点的入库数')
    + '</div>';
  return h;
}
function renderTrend(api){
  var box = document.getElementById('trendChart');
  if (!box) return;
  box.innerHTML = '<span style="font-size:12px;color:var(--text4,#9ca3af)">加载中...</span>';
  loadItems(api).then(function(items){
    var el = document.getElementById('trendChart');
    if (!el) return;
    // 选中的时间点不在当前粒度里就清掉，避免详情行显示上一个粒度的残留
    var keys = S.grain === 'day' ? dayKeys(new Date(), DAYS) : monthKeys(new Date(), MONTHS);
    if (S.sel && keys.indexOf(S.sel) < 0) S.sel = null;
    el.innerHTML = trendHtml(items);
  }).catch(function(){
    var el = document.getElementById('trendChart');
    if (el) el.innerHTML = '<span style="font-size:12px;color:var(--text4,#9ca3af)">加载失败</span>';
  });
}

// ── T13 目录两层 ──
function folderTitle(name){ return String(name) }

function dirListHtml(items, filter){
  var g = groupTop(items);
  var f = String(filter || '').toLowerCase();
  var h = '';
  var shown = 0;
  g.names.forEach(function(name){
    var list = g.folders[name];
    if (f && name.toLowerCase().indexOf(f) < 0) return;
    shown++;
    h += '<div onclick="kbUsageBoost.open(\'' + jss(name) + '\')" title="' + esc(name) + '"'
      + ' style="display:flex;align-items:center;justify-content:space-between;gap:6px;padding:5px 6px;border-radius:6px;cursor:pointer;font-size:13px"'
      + ' onmouseover="this.style.background=\'var(--hover,#f3f4f6)\'" onmouseout="this.style.background=\'transparent\'">'
      + '<span style="color:var(--text,#111827);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">📂 ' + esc(folderTitle(name)) + '</span>'
      + '<span style="font-size:10px;color:var(--text4,#9ca3af);flex-shrink:0">' + list.length + ' 篇</span>'
      + '</div>';
  });
  if (g.rootNotes.length) {
    h += '<div style="font-size:11px;color:var(--text4,#9ca3af);margin:8px 0 2px">📄 根目录笔记</div>';
    g.rootNotes.forEach(function(it){
      if (f && String(it.path || '').toLowerCase().indexOf(f) < 0 && String(it.title || '').toLowerCase().indexOf(f) < 0) return;
      shown++;
      h += noteRow(it, String(it.path || ''));
    });
  }
  if (!shown) h = '<p style="font-size:12px;color:var(--text4,#9ca3af);text-align:center;padding:12px 0">无匹配内容</p>';
  return h;
}

function noteRow(it, label){
  var p = String((it && it.path) || '');
  var checked = (window.batchSet && window.batchSet.has(p)) ? ' checked' : '';
  var icon = it.type === 'project' ? '📁' : it.type === 'diary' ? '📅' : '📄';
  return '<div style="display:flex;align-items:center;gap:4px;padding:3px 6px;border-radius:6px;font-size:13px"'
    + ' onmouseover="this.style.background=\'var(--hover,#f3f4f6)\'" onmouseout="this.style.background=\'transparent\'">'
    + '<input type="checkbox" class="batch-check" data-bp="' + esc(p) + '"' + checked
    + ' onclick="event.stopPropagation();bcToggle(\'' + jss(p) + '\')">'
    + '<span style="flex:1;min-width:0;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#111827)"'
    + ' onclick="rn(\'' + jss(p) + '\')" title="' + esc(p) + '">' + icon + ' ' + esc(label) + '</span></div>';
}

function dirFolderHtml(items, folder, filter){
  var f = String(filter || '').toLowerCase();
  var rows = (items || []).filter(function(it){
    var p = String((it && it.path) || '');
    if (p.indexOf(folder + '/') !== 0) return false;
    var base = p.split('/').pop();
    if (base.charAt(0) === '.' || base.indexOf('{{') === 0) return false;
    if (!f) return true;
    return p.toLowerCase().indexOf(f) >= 0 || String(it.title || '').toLowerCase().indexOf(f) >= 0;
  });
  var h = '<div style="display:flex;align-items:center;gap:5px;font-size:12px;margin-bottom:6px;flex-wrap:wrap">'
    + '<a onclick="kbUsageBoost.back()" style="cursor:pointer;color:var(--blue,#3b82f6);text-decoration:none">🏠 全部</a>'
    + '<span style="color:var(--text4,#9ca3af)">/</span>'
    + '<span style="color:var(--text2,#4b5563)">📂 ' + esc(folderTitle(folder)) + '</span>'
    + '<span style="font-size:10px;color:var(--text4,#9ca3af)">（' + rows.length + ' 篇）</span></div>';
  if (!rows.length) {
    h += '<p style="font-size:12px;color:var(--text4,#9ca3af);text-align:center;padding:12px 0">'
      + (f ? '无匹配内容' : '这个文件夹是空的') + '</p>';
    return h;
  }
  // 显示相对该文件夹的路径，子目录里的笔记也能一眼看出层级
  rows.forEach(function(it){
    h += noteRow(it, String(it.path || '').slice(folder.length + 1));
  });
  return h;
}

function renderDir(api){
  var box = document.getElementById('nt');
  if (!box) return;
  var filterEl = document.getElementById('noteFilter');
  var filter = filterEl ? filterEl.value : '';
  loadItems(api).then(function(items){
    var el = document.getElementById('nt');
    if (!el) return;
    el.innerHTML = S.folder ? dirFolderHtml(items, S.folder, filter) : dirListHtml(items, filter);
  }).catch(function(){
    var el = document.getElementById('nt');
    if (el) el.innerHTML = '<p style="font-size:12px;color:#ef4444;text-align:center;padding:12px 0;cursor:pointer" onclick="lt()">目录加载失败，点此重试</p>';
  });
}

// ── 接管全局函数 ──
function install(api){
  if (window.__kbUsageBoostOn) return;
  window.__kbUsageBoostOn = true;
  S.api = api;

  window.kbUsageBoost = {
    grain: function(g){
      if (g !== 'day' && g !== 'month') return;
      S.grain = g; S.sel = null;
      renderTrend(S.api);
    },
    pick: function(k){
      S.sel = (S.sel === k) ? null : k;
      renderTrend(S.api);
    },
    open: function(name){
      S.folder = name;
      renderDir(S.api);
    },
    back: function(){
      S.folder = null;
      renderDir(S.api);
    }
  };

  window.ltr = function(){ renderTrend(S.api) };
  window.lt = function(){ renderDir(S.api) };
  window.ltFilter = function(){ renderDir(S.api) };
  // 保存/新建后会调 ltInvalidate()，缓存必须一起作废，否则新笔记不进目录
  var origInv = window.ltInvalidate;
  window.ltInvalidate = function(){
    invalidate();
    if (typeof origInv === 'function') { try { origInv() } catch (e) {} }
  };

  // 首屏 ld()/lt() 可能已经用旧接口画过一遍，这里补一次全量重绘。
  // 目录面板只在笔记 tab 打开时才有内容，容器始终在 DOM 里，所以都直接重绘。
  if (document.getElementById('trendChart')) renderTrend(S.api);
  if (document.getElementById('nt')) renderDir(S.api);
}

window.kbPlugins[NAME] = {
  mount: function(el, ctx){
    var api = (ctx && typeof ctx.api === 'function') ? ctx.api : (typeof window.ap === 'function' ? window.ap : null);
    if (!api) return;
    // 开关兜底：脚本若在插件停用后被加载（缓存/手工注入），这里拒绝接管，页面保持原行为
    api('/api/plugins/enabled').then(function(d){
      var list = (d && d.plugins) || [];
      var on = list.some(function(p){ return p && p.name === NAME });
      if (!on) return;
      install(api);
    }).catch(function(){ /* 查不到启用状态就不接管，宁可回到原行为 */ });
  }
};

// 纯函数出口：给打桩测试直接调，不必模拟 DOM
window.kbUsageBoostInternals = {
  monthKeys: monthKeys, dayKeys: dayKeys, countBy: countBy,
  groupTop: groupTop, folderTitle: folderTitle, CANON: CANON.slice()
};
})();
