// 知识库 Service Worker（版本号见 kb_lib/config.py，缓存名固定不随版本改）
// 静态资源缓存 + API 离线降级

const CACHE = "kb-static";

// 入口文件（index.html / app.js / sw.js）不带内容哈希，升级后必须走网络优先，
// 否则浏览器会一直用缓存里的旧版；后端对 /web/ 已明确下发 no-store，
// 缓存优先与后端策略冲突。
const ENTRY = ["/web/", "/web/index.html", "/web/app.js", "/web/ui.js", "/web/sw.js"];
// vendor 下的第三方库按固定版本引入，可以缓存优先。
const VENDOR = [
  "/web/manifest.json",
  "/web/vendor/tailwind.build.css",
  "/web/vendor/d3.v7.min.js",
  "/web/vendor/marked.min.js"
];

// 安装：预缓存（逐个 add，单个 404 不会让整批失败）
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      Promise.all([...ENTRY, ...VENDOR].map(u => c.add(u).catch(() => {})))
    )
  );
  self.skipWaiting();
});

// 激活：清理旧版本缓存
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isEntry(pathname) {
  return pathname === "/web/" ||
         pathname === "/web/index.html" ||
         pathname === "/web/app.js" ||
         pathname === "/web/ui.js" ||
         pathname === "/web/sw.js" ||
         // 主题插件：themes/ 下每个 CSS 都是一套主题，按入口处理走网络优先，
         // 否则改完主题样式要等缓存过期才生效
         pathname.startsWith("/web/themes/");
}

// 请求拦截：入口网络优先 + vendor 缓存优先 + API 离线降级
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);

  const isAPI = url.pathname.startsWith("/api/") ||
                url.pathname.startsWith("/auth/") ||
                url.pathname === "/health" ||
                url.pathname === "/notes" ||
                url.pathname === "/dashboard" ||
                url.pathname === "/upload" ||
                url.pathname === "/search";

  if (isAPI) {
    // API：网络优先；离线时返回带 offline 标记的 503，前端据此提示
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({
          error: "离线模式 — 请联网后重试",
          offline: true
        }), {
          status: 503,
          headers: { "Content-Type": "application/json" }
        })
      )
    );
    return;
  }

  if (url.pathname.startsWith("/web/")) {
    if (isEntry(url.pathname)) {
      // 入口文件：网络优先，失败才退回缓存
      e.respondWith(
        fetch(e.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone)).catch(() => {});
          }
          return response;
        }).catch(() => caches.match(e.request).then(c => c || Response.error()))
      );
    } else {
      // vendor 等：缓存优先 + 后台更新
      e.respondWith(
        caches.match(e.request).then(cached => {
          const fetched = fetch(e.request).then(response => {
            if (response.ok) {
              const clone = response.clone();
              caches.open(CACHE).then(c => c.put(e.request, clone)).catch(() => {});
            }
            return response;
          }).catch(() => cached);
          return cached || fetched;
        })
      );
    }
  }
});
