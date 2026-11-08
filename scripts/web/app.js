/* 知识库 Web UI app.js — 从 index.html 抽出独立维护 */
let A='',T='';var uploadFileData=null,currentEditPath='';
// ─── Toast 通知系统 (v5.5 J3) ───
var toast={show:function(m,type,ms){type=type||'info';ms=ms||3500;var ct=document.getElementById('toast-ct'),d=document.createElement('div');d.className='toast '+type;d.textContent=m;d.onclick=function(){d.remove()};ct.appendChild(d);setTimeout(function(){d.style.opacity='0';d.style.transition='opacity .3s';setTimeout(function(){d.remove()},300)},ms)}};
// ─── 骨架屏 (v5.5 J5) ───
function skDash(){return'<div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">'+Array(6).fill('<div class="card p-5 text-center"><div class="skeleton" style="height:36px;width:50px;margin:0 auto 8px"></div><div class="skeleton sk-row" style="height:16px;width:40px;margin:0 auto"></div></div>').join('')+'</div>'}
function skNote(){return'<div class="card p-6"><div class="skeleton sk-h1"></div><div class="skeleton sk-row sk-w60"></div><div class="skeleton sk-row sk-w40"></div><div class="skeleton sk-row sk-w80"></div><div class="skeleton sk-row sk-w60"></div><div class="skeleton sk-row" style="height:40px"></div><div class="skeleton sk-row sk-w80"></div><div class="skeleton sk-row sk-w40"></div><div class="skeleton sk-img"></div><div class="skeleton sk-row sk-w60"></div><div class="skeleton sk-row"></div></div>'}
function skSearch(){return Array(4).fill('<div class="card p-4"><div class="skeleton sk-row sk-w60" style="height:18px"></div><div class="skeleton sk-row sk-w80" style="height:14px;margin-top:8px"></div><div class="skeleton sk-row" style="height:36px;margin-top:8px"></div></div>').join('')}
// ─── 面包屑 (v5.5 K3) ───
function bc(path){var p=String(path||'').replace(/\.\w+$/,'').split('/'),h='',cum='';for(var i=0;i<p.length;i++){cum+=(cum?'/':'')+p[i];if(i==p.length-1)h+='<span>📄 '+esc(p[i])+'</span>';else h+='<a onclick="st(\'notes\');setTimeout(function(){lt()},200)">🏠</a><span>/</span><a onclick="rn(\''+jss(cum+'/')+'\')">📂 '+esc(p[i])+'</a><span>/</span>'}return'<div class="breadcrumb">'+h+'</div>'}
// 401 统一处理：首屏会并发发起多个请求，无 Token 或 Token 失效时每个请求各返回一次 401，
// 旧写法逐个弹 confirm，表现为「一直提示返回」。改为单次提示 + 并发去重：
//   · 只处理第一次 401，后续 401 静默丢弃；
//   · 已进入面板才触发（登录页里的 401 由 dl() 就地提示，避免双重提示）；
//   · 不弹确认框，直接回登录页并给出一次明确引导。
var _auth401Done=false;
function on401(){
  if(_auth401Done)return;
  if(document.getElementById('app').classList.contains('hidden'))return;
  _auth401Done=true;
  rl();
  toast.show('登录状态已失效，请重新输入 Token 后进入面板','warning',6000);
}
function ap(p,o={}){return fetch(A+p,{...o,headers:{'Authorization':'Bearer '+T,...o.headers||{}}}).then(r=>r.json().then(d=>{if(d.offline)notifyOffline();if(d.error&&r.status===401)on401();return d}))}
// Service Worker 在断网时返回 {offline:true}，与真正的业务错误区分开，只提示一次
var _offlineNotified=false;
function notifyOffline(){
  if(_offlineNotified)return;
  _offlineNotified=true;
  toast.show('当前离线，显示的可能不是最新内容','warning',5000);
  window.addEventListener('online',function(){_offlineNotified=false}, {once:true});
}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
// 内联 JS 字符串参数的安全转义（onclick="rn('...')" 这类）。
// 先按 JS 字符串规则转义反斜杠与单引号，再做 HTML 属性转义；
// HTML 解析器会把 &#39; 还原成 '，JS 看到的就是已转义的 \'。
function jss(s){return escapeHtml(String(s??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'"))}
// 数值/枚举等可信值直接用；此别名让调用点读起来更短
var esc=escapeHtml;
function downloadAuth(p,filename){fetch(A+p,{headers:{'Authorization':'Bearer '+T}}).then(async r=>{if(!r.ok){let d={};try{d=await r.json()}catch(e){};toast.show(d.error||('下载失败: HTTP '+r.status),'error');return}const blob=await r.blob();const u=URL.createObjectURL(blob);const a=document.createElement('a');a.href=u;a.download=filename;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u);toast.show('下载已开始','success')}).catch(()=>toast.show('下载失败','error'))}
const fileUrlCache={};
const _FILE_URL_CACHE_MAX=50; // 限制缓存数量，防止 Blob URL 内存泄漏
function authFileUrl(path){
  var key=String(path||'');
  if(fileUrlCache[key])return fileUrlCache[key];
  // 超限时清理最早的缓存项
  var keys=Object.keys(fileUrlCache);
  if(keys.length>=_FILE_URL_CACHE_MAX){
    var oldest=keys[0];
    try{URL.revokeObjectURL(fileUrlCache[oldest])}catch(e){}
    delete fileUrlCache[oldest];
  }
  fetch(A+'/files/'+encodeURIComponent(key),{headers:{'Authorization':'Bearer '+T}}).then(async r=>{if(!r.ok)throw new Error('HTTP '+r.status);var blob=await r.blob();var url=URL.createObjectURL(blob);fileUrlCache[key]=url;document.querySelectorAll('[data-auth-src="'+CSS.escape(key)+'"]').forEach(el=>el.src=url)}).catch(()=>{});return ''
}
function downloadFilePath(path){var name=(String(path||'file').split('/').pop()||'file');downloadAuth('/files/'+encodeURIComponent(path),name)}

function sl(t){document.querySelectorAll('.login-btn').forEach(b=>{b.classList.remove('border-blue-500','text-blue-600');b.classList.add('border-transparent','text-gray-400')});var b=document.querySelector('[data-lt="'+t+'"]');b.classList.add('border-blue-500','text-blue-600');b.classList.remove('border-transparent','text-gray-400');['token','rq','res'].forEach(x=>document.getElementById('login-'+x).classList.add('hidden'));document.getElementById('login-'+t).classList.remove('hidden');document.getElementById('le').classList.add('hidden')}
function rl(){T='';localStorage.removeItem('kb_token');document.getElementById('app').classList.add('hidden');document.getElementById('login').classList.remove('hidden');sl('token');document.getElementById('ti').value=''}
function lo(){rl()}
function se(m){const e=document.getElementById('le');e.textContent=m;e.classList.remove('hidden');setTimeout(()=>e.classList.add('hidden'),5000)}
function dl(auto){var v=document.getElementById('ti').value.trim();if(v)T=v;if(!T)return se('请输入Token');A=location.origin;_auth401Done=false;ap('/dashboard').then(d=>{if(d.error){se(d.error);return}localStorage.setItem('kb_token',T);ea()}).catch(()=>se('连接失败'))}
var authPollTimer=null;
function copyText(t){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(()=>toast.show('已复制','success')).catch(()=>copyTextFallback(t));return}copyTextFallback(t)}
function copyTextFallback(t){const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();toast.show('已复制','success')}
function tokenCard(d,source){return `<div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4 text-sm"><div class="font-semibold text-emerald-800 mb-2">${escapeHtml(source||'Token 已生成')}</div><div class="text-xs text-emerald-700 mb-2">设备：${escapeHtml(d.device||d.name||'')} · 权限：${escapeHtml(d.permission||'')} · 过期：${escapeHtml((d.expires_at||'').slice(0,16))}</div><div class="font-mono text-xs bg-white border border-emerald-100 rounded p-2 break-all">${escapeHtml(d.token||'')}</div><div class="mt-3 flex gap-2"><button onclick="copyText('${escapeHtml(d.token||'')}')" class="bg-emerald-600 text-white px-3 py-1 rounded text-sm">复制 Token</button><button onclick="document.getElementById('issuedToken')?.classList.add('hidden')" class="bg-gray-100 text-gray-600 px-3 py-1 rounded text-sm">关闭</button></div></div>`}
function showIssuedToken(d,source){const el=document.getElementById('issuedToken');if(!el)return;el.classList.remove('hidden');el.innerHTML=tokenCard(d,source)}
function splitList(v){return String(v||'').split(/[,，、]/).map(x=>x.trim()).filter(Boolean)}
function dr(){const n=document.getElementById('rn').value.trim()||'Web',p=document.getElementById('rp').value;const body={name:n,permission:p,identity_label:document.getElementById('ridLabel').value.trim(),platform:document.getElementById('rPlatform').value.trim(),project_scope:document.getElementById('rScope').value.trim(),default_dir:document.getElementById('rDefaultDir').value.trim(),default_tags:splitList(document.getElementById('rDefaultTags').value)};document.getElementById('rb').disabled=true;fetch(A+'/auth/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(d=>{sl('res');if(d.error){document.getElementById('lrc').innerHTML=`<p class="text-red-500">${escapeHtml(d.error)}</p>`;return}const req={request_id:d.request_id,claim:d.claim,created_at:new Date().toISOString()};localStorage.setItem('kb_auth_request',JSON.stringify(req));document.getElementById('lrc').innerHTML=`<div class="text-center"><p class="text-5xl mb-3">📤</p><p class="text-emerald-600 font-semibold">已提交</p><p class="text-gray-500 text-xs mt-1">ID: <span class="font-mono text-blue-600">${escapeHtml(d.request_id)}</span></p><button onclick="copyText('${escapeHtml(d.request_id)}')" class="mt-2 bg-gray-100 text-gray-600 px-3 py-1 rounded text-xs">复制申请 ID</button><p id="rqStatus" class="text-amber-500 text-xs mt-3">⏳ 等待主Token审批，正在自动查询…</p></div>`;startRequestPolling(d.request_id,d.claim)}).catch(e=>{document.getElementById('lrc').innerHTML=`<p class="text-red-500">${escapeHtml(e)}</p>`}).finally(()=>{document.getElementById('rb').disabled=false})}
function startRequestPolling(id,claim){stopRequestPolling();checkRequestStatus(id,claim);authPollTimer=setInterval(()=>checkRequestStatus(id,claim),5000)}
function stopRequestPolling(){if(authPollTimer){clearInterval(authPollTimer);authPollTimer=null}}
function checkRequestStatus(id,claim){fetch(A+'/auth/request/'+encodeURIComponent(id)+'/status?claim='+encodeURIComponent(claim)).then(r=>r.json()).then(d=>{const el=document.getElementById('rqStatus')||document.getElementById('lrc');if(d.error){if(el)el.innerHTML='<span class="text-red-500">'+escapeHtml(d.error)+'</span>';stopRequestPolling();return}if(d.status==='pending'){if(el)el.textContent='⏳ 等待审批中…';return}if(d.status==='rejected'){if(el)el.innerHTML='<span class="text-red-500">申请已拒绝</span>';localStorage.removeItem('kb_auth_request');stopRequestPolling();return}if(d.status==='approved'&&d.token){localStorage.removeItem('kb_auth_request');stopRequestPolling();document.getElementById('lrc').innerHTML=`<div class="text-left">${tokenCard(d,'申请已批准')}<button onclick="useReturnedToken('${escapeHtml(d.token)}')" class="mt-3 w-full bg-blue-600 text-white px-3 py-2 rounded text-sm">用此 Token 登录</button></div>`}}).catch(()=>{})}
function useReturnedToken(t){T=t;localStorage.setItem('kb_token',T);ea()}
function ea(){document.getElementById('login').classList.add('hidden');document.getElementById('app').classList.remove('hidden');ap('/api/index/status').then(d=>document.getElementById('ixs').textContent='📑 '+d.indexed+' 篇').catch(()=>{document.getElementById('ixs').textContent='📑 索引状态不可用'});checkVersion();loadTemplates();ld()}

function st(n){
  if(n!=='graph'&&window.__gsim){try{window.__gsim.stop()}catch(e){}}
  document.querySelectorAll('.sidebar-item').forEach(b=>b.classList.remove('active'));
  document.querySelector('[data-sb="'+n+'"]')?.classList.add('active');
  document.querySelectorAll('[id^="tab-"]').forEach(d=>d.classList.add('hidden'));
  var t=document.getElementById('tab-'+n);if(t)t.classList.remove('hidden');
  if(n=='dash')ld();if(n=='graph')lg();if(n=='notes')lt();if(n=='upload')loadUploadHistory();
  if(n=='auth')lau();if(n=='log')ll(1);if(n=='set')ls();if(n=='skills')lsk();if(n=='market')lmarket();if(n=='apidoc')lad();
  // 移动端导航同步
  document.querySelectorAll('.mn-item').forEach(b=>b.classList.toggle('active',b.dataset.mn===n));
  // 移动端：切 tab 时收起左侧目录抽屉与遮罩（桌面端无 .mobile-show，此调用无副作用）
  closeMobilePanels();
}

// ─── v5.6 标签云 M4 ───
function ltc(){
  ap('/api/tags').then(d=>{
    var max=Math.max(...d.tags.map(t=>t.count),1),h='';
    d.tags.slice(0,50).forEach(t=>{
      var size=.7+(t.count/max)*1.5;
      var safeName=escapeHtml(t.name);
      h+='<span class="tag-item" style="font-size:'+(size*12)+'px" data-tag="'+safeName+'" title="'+t.count+'篇">'+safeName+'</span>'
    });
    document.getElementById('tagCloud').innerHTML=h||'<span class="text-gray-400 text-xs">暂无标签</span>';
    document.getElementById('tagCloud').querySelectorAll('.tag-item').forEach(function(el){
      el.onclick=function(){st('notes');setTimeout(function(){document.getElementById('noteFilter').value=el.dataset.tag;lt()},300)}
    })
  }).catch(()=>{document.getElementById('tagCloud').innerHTML='<span class="text-gray-400 text-xs">加载失败</span>'})
}
function ld(){document.getElementById('dc').innerHTML=skDash();ap('/dashboard').then(d=>{const fb=d.dir_fallback||{};const cs=[{k:'projects',l:'项目',v:d.projects,n:d.projects_notes,c:'text-blue-600'},{k:'knowledge',l:'知识',v:d.knowledge,n:d.knowledge_notes,c:'text-emerald-600'},{k:'writing',l:'写作',v:d.writing,n:d.writing_notes,c:'text-violet-600'},{k:'diaries',l:'日记',v:d.diaries,n:d.diaries_notes,c:'text-amber-600'},{k:'archived',l:'归档',v:d.archived,n:d.archived_notes,c:'text-gray-500'},{k:'total',l:'总计',v:d.total_dirs,n:d.total,c:'text-rose-600'}];document.getElementById('dc').innerHTML=cs.map(c=>{const deg=!!fb[c.k];const main=deg?(c.n||0):(c.v||0);const sub=deg?`${c.n||0} 篇笔记（无子目录，按篇数显示）`:`${c.n||0} 篇笔记`;return `<div class="card p-5 text-center"><div class="text-3xl font-bold ${c.c}">${esc(main)}</div><div class="text-gray-500 text-sm mt-1">${esc(c.l)}</div><div class="text-gray-400 text-xs mt-1">${esc(sub)}</div></div>`}).join('')}).catch(()=>{document.getElementById('dc').innerHTML='<p class="text-red-500 p-4">仪表盘加载失败</p>'});ltc();ltr();lmcp()}
function lmcp(){ap('/api/mcp-usage').then(d=>{if(d.error)return;var last=d.last||{},t=last.time||'';if(t)t=t.slice(5,16);var html='<div class="card p-4"><h3 class="text-sm font-semibold text-gray-500 mb-3">🤖 MCP 使用统计</h3><div class="grid grid-cols-3 gap-3"><div class="text-center"><div class="text-xl font-bold text-violet-600">'+esc(d.today)+'</div><div class="text-gray-500 text-xs">今日调用</div></div><div class="text-center"><div class="text-xl font-bold text-emerald-600">'+esc(d.ok)+'</div><div class="text-gray-500 text-xs">成功</div></div><div class="text-center"><div class="text-xl font-bold text-gray-600">'+esc(d.total)+'</div><div class="text-gray-500 text-xs">累计</div></div></div><div class="text-xs text-gray-400 mt-2">最近: '+esc(t)+(last.tool?' | '+esc(last.tool):'')+'</div></div>';document.getElementById('dc').insertAdjacentHTML('beforeend',html)}).catch(function(){})}

// ─── v5.6 P3: 趋势图 ───
function ltr(){
  ap('/notes?limit=500').then(d=>{
    var months={},now=new Date();
    for(var i=0;i<12;i++){var k=new Date(now.getFullYear(),now.getMonth()-i,1);months[k.toISOString().slice(0,7)]=0}
    d.items.forEach(i=>{var m=(i.modified||'').slice(0,7);if(m&&months.hasOwnProperty(m))months[m]++});
    var keys=Object.keys(months).sort(),max=Math.max(1,...Object.values(months));
    var h='<div style="display:flex;align-items:flex-end;gap:4px;height:180px;padding-top:20px">';
    keys.forEach(k=>{var pct=(months[k]/max*100);h+='<div style="flex:1;text-align:center"><div class="stat-bar" style="height:'+Math.max(pct,2)+'%" title="'+k+': '+months[k]+'篇"></div><div class="text-gray-400" style="font-size:9px;transform:rotate(-45deg);transform-origin:left top;margin-top:4px">'+k.slice(2)+'</div></div>'});
    h+='</div>';
    document.getElementById('trendChart').innerHTML=h
  }).catch(()=>{document.getElementById('trendChart').innerHTML='<span class="text-gray-400 text-xs">加载失败</span>'})
}

// ─── v5.6 P2: 自动目录 TOC ───
function renderNoteHtml(md){
  var html=m2(md),d=document.createElement('div');d.innerHTML=html;
  var headings=d.querySelectorAll('h1,h2,h3'),toc=[];
  headings.forEach((h,i)=>{
    var id='toc-'+i;h.id=id;
    toc.push({level:h.tagName.toLowerCase(),text:h.textContent.slice(0,30),id:id})
  });
  var tocHtml='';
  if(toc.length>=2){
    tocHtml='<h4>📑 本节目录</h4>';
    toc.forEach(t=>{tocHtml+='<a class="toc-item '+t.level+'" href="#'+t.id+'">'+esc(t.text)+'</a>'});
  }
  return {html:d.innerHTML,toc:tocHtml}
}

/**
 * 图谱刷新参数（第 2 项）。抽出来是为了能被测试直接验证：
 * 原来「默认只画 300」和「状态栏传参」都写在 lgRefresh 里，
 * 测试只能调 lgPrune/gdStat 这类纯函数，把 lgRefresh 改坏测试照样绿
 * ——审查明确点过这是假绿。
 * 口径：默认 cap=150（维护者 2026-10-18 拍板从 300 降下来），勾「显示全部」时不裁。
 */
function lgOpts(o){
  o=o||{};
  var all=!!o.all;
  return {cap: all? Infinity : 150, showAll: all};
}

/**
 * 每节点边上限（遗留 #16）。桌面 4 条、手机 3 条。
 * 依据：150 个节点 × 平均度 4 ≈ 300 条边，画在 800×600 的画布上是能看出簇的；
 * 再高就退回网。这个值不是拍脑袋——见 tests/js/graph_density.test.cjs 的密度断言。
 */
function lgEdgeCap(mobile){
  return mobile?3:4;
}

/**
 * 图谱标签文本（第 2 项改造，2026-10-18）。
 * 旧实现是 ctx.fillText(title.slice(0,8))——硬截前 8 字。
 * 库里有一批同前缀标题（示例只列几条，不是全量清单），
 * 前 8 字完全相同，于是屏幕上出现一堆一模一样的「示例项目 组」。
 *
 * 现在：同前缀的一组节点，从**尾部**保留区分度最高的片段——
 * 因为真正区分它们的是结尾的日期/编号（_2026-09 / _2026-10），不是开头。
 * 组内还有完全同名的（不同目录下的同名笔记），退回用目录名区分。
 */
function labText(title, peers, dirHint){
  var t=String(title||'');
  if(!t) return '';
  var strip=function(s){return s.replace(/^[\s\-_·、,，.。|]+/,'')};
  if(!peers||peers.length<2){
    return t.length>12?strip(t.slice(-12)):t;
  }
  // 求共同前缀长度：从第 0 字往后逐字比对，到第一个不同处停下
  var cp=0;
  while(cp<t.length){
    var ch=t.charAt(cp),same=true;
    for(var i=0;i<peers.length;i++){
      if(String(peers[i]).charAt(cp)!==ch){same=false;break}
    }
    if(!same) break;
    cp++;
  }
  var seg=strip(t.slice(cp));
  // 标题完全相同（不同目录下的同名笔记）：seg 为空，改用目录区分。
  // dirHint 传的是完整 id 路径，这里取「最短能区分的目录段」，截尾会撞车
  // （两个路径的末段都是 chatgpt2api-服务.md），所以取头部差异段。
  if(!seg){
    var h=String(dirHint||'');
    if(h){
      var parts=h.split('/').filter(function(x){return x});
      // 跳过公共的开头（如 50-档案），取第一个能区分目录层级的段
      var pick='';
      for(var q=0;q<parts.length-1;q++){
        if(/^(50-档案|20-知识|10-项目|30-记录|40-日记|70-经验|笔记|_assets)$/.test(parts[q])) continue;
        pick=parts[q]; break;
      }
      // 单层路径（10-项目/xxx.md）没有可用的目录段，用文件名主干，别显示成「10-项目」
      if(!pick){
        var last=parts[parts.length-1]||'';
        pick=last.replace(/\.md$/,'')||parts[0]||'';
      }
      return pick.length>14?pick.slice(0,14):pick;
    }
    return strip(t).slice(-12);
  }
  // 组内所有 seg 一起算，本节点的尾部片段必须与别人不同
  var others=[];
  for(var j=0;j<peers.length;j++){
    var p=String(peers[j]);
    if(p===t) continue;
    others.push(strip(p.slice(cp)));
  }
  // 先剥掉「组内共有的尾部」——多数笔记结尾都是日期（_2026-09-16）或括号年份。
  // 不剥的话，尾部截断会被共有的日期吃满，截出「叠 2026-09-16」「面 2026-09-16」
  // 这种从词中间切开的碎片，屏幕上看着像乱码（真机实测抓到过）。
  var cutTail=function(a, bs){
    var n=0;
    while(n<a.length){
      var ch=a.charAt(a.length-1-n), same=true;
      for(var i=0;i<bs.length;i++){
        var b=bs[i];
        if(b.length<=n || b.charAt(b.length-1-n)!==ch){same=false;break}
      }
      if(!same) break;
      n++;
    }
    return n? a.slice(0, a.length-n) : a;
  };
  var segCut=cutTail(seg, others);
  if(!segCut) segCut=seg;
  var othersCut=others.map(function(o){return cutTail(o, [seg])});

  // 从 16 字起试（不是 12）：12 字经常正好切在词中间（「因排查_20260914」），
  // 16 字能多容纳一个完整词，可读性明显好；唯一性只会更强，不会变弱。
  var keep=Math.min(16, segCut.length);
  while(keep<segCut.length){
    var cand=segCut.slice(-keep);
    var clash=false;
    for(var k=0;k<othersCut.length;k++){
      if(othersCut[k].slice(-keep)===cand){clash=true;break}
    }
    if(!clash) return strip(cand);
    keep++;
  }
  return strip(segCut.slice(-Math.min(segCut.length, 16)));
}

/**
 * 图谱标签挑选（第 2 项改造）。只给「大节点」贴标签，不是全贴。
 * 规则：按 degree 降序取前 cap 个，且 degree 至少 3（叶子节点贴标签只会糊屏）。
 * 返回 {节点id: 标签文本}，文本已按同前缀组去重。
 */
function labPick(nodes, cap){
  // 只给「大节点」贴标签：按度数降序取前 cap 个，且必须有边。
  // 不能用 degree>=3 这种硬阈值——真实库里绝大多数节点是孤立点(degree=0)，
  // degree>=3 只有 19 个，屏幕上几乎不出现标签，等于把标签功能关掉。
  // 取「有边的节点按度数排序取前 N」，手机 30 / 桌面 40，小图也能看出主干。
  var arr=(nodes||[]).filter(function(n){return (n.degree||0)>=1});
  arr.sort(function(a,b){return (b.degree||0)-(a.degree||0)});
  var top=arr.slice(0,cap||0);
  // 按前 8 字分组，组内多于一个就要靠分歧点区分
  var byPre={};
  top.forEach(function(n){
    var p=String(n.title||'').slice(0,8);
    (byPre[p]=byPre[p]||[]).push(String(n.title||''));
  });
  // 标题完全同名的组（不同目录下的同名笔记，生产实测有 74 组）：
  // 光靠标题怎么截都一样，必须用路径区分。取组内路径的「第一个分歧段」。
  var byTitle={};
  top.forEach(function(n){
    var t=String(n.title||'');
    (byTitle[t]=byTitle[t]||[]).push(n);
  });
  var sameName={};
  Object.keys(byTitle).forEach(function(t){
    var grp=byTitle[t];
    if(grp.length<2) return;
    var segs=grp.map(function(n){
      return String(n.id||'').replace(/\.md$/,'').split('/');
    });
    // 逐段比对，找到第一处不同
    var cp=0;
    while(true){
      var ch=null,allSame=true;
      for(var i=0;i<segs.length;i++){
        if(segs[i].length<=cp){allSame=false;break}
        if(ch===null) ch=segs[i][cp];
        else if(segs[i][cp]!==ch){allSame=false;break}
      }
      if(!allSame) break;
      cp++;
    }
    grp.forEach(function(n,i){
      // 分歧段之后的整段路径最有辨识度；没有后续就用文件名主干
      var rest=segs[i].slice(cp).filter(function(x){return x});
      var lab=rest.length?rest.join('/'):(segs[i][segs[i].length-1]||'');
      sameName[n.id]=lab.length>16?lab.slice(0,16):lab;
    });
  });

  var out={};
  top.forEach(function(n){
    var t=String(n.title||'');
    if(sameName[n.id]){ out[n.id]=sameName[n.id]; return; }
    out[n.id]=labText(t, byPre[t.slice(0,8)]);
  });
  return out;
}

/**
 * 图谱请求参数（第 2 项）。语义边默认关、wiki 边默认开是验收第 2/3 条，
 * 这两条也必须能被测试验证，否则把 gSem 的默认值改回去测试不会报红。
 */
function lgQuery(o){
  o=o||{};
  var ms=(o.minSim==null?0.80:o.minSim);
  var ml=(o.maxLinks==null?8:o.maxLinks);
  return '/api/graph?min_sim='+ms+'&max_links='+ml+
    '&include_semantic='+(o.sem?1:0)+'&include_wiki='+(o.wiki===false?0:1);
}

function lg(){lgDefaults();lgRefresh()}

// 第 2 项：图谱主干裁剪。抽成独立函数是为了能被测试直接调用——
// 写在 lg() 里就只能靠 grep 断言，那是假绿（审查明确点过这个套路）。
// 规则：按 degree 降序取前 cap 个节点，连带只保留两端都在保留集里的边。
// 返回新对象，不改入参（lg() 里还要用 meta 显示总数）。
function lgPrune(gd, cap, showAll){
  var nodes=(gd&&gd.nodes)||[], links=(gd&&gd.links)||[];
  var total=nodes.length;
  if(showAll||cap>=total) return {nodes:nodes, links:links, total:total, capped:false};
  // 裁剪优先保留有边的节点：真实库里绝大多数节点是孤立点(degree=0)，
  // 纯按 degree 排序取前 150 会混进 34 个没连线的散点，画出来是一屏漂浮的点。
  // 先取有边的（degree>=1），不够再拿孤立点补足 cap。
  //
  // 遗留 #16 修正：光按 degree 排序还不够。带 wiki 边的节点
  // （= 有 [](./xx.md) 人工双链的笔记）在 degree 序里只挤进 1 个——人工双链被
  // 自动相似度产生的散点全挤掉了，图上看不出「哪几篇是我手动串起来的」。
  // 所以第一优先级改成「带 wiki 边的节点」，其次才按 degree。
  var wikiNode={};
  for(var i=0;i<links.length;i++){
    var _l=links[i];
    if(_l.type!=='wiki') continue;
    var _s=(_l.source&&_l.source.id)||_l.source, _t=(_l.target&&_l.target.id)||_l.target;
    if(_s!=null) wikiNode[_s]=1;
    if(_t!=null) wikiNode[_t]=1;
  }
  var sorted=nodes.slice().sort(function(a,b){
    var wa=wikiNode[a.id]?1:0, wb=wikiNode[b.id]?1:0;
    if(wa!==wb) return wb-wa;                      // 有人工双链的先留
    return (b.degree||0)-(a.degree||0);
  });
  var linked=sorted.filter(function(n){return (n.degree||0)>=1});
  var kept=linked.length>=cap?linked.slice(0,cap)
    :linked.concat(sorted.filter(function(n){return (n.degree||0)<1}).slice(0,cap-linked.length));
  var keep={}; kept.forEach(function(n){keep[n.id]=1});
  var kl=links.filter(function(l){
    var s=(l.source&&l.source.id)||l.source, t=(l.target&&l.target.id)||l.target;
    return keep[s]&&keep[t];
  });
  return {nodes:kept, links:kl, total:total, capped:true};
}

/**
 * 遗留 #16「去蜘蛛网」：边上限裁剪。抽成独立函数才能被测试直接验证真实行为。
 *
 * 问题实测：真实图里绝大多数边是语义边（约 98%）。只裁节点没用——
 * 150 个节点之间的语义边仍能把每个点连到十几个邻居，视觉上还是一张网。
 * 规则：按「优先级 + 权重」排序后，每个节点最多保留 perNode 条已接收的边，
 * 超出直接丢弃。wiki 边（人工双链）永远优先于语义边，不让自动相似度淹没人工结构。
 * 返回新数组，不改入参。
 */
function lgCap(gd, perNode){
  var links=(gd&&gd.links)||[];
  if(!perNode||perNode<1) return links.slice();
  var sorted=links.slice().sort(function(a,b){
    var pa=(a.type==='wiki')?1:0, pb=(b.type==='wiki')?1:0;
    if(pa!==pb) return pb-pa;                       // wiki 优先
    return (b.weight||0)-(a.weight||0);             // 同类型按权重降序
  });
  var cnt={}, out=[];
  for(var i=0;i<sorted.length;i++){
    var l=sorted[i];
    var s=(l.source&&l.source.id)||l.source, t=(l.target&&l.target.id)||l.target;
    if(s==null||t==null) continue;
    if((cnt[s]||0)>=perNode||(cnt[t]||0)>=perNode) continue;
    cnt[s]=(cnt[s]||0)+1; cnt[t]=(cnt[t]||0)+1;
    out.push(l);
  }
  return out;
}

/**
 * 图谱状态栏文案（第 2 项）。抽成独立函数是为了能被测试直接调用：
 * 之前这段写在 lg() 里，只能靠肉眼在浏览器里看，而它恰恰出过
 * 「连线 226（wiki 99 / 语义 641）」这种 99+641≠226 的自相矛盾。
 * 口径：外面和括号里都用「当前实际画出来的」，原始总数只在裁剪时单独标注。
 */
function lgStat(o){
  var shown=o.shown||0, total=o.total||0, capped=!!o.capped;
  var s='节点 '+shown+(shown<total?' / '+total:'');
  s+=' · 连线 '+(o.links||0)+'（wiki '+(o.wiki||0)+' / 语义 '+(o.semantic||0)+'）';
  if(capped) s+='（已裁剪，原 wiki '+(o.metaWiki||0)+' / 语义 '+(o.metaSemantic||0)+'）';
  return s;
}

function lgDefaults(){
  var e=document.getElementById('gMinSim'),x=document.getElementById('gMaxLinks');
  if(!e||!x||e.dataset.init) return;
  var mob=window.innerWidth<768;
  e.value=mob?'0.88':'0.80';x.value=mob?'4':'8';
  var v=document.getElementById('gMinSimV');if(v)v.textContent=e.value;
  e.dataset.init='1';
}

function lgRefresh(){
  var c=document.getElementById('gc');
  if(!c) return;
  c.innerHTML='';
  if(window.__gsim){try{window.__gsim.stop()}catch(e){}window.__gsim=null;}
  var el=function(i){return document.getElementById(i)};
  var ms=parseFloat((el('gMinSim')||{}).value||'0.80');
  var ml=parseInt((el('gMaxLinks')||{}).value||'8',10);
  var sem=!!(el('gSem')&&el('gSem').checked);
  var wik=!el('gWiki')||el('gWiki').checked;
  var q=lgQuery({minSim:ms,maxLinks:ml,sem:sem,wiki:wik});
  ap(q).then(function(gd){
    var _showAll=!!(el('gAll')&&el('gAll').checked);
    // cap 与 showAll 统一走 lgOpts，避免两处各写一套判据（改一处漏一处）
    var _opt=lgOpts({all:_showAll});
    var _pr=lgPrune(gd, _opt.cap, _opt.showAll);
    // 遗留 #16：节点裁剪之后再压边。每个节点最多留 lgEdgeCap 条，
    // 否则 150 个点之间的语义边会连成一张网（实测平均度 15）。
    var _edgeCap=lgEdgeCap(window.innerWidth<768);
    _pr.links=lgCap({links:_pr.links}, _edgeCap);
    var _meta=gd.meta||{};
    // 第 12 项：_mob 从 lgRefresh 尾部提到这里（lgRefresh 开头），
    // 与 draw() 里的门限判定放在一起。原代码时序上没有真 bug——lgRefresh 是同步跑完的，
    // 首个 draw() 在 _mob 赋值之后才执行。这是加固，不是缺陷修复：
    // 让 _mob 在 lgRefresh 内先于所有闭包声明，避免以后有人在 draw 之前插入异步回调时踩空。
    // 作用域仍是 lgRefresh 的局部变量（不是全局）。
    var _mob=window.innerWidth<768;
    gd={nodes:_pr.nodes, links:_pr.links, meta:_meta};
    var w=c.clientWidth||window.innerWidth||360;
    var h=c.clientHeight||(window.innerHeight-200)||480;
    var dpr=Math.min(window.devicePixelRatio||1,2);
    var cv=document.createElement('canvas');
    cv.width=Math.round(w*dpr); cv.height=Math.round(h*dpr);
    cv.style.width=w+'px'; cv.style.height=h+'px';
    cv.style.display='block';
    // 第 12 项：触屏手势交给 d3.zoom 处理。原来是 pan-y，浏览器会把竖直方向
    // 的滑动判给页面滚动，d3 只收到一半手势，结果就是「划不动」。
    // 设成 none 后 canvas 上的触摸不再触发页面滚动/缩放，平移和双指缩放才完整。
    cv.style.touchAction='none';
    c.appendChild(cv);
    var ctx=cv.getContext('2d');
    var tf={k:1,x:0,y:0};
    // 第 2 项：自动适配视图。斥力 -320 会把图撑到几千像素宽，
    // 而画布只有几百像素——不做 fit 的话首屏只能看到中间一小块空白，
    // 裁剪到 300 个节点也白搭。仿真停下后按节点包围盒算出缩放和平移。
    function fitView(){
      var ns=gd.nodes.filter(function(n){return typeof n.x==='number'&&typeof n.y==='number'});
      if(!ns.length) return;
      var x0=1e9,x1=-1e9,y0=1e9,y1=-1e9;
      ns.forEach(function(n){
        var r=nodeRadius(n);
        x0=Math.min(x0,n.x-r);x1=Math.max(x1,n.x+r);
        y0=Math.min(y0,n.y-r);y1=Math.max(y1,n.y+r);
      });
      var pad=24;
      var bw=Math.max(1,x1-x0), bh=Math.max(1,y1-y0);
      var k=Math.min((w-pad*2)/bw,(h-pad*2)/bh);
      k=Math.max(0.05,Math.min(k,2));
      tf.k=k;
      tf.x=(w-(x0+x1)*k)/2;
      tf.y=(h-(y0+y1)*k)/2;
      try{d3.select(cv).call(zr.transform,d3.zoomIdentity.translate(tf.x,tf.y).scale(tf.k))}catch(e){}
      // 半径按 1/k 补偿后节点可能互相重叠（仿真是按原始半径排的），
      // 适配完再跑一轮短碰撞把重叠推开，否则小图上会糊成一团。
      try{
        var cs=sim.force('collide');
        if(cs&&typeof cs.radius==='function'){sim.alpha(0.25);sim.restart()}
      }catch(e){}
      draw();
    }
    function nodeAt(px,py){
      var best=null,bd=1e9;
      for(var i=0;i<gd.nodes.length;i++){
        var d=gd.nodes[i],dx=d.x-px,dy=d.y-py,dd=Math.sqrt(dx*dx+dy*dy),rr=nodeRadius(d)+8;
        if(dd<rr&&dd<bd){bd=dd;best=d}
      }
      return best;
    }
    // 第 2 项：点击聚焦。focusId 非空时只高亮它的一跳邻域，其余节点/边淡出，
    // 而不是像原来那样一点就跳走（跳走前全图还在乱晃，看不清邻居）。
    // 再点同一个节点才打开笔记——保留原来的跳转能力。
    var focusId=null,focusSet={};
    // 第 2 项改造：悬停节点也要显示标签（大节点之外的补一个），
    // 否则小节点完全看不出是哪篇。手机上 touchstart 也会临时置上。
    var hoverId=null;
    function setFocus(id){
      if(focusId===id){focusId=null;focusSet={};}
      else{
        focusId=id;focusSet={};focusSet[id]=1;
        gd.links.forEach(function(l){
          var s=(l.source&&l.source.id)||l.source, t=(l.target&&l.target.id)||l.target;
          if(s===id)focusSet[t]=1;
          if(t===id)focusSet[s]=1;
        });
      }
      draw();
    }
    function draw(){
      ctx.setTransform(dpr,0,0,dpr,0,0);
      ctx.clearRect(0,0,w,h);
      ctx.save();
      ctx.translate(tf.x,tf.y); ctx.scale(tf.k,tf.k);
      ctx.lineCap='round';
      if(wikLinks.length){
        ctx.setLineDash([]); ctx.strokeStyle='rgba(148,163,184,0.62)'; ctx.lineWidth=1.1;
        ctx.beginPath();
        for(var i=0;i<wikLinks.length;i++){
          var l=wikLinks[i],s=l.source,t=l.target;
          if(!s||!t||typeof s!=='object'||typeof t!=='object')continue;
          if(focusId&&!(focusSet[s.id]&&focusSet[t.id]))continue;
          ctx.moveTo(s.x,s.y); ctx.lineTo(t.x,t.y);
        }
        ctx.stroke();
      }
      if(semLinks.length){
        ctx.setLineDash([3,3]); ctx.strokeStyle='rgba(96,165,250,0.28)'; ctx.lineWidth=0.9;
        ctx.beginPath();
        for(var j=0;j<semLinks.length;j++){
          var l2=semLinks[j],s2=l2.source,t2=l2.target;
          if(!s2||!t2||typeof s2!=='object'||typeof t2!=='object')continue;
          if(focusId&&!(focusSet[s2.id]&&focusSet[t2.id]))continue;
          ctx.moveTo(s2.x,s2.y); ctx.lineTo(t2.x,t2.y);
        }
        ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.strokeStyle='#fff'; ctx.lineWidth=Math.max(0.6,1.2/tf.k);
      for(var col in groups){
        var arr=groups[col];
        ctx.beginPath();
        for(var k=0;k<arr.length;k++){
          var d=arr[k];
          if(d.x===undefined)continue;
          var r=nodeRadius(d);
          ctx.moveTo(d.x+r,d.y); ctx.arc(d.x,d.y,r,0,6.283185307179586);
        }
        // 聚焦时非邻域节点淡到 0.15，邻域保持 0.95，一眼能看清直接邻居。
        ctx.globalAlpha=focusId?0.15:0.9; ctx.fillStyle=col; ctx.fill();
        if(focusId){
          ctx.beginPath();
          for(var k2=0;k2<arr.length;k2++){
            var d2=arr[k2];
            if(d2.x===undefined||!focusSet[d2.id])continue;
            var r2=nodeRadius(d2);
            ctx.moveTo(d2.x+r2,d2.y); ctx.arc(d2.x,d2.y,r2,0,6.283185307179586);
          }
          ctx.globalAlpha=0.95; ctx.fill();
        }
        ctx.globalAlpha=1; ctx.stroke();
      }
      // 第 12 项：原来固定 tf.k>0.45 才画标签。手机把 150 节点 fitView 进 390px 宽后，
      // 缩放比例通常只有 0.2~0.4，这条门限等于把手机上的标签全关掉——这是
      // 「手机上不显示标签」的直接原因。手机端放宽到 0.18，桌面维持 0.45。
      if(tf.k>(_mob?0.18:0.45)){
        ctx.fillStyle='#6b7280';
        ctx.font='8px -apple-system,BlinkMacSystemFont,sans-serif';
        ctx.textAlign='center'; ctx.textBaseline='bottom';
        for(var m=0;m<gd.nodes.length;m++){
          var dn=gd.nodes[m];
          if(dn.x===undefined)continue;
          // 第 2 项改造：只给大节点贴标签（lab 由 labPick 挑），
          // 悬停/聚焦的节点即使不在大节点里也补一个标签，保证点得准。
          var _lt=lab[dn.id];
          if(!_lt&&(dn.id===hoverId||dn.id===focusId)) _lt=labText(dn.title, null);
          if(!_lt)continue;
          ctx.fillText(_lt,dn.x,dn.y-nodeRadius(dn)-2);
        }
      }
      ctx.restore();
    }
    var zr=d3.zoom().scaleExtent([0.1,4])
      // 第 12 项：原来这里对 touchstart/touchmove 直接 return false，
      // 等于把手机上的单指平移和双指缩放整条路关掉（只能看不能动）。
      // 现在放行触屏：canvas 已设 touch-action:none，手势不会再和页面滚动打架。
      // 桌面保持原样：只认无按键左键拖动。
      .filter(function(ev){
        if(ev.type==='touchstart'||ev.type==='touchmove'||ev.type==='touchend')return true;
        if(ev.type==='wheel')return true;
        return !ev.button;
      })
      .on('zoom',function(e){tf=e.transform;draw()});
    d3.select(cv).call(zr).on('dblclick.zoom',null);
    cv.addEventListener('click',function(ev){
      var rc=cv.getBoundingClientRect();
      var px=(ev.clientX-rc.left-tf.x)/tf.k, py=(ev.clientY-rc.top-tf.y)/tf.k;
      var hit=nodeAt(px,py);
      // 第一次点：聚焦看邻居；再点同一个：打开笔记（原来的行为）。
      if(hit){
        if(focusId===hit.id){st('notes');setTimeout(function(){rn(hit.id)},200)}
        else setFocus(hit.id);
      }else if(focusId){focusId=null;focusSet={};draw()}
    });
    // 第 2 项改造：悬停显示该节点标签。桌面 mousemove 走这里；
    // 手机没有 hover，用 touchstart 取一次坐标，抬手 1.2 秒后清掉。
    function hoverAt(clientX,clientY){
      var rc=cv.getBoundingClientRect();
      var px=(clientX-rc.left-tf.x)/tf.k, py=(clientY-rc.top-tf.y)/tf.k;
      var hit=nodeAt(px,py);
      var id=hit?hit.id:null;
      if(id!==hoverId){hoverId=id;draw()}
    }
    cv.addEventListener('mousemove',function(ev){hoverAt(ev.clientX,ev.clientY)});
    cv.addEventListener('mouseleave',function(){if(hoverId){hoverId=null;draw()}});
    var _hoverT=null;
    cv.addEventListener('touchstart',function(ev){
      if(!ev.touches||!ev.touches.length)return;
      var t=ev.touches[0];
      hoverAt(t.clientX,t.clientY);
      clearTimeout(_hoverT);
      _hoverT=setTimeout(function(){hoverId=null;draw()},1200);
    },{passive:true});
    var dirCols={'10-项目':'#3b82f6','20-知识':'#10b981','30-写作':'#8b5cf6','30-记录':'#8b5cf6','40-日记':'#f59e0b','40-系统运维':'#ef4444','50-档案':'#6b7280','99-待整理':'#9ca3af'};
    // 第 2 项：分簇着色按一级目录。图例由实际出现的目录动态生成，
    // 不写死——写死的话新增目录会变成「无颜色」的灰点，看不出簇。
    function dirOf(d){
      if(d.dir&&dirCols[d.dir])return d.dir;
      var p=String(d.id||'');
      var m=p.match(/^(\d{2}-[^/]+)\//);
      return m?m[1]:'其他';
    }
    function nodeColor(d){
      var k=dirOf(d);
      if(dirCols[k]) return dirCols[k];
      return '#9ca3af';
    }
    function drawLegend(){
      var box=document.getElementById('gLegend');
      if(!box)return;
      var seen={},order=[];
      gd.nodes.forEach(function(d){var k=dirOf(d);if(!seen[k]){seen[k]=1;order.push(k)}});
      order.sort();
      var h='';
      order.forEach(function(k){
        var col=dirCols[k]||'#9ca3af';
        h+='<span class="flex items-center gap-1"><i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+col+'"></i>'+esc(k.replace(/^\d+-/,''))+'</span>';
      });
      h+='<span class="flex items-center gap-1"><i style="display:inline-block;width:16px;border-top:2px solid #94a3b8"></i>wiki</span>';
      // 语义边是否存在要看当前边集，不能引用后面才赋值的 semLinks（var 提升成 undefined，
      // 图例里的「语义」项永远不出现，等于骗人）。
      var hasSem=gd.links.some(function(l){return l.type==='semantic'});
      if(hasSem)h+='<span class="flex items-center gap-1"><i style="display:inline-block;width:16px;border-top:2px dashed #60a5fa"></i>语义</span>';
      box.innerHTML=h;
    }
    function nodeRadius(d){
      // 第 2 项：半径按缩放补偿。全图适配后 k 常在 0.2 左右，
      // 原来固定 4~16 的半径落到屏幕上只剩不到 3 像素，看着像没画。
      // 按 1/k 放大并封顶：屏幕上至少 4 像素、最大 26 像素。
      // 上界必须有——k 触底 0.05 时 4/zk 会算出 80，节点糊满整屏。
      var base=Math.max(6,Math.min(18,4+(d.degree||0)*0.9));
      var zk=(tf&&tf.k)||1;
      return Math.max(4/zk, Math.min(base, 26/zk));
    }
    function isSem(l){return l.type==='semantic'}
    var ids={};
    gd.nodes.forEach(function(n){ids[n.id]=true});
    var vl=[];
    gd.links.forEach(function(l){
      if(ids[l.source]&&ids[l.target]) vl.push(l);
    });
    var sim=d3.forceSimulation(gd.nodes)
      .force('link',d3.forceLink(vl).id(function(d){return d.id})
        .distance(function(l){return isSem(l)?160:55})
        .strength(function(l){return isSem(l)?0.08:0.85}))
      // 第 2 项：斥力实测调参。forceManyBody 默认 distanceMax=Infinity，
      // 300 个节点两两全局排斥会把图撑到上万像素，fitView 后 k 掉到 0.1、
      // 节点不足 1 像素等于看不见。加 distanceMax 让排斥只在局部起作用，
      // 簇内散开、簇间靠 link 力连住，图才能收进画布。
      .force('charge',d3.forceManyBody().strength(-140).distanceMax(240))
      .force('center',d3.forceCenter(w/2,h/2))
      .force('collision',d3.forceCollide().radius(function(d){return nodeRadius(d)+6}));
    window.__gsim=sim;
    sim.alphaDecay(0.05);
    sim.alphaMin(0.03);
    var _N=gd.nodes.length;
    // 第 2 项改造：默认 150 个节点时贴标签的只取「大节点」——按 degree 降序取前 N。
    // 第 12 项：手机端 N 由 30 提到 40（17:49 维护者拍板的最终口径，优先于正文的 30）。
    // 手机屏幕小，但 30 个标签在 150 节点里几乎认不出结构，40 个是能读又不糊的量。
    var _cap=40;
    var lab=labPick(gd.nodes, _cap);
    var groups={};
    gd.nodes.forEach(function(d){var col=nodeColor(d);(groups[col]=groups[col]||[]).push(d)});
    var wikLinks=[],semLinks=[];
    vl.forEach(function(l){(isSem(l)?semLinks:wikLinks).push(l)});
    var st0=el('gStat');
    if(st0){
      st0.textContent=lgStat({
        shown:gd.nodes.length, total:_meta.node_count||_pr.total, capped:_pr.capped,
        links:gd.links.length, wiki:wikLinks.length, semantic:semLinks.length,
        metaWiki:_meta.wiki_count, metaSemantic:_meta.semantic_count
      });
    }
    drawLegend();
    var _gsT0=Date.now(),_fitDone=false;
    // 仿真通常 1 秒左右就因 alphaMin 自然停（alphaDecay 0.05），2.5 秒的超时判据
    // 根本轮不到，fitView 必须挂在 end 事件上；超时也要兜一次，否则慢仿真时
    // 首屏永远不适配（审查点出过）。用 _fitDone 防重入：fitView 里会 restart
    // 跑碰撞，restart 又触发 tick/end，不加标志会无限循环。
    function doFit(){ if(_fitDone) return; _fitDone=true; fitView(); }
    sim.on('end',doFit);
    // 第 12 项：仿真期间降帧重绘。原来每个 tick 都整屏重画 canvas，
    // 手机上力导向一秒钟能触发几十次重绘，叠加上手势就掉帧、看着卡。
    // 这里限制到约 30fps（33ms 一帧），仿真停下后 fitView 会补最后一帧。
    var _lastDraw=0;
    sim.on('tick',function(){
      if(Date.now()-_gsT0>2500){sim.stop();doFit();}
      var _now=Date.now();
      if(_now-_lastDraw<33)return;
      _lastDraw=_now;
      draw();
    });
    draw();
  }).catch(function(){var e=document.getElementById('gc');if(e)e.innerHTML='<p class="text-red-500 p-4">图谱加载失败</p>'});
}

// 目录过滤输入框的防抖：原 oninput 每敲一个字符就打一次 /notes?limit=200
var _ltT=null;function ltD(){clearTimeout(_ltT);_ltT=setTimeout(ltFilter,200)}
// 全量树内存缓存：过滤是纯前端筛选，不再每敲一个字重拉一次全量响应（实测该响应数百 KB）
// 注意语义边界：只有「过滤输入」这条路走缓存；切页签/点标签/批量操作后仍走 lt() 重新拉取。
var _ntItems=null;
// 写入笔记后失效缓存：下一次切页签/点标签时会重新拉取全量（与改动前行为一致——原实现保存后也不主动刷树）
function ltInvalidate(){_ntItems=null}
function ltFilter(){if(!_ntItems){lt();return}renderTree(_ntItems,document.getElementById('noteFilter')?.value||'')}
function lt(){var box=document.getElementById('nt');return ap('/api/notes/tree').then(d=>{if(d.error||!Array.isArray(d.items)){throw new Error(d.error||'目录数据异常')}_ntItems=d.items;renderTree(_ntItems,document.getElementById('noteFilter')?.value||'');return _ntItems}).catch(e=>{_ntItems=null;if(box)box.innerHTML='<p class="text-red-500 text-xs py-4 text-center cursor-pointer" onclick="lt()">目录加载失败，点此重试</p>';return null})}
function renderTree(items,filterRaw){var filter=(filterRaw||'').toLowerCase();_ntLazy={};const tree={};(items||[]).forEach(i=>{if(filter&&!(i.title||'').toLowerCase().includes(filter)&&!(i.path||'').toLowerCase().includes(filter))return;var fn=String(i.path||'').split('/').pop();if(fn.startsWith('{{')||fn.startsWith('.'))return;const p=String(i.path||'').split('/');let n=tree;p.forEach((x,j)=>{if(j==p.length-1){n._f=n._f||[];n._f.push(i)}else{n[x]=n[x]||{};n=n[x]}})});var projects={},notes={};for(var k in tree){if(k=='10-项目')projects[k]=tree[k];else if(k!='_f')notes[k]=tree[k];else{notes._f=notes._f||[];notes._f=notes._f.concat(tree[k])}}var h='';var nc=Object.keys(notes).length||(notes._f?.length||0);var pc=Object.keys(projects).length||(projects._f?.length||0);if(!filter&&(nc||pc)){if(nc)h+='<div class="text-xs font-semibold text-gray-400 mt-2 mb-1">📚 知识笔记</div>'+rT(notes,0,null,!!filter);if(pc)h+='<div class="text-xs font-semibold text-gray-400 mt-3 mb-1 border-t border-gray-200 pt-2">📁 项目</div>'+rT(projects,0,true,!!filter)}else{h=rT(tree,0,null,true)}document.getElementById('nt').innerHTML=h||'<p class="text-gray-400 text-xs py-4 text-center">无内容</p>'}
// 笔记树折叠：深层目录默认折叠，nt-body 用 hidden class 控制显隐，展开时才按需渲染子树
var _ntLazy={},_ntLazyId=0;
function ntToggle(el){var b=el.nextElementSibling;if(!b||!b.classList.contains('nt-body'))return;var op=b.classList.toggle('hidden')===false;var ar=el.querySelector('.nt-ar');if(ar)ar.textContent=op?'▾':'▸';if(op){var lz=b.getAttribute('data-lz');if(lz&&_ntLazy[lz]){b.innerHTML=rT(_ntLazy[lz].n,_ntLazy[lz].d,_ntLazy[lz].ht,_ntLazy[lz].fe);b.removeAttribute('data-lz');delete _ntLazy[lz]}}}
function rT(n,d,ht,fe){let h='';for(let[k,v]of Object.entries(n)){if(k=='_f'){v.forEach(f=>{var fp=String(f.path||''),ft=String(f.title||fp.split('/').pop().slice(0,20));h+=`<div class="py-1 hover:bg-gray-100 rounded px-2 text-sm text-gray-700 flex items-center gap-1"><input type="checkbox" class="batch-check" data-bp="${esc(fp)}" onclick="event.stopPropagation();bcToggle('${jss(fp)}')" ${batchSet.has(f.path)?'checked':''}><span class="cursor-pointer flex-1" onclick="rn('${jss(fp)}')">${f.type=='project'?'📁':f.type=='diary'?'📅':'📄'} ${esc(ft)}</span></div>`})}else if(ht&&d==0){h+=rT(v,d+1,ht,fe)}else{var op=(d==0||!!fe),lz='';if(!op){_ntLazy[++_ntLazyId]={n:v,d:d+1,ht:ht,fe:!!fe};lz=' data-lz="'+_ntLazyId+'"'}h+='<div class="nt-hd py-1 text-gray-400 text-xs font-medium mt-1 cursor-pointer hover:bg-gray-100 rounded px-2" onclick="ntToggle(this)"><span class="nt-ar">'+(op?'▾':'▸')+'</span> 📂 '+esc(k.replace(/^\d+-/,''))+'</div><div class="nt-body'+(op?'':' hidden')+'"'+lz+'>'+(op?rT(v,d+1,ht,fe):'')+'</div>'}}return h}
function rn(p,wl){if(wl){ap('/api/links/resolve?target='+encodeURIComponent(p)+(currentEditPath?'&from='+encodeURIComponent(currentEditPath):'')).then(d=>{if(d&&d.resolved)rn(d.resolved);else wlMissing(p)}).catch(()=>toast.show('双链解析失败','error'));return}var pp=p;if(!p.includes('.'))pp=p+'.md';ap('/notes/'+encodeURIComponent(pp)).then(d=>{if(d.error&&!p.includes('.')){ap('/notes/'+encodeURIComponent(p)).then(d2=>sn(d2)).catch(()=>toast.show('笔记加载失败','error'))}else sn(d)}).catch(()=>toast.show('笔记加载失败','error'))}
function sn(d){document.getElementById('nv').innerHTML=skNote();if(d.error){document.getElementById('nv').innerHTML=`<p class="text-red-500">${esc(d.error)}</p>`;return}const mc={project:'blue',knowledge:'emerald',writing:'violet',diary:'amber'};var cr=d.meta.created||'',up=d.meta.updated||'',timeInfo='';if(cr)timeInfo+=cr.slice(0,10);if(up&&up!=cr)timeInfo+=' → '+up.slice(0,10);if(timeInfo)timeInfo='<span class="text-gray-400 text-xs">🕐 '+timeInfo+'</span>';currentEditPath=d.path;var rendered=renderNoteHtml(d.content);let h=bc(d.path)+`<div class="edit-toolbar"><button onclick="ne('${jss(d.path)}')" class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 px-3 py-1 rounded transition" style="background:var(--hover);color:var(--text2)">✏️ 编辑</button><button onclick="nd('${jss(d.path)}')" class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 px-3 py-1 rounded transition" style="background:var(--hover);color:var(--text2)">💾 下载 .md</button><button onclick="nh('${jss(d.path)}')" class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 px-3 py-1 rounded transition" style="background:var(--hover);color:var(--text2)">📜 历史</button><button onclick="nm('${jss(d.path)}')" class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 px-3 py-1 rounded transition" style="background:var(--hover);color:var(--text2)">📁 移动</button><button onclick="nq('${jss(d.path)}')" class="text-xs bg-blue-50 text-blue-600 px-2 py-1 rounded cursor-pointer hover:bg-blue-100" title="AI质量评估">📊 评估</button></div><div class="mb-3 flex gap-2 flex-wrap items-center">${(d.meta.tags||[]).map(t=>`<span class="bg-${mc[d.meta.type]||'gray'}-50 text-${mc[d.meta.type]||'gray'}-600 text-xs px-2 py-0.5 rounded-full border border-${mc[d.meta.type]||'gray'}-200">${esc(t)}</span>`).join('')}<span class="text-gray-400 text-xs">${esc(d.path)}</span>${timeInfo}</div><div class="note-content" id="noteViewContent">${rendered.html}</div><div id="noteEditArea" class="hidden"></div><div id="nback" class="mt-6"></div><div id="kbPluginNote" class="mt-6"></div><div id="nrel" class="mt-6"><p class="text-xs text-gray-400 cursor-pointer hover:text-blue-500" onclick="nrl('${jss(d.path)}')">📎 加载相关内容...</p></div>`;document.getElementById('nv').innerHTML=h;if(rendered.toc){document.getElementById('tocPanel').classList.remove('hidden');document.getElementById('tocContent').innerHTML=rendered.toc}else{document.getElementById('tocPanel').classList.add('hidden')}
// 正文渲染完再做两件异步事，都不阻塞首屏：
// ① 批量解析双链，标出断链；② 拉反向链接面板。
try{resolveLinksIn(document.getElementById('noteViewContent'),d.path)}catch(e){}
// 内嵌 ![[笔记]]：解析目标后逐篇拉正文填入（异步，不阻塞首屏）
try{embedWikilinksIn(document.getElementById('noteViewContent'),d.path)}catch(e){}
// 第 5 项：批量探测正文里的绝对路径，把占位徽标换成真实状态。
try{probePathsIn(document.getElementById('noteViewContent'))}catch(e){}
try{
  var _bp=document.getElementById('nback');
  if(_bp)ap('/api/notes/'+encodeURIComponent(d.path)+'/backlinks').then(function(b){
    renderBacklinks(_bp,(b&&b.backlinks)||[],d.path);
  }).catch(function(){});
}catch(e){}
// ③ 笔记详情挂载点：每次打开笔记容器都是新建的，已挂记录要先清掉再挂
try{
  if(typeof window.kbPluginInvalidate==='function')window.kbPluginInvalidate();
  if(typeof window.kbPluginMount==='function')window.kbPluginMount('note-detail');
}catch(e){}
// 移动端：笔记已打开，收起左侧目录抽屉与遮罩，避免遮罩盖住正文（桌面端无 .mobile-show，无副作用）
closeMobilePanels();}
function nrl(p){document.getElementById('nrel').innerHTML='<p class="text-xs text-gray-400">加载中...</p>';ap('/api/notes/'+encodeURIComponent(p)+'/related').then(d=>{var r=d.related||[],fs=x=>{var s=Number(x.display_score??x.score??0);if(!isFinite(s))s=0;s=Math.max(0,Math.min(1,s));return (s*100).toFixed(0)+'%'};document.getElementById('nrel').innerHTML=r.length?('<h4 class="text-sm font-semibold text-gray-500 mb-2">📎 相关内容 ('+r.length+'篇)</h4>'+r.map(x=>`<div class="card p-3 mb-2 cursor-pointer hover:border-blue-300 transition" onclick="st('notes');setTimeout(()=>rn('${jss(x.path)}'),200)"><span class="font-medium text-sm text-gray-800">${esc(x.title||String(x.path||'').split('/').pop())}</span><span class="text-xs text-blue-500 ml-2" title="原始分数: ${esc(x.score??'')}">${fs(x)}</span><div class="text-xs text-gray-400">${esc(x.path)}</div></div>`).join('')):'<p class="text-xs text-gray-400">无相关内容</p>'}).catch(()=>{document.getElementById('nrel').innerHTML='<p class="text-xs text-gray-400">加载失败</p>'})}
function nq(p){ap('/api/llm/quality',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).then(d=>{var q=d.quality||{},score=q.score||0,c=score>=80?'emerald':score>=60?'blue':score>=40?'amber':'red';var h='<div class="mt-2 p-3 rounded-lg border border-'+c+'-200 bg-'+c+'-50"><div class="flex items-center gap-2"><span class="text-2xl font-bold text-'+c+'-600">'+score+'</span><span class="text-xs text-gray-500">/100</span></div>';if(q.strengths?.length)h+='<div class="text-xs text-emerald-600 mt-1">✓ '+esc(q.strengths.slice(0,2).join(', '))+'</div>';if(q.improvements?.length)h+='<div class="text-xs text-amber-600 mt-1">→ '+esc(q.improvements.slice(0,2).join(', '))+'</div>';h+='</div>';document.getElementById('nv').insertAdjacentHTML('beforeend',h)}).catch(()=>{toast.show('评估失败','error')})}
// ─── 笔记编辑 K1 ───
function ne(p){var vc=document.getElementById('noteViewContent'),ea=document.getElementById('noteEditArea');vc.classList.add('hidden');ea.classList.remove('hidden');ap('/notes/'+encodeURIComponent(p)).then(d=>{if(d.error){toast.show(d.error,'error');return}ea.innerHTML='<textarea id="editTextarea" class="edit-textarea">'+d.content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')+'</textarea><div class="edit-toolbar mt-3"><button onclick="ns(\''+jss(p)+'\')" id="saveBtn" class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded text-sm font-medium transition">💾 保存</button><button onclick="document.getElementById(\'noteViewContent\').classList.remove(\'hidden\');document.getElementById(\'noteEditArea\').classList.add(\'hidden\')" class="bg-gray-100 hover:bg-gray-200 text-gray-600 px-4 py-2 rounded text-sm transition" style="background:var(--hover);color:var(--text2)">取消</button><span class="text-xs text-gray-400 ml-2">Ctrl+S 快捷保存</span></div>';var ta=document.getElementById('editTextarea');ta.focus();ta.addEventListener('keydown',function(e){if(e.ctrlKey&&e.key==='s'){e.preventDefault();ns(p)}});if(!_ntItems)lt();wlBindComplete(ta)}).catch(e=>{toast.show('加载失败','error');vc.classList.remove('hidden');ea.classList.add('hidden')})}
function ns(p){var btn=document.getElementById('saveBtn'),ct=document.getElementById('editTextarea').value;if(btn)btn.disabled=true;ap('/notes/'+encodeURIComponent(p),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ct})}).then(d=>{if(d.error){toast.show(d.error,'error');if(btn)btn.disabled=false;return}toast.show('✅ 已保存','success');document.getElementById('noteViewContent').classList.remove('hidden');document.getElementById('noteEditArea').classList.add('hidden');ltInvalidate();rn(p)}).catch(e=>{toast.show('保存失败','error');if(btn)btn.disabled=false})}
// ─── 笔记下载 K2 ───
function nd(p){ap('/notes/'+encodeURIComponent(p)).then(d=>{if(d.error){toast.show(d.error,'error');return}var fn=(p.split('/').pop()||'note');if(!fn.endsWith('.md'))fn+='.md';var blob=new Blob([d.content||''],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=fn;document.body.appendChild(a);a.click();document.body.removeChild(a);URL.revokeObjectURL(url);toast.show('📥 下载完成','success')}).catch(e=>{toast.show('下载失败','error')})}
// ─── 笔记改名 / 移动 K4（复用后端 /api/notes/<path>/move） ───
/**
 * 改名 / 移动：先 dry_run 让用户看到「会改哪些笔记的引用」，确认后再真移。
 * update_links=true 由后端事务性改写双链（中途失败会整体回滚）。
 * 注意：dry_run 的预演清单是后端「真算一遍链接改写再丢弃」，不是猜的。
 */
function nm(p){
  var to=prompt('移动到（相对笔记库的路径，要含文件名）：',p);
  if(to===null)return;
  to=String(to).trim();
  if(!to||to===p)return;
  ap('/api/notes/'+encodeURIComponent(p)+'/move',
     {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({to:to,update_links:true,dry_run:true})})
  .then(function(d){
    if(d&&d.error){toast.show('预演失败：'+(d.error||''),'error');return}
    var ls=(d&&d.links_updated)||[];
    var msg='移动到：\n'+to+'\n\n';
    msg+= ls.length
      ? '将同时更新 '+ls.length+' 篇笔记里的引用：\n'
        + ls.slice(0,8).map(function(x){return '· '+x.path+'（'+x.count+' 处）'}).join('\n')
        + (ls.length>8?'\n… 共 '+ls.length+' 篇':'')
      : '没有其它笔记引用它，不会改动别的文件。';
    if(!confirm(msg+'\n\n确认移动？'))return;
    ap('/api/notes/'+encodeURIComponent(p)+'/move',
       {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({to:to,update_links:true,dry_run:false})})
    .then(function(r){
      if(r&&r.error){toast.show('移动失败：'+(r.error||''),'error');return}
      toast.show('✅ 已移动到 '+to,'success');
      ltInvalidate();lt();
      rn(to);
    }).catch(function(){toast.show('移动失败','error')});
  }).catch(function(){toast.show('预演失败','error')});
}

// ── 安全渲染：笔记正文是不可信输入（可能来自上传、MCP 写入、他人分享） ──
// marked 默认放行原始 HTML，等于把 <img src=x onerror=...> 直接送进 innerHTML。
// 这里关掉 raw HTML 通路，并约束链接/图片协议，仅保留 markdown 自身语义。
var _SAFE_URL=/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/i;
var _BAD_URL=/^(javascript|data|vbscript|file|blob):/i;
function safeUrl(u){u=String(u==null?'':u).trim();if(!u)return '';if(_BAD_URL.test(u))return '';return u}
// marked 由 vendor/marked.min.js 提供，index.html 用 defer 加载（不阻塞首屏）。
// 因此不能在 app.js 顶层调用 marked.use —— 那时 marked 还没定义。
// 改为首次渲染时惰性注册，只执行一次。
var _markedHardened=false;
function hardenMarked(){
  if(_markedHardened||typeof marked==='undefined')return;
  _markedHardened=true;
  marked.use({renderer:{
    html(token){return escapeHtml((token&&token.text)||'')},
    link(token){
      var href=safeUrl(token.href);
      if(!href)return escapeHtml((token&&token.text)||'');
      var body=(token.tokens&&token.tokens.length)?this.parser.parseInline(token.tokens):escapeHtml(token.text||'');
      var title=token.title?' title="'+escapeHtml(token.title)+'"':'';
      return '<a href="'+escapeHtml(href)+'"'+title+'>'+body+'</a>';
    },
    image(token){
      var href=safeUrl(token.href);
      if(!href)return escapeHtml((token&&token.text)||'');
      return '<img src="'+escapeHtml(href)+'" alt="'+escapeHtml(token.text||'')+'">';
    }
  }});
}
function m2(md,opts){
  hardenMarked();
  marked.setOptions({breaks:true,gfm:true,headerIds:false,mangle:false});
  var s=String(md==null?'':md);
  var allowEmbed=!(opts&&opts.embed===false);
  // 0. Pre-process: 嵌入 ![[x]] → 占位。
  //    必须早于普通双链：否则 ![[x]] 里的 [[x]] 先被下面的 wikilink 规则吃掉，
  //    只剩一个孤零零的 "!"。同样按代码段切开，代码里的语法示范不处理。
  var embeds=[];
  var segE=s.split(/(```[\s\S]*?```|`[^`\n]*`)/);
  for(var ei=0;ei<segE.length;ei++){
    if(ei%2===1)continue;
    segE[ei]=segE[ei].replace(/!\[\[([^\]]+)\]\]/g,function(m,inner){
      var p=inner.split('|')[0].replace(/\\+$/,'').trim();
      if(!p||p.endsWith('/'))return m;
      if(!allowEmbed)return '[['+inner+']]';   // 内层不再展开，退化成普通双链
      embeds.push(p);
      return '␟EM'+(embeds.length-1)+'␟';
    });
  }
  s=segE.join('');
  // 1. Pre-process: wikilinks → unique HTML placeholders
  //    只在「非代码段」里替换。文档里写「示例：`[[笔记名]]`」这种代码内的语法示范
  //    也是 [[...]]，整篇正则替换会把它变成可点链接——加了断链标注后还会标红，
  //    看起来像全库都是断链。按代码段切开处理，代码段原样交给 marked 渲染。
  var wl=[];
  var seg=s.split(/(```[\s\S]*?```|`[^`\n]*`)/);
  for(var si=0;si<seg.length;si++){
    if(si%2===1)continue;                       // 奇数位是代码段，跳过
    seg[si]=seg[si].replace(/\[\[([^\]]+)\]\]/g,function(m,inner){
    var p=inner.split('|'),path=p[0].replace(/\\+$/,'').trim();
    if(path.endsWith('/')) return '📁 '+escapeHtml(path);
    var l=p[1]||path.split('/').pop().replace('.md','');
    // data-wl 保存双链原始目标：渲染后由 resolveLinksIn() 批量解析，
    // 解析不到的加 .wl-broken（断链）。放在属性里而不是这里发请求——
    // m2() 是纯字符串函数，发网络请求会拖慢首屏且难以测试。
    wl.push('🔗 <a href="#" data-wl="'+escapeHtml(path)+'" onclick="rn(\''+jss(path)+'\',1);return false" class="text-blue-600 hover:underline" title="'+escapeHtml(path)+'">'+escapeHtml(l)+'</a>');
    return '␟WL'+(wl.length-1)+'␟';
    });
  }
  s=seg.join('');
  // 2. Pre-process: image URLs → authenticated blob placeholder
  //    注意：必须用占位符而不是直接内联 <img> —— 渲染器的 html() 会把原始 HTML
  //    转义掉（这是防注入的关键），内联的 <img> 会被转成可见文本。占位符走的是
  //    与 wikilink 相同的通路：先在 markdown 里留一个纯文本标记，marked 渲染完后
  //    再替换成真实标签。
  var imgs=[];
  s=s.replace(/\!\[([^\]]*)\]\(([^)]+)\)/g,function(m,alt,url){
    if(/^https?:\/\//.test(url)) return m;
    imgs.push('<img data-auth-src="'+escapeHtml(url)+'" alt="'+escapeHtml(alt||'')+'" src="'+escapeHtml(authFileUrl(url))+'">');
    return '␟IM'+(imgs.length-1)+'␟';
  });
  // 3. Render via marked.js
  var html=marked.parse(s);
  // 4. Post-process: restore wikilink + image + embed placeholders
  for(var i=0;i<wl.length;i++)html=html.split('␟WL'+i+'␟').join(wl[i]);
  for(var j=0;j<imgs.length;j++)html=html.split('␟IM'+j+'␟').join(imgs[j]);
  // 嵌入占位：真身由 embedWikilinksIn() 在挂载后异步填入。m2() 是纯字符串函数，
  // 在这里发请求会拖慢首屏，也没法单测。
  for(var k=0;k<embeds.length;k++){
    html=html.split('␟EM'+k+'␟').join(
      '<div class="wl-embed" data-wl-embed="'+escapeHtml(embeds[k])+'">'+
      '<div class="wl-embed-head"><span class="wl-embed-name">📄 '+escapeHtml(embeds[k])+'</span>'+
      '<button type="button" class="wl-embed-refresh" title="重新加载这篇" onclick="wlEmbedRefresh(this);return false">↻</button></div>'+
      '<div class="wl-embed-body"><span class="wl-embed-loading">加载中…</span></div></div>');
  }
  // 5. Post-process: local file links → authenticated download handler
  html=html.replace(/<a href="((?!https?:\/\/|#)[^"]+)"/g,function(m,href){
    // href 此时是 HTML 转义后的字符串（&amp; 等），需还原再按 JS 规则转义
    var raw=href.replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
    return '<a href="#" onclick="downloadFilePath(\''+jss(raw)+'\');return false"';
  });
  // 6. 第 5 项：正文绝对路径 → 待探测徽标（只认 / 开头，不认 ./ 相对写法）
  //    这里只做「包成待探测的 span」，不发请求：m2() 是纯字符串函数，
  //    发网络请求会拖慢首屏且难测。探测由 probePathsIn() 在挂载后统一批量做。
  html=markAbsPaths(html);
  // 7. Add loading=lazy to images
  html=html.replace(/<img /g,'<img loading="lazy" class="max-w-full rounded-lg my-2" ');
  return html;
}

