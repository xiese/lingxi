/* 示例插件：笔记字数
 *
 * 用来验证插件机制是否真的通了——
 *   装：web/plugins/note-wordcount/ 一放，刷新笔记页就能看到「本篇 N 字」
 *   卸：设置页关掉它（或删掉整个目录），这里就消失
 *
 * 只读 DOM，不发请求，不碰笔记数据。
 */
(function(){
'use strict';
window.kbPlugins = window.kbPlugins || {};

window.kbPlugins['note-wordcount'] = {
  mount: function(el, ctx){
    var body = document.getElementById('noteViewContent');
    if(!body) return;

    var text = (body.innerText || body.textContent || '').trim();
    // 中英混排：中文按字算，英文按词算，都折算成「字」的口径
    var cn = (text.match(/[\u4e00-\u9fa5]/g) || []).length;
    var en = (text.replace(/[\u4e00-\u9fa5]/g, ' ').match(/[A-Za-z0-9_]+/g) || []).length;
    var count = cn + en;
    // 中文阅读速度约 300 字/分钟
    var mins = Math.max(1, Math.round(count / 300));

    el.innerHTML =
      '<div class="card p-3" style="background:var(--card-bg);border:1px solid var(--card-border)">' +
        '<div class="text-xs" style="color:var(--text2)">' +
          '📝 本篇 <b>' + count + '</b> 字 · 约 <b>' + mins + '</b> 分钟读完' +
          '<span class="ml-2" style="opacity:.6">（插件：' + (ctx && ctx.title || '笔记字数') + '）</span>' +
        '</div>' +
      '</div>';
  }
};
})();