/**
 * 把已渲染 HTML 里的绝对路径包成徽标占位（不探测、不发请求）。
 *
 * 只处理文本节点：<code>/srv/notes/a</code>、<pre> 里的示例路径不算真路径，
 * 一并包成徽标会让「文档里写的示例」也挂上灰标，看着像全库路径都失效。
 * 做法是按标签切开，跳过 code/pre/a 内部。
 */
function markAbsPaths(html){
  var s=String(html||'');
  if(s.indexOf('/')<0)return s;
  // m2 走到这里时正文已被 HTML 转义（`&` → `&amp;`）。
  // 不能整个反解再转义：正文里字面写 `&lt;script&gt;` 是「显示尖括号」的意思，
  // 反解后会变成真标签。所以只在出口跳过已存在的实体，不重复转义。
  function escKeepEnt(x){
    return String(x)
      .replace(/&(?!(?:amp|lt|gt|quot|#39|#\d+);)/g,'&amp;')
      .replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  var parts=s.split(/(<[^>]*>)/);
  var skip=0;
  for(var i=0;i<parts.length;i++){
    var seg=parts[i];
    if(seg.charAt(0)==='<'){
      // 维护「当前是否在 code/pre/a 内部」的深度
      var close=/^<\//.test(seg);
      var tag=(seg.match(/^<\/?\s*([a-zA-Z0-9]+)/)||[])[1];
      if(tag){
        tag=tag.toLowerCase();
        if(tag==='code'||tag==='pre'||tag==='a'||tag==='script'||tag==='style'){
          skip+=(close?-1:1);
          if(skip<0)skip=0;
        }
      }
      continue;
    }
    if(skip>0||!seg)continue;
    // 只认绝对路径。不用 lookbehind：ES2018 语法在旧 WebView（iOS<16.4）会让
    // 整个 app.js 解析失败、全站白屏。改用 offset 看左邻字符。
    // 左邻是单词字符/斜杠/点/冒号 → 不是路径起点（挡 URL、相对路径、日期分数）；
    // 左邻是汉字/假名/谚文 → 是「前端/后端」这类词组，不是路径。
    // 全角标点（：，。）不算词组，仍当边界，否则「路径：/srv/notes/a.md」识别不出来。
    parts[i]=seg.replace(/(\/[^\s`'"()\[\]<>|，。；：、！？（）【】「」《》“”‘’…—]{2,})/g,function(m,p,off,whole){
      if(off>0){
        var prev=whole.charAt(off-1);
        if(/[\w\/.:-]/.test(prev))return m;
        if(/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/.test(prev))return m;
      }
      // 右邻：路径后面紧跟空格时，空格可能是文件名的一部分（真存在
      // `/srv/notes/a b.md` 这种文件），也可能只是句子里的分词。
      //   ` /srv/notes/b.md` → 空格后是新路径，当前这条照常标；
      //   ` b.md`       → 像文件名片段，无法判断 → 整段保持纯文字。
      // 不这样处理会把 `/srv/notes/a b.md` 截成 `/srv/notes/a` 去 stat，得到一个
      // 跟原文无关的徽标（浏览器实测发现：显示成「其他服务器」）。
      // 扩展名只认纯字母（`.md`/`.py`/`.json`），这样 ` 2.0 版本` 里的
      // 版本号不会被当成文件名片段，避免误伤「/srv/notes/a.md 2.0 版本」这种句子。
      // 空格后可能是 `b.md`，也可能是 `docs/x.py` 这种带子目录的片段，两种都算。
      // 但空格后紧跟 `/` 是新路径的起点（`/srv/notes/a.md /srv/notes/b.md`），不算片段。
      var rest=whole.substr(off+m.length);
      if(/^\s+(?!\/)[A-Za-z0-9_.\-\/]*\.[A-Za-z]{1,8}(?![A-Za-z0-9_])/.test(rest))return m;
      // 已在 <a> 里的不重复包（外链、下载链接）
      return '<span class="abs-path" data-abs="'+escKeepEnt(p)+'" onclick="return openAbsPath(this)"><span class="abs-badge">…</span>'+escKeepEnt(p)+'</span>';
    });
  }
  return parts.join('');
}

// ─── 双链输入补全 K3 ───
/**
 * 判断光标是否处在「未闭合的 [[」里，返回 {start, query} 或 null。
 *  - start：`[[` 的起始下标（含）；query：`[[` 之后到光标之间的文本
 * 从光标往前扫：先撞 `]]` 或换行 → 不在补全中；先撞 `[[` → 在补全中。
 * 已经在写别名/锚点（`|` / `#`）时不再补全，避免把 `[[x|别名` 也当查询。
 */
function wlCompleteContext(value,caret){
  var s=String(value==null?'':value);
  var c=Math.max(0,Math.min(Number(caret)||0,s.length));
  var head=s.slice(0,c);
  var open=head.lastIndexOf('[[');
  if(open<0)return null;
  var seg=head.slice(open+2);
  if(seg.indexOf('\n')>=0||seg.indexOf(']]')>=0)return null;
  if(seg.indexOf('|')>=0||seg.indexOf('#')>=0)return null;
  return {start:open,query:seg};
}

/**
 * 按 query 过滤候选（数据源 = /api/notes/tree 的完整条目）。纯函数，便于单测。
 * 排序：标题前缀命中 > 路径命中 > 其它包含；同档短的标题优先，再按路径字典序。
 * 空 query 时按「短的优先」列出，用户打第一个字就能快速收敛。
 */
function wlCandidates(items,query,limit){
  var q=String(query||'').toLowerCase().trim();
  var out=[];
  (items||[]).forEach(function(it){
    var p=String((it&&it.path)||'');
    if(!p)return;
    var t=String((it&&it.title)||p.split('/').pop()||'');
    var hay=(t+' '+p).toLowerCase();
    if(q&&hay.indexOf(q)<0)return;
    var stem=t.toLowerCase(),pl=p.toLowerCase();
    var rank=q?(stem.indexOf(q)===0?0:(pl.indexOf(q)>=0?1:2)):2;
    out.push({path:p,title:t,rank:rank});
  });
  out.sort(function(a,b){
    if(a.rank!==b.rank)return a.rank-b.rank;
    if(a.title.length!==b.title.length)return a.title.length-b.title.length;
    return a.path<b.path?-1:(a.path>b.path?1:0);
  });
  return out.slice(0,Math.max(1,Number(limit)||20));
}

/** 把选中的候选写回 textarea：替换掉 `[[` 之后到光标的内容，补上 `]]`。 */
function wlInsert(ta,ctx,item){
  if(!ta||!ctx||!item)return;
  var v=ta.value;
  var c=ta.selectionStart==null?(ctx.start+2+ctx.query.length):ta.selectionStart;
  var ins=String(item.path||'').replace(/\.md$/,'')+']]';
  ta.value=v.slice(0,ctx.start+2)+ins+v.slice(c);
  var pos=ctx.start+2+ins.length;
  try{ta.setSelectionRange(pos,pos)}catch(e){}
  ta.focus();
}

/** 候选浮层：单例，挂在 body 上，贴着编辑区下沿显示。 */
function wlCompleteHide(){
  var el=document.getElementById('wlComplete');
  if(el){el.classList.add('hidden');el.innerHTML=''}
}
function wlBindComplete(ta){
  if(!ta)return;
  var box=document.getElementById('wlComplete');
  if(!box){box=document.createElement('div');box.id='wlComplete';box.className='wl-complete hidden';document.body.appendChild(box)}
  var cur=[],sel=0,ctx=null;
  function place(){
    var r=ta.getBoundingClientRect();
    box.style.left=r.left+'px';
    box.style.top=(r.bottom+4)+'px';
    box.style.width=Math.min(r.width,420)+'px';
  }
  function render(){
    if(!ctx||!cur.length)return wlCompleteHide();
    place();box.classList.remove('hidden');
    box.innerHTML=cur.map(function(x,i){
      return '<div class="wl-cand'+(i===sel?' wl-cand-on':'')+'" data-i="'+i+'">'
        +'<span class="wl-cand-t">'+escapeHtml(x.title)+'</span>'
        +'<span class="wl-cand-p">'+escapeHtml(x.path)+'</span></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.wl-cand'),function(n){
      n.onmousedown=function(e){e.preventDefault()};   // 别让 textarea 失焦，否则 click 前就被隐藏
      n.onclick=function(){var i=Number(n.getAttribute('data-i')||0);wlInsert(ta,ctx,cur[i]);ctx=null;cur=[];wlCompleteHide()};
    });
  }
  var prefetch=null;   // 目录树未加载时的预取，避免每次按键都发一次请求
  function refresh(){
    ctx=wlCompleteContext(ta.value,ta.selectionStart);
    if(!ctx)return wlCompleteHide();
    if(!_ntItems){
      var p=prefetch||(prefetch=lt()||Promise.resolve());
      p.then(function(){ if(!_ntItems){prefetch=null;return} prefetch=null; refresh(); });
      return wlCompleteHide();
    }
    cur=wlCandidates(_ntItems,ctx.query,20);sel=0;render();
  }
  ta.addEventListener('input',refresh);
  ta.addEventListener('click',refresh);
  ta.addEventListener('keydown',function(e){
    if(!ctx||!cur.length)return;
    if(e.key==='ArrowDown'){e.preventDefault();sel=(sel+1)%cur.length;render()}
    else if(e.key==='ArrowUp'){e.preventDefault();sel=(sel-1+cur.length)%cur.length;render()}
    else if(e.key==='Enter'||e.key==='Tab'){e.preventDefault();wlInsert(ta,ctx,cur[sel]);ctx=null;cur=[];wlCompleteHide()}
    else if(e.key==='Escape'){ctx=null;cur=[];wlCompleteHide()}
  });
}

// ─── 第 7-1 项：断链标注 + 反向链接 ───
/**
 * 把批量解析结果标到已渲染的正文上：解析不到（null）的双链加 .wl-broken。
 * 抽成纯函数是为了能测：传入容器 + 解析结果，只看 class 变化，不碰网络。
 */
function markBrokenLinks(container,resolved){
  if(!container||!resolved)return 0;
  var hit=container.querySelectorAll('[data-wl]'),n=0;
  for(var i=0;i<hit.length;i++){
    var el=hit[i],t=el.getAttribute('data-wl');
    if(!t)continue;
    if(Object.prototype.hasOwnProperty.call(resolved,t)&&!resolved[t]){
      el.classList.add('wl-broken');
      el.setAttribute('title',t+'（目标笔记不存在）');
      n++;
    }else{
      el.classList.remove('wl-broken');
    }
  }
  return n;
}

/**
 * 收集正文里所有双链目标 → 一次 POST 批量解析 → 标断链。
 * 逐个调 /api/links/resolve 会打几十次请求、每次重建全库链接索引，手机端会卡。
 */
function resolveLinksIn(container,from){
  if(!container||typeof ap!=='function')return Promise.resolve(0);
  var hit=container.querySelectorAll('[data-wl]'),targets=[];
  for(var i=0;i<hit.length;i++){
    var t=hit[i].getAttribute('data-wl');
    if(t&&targets.indexOf(t)<0)targets.push(t);
  }
  if(!targets.length)return Promise.resolve(0);
  return ap('/api/links/resolve-batch',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({from:from||currentEditPath||'',targets:targets})
  }).then(function(d){
    return markBrokenLinks(container,(d&&d.resolved)||{});
  }).catch(function(){return 0});
}

/**
 * 内嵌 `![[笔记]]`：批量解析目标 → 逐篇拉正文 → 用 m2(embed:false) 渲染填入。
 * 只展开一层（内层不再展开嵌入，避免自引用 / 互引死循环）；
 * 自引用与解析不到的目标都不发请求，就地给出提示。
 */
function embedWikilinksIn(container,from){
  if(!container||typeof ap!=='function')return Promise.resolve(0);
  var nodes=container.querySelectorAll('[data-wl-embed]');
  if(!nodes.length)return Promise.resolve(0);
  var targets=[];
  for(var i=0;i<nodes.length;i++){
    var t=nodes[i].getAttribute('data-wl-embed');
    if(t&&targets.indexOf(t)<0)targets.push(t);
  }
  if(!targets.length)return Promise.resolve(0);
  var cur=from||currentEditPath||'';
  return ap('/api/links/resolve-batch',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({from:cur,targets:targets})
  }).then(function(d){
    var res=(d&&d.resolved)||{},jobs=[];
    for(var i=0;i<nodes.length;i++)(function(el){
      var t=el.getAttribute('data-wl-embed'),body=el.querySelector('.wl-embed-body'),p=res[t];
      function miss(msg){
        el.classList.add('wl-broken');
        if(body)body.innerHTML='<span class="wl-embed-missing">'+msg+'</span>';
      }
      if(!p)return miss('目标不存在：'+escapeHtml(t));
      if(p===cur)return miss('（跳过自引用）');
      el.classList.remove('wl-broken');
      jobs.push(wlFillEmbedResolved(el,p));
    })(nodes[i]);
    return Promise.all(jobs).then(function(){
      // 嵌入正文里也有双链，一并标断链（m2 已给它们 data-wl）
      return resolveLinksIn(container,cur);
    });
  }).catch(function(){return 0});
}

/** 把一篇笔记的正文渲染进嵌入块（首次填充与「↻ 重新加载」共用）。 */
function wlFillEmbedResolved(el,p){
  var body=el?el.querySelector('.wl-embed-body'):null;
  if(!body)return Promise.resolve();
  return ap('/notes/'+encodeURIComponent(p)).then(function(nd){
    if(!nd||nd.error||typeof nd.content!=='string'){
      body.innerHTML='<span class="wl-embed-missing">加载失败</span>';return;
    }
    body.innerHTML=m2(nd.content,{embed:false});
  }).catch(function(){body.innerHTML='<span class="wl-embed-missing">加载失败</span>'});
}

/**
 * 嵌入块标题行的「↻」：重解析 + 重拉这一篇。
 * 页面开着时目标笔记被改了，点一下就能看到最新；只刷这一块，不动同页其它嵌入。
 */
function wlEmbedRefresh(btn){
  var el=btn&&btn.closest?btn.closest('.wl-embed'):null;
  if(!el)return;
  var body=el.querySelector('.wl-embed-body'),t=el.getAttribute('data-wl-embed');
  if(!t||!body)return;
  var cur=currentEditPath||'';
  body.innerHTML='<span class="wl-embed-loading">加载中…</span>';
  return ap('/api/links/resolve-batch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({from:cur,targets:[t]})})
  .then(function(d){
    var p=((d&&d.resolved)||{})[t];
    if(!p){el.classList.add('wl-broken');body.innerHTML='<span class="wl-embed-missing">目标不存在：'+escapeHtml(t)+'</span>';return}
    if(p===cur){body.innerHTML='<span class="wl-embed-missing">（跳过自引用）</span>';return}
    el.classList.remove('wl-broken');
    return wlFillEmbedResolved(el,p);
  }).catch(function(){body.innerHTML='<span class="wl-embed-missing">加载失败</span>'});
}

/**
 * 断链处理：点「目标不存在」的红链时，问一句要不要就地新建这篇。
 * 走现有 POST /notes（经写入守卫；quality_check=false 以便先落草稿），
 * 被守卫拦下时把后端错误原样提示，不静默失败。
 */
function wlMissing(p){
  var seg=String(p).split('/'),title=seg.pop()||String(p),subdir=seg.join('/');
  if(!confirm('「'+p+'」还不存在。\n\n现在新建这篇笔记？\n落点：'+(subdir?subdir+'/':'')+title+'.md'))return;
  ap('/notes',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:title,subdir:subdir,type:'knowledge',
                         content:'# '+title+'\n',quality_check:false})})
  .then(function(d){
    if(d&&d.error){toast.show('新建失败：'+(d.error||''),'error');return}
    toast.show('✅ 已新建：'+((d&&d.path)||title),'success');
    ltInvalidate();lt();
    // 落点由后端规范化决定，未必正好命中双链目标——复查一次，仍断就如实说。
    var c=document.getElementById('noteViewContent');
    if(c)resolveLinksIn(c,currentEditPath).then(function(n){
      if(n>0)toast.show('已新建，但仍有双链未解析到，请核对路径','error');
    }).catch(function(){});
  }).catch(function(){toast.show('新建失败','error')});
}

/**
 * 探测没拿到结果时的降级：灰标「未探测」，不可点。
 * 为什么必须有：占位符「…」是「还在加载」的意思。请求失败、超时、
 * 或者路径数超过接口 300 条上限被截断时，如果一直停在「…」，
 * 用户会以为还在等，实际上永远不会再来了 —— 那是假状态。
 */
function absDegrade(el){
  if(!el)return;
  var b=el.querySelector('.abs-badge');
  if(b)b.textContent='未探测';
  el.classList.remove('abs-note','abs-file','abs-remote');
  el.classList.add('abs-unknown');
  el.removeAttribute('data-note');
  el.setAttribute('title',(el.getAttribute('data-abs')||'')+'（未探测到结果）');
  el.style.cursor='default';
}

/**
 * 把已经画上的占位徽标还原成纯文字。
 * invalid（空串/相对路径/隐藏路径/形状不符）不该有徽标，但占位符是在
 * 渲染阶段就插进去的，所以这里必须主动拆干净：去掉 badge 节点、
 * 去掉 data-abs 与事件，只留文本。
 */
function absUnmark(el){
  if(!el)return;
  var b=el.querySelector('.abs-badge');
  if(b&&b.parentNode)b.parentNode.removeChild(b);
  el.classList.remove('abs-note','abs-file','abs-remote','abs-unknown');
  el.removeAttribute('data-note');
  el.removeAttribute('onclick');
  el.removeAttribute('title');
  el.style.cursor='';
}

/**
 * 把探测结果画成徽标。抽成纯函数是为了能测：只吃 DOM + 数据，不发请求。
 *   local_note → 绿色可点，点了跳对应笔记
 *   local_file → 绿色不可点，悬停显示完整路径
 *   remote     → 灰色不可点
 * 可点性只看 kind，不看 server 名字 —— 前缀配置只改徽标文字，不改能不能点。
 */
function markPathBadges(container,probed){
  if(!container||!probed)return 0;
  var hit=container.querySelectorAll('[data-abs]'),n=0;
  for(var i=0;i<hit.length;i++){
    var el=hit[i],key=el.getAttribute('data-abs');
    var d=key?probed[key]:null;
    // 结果里没有这条（超 300 条被截断 / 后端漏回）：降级，不能留「…」。
    if(!d){absDegrade(el);continue;}
    el.classList.remove('abs-note','abs-file','abs-remote');
    var lab='',cls='',clickable=false;
    if(d.kind==='local_note'){
      cls='abs-note';lab='本机';clickable=true;
    }else if(d.kind==='local_file'){
      cls='abs-file';lab='本机文件';
    }else if(d.kind==='remote'){
      cls='abs-remote';lab=d.server||'其他服务器';
    }else{
      // invalid（空串、相对路径、隐藏路径、形状不符）：还原成纯文字，不画徽标。
      // 注意：占位徽标是在渲染时就画上去的，这里必须主动拆掉 ——
      // 直接 continue 会把它永远留在「…」，那是假加载状态。
      absUnmark(el);
      continue;
    }
    el.classList.add(cls);
    el.setAttribute('title',(d.clean||key)+(d.kind==='local_note'?'（点击打开）':(d.kind==='remote'?'（本机不存在）':'（本机存在，不在库内）')));
    var b=el.querySelector('.abs-badge');
    if(b)b.textContent=lab;
    if(clickable&&d.note){
      el.setAttribute('data-note',d.note);
      el.style.cursor='pointer';
    }else{
      el.removeAttribute('data-note');
      el.style.cursor='default';
    }
    n++;
  }
  return n;
}

/**
 * 收集正文里所有绝对路径 → 一次 POST 批量探测 → 画徽标。
 * 逐条发请求会让手机端卡（一篇笔记几十个路径）。
 *
 * 超过接口 300 条上限时分批发：不分批的话后端只处理前 300 条，
 * 后面的路径全部拿不到结果，只能显示「未探测」——那等于白探测。
 */
var ABS_BATCH=300;
function probePathsIn(container){
  if(!container||typeof ap!=='function')return Promise.resolve(0);
  var hit=container.querySelectorAll('[data-abs]'),paths=[];
  for(var i=0;i<hit.length;i++){
    var k=hit[i].getAttribute('data-abs');
    if(k&&paths.indexOf(k)<0)paths.push(k);
  }
  if(!paths.length)return Promise.resolve(0);
  var batches=[],i;
  for(i=0;i<paths.length;i+=ABS_BATCH)batches.push(paths.slice(i,i+ABS_BATCH));
  var total=0;
  var chain=Promise.resolve();
  batches.forEach(function(b){
    chain=chain.then(function(){
      return ap('/api/path-probe',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({paths:b})
      }).then(function(d){
        total+=markPathBadges(container,(d&&d.probed)||{});
      });
    });
  });
  return chain.then(function(){
    return total;
  }).catch(function(){
    // 探测失败不能一直停在「…」——那是假状态，用户会以为还在加载。
    // 降级成「未探测」灰标，明确表示没拿到结果。
    var hit=container.querySelectorAll('[data-abs]');
    for(var i=0;i<hit.length;i++)absDegrade(hit[i]);
    return 0;
  });
}

/** 点绿色徽标跳笔记；不可点的徽标点了没反应（不是假跳转）。 */
function openAbsPath(el){
  if(!el)return false;
  var note=el.getAttribute('data-note');
  if(note){rn(note,1);return false}
  return false;
}

/** 反链面板：哪些笔记链到本篇。解析失败静默隐藏，不打断阅读。 */
function renderBacklinks(panel,rows,cur){
  if(!panel)return;
  rows=rows||[];
  if(!rows.length){panel.innerHTML='';return}
  panel.innerHTML='<h4 class="text-sm font-semibold text-gray-500 mb-2">🔗 反向链接 ('+rows.length+')</h4>'+
    rows.map(function(r){
      var p=r.path||r.source||'',t=r.title||String(p).split('/').pop().replace(/\.md$/,'');
      return '<div class="card p-2 mb-1 cursor-pointer hover:bg-gray-50" onclick="rn(\''+jss(p)+'\')">'+
        '<span class="text-sm text-blue-600">'+escapeHtml(t)+'</span>'+
        '<span class="text-xs text-gray-400 ml-2">'+escapeHtml(p)+'</span></div>';
    }).join('');
}

// ─── 搜索结果卡片化：标题 + 目录徽章 + 摘要截断 + 相似度条 ───
function srDirBadge(p){
  var a=String(p||'').split('/').filter(Boolean);
  if(a.length<2)return '';
  var d=a[0].replace(/^\d+-/,'');
  if(a.length>2)d+=' / '+a[1].replace(/^\d+-/,'');
  return d;
}
function srPct(i){var s=Number(i.display_score??i.score??0);if(!isFinite(s))s=0;s=Math.max(0,Math.min(1,s));return Math.round(s*100)}
function srSnip(t){t=String(t||'').replace(/\s+/g,' ').trim();return t.length>140?t.slice(0,140)+'…':t}
// ─── 搜索结果：查询词高亮 + 按结果 tags 的前端标签筛选（纯前端，不动后端） ───
// hl 先 esc() 再包 <mark>：否则笔记内容里的 <img onerror> 会原样进入 innerHTML
function hl(t,kw){var s=esc(t);var words=String(kw||'').split(/\s+/).filter(w=>w.length>0);if(!words.length)return s;var re=new RegExp('('+words.map(w=>w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')','gi');return s.replace(re,'<mark class="hl">$1</mark>')}
function fmtScore(i){var s=Number(i.display_score??i.score??0);if(!isFinite(s))s=0;s=Math.max(0,Math.min(1,s));return (s*100).toFixed(0)+'%'}
// 本次搜索的状态：查询词、结果集、当前选中标签。标签筛选只动前端，不重新请求
var _srState={q:'',items:[],tag:'',degraded:false,reason:''};
function dss(){
  const q=document.getElementById('sq').value.trim();if(!q)return;
  document.getElementById('sr').innerHTML=skSearch();
  ap('/api/search/semantic?q='+encodeURIComponent(q)+'&limit=8').then(d=>{
    // 后端在检索不可用时返回 degraded=true + reason（索引损坏等），
    // 此时空结果是「检索失败」而不是「确实没有匹配」，渲染时区分显示。
    _srState.q=q;_srState.items=d.items||[];_srState.tag='';
    _srState.degraded=!!d.degraded;_srState.reason=d.reason||'';
    renderSrTags();
    renderSr();
  }).catch(()=>{document.getElementById('sr').innerHTML='<p class="text-red-500 p-3">搜索请求失败</p>'});
}
// 标签 chips：只统计本次结果里出现的 tags，点击前端过滤；再点一次取消
function renderSrTags(){
  var bar=document.getElementById('srTagBar');
  if(!bar)return;
  var counts={};
  (_srState.items||[]).forEach(function(i){
    var tg=i&&i.tags;
    if(!Array.isArray(tg))return;
    tg.forEach(function(t){var k=String(t==null?'':t).trim();if(k)counts[k]=(counts[k]||0)+1});
  });
  var keys=Object.keys(counts).sort(function(a,b){return counts[b]-counts[a]||a.localeCompare(b)});
  if(!keys.length){bar.classList.add('hidden');bar.innerHTML='';return}
  bar.classList.remove('hidden');
  var h='<span class="text-gray-400 mr-1 self-center">🏷 标签</span>'
    +'<span class="tag-item'+(_srState.tag?'':' selected')+'" data-tag="">全部</span>';
  keys.forEach(function(k){
    h+='<span class="tag-item'+(_srState.tag===k?' selected':'')+'" data-tag="'+esc(k)+'" title="'+counts[k]+' 篇">#'+esc(k)+' '+counts[k]+'</span>';
  });
  bar.innerHTML=h;
  bar.querySelectorAll('.tag-item').forEach(function(el){
    el.onclick=function(){
      var t=el.getAttribute('data-tag')||'';
      _srState.tag=(_srState.tag===t)?'':t;
      renderSrTags();
      renderSr();
    };
  });
}
function renderSr(){
  var q=_srState.q,tag=_srState.tag;
  var items=_srState.items||[];
  if(tag)items=items.filter(function(i){return Array.isArray(i&&i.tags)&&i.tags.indexOf(tag)>=0});
  var warn=_srState.degraded?'<p class="text-amber-600 text-sm p-3 bg-amber-50 rounded">⚠ 检索不可用：'+esc(_srState.reason||'未知原因')+'（结果可能不完整）</p>':'';
  var empty=tag?'<p class="text-gray-500">标签「'+esc(tag)+'」下无结果</p>':'<p class="text-gray-500">无结果</p>';
  document.getElementById('sr').innerHTML=warn+(items.map(function(i){
    var pc=srPct(i),dir=srDirBadge(i.path),title=i.title||String(i.path||'').split('/').pop();
    return '<div class="card p-4 cursor-pointer hover:border-blue-300 transition sr-card" onclick="st(\'notes\');setTimeout(function(){rn(\''+jss(i.path)+'\')},300)">'
      +'<div class="sr-head"><span class="sr-title" style="color:var(--text)">'+hl(title,q)+'</span>'
      +(dir?'<span class="sr-badge" title="'+escapeHtml(i.path||'')+'">📂 '+escapeHtml(dir)+'</span>':'')+'</div>'
      +'<div class="sr-path">'+hl(i.path||'',q)+'</div>'
      +'<div class="sr-snip">'+hl(srSnip(i.snippet),q)+'</div>'
      +'<div class="sr-meta"><span>相似度</span><span class="sr-pct" title="原始分数: '+(i.score??'')+'">'+fmtScore(i)+'</span></div>'
      +'<div class="sr-bar"><i style="width:'+pc+'%"></i></div>'
      +'</div>';
  }).join('')||empty);
}
function rbi(){
  ap('/api/index/rebuild/async',{method:'POST'}).then(function(d){
    if(d.status==='already_running'){toast.show('重建已在进行中','info')}
    else if(d.status==='started'){toast.show('后台重建已启动，请稍候…','info')}
    else{toast.show(d.error||'启动失败','error');return}
    rbiPoll();
  }).catch(function(){toast.show('启动失败','error')})
}
function rbiPoll(){
  var box=document.getElementById('rbiBox');if(!box)return;
  box.classList.remove('hidden');
  ap('/api/index/rebuild/status').then(function(s){
    var b=document.getElementById('rbiBox');if(!b)return;
    var pct=s.total?Math.round(s.processed*100/s.total):0;
    var label=s.state==='running'?('⏳ 重建中 '+s.processed+' / '+s.total+'（'+pct+'%）')
      :s.state==='done'?'✅ 重建完成':s.state==='failed'?('❌ 重建失败：'+esc(s.error||'')):'空闲';
    var col=s.state==='failed'?'bg-red-500':s.state==='done'?'bg-emerald-500':'bg-blue-500';
    b.innerHTML='<div class="flex items-center justify-between text-xs mb-1"><span>'+esc(label)+'</span>'
      +((s.state==='done'||s.state==='failed')?'<span class="text-gray-400 cursor-pointer" onclick="document.getElementById(\'rbiBox\').classList.add(\'hidden\')">关闭</span>':'')
      +'</div><div class="h-2 bg-gray-200 rounded-full overflow-hidden"><div class="'+col+'" style="width:'+pct+'%;height:100%;transition:width .3s"></div></div>';
    if(s.state==='running'){setTimeout(rbiPoll,1500)}
    else if(s.state==='done'){ap('/api/index/status').then(function(x){document.getElementById('ixs').textContent='📑 '+x.indexed+' 篇'}).catch(function(){});toast.show('✅ 索引重建完成','success')}
  }).catch(function(){})
}

function la(){const q=document.getElementById('lq').value.trim();if(!q)return;const a=document.getElementById('laa');a.classList.remove('hidden');a.innerHTML='<p class="text-gray-500">思考中...</p>';
// 使用 SSE 流式输出
var sseUrl=A+'/api/llm/ask/stream';
fetch(sseUrl,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+T},body:JSON.stringify({q})}).then(response=>{
  if(!response.ok)throw new Error('HTTP '+response.status);
  var reader=response.body.getReader(),decoder=new TextDecoder(),buffer='',ans='',sources=[];
  a.innerHTML='<div class="text-gray-800" id="streamAns"></div>';
  function processLine(line){
    if(line.startsWith('event:')){
      var ev=line.slice(6).trim();
      if(ev==='start')return;
      if(ev==='done'){document.getElementById('streamAns').textContent+=' ✓';return}
      if(ev==='error')return;
    }
    if(line.startsWith('data:')){
      try{
        var d=JSON.parse(line.slice(5).trim());
        if(d.sources)sources=d.sources;
        if(d.token){ans+=d.token;document.getElementById('streamAns').textContent=ans}
      }catch(e){}
    }
  }
  function pump(){
    reader.read().then(({done,value})=>{
      if(done){if(ans){var s='';if(sources.length)s='<div class="mt-3 pt-3 border-t border-gray-200 text-xs text-gray-400">来源: '+esc(sources.join(', '))+'</div>';if(s)a.insertAdjacentHTML('beforeend',s)}return}
      buffer+=decoder.decode(value,{stream:true});
      var lines=buffer.split('\n');buffer=lines.pop()||'';
      lines.forEach(processLine);
      pump();
    }).catch(err=>{a.innerHTML='<p class="text-red-500">流中断: '+esc(err)+'</p>'})
  }
  pump();
}).catch(err=>{
  // SSE 降级到普通请求
  ap('/api/llm/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q})}).then(d=>{
    if(d.error)a.innerHTML=`<p class="text-red-500">${esc(d.error)}</p>`;
    else{var s='';if(d.sources?.length)s='<div class="mt-3 pt-3 border-t border-gray-200 text-xs text-gray-400">来源: '+esc(d.sources.join(', '))+'</div>';a.innerHTML=`<div class="text-gray-800">${esc(d.answer||'(无回答)')}</div>${s}`}
  }).catch(()=>{a.innerHTML='<p class="text-red-500">问答请求失败</p>'})
})}
var _tplCache={};
function loadTemplates(){
  var sel=document.getElementById('noteTemplate');
  if(!sel)return;
  ap('/api/templates').then(function(d){
    var list=(d&&d.templates)||[];
    _tplCache={};
    list.forEach(function(x){if(x&&x.name)_tplCache[x.name]=x});
    sel.innerHTML='<option value="">模板（可选）</option>';
    list.forEach(function(x){
      if(!x||!x.name)return;
      var o=document.createElement('option');o.value=x.name;o.textContent=x.name;sel.appendChild(o);
    });
  }).catch(function(){});
}
function applyTemplate(){
  var sel=document.getElementById('noteTemplate');
  var ta=document.getElementById('lt');
  if(!sel||!ta)return;
  var name=sel.value;
  if(!name)return;
  var tpl=_tplCache[name];
  if(!tpl){
    ap('/api/templates').then(function(d){
      var list=(d&&d.templates)||[];
      list.forEach(function(x){if(x&&x.name)_tplCache[x.name]=x});
      var one=_tplCache[name];
      if(one&&!String(ta.value||'').trim())ta.value=one.body||'';
    }).catch(function(){});
    return;
  }
  if(String(ta.value||'').trim())return;
  // 只用 body：body 为空时宁可留空，也不能把含 frontmatter 的原文塞进 textarea
  ta.value=tpl.body||'';
}
function ltgOrCreate(){const t=document.getElementById('lt').value.trim();if(!t)return;var _ts=document.getElementById('noteTemplate'),_tpl=_ts?_ts.value:'';if(t.startsWith('#')||_tpl){var m=t.match(/^#\s*(.+?)(?:\n|$)/),title=m?m[1].trim():'';if(!title||title.indexOf('{{')>=0)title=_tpl||'新建笔记';var content=_tpl?t.replace(/\{\{title\}\}/g,title):t.replace(/^#\s*[^\n]*\n?/,'').trim();var body={type:document.getElementById('noteType').value,title:title,content:content,tags:splitList(document.getElementById('noteTags').value),relations:splitList(document.getElementById('noteRelations').value),subdir:document.getElementById('noteSubdir').value.trim(),template:(document.getElementById('noteTemplate')?document.getElementById('noteTemplate').value:'')||''};document.getElementById('ltr').innerHTML='<span class="text-gray-500 text-sm">创建中...</span>';ap('/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(d.error){document.getElementById('ltr').innerHTML=`<span class="text-red-500">${esc(d.error)}</span>`;return}document.getElementById('ltr').innerHTML=`<span class="text-emerald-600 text-sm">✅ 已创建: ${esc(d.path)}</span>`;document.getElementById('lt').value='';ltInvalidate()}).catch(e=>{document.getElementById('ltr').innerHTML='<span class="text-red-500">请求失败</span>'})}else{document.getElementById('ltr').innerHTML='<span class="text-gray-500 text-sm">分析中...</span>';ap('/api/llm/tags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})}).then(d=>{if(d.error){document.getElementById('ltr').innerHTML=`<span class="text-red-500">${esc(d.error)}</span>`;return}document.getElementById('ltr').innerHTML=(d.tags||[]).map(t=>`<span class="bg-blue-50 text-blue-600 border border-blue-200 px-3 py-1 rounded-full text-sm">${esc(t)}</span>`).join('')}).catch(e=>{document.getElementById('ltr').innerHTML='<span class="text-red-500">请求失败</span>'})}}

function smartUpload(){var t=document.getElementById('smartDoc').value.trim();if(!t)return;var btn=document.getElementById('smartBtn'),res=document.getElementById('smartResult');var body={text:t,title:document.getElementById('smartTitle').value.trim(),type:document.getElementById('smartType').value,subdir:document.getElementById('smartSubdir').value.trim(),tags:splitList(document.getElementById('smartTags').value),relations:splitList(document.getElementById('smartRelations').value)};btn.disabled=true;btn.textContent='⏳ 全自动处理中...';res.classList.remove('hidden');res.innerHTML='<p class="text-gray-500">🔍 分析中 → ⚖ 判断去重 → 📝 创建/更新...</p>';ap('/api/smart-upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(d.error){res.innerHTML='<p class="text-red-500">❌ '+esc(d.error)+'</p>'}else{var icon=d.action=='updated'?'🔄':'🆕',color=d.action=='updated'?'amber':'emerald';res.innerHTML='<div class="bg-'+color+'-50 border border-'+color+'-200 rounded-lg p-4"><p class="font-medium text-'+color+'-800">'+icon+' '+esc(d.title)+'</p><p class="text-xs text-'+color+'-600 mt-1">操作: '+esc(d.action)+' · 路径: '+esc(d.path)+'</p>'+((d.match&&d.action=='updated')?'<p class="text-xs text-amber-500 mt-1">匹配: '+esc(d.match)+'</p>':'')+'</div>';document.getElementById('smartDoc').value='';ltInvalidate()}btn.disabled=false;btn.textContent='📤 智能上传（自动分析+去重）';setTimeout(()=>{res.classList.add('hidden')},5000)}).catch(e=>{res.innerHTML='<p class="text-red-500">❌ '+e+'</p>';btn.disabled=false;btn.textContent='📤 重试'})}
function lau(){ap('/auth/requests').then(d=>{const p=Object.values(d.requests||{}).filter(r=>r.status=='pending');document.getElementById('pr').innerHTML=p.length?p.map(r=>`<div class="bg-gray-50 rounded-lg p-4 border border-gray-200"><div class="flex justify-between items-start mb-2"><div><span class="font-medium text-gray-800">${esc(r.device||'?')}</span><span class="text-xs text-gray-500 ml-2">${esc(r.ip)}</span></div><span class="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full font-medium">${esc(r.permission)}</span></div><div class="text-xs text-gray-400 mb-3">${esc((r.created||'').slice(0,16))}</div><div class="flex gap-2"><button onclick="apr('${jss(r.id)}')" class="bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-1 rounded text-sm transition">✅ 批准</button><button onclick="rjr('${jss(r.id)}')" class="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded text-sm transition">❌ 拒绝</button></div></div>`).join(''):'<p class="text-gray-500">✅ 无待审批</p>'}).catch(()=>{document.getElementById('pr').innerHTML='<p class="text-gray-500">需主Token</p>'});
ap('/auth/sessions').then(d=>{document.getElementById('as').innerHTML=(d.sessions||[]).length?(d.sessions||[]).map(s=>`<div class="bg-gray-50 rounded-lg p-3 flex justify-between items-center border border-gray-200"><div><span class="font-medium text-gray-800">${esc(s.device||s.name||'?')}</span><span class="text-xs text-gray-500 ml-2">${esc(s.ip)}</span><span class="text-xs ml-2 ${s.permission=='rw'?'text-amber-600':'text-blue-600'} font-medium">${esc(s.permission)}</span><div class="text-xs text-gray-400 mt-1">${esc(s.created)} · <span class="${s.mins_left<5?'text-red-500':''}">${esc(s.remaining)}</span></div></div><button onclick="rvs('${jss(s.prefix)}')" class="text-red-400 hover:text-red-600 text-sm">✕</button></div>`).join(''):'<p class="text-gray-500">无活跃会话</p>'}).catch(()=>{document.getElementById('as').innerHTML='<p class="text-gray-500">需主Token</p>'})}
function createToken(){const body={name:document.getElementById('tokName').value.trim(),device:document.getElementById('tokDevice').value.trim(),permission:document.getElementById('tokPerm').value,ttl:document.getElementById('tokTtl').value.trim(),note:document.getElementById('tokNote').value.trim(),identity_label:document.getElementById('tokIdentity').value.trim(),platform:document.getElementById('tokPlatform').value.trim(),project_scope:document.getElementById('tokScope').value.trim(),default_dir:document.getElementById('tokDefaultDir').value.trim(),default_tags:splitList(document.getElementById('tokDefaultTags').value)};fetch(A+'/auth/token',{method:'POST',headers:{'Authorization':'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(d=>{if(d.token){showIssuedToken(d,'手动 Token 已生成');lau()}else toast.show(d.error||'创建失败','error')}).catch(()=>toast.show('创建失败','error'))}
function apr(id){fetch(A+'/auth/approve/'+id,{method:'POST',headers:{'Authorization':'Bearer '+T}}).then(r=>r.json()).then(d=>{if(d.token)showIssuedToken(d,'申请已批准');else toast.show(d.error||'失败','error');lau()})}
function rjr(id){if(!confirm('拒绝此申请?'))return;fetch(A+'/auth/reject/'+id,{method:'POST',headers:{'Authorization':'Bearer '+T}}).then(()=>lau())}
function rvs(h){if(!confirm('撤销此会话?'))return;fetch(A+'/auth/revoke/'+h,{method:'DELETE',headers:{'Authorization':'Bearer '+T}}).then(()=>lau())}

// ─── 上传 ───
function handleFiles(files){const f=files[0];uploadFileData=f;document.getElementById('fileName').textContent=f.name;document.getElementById('fileSize').textContent=(f.size/1024).toFixed(1)+'KB';document.getElementById('fileInfo').classList.remove('hidden');document.getElementById('upBtn').disabled=false;document.getElementById('upBtn').textContent='📤 上传 '+f.name}
function doUpload(){if(!uploadFileData)return;const btn=document.getElementById('upBtn');btn.disabled=true;btn.textContent='⏳ 上传中...';const fd=new FormData();fd.append('file',uploadFileData);fd.append('dir',document.getElementById('uploadDir').value||'_assets');fd.append('type',document.getElementById('uploadType').value||'');fetch(A+'/upload',{method:'POST',headers:{'Authorization':'Bearer '+T},body:fd}).then(r=>r.json()).then(d=>{if(d.error)throw new Error(d.error);const res=document.getElementById('uploadResult');res.classList.remove('hidden');res.innerHTML=`<div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4 text-sm text-emerald-700">✅ 上传成功: ${esc(d.path)} (${esc((d.size/1024).toFixed(1))}KB)</div>`;uploadFileData=null;document.getElementById('fileInfo').classList.add('hidden');btn.textContent='📤 上传完成';setTimeout(()=>{btn.disabled=false;btn.textContent='📤 选择文件后上传'},2000)}).catch(e=>{document.getElementById('uploadResult').classList.remove('hidden');document.getElementById('uploadResult').innerHTML=`<div class="bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-red-600">❌ ${esc(e.message)}</div>`;btn.disabled=false;btn.textContent='📤 重试'})}
function loadUploadHistory(){ap('/admin/audit?action=upload&limit=5').then(d=>{var h=document.getElementById('uploadResult');if(d.entries?.length&&!uploadFileData){h.classList.remove('hidden');h.innerHTML='<h4 class="text-sm font-semibold text-gray-500 mb-2">📋 最近上传</h4>'+d.entries.map(e=>`<div class="flex items-center gap-2 text-sm p-2 bg-gray-50 rounded-lg"><span class="text-xs text-gray-400">${(e.time||'').slice(5,16).replace('T',' ')}</span><span class="text-gray-600 truncate">${esc(e.path||'')}</span></div>`).join('')}}).catch(()=>{})}

// ─── 审计日志 ───
// 2026-10-18 第 4 项：原来 ll() 一次 100 条却把全部分页按钮铺出来（生产数万条 = 数百页）。
// 改成「首屏 50 条 + 加载更多，每次加 50」，彻底去掉分页按钮；同时加类别筛选与手动清理。
var logPage=1, logCatSel='', logLoaded=0, logTotal=0;
var LOG_PAGE_SIZE=50;
function llCat(c){
  logCatSel=c||'';
  document.querySelectorAll('#logCats .log-cat').forEach(function(b){
    b.classList.toggle('is-on', (b.getAttribute('data-cat')||'')===logCatSel);
  });
  ll(1);
}
function ll(pg){
  pg=pg||1;
  if(pg===1){logPage=1;logLoaded=0;document.getElementById('logBody').innerHTML='';}
  const act=document.getElementById('logAct').value,dev=document.getElementById('logDev').value;
  let url='/admin/audit?page='+pg+'&limit='+LOG_PAGE_SIZE;
  if(act)url+='&action='+encodeURIComponent(act);
  if(dev)url+='&device='+encodeURIComponent(dev);
  if(logCatSel)url+='&category='+encodeURIComponent(logCatSel);
  ap(url).then(d=>{
    if(d.error){document.getElementById('logBody').innerHTML='<tr><td colspan="5" class="p-6 text-center text-red-400">'+esc(d.error)+'</td></tr>';return}
    logPage=pg;logTotal=d.total||0;
    const rows=(d.entries||[]).map(e=>`<tr class="border-t border-gray-100 hover:bg-gray-50"><td class="p-3 text-xs text-gray-500 whitespace-nowrap">${(e.time||'').slice(0,19).replace('T',' ')}</td><td class="p-3"><span class="text-xs px-2 py-0.5 rounded-full ${e.status>=400?'bg-red-50 text-red-600':'bg-gray-100 text-gray-700'}">${esc(e.action)}</span></td><td class="p-3 text-xs text-gray-500 max-w-xs truncate">${esc(e.path||'')}</td><td class="p-3 text-xs text-gray-500"><span title="${esc(e.device||'')}">${esc((e.device||'').slice(0,25))}</span><br><span class="text-gray-400">${esc(e.ip||'')}</span></td><td class="p-3 text-xs font-medium ${e.status>=400?'text-red-500':'text-emerald-600'}">${e.status}</td></tr>`).join('');
    const body=document.getElementById('logBody');
    if(pg===1)body.innerHTML=rows||'<tr><td colspan="5" class="p-6 text-center text-gray-400">暂无日志</td></tr>';
    else body.insertAdjacentHTML('beforeend',rows);
    logLoaded=body.querySelectorAll('tr').length;
    if(!rows&&pg===1)logLoaded=0;
    document.getElementById('logCount').textContent='共 '+logTotal+' 条，已加载 '+logLoaded+' 条';
    // 是否还有下一页，只看「本页有没有取满」。
    // 不能用 logLoaded<logTotal：读日志这个动作本身也会写一条审计记录，
    // 每翻一页 total 就涨 1，永远差一条，按钮永远在，最后变成点不完的「加载更多」。
    const more=(d.entries||[]).length>=LOG_PAGE_SIZE;
    document.getElementById('logPg').innerHTML=more
      ? '<button type="button" onclick="ll('+(logPage+1)+')" class="px-4 py-1.5 rounded bg-gray-100 text-gray-700 text-sm hover:bg-gray-200">加载更多</button>'
      : (logLoaded>0?'<span class="text-xs text-gray-400">已全部加载</span>':'');
  }).catch(()=>{document.getElementById('logBody').innerHTML='<tr><td colspan="5" class="p-6 text-center text-gray-400">需主Token权限</td></tr>'});
  // 统计卡只在首屏刷新：翻页时再拉一次 /admin/audit/stats 没意义，还会多写一条日志。
  if(pg!==1)return;
  ap('/admin/audit/stats').then(d=>{if(d.error)return;
    document.getElementById('logStats').innerHTML=`<div class="card p-4 text-center"><div class="text-xl font-bold text-blue-600">${esc(d.total||0)}</div><div class="text-gray-500 text-xs">总记录</div></div><div class="card p-4 text-center"><div class="text-xl font-bold text-emerald-600">${esc(d.today||0)}</div><div class="text-gray-500 text-xs">今日</div></div><div class="card p-4 text-center"><div class="text-xl font-bold text-violet-600">${esc(Object.keys(d.devices||{}).length||0)}</div><div class="text-gray-500 text-xs">设备数</div></div>`;
    const sel=document.getElementById('logAct');if(sel.options.length<=1){Object.keys(d.actions||{}).sort().forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=a+' ('+(d.actions[a]||0)+')';sel.appendChild(o)})}
    const cats=d.categories||{};
    // 标签名取 data-label，不从 textContent 反取：
    // 首次渲染后 textContent 变成「全部39468」，下次 split(' ') 切不开，
    // 计数会一轮轮并进标签，越刷越长（glm 审查实测点出的真 bug）。
    document.querySelectorAll('#logCats .log-cat').forEach(function(b){
      const c=b.getAttribute('data-cat')||'';
      const n=c?(cats[c]||0):(d.total||0);
      b.innerHTML=esc(b.getAttribute('data-label')||'')+'<span class="log-cat-n">'+n+'</span>';
    });
  }).catch(()=>{})}

// ── 清理弹窗（第 4 项）──
// 维护者定的口径：备份是勾选框，由维护者自己决定这次要不要留底，不强制、不默认勾选。
function logCleanOpen(){
  const m=document.getElementById('logCleanMdl');
  m.classList.remove('hidden');
  document.getElementById('logCleanTotal').textContent='…';
  document.getElementById('logCleanWill').textContent='正在计算…';
  document.getElementById('logCleanBackup').checked=false;
  ap('/admin/audit/stats').then(d=>{if(!d.error)document.getElementById('logCleanTotal').textContent=(d.total||0)}).catch(()=>{});
  logCleanPreview();
}
function logCleanClose(){document.getElementById('logCleanMdl').classList.add('hidden')}
function logCleanDays(){return document.getElementById('logCleanDays').value}
function logCleanPreview(){
  const v=logCleanDays(), el=document.getElementById('logCleanWill');
  if(v==='all'){
    // all 模式也要给具体条数，不能只说「全部记录」——维护者点确认前得知道要删多少。
    el.textContent='正在计算…';
    ap('/admin/audit/stats').then(d=>{
      if(d.error){el.textContent='计算失败';return}
      el.textContent='将删除全部 '+((d.total||0))+' 条记录';
    }).catch(()=>{el.textContent='计算失败'});
    return;
  }
  el.textContent='正在计算…';
  ap('/admin/audit/cleanup/preview?days='+encodeURIComponent(v)).then(d=>{
    if(d.error){el.textContent='计算失败';return}
    el.textContent='将删除 '+d.will_delete+' 条';
  }).catch(()=>{el.textContent='计算失败'});
}
function logCleanRun(){
  const v=logCleanDays(), backup=document.getElementById('logCleanBackup').checked;
  const body=(v==='all')?{mode:'all',confirm:true,backup:backup}:{mode:'days',days:parseInt(v,10),backup:backup};
  const btn=document.getElementById('logCleanGo');
  btn.disabled=true;btn.textContent='删除中…';
  ap('/admin/audit/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{
    btn.disabled=false;btn.textContent='确认删除';
    if(d.error){toast.show('清理失败：'+d.error,'error',5000);return}
    logCleanClose();
    toast.show('已删除 '+d.deleted+' 条'+(d.backup_path?'，备份在 '+d.backup_path:''),'success',5000);
    ll(1);
  }).catch(()=>{btn.disabled=false;btn.textContent='确认删除';toast.show('清理请求失败','error',5000)});
}

// ─── 设置 ───
// 2026-10-18 第 1 项：按设计稿分组重做。三组卡片 + 退出登录区，22 项全部可见可改。
// 渲染约束：每项仍带 id="s_<键名>"，sv() 靠 #sf [id^="s_"] 收集，保存逻辑不动。
// 设置页结构由后端 `/api/settings/schema` 提供（单一真相源 = config.SETTINGS_SCHEMA）：
// 加设置项只改后端，前端按 t 画控件；首次打开设置页时异步拉取，拉到前显示「加载中」。
var _SET_GROUPS = [];
var _SET_SCHEMA_LOADED = false;
var _SET_SCHEMA_TRIED = false;
function loadSettingsSchema(){
  if(_SET_SCHEMA_LOADED)return Promise.resolve(_SET_GROUPS);
  if(typeof ap!=='function')return Promise.resolve(_SET_GROUPS);
  _SET_SCHEMA_TRIED=true;
  return ap('/api/settings/schema').then(function(d){
    var gs=(d&&d.groups)||[];
    if(gs.length){_SET_GROUPS=gs;_SET_SCHEMA_LOADED=true}
    return _SET_GROUPS;
  }).catch(function(){return _SET_GROUPS});
}
// 二级页专用：只渲染控件，不再重复输出标签和说明（标题已由 _setDetail 的大标题给出）
function _setField(f,d){
  var curVal=d[f.k];
  if(typeof curVal==='boolean')curVal=curVal?'true':'false';
  if(typeof curVal==='number')curVal=String(curVal);
  if(curVal===undefined||curVal===null)curVal='';
  var inp;
  if(f.t==='checks'){
    var cur=String(d[f.k]==null?'':d[f.k]).split(',').map(function(x){return x.trim()}).filter(Boolean);
    var box='<div class="set-checks" data-key="'+esc(f.k)+'">';
    Object.keys(f.opts||{}).forEach(function(o){
      box+='<label class="set-check"><input type="checkbox" class="s_chk" data-v="'+esc(o)+'"'
        +(cur.indexOf(o)>=0?' checked':'')+'><span>'+esc(f.opts[o])+'</span></label>';
    });
    box+='</div>';
    return '<div class="set-detail-box">'+box+'</div>';
  }
  if(f.t==='select'){
    inp='<select id="s_'+esc(f.k)+'" class="set-val set-select set-val-full">'+Object.entries(f.opts).map(function(e){return '<option value="'+esc(e[0])+'" '+(curVal===e[0]?'selected':'')+'>'+esc(e[1])+'</option>'}).join('')+'</select>';
  }else if(f.t==='textarea'){
    inp='<textarea id="s_'+esc(f.k)+'" rows="4" class="set-val set-textarea set-val-full" spellcheck="false">'+esc(curVal)+'</textarea>';
  }else{
    inp='<input id="s_'+esc(f.k)+'" type="'+esc(f.t)+'" value="'+esc(curVal)+'" class="set-val set-val-full'+(f.t==='password'?' set-secret':'')+'" autocomplete="off">';
  }
  return '<div class="set-detail-box">'+inp+'</div>';
}
// ─── 设置：一级分组列表 / 二级页单项编辑（2026-10-18 第 13 项）───
// 口径（12:48 维护者定）：参考苹果「设置」——一级是分组圆角列表（名称 + 右侧当前值 + 箭头），
// 点进二级页再改；顶部可搜索设置项。所有原有设置项一项不能少。
var _SET_CACHE = null;      // 最近一次 /admin/settings 的响应
var _SET_PAGE = '';         // '' = 一级页；否则是二级页的 key
var _SET_Q = '';            // 一级页搜索词

// ─── 梦境设置（2026-10-24）───
// 19 个键不属 DEFAULT_SETTINGS，走 /admin/settings 会被写入白名单丢弃，
// 因此单开一组、单走 /admin/dream/settings。落盘 $KB_ROOT/dream_settings.json。
// 口径：数字用 number、长文本用 textarea、密钥用 password 且只显示「已设置」不回显明文；
// 每个控件下方一行小字写默认值。
var _DREAM_GROUPS = [
  {t:'梦境 · 模型服务', d:'调用哪个模型、给多少配额', items:[
    {k:'llm_base', l:'服务地址', t:'text', i:'link', c:'i-blue', g:'连接'},
    {k:'llm_model', l:'模型名', t:'text', i:'chip', c:'i-purple', g:'连接'},
    {k:'llm_key', l:'服务密钥', t:'password', i:'key', c:'i-orange', g:'连接'},
    {k:'llm_max_tokens', l:'单次请求上限', t:'number', i:'slider', c:'i-teal', g:'配额'},
    {k:'llm_max_tokens_hard', l:'重试翻倍的硬上限', t:'number', i:'timer', c:'i-teal', g:'配额'},
    {k:'min_dream_chars', l:'正文最短长度', t:'number', i:'doc', c:'i-gray', g:'配额',
      hint:'短于此长度视为无效生成，不写库'},
    {k:'llm_system_prompt', l:'系统提示词', t:'textarea', i:'doc', c:'i-blue', g:'提示词'}
  ]},
  {t:'梦境 · 取材范围', d:'每次读多少笔记、截多长', items:[
    {k:'RECENT_DAYS', l:'「最近」判定天数', t:'number', i:'clock', c:'i-teal', g:'取材'},
    {k:'MAX_NOTES', l:'每次读入笔记数', t:'number', i:'doc', c:'i-gray', g:'取材'},
    {k:'MAX_RECENT', l:'其中最近笔记上限', t:'number', i:'doc', c:'i-gray', g:'取材'},
    {k:'CHARS_PER_NOTE', l:'每篇截断长度', t:'number', i:'doc', c:'i-gray', g:'截断'},
    {k:'MAX_TOTAL_CHARS', l:'单次读入总字数', t:'number', i:'doc', c:'i-gray', g:'截断'},
    {k:'MAX_ISLANDS', l:'孤岛候选上限', t:'number', i:'radar', c:'i-pink', g:'孤岛'}
  ]},
  {t:'梦境 · 正文反查', d:'判断两篇笔记是否真的互相引用', items:[
    {k:'REF_MIN_KEY', l:'反查键最短长度', t:'number', i:'search', c:'i-blue', g:'反查',
      hint:'太短会误匹配'},
    {k:'REF_MIN_QUOTE', l:'短键必须带引号的长度', t:'number', i:'search', c:'i-blue', g:'反查'},
    {k:'REF_WORKERS', l:'抓正文并发数', t:'number', i:'refresh', c:'i-teal', g:'并发',
      hint:'再高会被服务端限流'},
    {k:'REF_TIMEOUT', l:'抓正文超时（秒）', t:'number', i:'timer', c:'i-teal', g:'并发'}
  ]},
  {t:'梦境 · 状态与凭据', d:'去重窗口与知识库令牌', items:[
    {k:'SEEN_KEEP_DAYS', l:'去重记录保留天数', t:'number', i:'clock', c:'i-gray', g:'状态'},
    {k:'kb_token', l:'知识库令牌', t:'password', i:'lock', c:'i-orange', g:'凭据'}
  ]}
];
// 拍平成一个查找表：二级页按 key 找项时不用再套两层循环
function _dreamItems(){
  var out=[];
  _DREAM_GROUPS.forEach(function(g){g.items.forEach(function(f){out.push(f)})});
  return out;
}
function _dreamFind(pageKey){
  var k=String(pageKey||'').replace(/^dream\./,'');
  var hit=null;
  _dreamItems().forEach(function(f){if(f.k===k)hit=f});
  return hit;
}
// 梦境项的一行摘要（一级列表右侧）
function _dreamSummary(f){
  var rec=(_SET_CACHE&&_SET_CACHE.__dream)||{};
  var it=rec[f.k];
  if(!it)return '未加载';
  if(f.t==='password')return it.set?'已设置':'未设置';
  if(f.t==='textarea'){var n=String(it.value||'').split('\n').filter(function(x){return x.trim()}).length;return n?n+' 行':'未设置'}
  if(it.value===''||it.value===undefined||it.value===null)return '未设置';
  return String(it.value);
}
// 二级页控件：数字 / 文本 / 长文本 / 密码各一种，密码不回显明文
function _dreamField(f){
  var rec=(_SET_CACHE&&_SET_CACHE.__dream)||{};
  var it=rec[f.k]||{};
  var cur=it.value;
  if(cur===undefined||cur===null)cur='';
  var inp;
  if(f.t==='textarea'){
    inp='<textarea id="d_'+esc(f.k)+'" rows="4" class="set-val set-textarea set-val-full" spellcheck="false">'+esc(cur)+'</textarea>';
  }else if(f.t==='password'){
    // 已设置就只给「已设置」占位，value 恒为空：留空提交 = 保持原值
    inp='<input id="d_'+esc(f.k)+'" type="password" value="" placeholder="'+(it.set?'已设置，留空则不修改':'未设置')+'" class="set-val set-val-full set-secret" autocomplete="new-password">';
  }else{
    inp='<input id="d_'+esc(f.k)+'" type="'+(f.t==='number'?'number':'text')+'" value="'+esc(cur)+'"'
      +(f.t==='number'?' step="1"':'')+' class="set-val set-val-full" autocomplete="off">';
  }
  var dv=it.default;
  if(dv===undefined||dv===null)dv='';
  var dtext=(f.t==='password')?'默认：未设置':('默认：'+(String(dv)===''?'（空）':String(dv)));
  return '<div class="set-detail-box">'+inp
    +'<div class="set-hint set-default">'+esc(dtext)+'</div></div>';
}
// 保存：只提交当前这一项。密码留空 = 不发这个键（后端按「不动」处理）
function _dreamSave(){
  var f=_dreamFind(_SET_PAGE);
  if(!f)return;
  var el=document.getElementById('d_'+f.k);
  if(!el)return;
  var v=el.value;
  if(f.t==='password'&&v===''){toast.show('未修改','success');_setBack();return}
  var body={};body[f.k]=v;
  fetch(A+'/admin/dream/settings',{method:'POST',headers:{'Authorization':'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json()}).then(function(d){
      if(d.error){toast.show(d.error,'error');return}
      if(_SET_CACHE&&_SET_CACHE.__dream)_SET_CACHE.__dream=d.settings;
      toast.show('设置已保存','success');
      _setBack();
    }).catch(function(){toast.show('保存失败','error')});
}


// 每项的一行摘要值（一级列表右侧显示）
function _setSummary(f,d){
  if(f.t==='devices')return '查看';
  if(f.t==='checks'){
    var n=String(d[f.k]==null?'':d[f.k]).split(',').filter(function(x){return x.trim()}).length;
    return n?n+' 项已勾选':'全部关闭';
  }
  var v=d[f.k];
  if(f.t==='select'&&f.opts&&f.opts[v]!==undefined)return f.opts[v];
  if(f.t==='password')return v?'••••••••':'未设置';
  if(f.t==='textarea'){var n=String(v||'').split('\n').filter(function(x){return x.trim()}).length;return n?n+' 行':'未设置'}
  if(v===''||v===undefined||v===null)return '未设置';
  return String(v);
}

// 一级页：分组圆角列表
// 白色线条 SVG（苹果系统设置是单色线条图形，emoji 在彩色底上会糊成豆腐块）。
// 2026-10-19：苹果设置每行图标各不相同且能表意，同组共用一个是纯装饰——
// 改成按图标名逐个映射，_SET_GROUPS 里每项用 i: 指定。
var _SVG = {
  link:'<path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"/>',
  key:'<circle cx="8" cy="8" r="4"/><path d="M11 11l9 9M17 17l2-2M14 14l2-2"/>',
  chip:'<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  db:'<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/>',
  rerank:'<path d="M4 7h13M4 12h9M4 17h5"/><path d="M17 14l3 3-3 3"/>',
  slider:'<path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>',
  copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 5H6a2 2 0 0 0-2 2v9"/>',
  clock:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  refresh:'<path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v4h-4"/>',
  timer:'<circle cx="12" cy="13" r="7.5"/><path d="M12 9.5V13l2.5 1.5M9.5 3h5"/>',
  doc:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
  radar:'<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.5"/><path d="M12 12 18 6"/>',
  phone:'<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>',
  moon:'<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
  bell:'<path d="M18 9a6 6 0 1 0-12 0c0 6-2.5 7-2.5 7h17S18 15 18 9z"/><path d="M10.5 20a2 2 0 0 0 3 0"/>',
  hook:'<path d="M12 3v9a4 4 0 1 0 4 4"/><circle cx="12" cy="3" r="1.2"/>',
  lock:'<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7a3.5 3.5 0 0 1 7 0v3.5"/>',
  target:'<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
  tag:'<path d="M4 5h8l8 8-7 7-8-8z"/><circle cx="8.5" cy="9.5" r="1.3"/>',
  user:'<circle cx="12" cy="8" r="3.2"/><path d="M5.5 19.5c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/>',check:'<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
  server:'<rect x="3.5" y="4" width="17" height="6" rx="2"/><rect x="3.5" y="14" width="17" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  gear:'<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v2.6M12 18.9v2.6M21.5 12h-2.6M5.1 12H2.5M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8M18.7 18.7l-1.8-1.8M7.1 7.1 5.3 5.3"/>'
};
function _svgOf(name){
  var p=_SVG[name]||_SVG.gear;
  return '<svg viewBox="0 0 24 24">'+p+'</svg>';
}
// 只渲染列表部分：搜索框本身不重建，否则中文输入法拼音会被打断（DS 审查指出）
function _setList(){
  var d=_SET_CACHE||{};
  var q=_SET_Q.trim().toLowerCase();
  var h='';
  var total=0;
  _SET_GROUPS.forEach(function(g){
    var items=g.items.filter(function(f){
      if(!q)return true;
      return (f.l+' '+f.k+' '+(f.hint||'')).toLowerCase().indexOf(q)>=0;
    });
    if(!items.length)return;
    total+=items.length;
    // 2026-10-19 遗留 #15：组内再按 f.g 拆成小卡（对话服务 / 向量服务 / 存储与清理 …）。
    // 没写 f.g 的项归到组名本身，保证不漏项；搜索时只渲染命中的小卡。
    var subs=[],subIdx={};
    items.forEach(function(f){
      var name=f.g||g.t;
      if(subIdx[name]===undefined){subIdx[name]=subs.length;subs.push({t:name,items:[]})}
      subs[subIdx[name]].items.push(f);
    });
    h+='<div class="set-group"><div class="set-group-head">'+esc(g.group||g.t)+'</div>';
    subs.forEach(function(sg){
      if(subs.length>1)h+='<div class="set-subhead">'+esc(sg.t)+'</div>';
      h+='<div class="set-card">';
      sg.items.forEach(function(f){
        h+='<div class="set-row set-row-link" onclick="_setOpen(\''+esc(f.k)+'\')"><span class="set-icon '+(f.c||'')+'">'+_svgOf(f.i)+'</span><div class="set-label"><span>'+esc(f.l)+'</span></div>'
          +'<span class="set-cur">'+esc(_setSummary(f,d))+'</span><span class="set-arrow">›</span></div>';
      });
      h+='</div>';
    });
    h+='</div>';
  });
  // 梦境组（2026-10-24）：独立接口 /admin/dream/settings，项来自 _DREAM_GROUPS。
  // 搜索时同一套匹配规则，命中才渲染。
  _DREAM_GROUPS.forEach(function(g){
    var items=g.items.filter(function(f){
      if(!q)return true;
      return ('梦境 '+f.l+' '+f.k+' '+(f.hint||'')).toLowerCase().indexOf(q)>=0;
    });
    if(!items.length)return;
    total+=items.length;
    var subs=[],subIdx={};
    items.forEach(function(f){
      var name=f.g||g.t;
      if(subIdx[name]===undefined){subIdx[name]=subs.length;subs.push({t:name,items:[]})}
      subs[subIdx[name]].items.push(f);
    });
    h+='<div class="set-group"><div class="set-group-head">'+esc(g.group||g.t)+'</div>';
    subs.forEach(function(sg){
      if(subs.length>1)h+='<div class="set-subhead">'+esc(sg.t)+'</div>';
      h+='<div class="set-card">';
      sg.items.forEach(function(f){
        // 页面键加 dream. 前缀：llm_base/llm_model/llm_key 三项与「大模型」组同名，
        // 不加前缀点进梦境项会串到原组那一项上（实测过）。
        h+='<div class="set-row set-row-link" onclick="_setOpen(\''+esc('dream.'+f.k)+'\')"><span class="set-icon '+(f.c||'')+'">'+_svgOf(f.i)+'</span><div class="set-label"><span>'+esc(f.l)+'</span></div>'
          +'<span class="set-cur">'+esc(_dreamSummary(f))+'</span><span class="set-arrow">›</span></div>';
      });
      h+='</div>';
    });
    h+='</div>';
  });
  // 账号组
  if(q&&'账号 退出登录 登录状态'.toLowerCase().indexOf(q)<0){}else{
    h+='<div class="set-group"><div class="set-group-head">账号</div><div class="set-card">'
      +'<div class="set-row"><span class="set-icon i-blue">'+_svgOf('user')+'</span><div class="set-label"><span>退出登录</span><span class="set-hint">清除本机保存的登录令牌，需要重新审批</span></div>'
      +'<button type="button" onclick="setLogout()" class="set-logout">退出登录</button></div></div></div>';
    total+=1;
  }
  if(!total)h+='<p class="set-empty">没有匹配的设置项</p>';
  return h;
}

function _setHome(){
  // 设置页结构来自后端 schema：首次打开先拉一次，未拉到前不渲染空列表。
  // 只试一次；失败给出明确提示，避免停在「永远加载中」的假状态。
  var sf=document.getElementById('sf');
  if(!_SET_GROUPS.length){
    if(_SET_SCHEMA_TRIED && !_SET_SCHEMA_LOADED){
      sf.innerHTML='<div class="set-title">设置</div><p class="set-empty">设置项加载失败，请刷新页面重试</p>';
      return;
    }
    sf.innerHTML='<div class="set-title">设置</div><p class="set-empty">正在加载设置项…</p>';
    loadSettingsSchema().then(function(){_setHome()});
    return;
  }
  var h='<div class="set-title">设置</div>'
    +'<div class="set-search"><input id="setSearch" type="search" placeholder="搜索设置项"'
    +' value="'+esc(_SET_Q)+'" oninput="_setSearch(this.value)"></div>'
    +'<div id="setList">'+_setList()+'</div>';
  sf.innerHTML=h;
}

// 二级页：单个设置项编辑
function _setDetail(){
  var d=_SET_CACHE||{}, f=null;
  _SET_GROUPS.forEach(function(g){g.items.forEach(function(x){if(x.k===_SET_PAGE)f=x})});
  // 梦境项走自己的控件与保存函数（接口不同，键也不在 d 里）。
  // 页面键带 dream. 前缀，先于原组匹配，避免同名键串页。
  if(!f || String(_SET_PAGE).indexOf('dream.')===0){
    var df=_dreamFind(_SET_PAGE);
    if(!df){_SET_PAGE='';return _setHome()}
    var hd='<div class="set-back" onclick="_setBack()">‹ 设置</div>'
      +'<div class="set-title">'+esc(df.l)+'</div>'
      +(df.hint?'<div class="set-foot">'+esc(df.hint)+'</div>':'')
      +'<div class="set-card">'+_dreamField(df)+'</div>'
      +'<div class="set-save-bar"><button type="button" class="set-save" onclick="_dreamSave()">保存</button></div>';
    document.getElementById('sf').innerHTML=hd;
    return;
  }
  if(f.t==='devices'){
    // iOS 范式：返回 › 大标题 › 说明小字 › 无边框白卡
    var h2='<div class="set-back" onclick="_setBack()">‹ 设置</div>'
      +'<div class="set-title">'+esc(f.l)+'</div>'
      +(f.hint?'<div class="set-foot">'+esc(f.hint)+'</div>':'')
      +'<div class="set-card"><div id="devList" class="set-dev-list"><div class="set-empty">加载中…</div></div></div>';
    document.getElementById('sf').innerHTML=h2;
    _setLoadDevices();
    return;
  }
  var h='<div class="set-back" onclick="_setBack()">‹ 设置</div>'
    +'<div class="set-title">'+esc(f.l)+'</div>'
    +(f.hint?'<div class="set-foot">'+esc(f.hint)+'</div>':'')
    +'<div class="set-card">'
    // 二级页只放控件：标题和说明已由上面的大标题/小字给出，_setRow 会重复输出
    +_setField(f,d)+'</div>'
    +'<div class="set-save-bar"><button type="button" class="set-save" onclick="_setSave()">保存</button></div>';
  document.getElementById('sf').innerHTML=h;
}

function _setLoadDevices(){
  var box=document.getElementById('devList');
  if(!box)return;
  ap('/auth/devices').then(function(d){
    var list=(d&&d.devices)||[];
    if(!list.length){box.innerHTML='<div class="set-empty">还没有登录记录</div>';return}
    var h='';
    list.forEach(function(it){
      var tks=it.tokens||[];
      h+='<div class="set-dev"><div class="set-dev-top">'
        +'<span class="set-dev-name">'+esc(it.label||it.device||'未知设备')+'</span>'
        +'<span class="set-dev-meta">'+esc(it.last_ip||'')+'</span></div>'
        +'<div class="set-dev-meta">首次 '+esc((it.first_seen||'').slice(0,16))
        +' · 最近 '+esc((it.last_seen||'').slice(0,16))
        +' · 使用 '+(it.uses||0)+' 次'
        +((it.fails||0)?' · 失败 '+it.fails+' 次':'')+'</div>';
      if(tks.length){
        h+='<div class="set-dev-tks">';
        tks.forEach(function(t){
          // master 令牌不给吊销按钮：删掉它维护者自己就登不进来了
          h+='<div class="set-dev-tk"><span>'+esc(t.name||t.prefix||'令牌')+'</span>'
            +'<span class="set-dev-meta">'+esc(t.expires_at||'')+'</span>'
            +(t.is_master
              ? '<span class="set-dev-meta">主令牌</span>'
              : '<button class="set-revoke" onclick="_setRevoke(\''+esc(t.prefix||'')+'\',\''+esc(it.device||'')+'\')">吊销</button>')
            +'</div>';
        });
        h+='</div>';
      }else{
        // 令牌已吊销（审计里有流水、令牌表里已没有）：单独标出来，
        // 不能显示成「未绑定设备」那样像是还能用的条目
        h+='<div class="set-dev-meta">'+(it.revoked?'已吊销':'无有效令牌')+'</div>';
      }
      h+='</div>';
    });
    box.innerHTML=h;
  }).catch(function(){box.innerHTML='<div class="set-empty">加载失败</div>'});
}
function _setRevoke(hint,device){
  if(!hint){toast.show('令牌信息缺失','error');return}
  if(!confirm('吊销「'+device+'」的这枚令牌？该设备立即失效。'))return;
  ap('/auth/revoke/'+encodeURIComponent(hint),'DELETE').then(function(){
    toast.show('已吊销','success');_setLoadDevices();
  }).catch(function(){toast.show('吊销失败','error')});
}
function _setOpen(k){_SET_PAGE=k;_setDetail();window.scrollTo(0,0)}
function _setBack(){_SET_PAGE='';_setHome()}
function _setSearch(v){
  _SET_Q=v||'';
  var box=document.getElementById('setList');
  if(box){box.innerHTML=_setList();return}
  // 兜底：万一结构被改坏，退回整页重绘（不影响输入法以外的行为）
  _setHome();
}

// 二级页保存：只提交当前这一项，成功后回到一级页并把新值写回缓存
function _setSave(){
  var el=document.querySelector('#sf [id^="s_"]');
  var k,v;
  if(el){
    k=el.id.slice(2); v=el.value;
    if(v==='<REDACTED>'){toast.show('未修改','success');_setBack();return}
  }else{
    // 多选（checks）没有单值控件，取 #sf 里勾上的复选框
    var box=document.querySelector('#sf .set-checks');
    if(!box)return;
    k=box.getAttribute('data-key');
    var picked=[];
    Array.prototype.forEach.call(box.querySelectorAll('input.s_chk'),function(c){
      if(c.checked)picked.push(c.getAttribute('data-v'));
    });
    v=picked.join(',');
  }
  var body={};body[k]=v;
  fetch(A+'/admin/settings',{method:'POST',headers:{'Authorization':'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json()}).then(function(d){
      if(d.error){toast.show(d.error,'error');return}
      if(_SET_CACHE)_SET_CACHE[k]=v;
      toast.show('设置已保存','success');
      _setBack();
    }).catch(function(){toast.show('保存失败','error')});
}

function ls(){
  // 两套接口并行拉：/admin/settings（原有项）与 /admin/dream/settings（梦境 19 项）。
  // 梦境接口失败不能把整个设置页拖成「加载失败」——单独降级成「未加载」。
  var p1=ap('/admin/settings');
  var p2=ap('/admin/dream/settings').catch(function(){return {settings:null}});
  Promise.all([p1,p2]).then(function(r){
    var d=r[0]||{};
    d.__dream=(r[1]&&r[1].settings)||null;
    _SET_CACHE=d;
    if(_SET_PAGE)_setDetail();else _setHome();
  }).catch(function(){document.getElementById('sf').innerHTML='<p class="text-red-500">设置加载失败</p>'});
}

function setLogout(){
  if(!confirm('确定退出登录？退出后需要重新审批才能访问。'))return;
  rl();  // 复用既有登出逻辑（清 kb_token、回登录页），不另写一套
}
function sv(){const s={};document.querySelectorAll('#sf [id^="s_"]').forEach(el=>{if(el.value==='<REDACTED>')return;const k=el.id.slice(2);s[k]=el.value});fetch(A+'/admin/settings',{method:'POST',headers:{'Authorization':'Bearer '+T,'Content-Type':'application/json'},body:JSON.stringify(s)}).then(r=>r.json()).then(d=>{if(d.error)toast.show(d.error,'error');else toast.show('设置已保存','success')})}

function lsk(){
  const box=document.getElementById('skillsList');
  box.innerHTML='<div class="card p-4 text-sm text-gray-500">正在加载分享中心预览…</div>';
  Promise.all([
    ap('/api/integrations/share/manifest'),
    ap('/api/integrations/share/config-preview?platform=claude&readonly=true'),
    ap('/api/integrations/share/scan')
  ]).then(([manifest,config,scan])=>{
    if(manifest.error||config.error||scan.error){box.innerHTML='<p class="text-red-500">'+escapeHtml(manifest.error||config.error||scan.error)+'</p>';return}
    const tools=manifest.tools||[];
    const defaultTools=tools.filter(t=>t.share_default).length;
    const blockers=scan.summary?.blockers||0;
    const warnings=scan.summary?.warnings||0;
    const findings=(scan.findings||[]).map(f=>`<tr><td class="px-2 py-1">${escapeHtml(f.target)}</td><td class="px-2 py-1">${escapeHtml(f.severity)}</td><td class="px-2 py-1">${escapeHtml(f.kind)}</td><td class="px-2 py-1">${escapeHtml(f.message)}</td></tr>`).join('')||'<tr><td colspan="4" class="px-2 py-2 text-gray-500">没有发现问题</td></tr>';
    box.innerHTML=`
    <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
      <div class="bg-blue-50 border border-blue-200 rounded-lg p-4"><div class="text-sm font-semibold text-blue-700">分享阶段</div><div class="text-xl font-bold text-blue-600 mt-1">Phase 1</div><div class="text-xs text-blue-600 mt-1">预览与脱敏</div></div>
      <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4"><div class="text-sm font-semibold text-emerald-700">默认分享工具</div><div class="text-2xl font-bold text-emerald-600 mt-1">${defaultTools}/${tools.length}</div></div>
      <div class="bg-amber-50 border border-amber-200 rounded-lg p-4"><div class="text-sm font-semibold text-amber-700">扫描警告</div><div class="text-2xl font-bold text-amber-600 mt-1">${warnings}</div></div>
      <div class="${blockers?'bg-red-50 border-red-200':'bg-slate-50 border-slate-200'} border rounded-lg p-4"><div class="text-sm font-semibold ${blockers?'text-red-700':'text-slate-700'}">阻断项</div><div class="text-2xl font-bold ${blockers?'text-red-600':'text-slate-600'} mt-1">${blockers}</div></div>
    </div>
    <div class="card p-4 mt-3"><h4 class="font-semibold text-gray-800 mb-2">下载 / API</h4><div class="flex gap-2 flex-wrap">
      <button onclick="downloadAuth('/api/integrations/share/download?artifact=manifest','kb-share-manifest.json')" class="bg-blue-600 text-white px-3 py-1 rounded text-sm">下载 manifest</button>
      <button onclick="downloadAuth('/api/integrations/share/download?artifact=config&platform=claude&readonly=true','kb-mcp-config-preview.json')" class="bg-emerald-600 text-white px-3 py-1 rounded text-sm">下载只读配置模板</button>
      <button onclick="downloadAuth('/api/integrations/share/download?artifact=scan','kb-share-sensitive-scan.json')" class="bg-violet-600 text-white px-3 py-1 rounded text-sm">下载扫描报告</button>
      <button disabled class="bg-gray-200 text-gray-500 px-3 py-1 rounded text-sm cursor-not-allowed">zip 导出：第二阶段</button>
    </div><p class="text-xs text-gray-500 mt-2">下载请求仅使用 Authorization Header，不把 token 放入 URL。</p></div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
      <div class="card p-4"><h4 class="font-semibold text-gray-800 mb-2">Manifest 预览</h4><pre class="text-xs overflow-x-auto bg-gray-50 p-3 rounded max-h-96">${escapeHtml(JSON.stringify(manifest,null,2))}</pre></div>
      <div class="card p-4"><h4 class="font-semibold text-gray-800 mb-2">只读 MCP 配置预览</h4><pre class="text-xs overflow-x-auto bg-gray-50 p-3 rounded max-h-96">${escapeHtml(JSON.stringify(config,null,2))}</pre></div>
    </div>
    <div class="card p-4 mt-3"><h4 class="font-semibold text-gray-800 mb-2">敏感扫描结果</h4><div class="overflow-x-auto"><table class="w-full text-xs"><thead><tr class="text-left text-gray-500"><th class="px-2 py-1">目标</th><th class="px-2 py-1">级别</th><th class="px-2 py-1">类型</th><th class="px-2 py-1">说明</th></tr></thead><tbody>${findings}</tbody></table></div></div>
    <div class="card p-4 mt-3"><h4 class="font-semibold text-gray-800 mb-2">暂不纳入第一阶段</h4><div class="flex flex-wrap gap-2 text-xs"><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">完整 skills 导出</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">SSE 分享配置</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">私有数据库</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">日志</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">settings 原文</span></div></div>`;
  }).catch(()=>{box.innerHTML='<p class="text-red-500">加载失败</p>'})
}
// ─── 插件商城（T6）：已装插件 / 主题 / 文档模板 / 本地导入说明 ───
// 只读接口用 ap()（自带 Authorization 头）；启停开关是 @require_master，
// 非主 Token 会拿到 403 {"error":"需主Token"}，这里把后端原文照原样显示，不吞错误。
function lmarket(){
  lmarketPlugins();lmarketThemes();lmarketTemplates();
  // 2026-10-19 T10：原「技能 / MCP」页并入商城，挂载点在原四个区块之后
  lmarketSkills();lmarketMcp();
}
function lmarketPlugins(){
  var box=document.getElementById('marketPlugins');
  if(!box)return;
  box.innerHTML='<span class="text-gray-400 text-xs">加载中...</span>';
  ap('/api/plugins').then(function(d){
    if(d&&d.error){box.innerHTML='<p class="text-red-500 text-xs">'+escapeHtml(d.error)+'</p>';return}
    var list=(d&&d.plugins)||[];
    if(!list.length){box.innerHTML='<p class="text-gray-500 text-xs">还没有插件。把插件目录放到 web/plugins/&lt;名字&gt;/ 下再刷新。</p>';return}
    box.innerHTML=list.map(function(p){
      var on=!!p.enabled;
      return '<div class="bg-gray-50 border border-gray-200 rounded-lg p-3">'
        +'<div class="flex items-center justify-between gap-3">'
        +'<div><span class="font-medium text-gray-800">'+escapeHtml(p.title||p.name)+'</span>'
        +'<span class="text-xs text-gray-400 ml-2 font-mono">'+escapeHtml(p.name)+'</span>'
        +'<span class="text-xs text-gray-400 ml-2">v'+escapeHtml(p.version||'-')+'</span></div>'
        +'<button onclick="lmarketToggle(\''+jss(p.name)+'\','+(on?'false':'true')+')" class="text-xs px-3 py-1 rounded text-white '+(on?'bg-emerald-600 hover:bg-emerald-700':'bg-gray-300')+'">'+(on?'已启用':'已停用')+'</button>'
        +'</div>'
        +'<p class="text-xs text-gray-600 mt-2">'+escapeHtml(p.description||'（无描述）')+'</p>'
        +'<p class="text-xs text-gray-400 mt-1">挂载点：'+escapeHtml(p.mount||'-')+' · 入口：'+escapeHtml(p.entry||'-')+'</p>'
        +'</div>';
    }).join('');
  }).catch(function(){box.innerHTML='<p class="text-red-500 text-xs">插件列表加载失败</p>'});
}
function lmarketToggle(name,enabled){
  ap('/api/plugins/'+encodeURIComponent(name)+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!!enabled})}).then(function(d){
    if(d&&d.error){toast.show(d.error,'error',6000);return}
    toast.show((enabled?'已启用：':'已停用：')+name,'success');
    lmarketPlugins();
  }).catch(function(){toast.show('启停请求失败','error')});
}
// 主题主色：只用于卡片上的预览色块，值与 themes/*.css 里的主色一致；
// 未来插件加的主题没有登记，用灰点兜底，不影响「应用」。
var LMARKET_THEME_COLORS={aurora:'#5b6cf9',console:'#238636',classic:'#2563eb'};
function lmarketThemes(){
  var box=document.getElementById('marketThemes');
  if(!box)return;
  box.innerHTML='<span class="text-gray-400 text-xs">加载中...</span>';
  ap('/api/themes').then(function(d){
    var list=(d&&d.themes)||[];
    if(d&&d.error&&!list.length){box.innerHTML='<p class="text-red-500 text-xs">'+escapeHtml(d.error)+'</p>';return}
    if(!list.length){box.innerHTML='<p class="text-gray-500 text-xs">没有可用主题</p>';return}
    // 当前主题直接读 localStorage 的 kb_theme（与 ui.js 的 theme() 同源），切换仍走 kbSetTheme
    var cur=localStorage.getItem('kb_theme')||'aurora';
    box.innerHTML=list.map(function(t){
      var on=t.id===cur;
      var dot=LMARKET_THEME_COLORS[t.id]||'#9ca3af';
      return '<div class="w-full flex items-center gap-3 bg-gray-50 border rounded-lg p-3 '+(on?'border-blue-500':'border-gray-200')+'">'
        +'<span class="inline-block w-6 h-6 rounded-full border border-white shadow-sm flex-shrink-0" style="background:'+dot+'" title="主题主色 '+dot+'"></span>'
        +'<div class="flex-1 min-w-0">'
        +'<span class="font-medium text-gray-800">'+escapeHtml(t.name||t.id)+'</span>'
        +'<span class="block text-xs text-gray-500 mt-1">'+escapeHtml(t.desc||'（无说明）')+'</span>'
        +'</div>'
        +'<button onclick="lmarketUseTheme(\''+jss(t.id)+'\')" '+(on?'disabled ':'')+'class="text-xs px-3 py-1 rounded text-white flex-shrink-0 '+(on?'bg-gray-300 cursor-default':'bg-blue-600 hover:bg-blue-700')+'">'+(on?'当前':'应用')+'</button>'
        +'</div>';
    }).join('')
      // 主题文件在服务器磁盘上，前端没有「卸载」这个动作，文案不造假
      +'<p class="text-xs text-gray-400 mt-1">主题只有「应用 / 当前」两种状态，不提供卸载：主题文件在服务器磁盘上，删除需管理员操作。</p>';
    lmarketThemeCustom(box);
  }).catch(function(){box.innerHTML='<p class="text-red-500 text-xs">主题列表加载失败</p>'});
}

// ─── 自定义配色（纯前端，无后端接口）───
// 三个颜色写进 localStorage.kb_theme_custom，并以内联 CSS 变量覆盖当前主题；
// 内联变量优先级高于主题 CSS 文件，所以刷新后由 kbCustomApply() 还原即可保持。
var KB_CUSTOM_KEY='kb_theme_custom';
var KB_CUSTOM_FIELDS=[['accent','主色','--k-accent'],['bg','背景','--bg'],['know','强调色','--k-know']];
var KB_CUSTOM_FALLBACK={accent:'#5b6cf9',bg:'#f3f5fb',know:'#3b82f6'};
function kbCustomRead(){
  try{var o=JSON.parse(localStorage.getItem(KB_CUSTOM_KEY)||'null');return (o&&typeof o==='object')?o:null}catch(e){return null}
}
// 取当前主题下该变量的实际颜色：极光的 --k-accent 是渐变，抽第一个 #rrggbb 给颜色选择器
function kbCustomColor(name){
  var v='';
  try{v=String(getComputedStyle(document.documentElement).getPropertyValue('--k-'+name)||'').trim()}catch(e){}
  if(name==='bg'){try{v=String(getComputedStyle(document.documentElement).getPropertyValue('--bg')||'').trim()}catch(e){}}
  var m=v.match(/#[0-9a-fA-F]{6}/);
  if(m)return m[0].toLowerCase();
  return KB_CUSTOM_FALLBACK[name];
}
function kbCustomApply(){
  var o=kbCustomRead();if(!o)return false;
  var h=document.documentElement,ok=false;
  KB_CUSTOM_FIELDS.forEach(function(f){
    var v=o[f[0]];
    if(typeof v==='string'&&/^#[0-9a-fA-F]{6}$/.test(v)){h.style.setProperty(f[2],v);ok=true}
  });
  return ok;
}
function kbCustomClear(){
  try{localStorage.removeItem(KB_CUSTOM_KEY)}catch(e){}
  var h=document.documentElement;
  KB_CUSTOM_FIELDS.forEach(function(f){h.style.removeProperty(f[2])});
}
function kbCustomFill(){
  var o=kbCustomRead()||{};
  KB_CUSTOM_FIELDS.forEach(function(f){
    var el=document.getElementById('kbCustom_'+f[0]);
    if(!el)return;
    var v=o[f[0]];
    el.value=(typeof v==='string'&&/^#[0-9a-fA-F]{6}$/.test(v))?v:kbCustomColor(f[0]);
  });
}
function kbCustomSave(){
  var vals={},bad='';
  KB_CUSTOM_FIELDS.forEach(function(f){
    var el=document.getElementById('kbCustom_'+f[0]);
    var v=el?String(el.value||'').toLowerCase():'';
    if(!/^#[0-9a-fA-F]{6}$/.test(v))bad=f[1];else vals[f[0]]=v;
  });
  if(bad){toast.show('颜色值不合法：'+bad,'error');return}
  try{localStorage.setItem(KB_CUSTOM_KEY,JSON.stringify(vals))}catch(e){toast.show('浏览器不允许写本地存储','error');return}
  kbCustomApply();
  toast.show('自定义配色已应用','success');
}
function kbCustomReset(){
  kbCustomClear();
  kbCustomFill();
  toast.show('已恢复当前主题默认配色','success');
}
// 面板挂在「主题」卡片内、列表下方；重复进商城只复用，不重复插入
function lmarketThemeCustom(box){
  var panel=document.getElementById('marketThemeCustom');
  if(!panel){
    panel=document.createElement('div');
    panel.id='marketThemeCustom';
    panel.className='mt-4 pt-4 border-t border-gray-200';
    panel.innerHTML='<p class="text-sm font-medium text-gray-800 mb-2">自定义配色</p>'
      +'<p class="text-xs text-gray-500 mb-2">只改当前浏览器，不动服务器上的主题文件；刷新后保持。</p>'
      +'<div class="flex flex-wrap items-end gap-3">'
      +KB_CUSTOM_FIELDS.map(function(f){
        return '<label class="text-xs text-gray-600">'+f[1]
          +'<input type="color" id="kbCustom_'+f[0]+'" class="block mt-1 w-14 h-9 p-0 border border-gray-300 rounded cursor-pointer">'
          +'</label>';
      }).join('')
      +'<button onclick="kbCustomSave()" class="text-xs px-3 py-1 rounded text-white bg-blue-600 hover:bg-blue-700">应用</button>'
      +'<button onclick="kbCustomReset()" class="text-xs px-3 py-1 rounded text-gray-700 bg-gray-100 hover:bg-gray-200">恢复默认</button>'
      +'</div>';
    box.parentNode.insertBefore(panel,box.nextSibling);
  }
  kbCustomFill();
}
kbCustomApply();
function lmarketUseTheme(id){
  // 复用 ui.js 已有的主题切换（写 kb_theme 并重载页面），不另写一套
  if(typeof window.kbSetTheme==='function')window.kbSetTheme(id);
  else toast.show('主题切换不可用','error');
}
function lmarketTemplates(){
  var box=document.getElementById('marketTemplates');
  if(!box)return;
  box.innerHTML='<span class="text-gray-400 text-xs">加载中...</span>';
  ap('/api/templates').then(function(d){
    if(d&&d.error){box.innerHTML='<p class="text-red-500 text-xs">'+escapeHtml(d.error)+'</p>';return}
    var list=(d&&d.templates)||[];
    if(!list.length){box.innerHTML='<p class="text-gray-500 text-xs">没有模板</p>';return}
    box.innerHTML=list.map(function(t){
      // 模板没有独立的描述字段：内置模板说明来源，其余用章节名概括
      var desc=t.builtin?'内置兜底模板（_模板/ 目录为空时使用）':((t.sections||[]).join(' · ')||'（无章节）');
      return '<div class="bg-gray-50 border border-gray-200 rounded-lg p-3">'
        +'<span class="font-medium text-gray-800">'+escapeHtml(t.name||t.title||'-')+'</span>'
        +(t.file?'<span class="text-xs text-gray-400 ml-2 font-mono">'+escapeHtml(t.file)+'</span>':'')
        +'<p class="text-xs text-gray-500 mt-1">'+escapeHtml(desc)+'</p>'
        +'</div>';
    }).join('');
  }).catch(function(){box.innerHTML='<p class="text-red-500 text-xs">模板列表加载失败</p>'});
}
// ─── T10：技能（原「技能 / MCP 分享中心」整页并入商城）───
// 数据仍走原来那三个接口，不新增后端接口；DOM 由本函数自建，index.html 只保留隐藏的旧容器。
function lmarketSkills(){
  var host=document.getElementById('tab-market');
  if(!host)return;
  var box=document.getElementById('marketSkills');
  if(!box){
    box=document.createElement('div');
    box.id='marketSkills';
    box.className='card p-4';
    box.innerHTML='<div class="flex items-center justify-between mb-2"><h4 class="font-semibold text-gray-800">技能</h4><button onclick="lmarketSkills()" class="text-xs px-3 py-1 rounded text-white bg-blue-600 hover:bg-blue-700">刷新预览</button></div>'
      +'<p class="text-xs text-gray-500 mb-3">第一阶段仅提供 manifest、只读 MCP 配置预览和敏感扫描报告；不导出完整 skills、私有数据库、日志、settings 或真实 token。</p>'
      +'<div id="marketSkillsList" class="space-y-3"><span class="text-gray-400 text-xs">加载中...</span></div>';
    var wrap=document.getElementById('marketBlocks')||host;
    wrap.appendChild(box);
  }
  var list=document.getElementById('marketSkillsList');
  if(!list)return;
  list.innerHTML='<span class="text-gray-400 text-xs">正在加载分享中心预览…</span>';
  Promise.all([
    ap('/api/integrations/share/manifest'),
    ap('/api/integrations/share/config-preview?platform=claude&readonly=true'),
    ap('/api/integrations/share/scan')
  ]).then(function(res){
    var manifest=res[0],config=res[1],scan=res[2];
    if(manifest.error||config.error||scan.error){list.innerHTML='<p class="text-red-500 text-xs">'+escapeHtml(manifest.error||config.error||scan.error)+'</p>';return}
    var tools=manifest.tools||[];
    var defaultTools=tools.filter(function(t){return t.share_default}).length;
    var blockers=(scan.summary&&scan.summary.blockers)||0;
    var warnings=(scan.summary&&scan.summary.warnings)||0;
    var findings=(scan.findings||[]).map(function(f){
      return '<tr><td class="px-2 py-1">'+escapeHtml(f.target)+'</td><td class="px-2 py-1">'+escapeHtml(f.severity)+'</td><td class="px-2 py-1">'+escapeHtml(f.kind)+'</td><td class="px-2 py-1">'+escapeHtml(f.message)+'</td></tr>';
    }).join('')||'<tr><td colspan="4" class="px-2 py-2 text-gray-500">没有发现问题</td></tr>';
    list.innerHTML=
      '<div class="grid grid-cols-1 md:grid-cols-4 gap-3">'
      +'<div class="bg-blue-50 border border-blue-200 rounded-lg p-4"><div class="text-sm font-semibold text-blue-700">分享阶段</div><div class="text-xl font-bold text-blue-600 mt-1">Phase 1</div><div class="text-xs text-blue-600 mt-1">预览与脱敏</div></div>'
      +'<div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4"><div class="text-sm font-semibold text-emerald-700">默认分享工具</div><div class="text-2xl font-bold text-emerald-600 mt-1">'+defaultTools+'/'+tools.length+'</div></div>'
      +'<div class="bg-amber-50 border border-amber-200 rounded-lg p-4"><div class="text-sm font-semibold text-amber-700">扫描警告</div><div class="text-2xl font-bold text-amber-600 mt-1">'+warnings+'</div></div>'
      +'<div class="'+(blockers?'bg-red-50 border-red-200':'bg-slate-50 border-slate-200')+' border rounded-lg p-4"><div class="text-sm font-semibold '+(blockers?'text-red-700':'text-slate-700')+'">阻断项</div><div class="text-2xl font-bold '+(blockers?'text-red-600':'text-slate-600')+' mt-1">'+blockers+'</div></div>'
      +'</div>'
      +'<div class="bg-gray-50 border border-gray-200 rounded-lg p-3 mt-3"><h4 class="font-semibold text-gray-800 mb-2">下载 / API</h4><div class="flex gap-2 flex-wrap">'
      +'<button onclick="downloadAuth(\'/api/integrations/share/download?artifact=manifest\',\'kb-share-manifest.json\')" class="bg-blue-600 text-white px-3 py-1 rounded text-sm">下载 manifest</button>'
      +'<button onclick="downloadAuth(\'/api/integrations/share/download?artifact=config&platform=claude&readonly=true\',\'kb-mcp-config-preview.json\')" class="bg-emerald-600 text-white px-3 py-1 rounded text-sm">下载只读配置模板</button>'
      +'<button onclick="downloadAuth(\'/api/integrations/share/download?artifact=scan\',\'kb-share-sensitive-scan.json\')" class="bg-violet-600 text-white px-3 py-1 rounded text-sm">下载扫描报告</button>'
      +'<button disabled class="bg-gray-200 text-gray-500 px-3 py-1 rounded text-sm cursor-not-allowed">zip 导出：第二阶段</button>'
      +'</div><p class="text-xs text-gray-500 mt-2">下载请求仅使用 Authorization Header，不把 token 放入 URL。</p></div>'
      +'<div class="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">'
      +'<div class="bg-gray-50 border border-gray-200 rounded-lg p-3"><h4 class="font-semibold text-gray-800 mb-2">Manifest 预览</h4><pre class="text-xs overflow-x-auto bg-white p-3 rounded max-h-96">'+escapeHtml(JSON.stringify(manifest,null,2))+'</pre></div>'
      +'<div class="bg-gray-50 border border-gray-200 rounded-lg p-3"><h4 class="font-semibold text-gray-800 mb-2">只读 MCP 配置预览</h4><pre class="text-xs overflow-x-auto bg-white p-3 rounded max-h-96">'+escapeHtml(JSON.stringify(config,null,2))+'</pre></div>'
      +'</div>'
      +'<div class="bg-gray-50 border border-gray-200 rounded-lg p-3 mt-3"><h4 class="font-semibold text-gray-800 mb-2">敏感扫描结果</h4><div class="overflow-x-auto"><table class="w-full text-xs"><thead><tr class="text-left text-gray-500"><th class="px-2 py-1">目标</th><th class="px-2 py-1">级别</th><th class="px-2 py-1">类型</th><th class="px-2 py-1">说明</th></tr></thead><tbody>'+findings+'</tbody></table></div></div>'
      +'<div class="bg-gray-50 border border-gray-200 rounded-lg p-3 mt-3"><h4 class="font-semibold text-gray-800 mb-2">暂不纳入第一阶段</h4><div class="flex flex-wrap gap-2 text-xs"><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">完整 skills 导出</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">SSE 分享配置</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">私有数据库</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">日志</span><span class="bg-gray-100 text-gray-700 px-2 py-1 rounded">settings 原文</span></div></div>';
  }).catch(function(){list.innerHTML='<p class="text-red-500 text-xs">加载失败</p>'});
}
// ─── T10：MCP 用量（原仪表盘「MCP 使用统计」卡片并入商城）───
// 字段与口径与原卡片一致：今日调用 / 成功 / 累计 + 最近一次调用。
function lmarketMcp(){
  var host=document.getElementById('tab-market');
  if(!host)return;
  var box=document.getElementById('marketMcp');
  if(!box){
    box=document.createElement('div');
    box.id='marketMcp';
    box.className='card p-4';
    box.innerHTML='<h4 class="font-semibold text-gray-800 mb-2">MCP 用量</h4>'
      +'<p class="text-xs text-gray-500 mb-3">数据来自本机用量库 .kb_mcp_usage.jsonl，统计口径：今日调用 / 成功 / 累计。</p>'
      +'<div id="marketMcpBody"><span class="text-gray-400 text-xs">加载中...</span></div>';
    var wrap=document.getElementById('marketBlocks')||host;
    wrap.appendChild(box);
  }
  var body=document.getElementById('marketMcpBody');
  if(!body)return;
  ap('/api/mcp-usage').then(function(d){
    if(d&&d.error){body.innerHTML='<p class="text-red-500 text-xs">'+escapeHtml(d.error)+'</p>';return}
    var last=(d&&d.last)||{},t=last.time||'';
    if(t)t=t.slice(5,16);
    body.innerHTML='<div class="grid grid-cols-3 gap-3">'
      +'<div class="text-center"><div class="text-xl font-bold text-violet-600">'+esc(d.today)+'</div><div class="text-gray-500 text-xs">今日调用</div></div>'
      +'<div class="text-center"><div class="text-xl font-bold text-emerald-600">'+esc(d.ok)+'</div><div class="text-gray-500 text-xs">成功</div></div>'
      +'<div class="text-center"><div class="text-xl font-bold text-gray-600">'+esc(d.total)+'</div><div class="text-gray-500 text-xs">累计</div></div>'
      +'</div>'
      +'<div class="text-xs text-gray-400 mt-2">最近: '+esc(t)+(last.tool?' | '+esc(last.tool):'')+'</div>';
  }).catch(function(){body.innerHTML='<p class="text-red-500 text-xs">MCP 用量加载失败</p>'});
}
function checkVersion(){fetch(A+'/api/version').then(r=>r.json()).then(d=>{var b=document.getElementById('verBadge');b.textContent='v'+d.server+(d.skill_installed?' 📦已安装':' ⚠缺技能');b.title='服务端: v'+d.server+' | 技能: v'+d.skill+' (兼容:'+d.compatible+') | 点击检查更新';b.onclick=()=>{b.textContent='✅ v'+d.server+' 最新'}}).catch(()=>{document.getElementById('verBadge').textContent='v?'})}
document.addEventListener('DOMContentLoaded',()=>{A=location.origin;const t=localStorage.getItem('kb_token');if(t){T=t;dl(true)}else{try{const req=JSON.parse(localStorage.getItem('kb_auth_request')||'null');if(req&&req.request_id&&req.claim&&Date.now()-new Date(req.created_at).getTime()<20*60*1000){sl('res');document.getElementById('lrc').innerHTML='<p id="rqStatus" class="text-amber-500 text-sm text-center">⏳ 恢复申请查询中…</p>';startRequestPolling(req.request_id,req.claim)}}catch(e){}}initDarkMode();if('serviceWorker' in navigator){navigator.serviceWorker.register('/web/sw.js').then(()=>console.log('[SW] Registered')).catch(()=>{})}})

// ─── 暗色模式 (v5.5) ───
function initDarkMode(){
  var saved=localStorage.getItem('kb_dark');
  if(saved==='1'||(!saved&&window.matchMedia('(prefers-color-scheme:dark)').matches)){
    document.documentElement.classList.add('dark');
    document.getElementById('dmt').textContent='☀️';
  }
  window.matchMedia('(prefers-color-scheme:dark)').addEventListener('change',e=>{
    if(!localStorage.getItem('kb_dark')){
      document.documentElement.classList.toggle('dark',e.matches);
      document.getElementById('dmt').textContent=e.matches?'☀️':'🌙';
    }
  });
}
// ─── v5.6 批量操作 M6 ───
var batchSet=new Set();
function bcToggle(p){if(batchSet.has(p))batchSet.delete(p);else batchSet.add(p);bu();var chk=document.querySelector('input[data-bp="'+CSS.escape(p)+'"]');if(chk)chk.checked=batchSet.has(p)}
function bu(){var cnt=batchSet.size;document.getElementById('batchCount').textContent=cnt?'已选 '+cnt+' 篇':'';document.getElementById('batchBar').classList.toggle('show',cnt>0)}
function bs(){batchSet.clear();bu();document.querySelectorAll('.batch-check').forEach(c=>c.checked=false)}
function bt(action){
  if(!batchSet.size)return;
  if(action=='tag'){var tags=prompt('输入标签（逗号分隔）:');if(!tags)return;tags=tags.split(/[,，、]/).map(t=>t.trim()).filter(Boolean)}
  if(action=='archive'&&!confirm('确认归档 '+batchSet.size+' 篇笔记？'))return;
  var paths=Array.from(batchSet);
  var body={action:action,paths:paths};
  if(tags)body.tags=tags;
  ap('/api/notes/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{
    var ok=d.results.filter(r=>r.action).length;
    toast.show(action+(tags?'标签':'归档')+': '+ok+'/'+d.total,'success');
    bs();lt()
  }).catch(()=>toast.show('批量操作失败','error'))
}
// ─── v5.6 版本历史 M5 ───
function nh(p){
  document.getElementById('histOvl').classList.remove('hidden');document.getElementById('histMdl').classList.remove('hidden');
  document.getElementById('histContent').innerHTML='<p class="text-gray-500">加载中...</p>';
  ap('/api/notes/'+encodeURIComponent(p)+'/history').then(d=>{
    var h='';
    if(d.error){h='<p class="text-red-500">'+esc(d.error)+'</p>'}
    else if(!d.commits.length){h='<p class="text-gray-500">无版本历史</p>'}
    else{
      h='<div class="text-xs text-gray-500 mb-2">'+esc(d.path)+' · '+esc(d.count)+' 个版本</div>';
      d.commits.forEach((c,i)=>{
        h+='<div class="flex items-center gap-3 py-2 border-b border-gray-100 text-sm"><span class="text-gray-400 text-xs w-28">'+esc(c.time.slice(0,16))+'</span><span class="flex-1 text-gray-700 truncate">'+esc(c.message)+'</span>'
        if(i<d.commits.length-1)h+='<button onclick="nhi(\''+jss(d.path)+'\',\''+jss(d.commits[i+1].hash)+'\',\''+jss(c.hash)+'\')" class="text-xs text-blue-500 hover:underline">diff</button>'
        h+='</div>'
      })
    }
    document.getElementById('histContent').innerHTML=h
  }).catch(()=>{document.getElementById('histContent').innerHTML='<p class="text-red-500">加载失败</p>'})
}
function hcl(){document.getElementById('histOvl').classList.add('hidden');document.getElementById('histMdl').classList.add('hidden')}
function nhi(p,h1,h2){
  document.getElementById('histContent').innerHTML='<p class="text-gray-500">加载 diff...</p>';
  ap('/api/notes/'+encodeURIComponent(p)+'/diff?h1='+h1.slice(0,8)+'&h2='+h2.slice(0,8)).then(d=>{
    var h='<button onclick="nh(\''+jss(p)+'\')" class="text-xs text-blue-500 hover:underline mb-3">← 返回历史</button>';
    h+='<div class="text-xs text-gray-500 mb-2">'+esc(d.from)+' → '+esc(d.to)+'</div>';
    h+='<pre class="history-diff">'+esc(d.diff||'(空)')+'</pre>';
    document.getElementById('histContent').innerHTML=h
  }).catch(()=>{document.getElementById('histContent').innerHTML='<p class="text-red-500">Diff 失败</p>'})
}
// ─── v5.6 P2: API 文档 ───
function lad(){
  var ep=document.getElementById('adBody');
  if(ep.innerHTML)return;
  ep.innerHTML=[['认证','POST','/auth/request','Any','设备申请授权'],
  ['','POST','/auth/approve/{id}','Master','批准设备'],
  ['','POST','/auth/reject/{id}','Master','拒绝设备'],
  ['','GET','/auth/sessions','Master','活跃会话列表'],
  ['','DELETE','/auth/revoke/{h}','Master','强制撤销'],
  ['数据','GET','/dashboard','Any','仪表盘统计'],
  ['','GET','/notes','Any','笔记列表 (?type=tag=&offset=&limit=)'],
  ['','POST','/notes','RW','创建笔记'],
  ['','GET','/notes/{path}','Any','读取笔记'],
  ['','DELETE','/notes/{path}','RW','归档笔记'],
  ['','GET','/notes/{path}/related','Any','相关内容推荐'],
  ['','POST','/api/notes/batch','RW','批量操作(标签/归档)'],
  ['搜索','GET','/search?q=&tag=','Any','关键词搜索'],
  ['','GET','/api/search/semantic?q=&limit=','Any','混合搜索(BGE-M3+Reranker)'],
  ['','GET','/api/tags','Any','标签云统计'],
  ['AI','POST','/api/llm/ask','Any','知识问答(RAG)'],
  ['','POST','/api/llm/ask/stream','Any','流式问答(SSE)'],
  ['','POST','/api/llm/tags','RW','智能标签推荐'],
  ['','POST','/api/llm/summarize','RW','智能摘要'],
  ['','POST','/api/llm/quality','RW','质量评估(0-100)'],
  ['','POST','/api/smart-upload','RW','全自动智能上传'],
  ['文件','POST','/upload','RW','上传文件'],
  ['','GET','/files/{path}','Any','下载文件'],
  ['管理','GET/POST','/admin/settings','Master','系统设置'],
  ['','GET','/admin/models','Master','探测模型'],
  ['','GET','/admin/audit','Master','审计日志(分页)'],
  ['','GET','/admin/audit/stats','Master','日志统计'],
  ['','POST','/admin/scan-new','Master','自动扫描整理'],
  ['版本','GET','/health','Any','健康检查'],
  ['','GET','/api/version','Any','版本信息'],
  ['','GET','/api/graph','Any','知识图谱数据'],
  ['','POST','/api/index/rebuild','RW','重建搜索索引'],
  ['','GET','/api/index/status','Any','索引状态']]
  .reduce((acc,r,i,arr)=>{
    var cat=r[0],m=r[1],url=r[2],auth=r[3],desc=r[4];
    if(cat){if(i>0)acc+='</tbody></table></div>';acc+='<div class="mb-4"><h4 class="text-sm font-semibold text-gray-600 mb-2">'+cat+'</h4><table class="w-full text-sm"><thead><tr class="bg-gray-50 text-gray-500 text-xs"><th class="p-2 text-left">方法</th><th class="p-2 text-left">端点</th><th class="p-2 text-left">认证</th><th class="p-2 text-left">说明</th></tr></thead><tbody>'}
    var mc=m=='GET'?'emerald':m=='POST'?'blue':m=='DELETE'?'red':'gray';
    acc+='<tr class="border-t border-gray-100 hover:bg-gray-50"><td class="p-2"><span class="text-xs bg-'+mc+'-50 text-'+mc+'-600 px-2 py-0.5 rounded font-medium">'+m+'</span></td><td class="p-2"><code class="text-xs text-gray-700">'+url+'</code></td><td class="p-2 text-xs text-gray-500">'+auth+'</td><td class="p-2 text-xs text-gray-600">'+desc+'</td></tr>';
    if(i===arr.length-1)acc+='</tbody></table></div>';
    return acc
  },'');
}
function toggleDark(){
  var isDark=document.documentElement.classList.toggle('dark');
  document.getElementById('dmt').textContent=isDark?'☀️':'🌙';
  localStorage.setItem('kb_dark',isDark?'1':'0');
}
// ─── 移动端：目录/TOC 切换 ───
function toggleMobileDir(){var p=document.getElementById('noteDirPanel'),o=document.getElementById('mobileOverlay');if(p.classList.contains('mobile-show')){p.classList.remove('mobile-show');o.classList.remove('show')}else{closeMobilePanels();p.classList.add('mobile-show');o.classList.add('show');if(!_ntItems){var box=document.getElementById('nt');if(box&&!box.innerHTML.trim())box.innerHTML='<p class="text-gray-400 text-xs py-4 text-center">加载中...</p>';lt()}}}
// 第 20 项：抽屉能弹出但一屏空白。唯一根因在 CSS——
//   手机媒体查询里 .toc-panel{display:none!important} 本意藏桌面右栏，
//   但抽屉内层 <div class="toc-panel" id="tocContent"> 的 class 同名，被一起隐藏了。
//   修法是在 index.html 加 #tocPanel.mobile-show #tocContent{display:block!important} 放行。
//
//   曾经误判过第二个根因（"#tocPanel 带 hidden，优先级压过 mobile-show"）：不成立。
//   .hidden 特异性 0,1,0 且无 !important，#tocPanel.mobile-show 是 1,1,0 带 !important，
//   前者压不过后者，打开抽屉时 .hidden 本来就不生效。
//   所以本函数保持原样，不要在这里增删 hidden。
function toggleMobileToc(){var p=document.getElementById('tocPanel'),o=document.getElementById('mobileOverlay');if(p.classList.contains('mobile-show')){p.classList.remove('mobile-show');o.classList.remove('show')}else{closeMobilePanels();p.classList.add('mobile-show');o.classList.add('show')}}
function closeMobilePanels(){document.querySelectorAll('.note-dir-panel,#tocPanel').forEach(function(e){e.classList.remove('mobile-show')});document.getElementById('mobileOverlay').classList.remove('show')}

// ─── 移动端下拉刷新（仅触屏 + 页面在顶部时生效） ───
(function(){
  var startY=0,pulling=false,dist=0,TH=64,el=null;
  function ind(){if(!el)el=document.getElementById('ptr');return el}
  function appVisible(){var a=document.getElementById('app');return a&&!a.classList.contains('hidden')}
  function topMost(){
    var m=document.querySelector('#app main');
    var a=document.scrollingElement?document.scrollingElement.scrollTop:0;
    var b=m?m.scrollTop:0;
    return Math.max(a||0,b||0);
  }
  function curTab(){var t=document.querySelector('#app main > div[id^="tab-"]:not(.hidden)');return t?t.id.replace('tab-',''):'dash'}
  // 第 12 项：图谱页整屏都是 canvas，手指往下拖是想平移图谱，
  // 不是想刷新页面。这里直接跳过，避免「划一下图谱变成刷新」。
  function onGraphTab(){return curTab()==='graph'}
  function refresh(){
    var n=curTab();
    if(n==='dash')ld();else if(n==='notes')lt();else if(n==='graph'&&typeof lg==='function')lg();
    else if(n==='auth')lau();else if(n==='log')ll(1);else if(n==='set')ls();
    else if(n==='skills')lsk();else if(n==='apidoc')lad();else if(n==='upload')loadUploadHistory();
    toast.show('已刷新','success',1500);
  }
  document.addEventListener('touchstart',function(e){
    if(!appVisible()||e.touches.length!==1)return;
    if(onGraphTab())return;
    if(topMost()>0)return;
    startY=e.touches[0].clientY;dist=0;pulling=true;
  },{passive:true});
  document.addEventListener('touchmove',function(e){
    if(!pulling)return;
    dist=e.touches[0].clientY-startY;
    if(dist<=0)return;
    var p=ind();if(!p)return;
    p.classList.add('on');
    var t=document.getElementById('ptrTxt');
    if(t)t.textContent=dist>=TH?'松手刷新':'下拉刷新';
    var ic=p.querySelector('.ptr-ico');
    if(ic)ic.style.transform='rotate('+(dist>=TH?180:0)+'deg)';
  },{passive:true});
  document.addEventListener('touchend',function(){
    if(!pulling)return;
    pulling=false;
    var p=ind();if(p)p.classList.remove('on');
    if(dist>=TH)refresh();
    dist=0;
  },{passive:true});
})();
