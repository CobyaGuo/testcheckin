// checkin-web Service Worker：让单文件应用可安装 + 离线可用
// 策略：
//  · 壳（HTML）：导航请求网络优先，但响应必须「健康」（收到且 2xx）才采用；
//    连不上（断网/拒绝连接）或返回失败响应（服务挂了 → 404/502/空体）一律兜底缓存里的 index.html
//    → 服务在不在、网在不在，壳都能打开，任何情况下不白屏。
//  · 静态资源（同域 GET）：缓存优先，命中即返；未命中试网络，网络也失败时再查缓存，
//    最终兜底空响应（504），不让页面请求悬挂/白屏。
//  · 数据层（IndexedDB/SQLite）本来就在本地，不归 SW 管。
// 版本号由构建脚本注入（checkin-web-20261107-015305-024 占位 = 构建时间戳精确到毫秒，每次构建必不同）；
// 图标文件名由构建注入版本号（["./icon.20261107-015305-024.svg","./icon-192.20261107-015305-024.png","./icon-512.20261107-015305-024.png","./maskable-512.20261107-015305-024.png"] 占位），不同构建不同名，不做同名覆盖。
// 旧缓存于 activate 阶段自动清理（按当前缓存名动态比对，不写死旧名）+ clients.claim 立即接管。
// 更新策略（2026-11-04 改为全自动）：新 SW 装好缓存后立即 skipWaiting 激活 + claim 接管，
// activate 清完旧缓存后广播 SW_UPDATED；页面侧据此弹「点击立即生效」（reload 属用户主动，
// 不会打断使用中的页面）。不点也不丢：导航网络优先（no-cache），下次打开必然拿到新版 HTML。
// 旧设计「等用户确认才 skipWaiting」已废弃——提示一闪而过 / 用户不点 → 新 SW 永远 waiting、
// 旧缓存永不清理，表现为「部署了新版本 PWA 却永远不更新」。
const CACHE = 'checkin-web-20261107-015305-024'
// 构建期注入：版本化图标文件名数组（JSON），随构建变化 → 每次发版预缓存的都是新文件
const ICON_FILES = ["./icon.20261107-015305-024.svg","./icon-192.20261107-015305-024.png","./icon-512.20261107-015305-024.png","./maskable-512.20261107-015305-024.png"]
const CORE_ASSETS = ['./', './index.html', './manifest.webmanifest']
const ASSETS = CORE_ASSETS.concat(ICON_FILES)

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE)
    // 核心壳（导航、HTML、manifest）必须成功，否则离线无兜底 —— 失败则安装失败（可接受，说明服务本身不可达）。
    // 预缓存同样 no-cache：HTTP/CDN 缓存的旧 HTML 不许进新版本缓存（否则离线兜底的是旧壳）。
    await cache.addAll(CORE_ASSETS.map((u) => new Request(u, { cache: 'no-cache' })))
    // 图标逐个缓存：单个失败（404/MIME）不再致命，避免 addAll 原子失败导致整个 SW 永不激活 → 离线直接白屏。
    await Promise.allSettled(ICON_FILES.map((f) => cache.add(f)))
    // 新版本缓存就绪 → 立即跳过 waiting 激活（自动更新）；activate 清旧缓存后广播 SW_UPDATED。
    await self.skipWaiting()
  })())
})

// 兼容保留：页面仍可主动发 SKIP_WAITING（skipWaiting 幂等，对已激活 SW 无副作用）
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting()
})

// 通知点击（2026-11-04 通知渠道改造）：聚焦已打开的应用，没有则打开首页。
// 通知本身由页面侧（utils/webNotify.js）经 showNotification / new Notification 发出，
// SW 不持有业务数据（SQLite 加密 + 主密钥不落 SW 可达范围），只负责点击路由。
self.addEventListener('notificationclick', (e) => {
  e.notification.close()
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const c of cs) {
      if ('focus' in c) { try { return await c.focus() } catch (_) { /* 聚焦失败落 openWindow */ } }
    }
    return self.clients.openWindow('./')
  })())
})

// periodicSync 周期对表（仅在浏览器授予 periodic-background-sync 权限时触发；
// 页面侧 registerPeriodicSync 注册）。SW 无数据访问权，只转发消息给打开的页面，
// 由页面计算「错过的提醒」补偿通知；无存活页面时本轮跳过（下次打开页面补偿兜底）。
self.addEventListener('periodicsync', (e) => {
  if (e.tag !== 'reminder-sync') return
  e.waitUntil((async () => {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    cs.forEach((c) => { try { c.postMessage({ type: 'REMINDER_SYNC' }) } catch (_) {} })
  })())
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({ includeUncontrolled: true }))
      .then((cs) => cs.forEach((c) => c.postMessage({ type: 'SW_UPDATED', version: CACHE })))
  )
})

// 导航兜底：优先缓存的壳（index.html），都没有时给一段最小可渲染 HTML，
// 保证「任何情况下不白屏」的最终防线（正常路径永远到不了这里）。
async function shellFallback() {
  const cache = await caches.open(CACHE)
  return (await cache.match('./index.html'))
    || (await cache.match('./'))
    || new Response(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>C打卡</title><p style="font-family:system-ui;padding:2rem">正在同步资源，请联网后刷新重试。</p>',
      { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    )
}

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return

  // 导航（页面打开）：网络优先 + 响应健康检查。
  // 关键修复：fetch 收到失败响应（服务挂了 → 404/502/空体）是「正常 resolve」而非 reject，
  // 旧实现只 catch 网络异常 → 失败响应直接上屏 = 白屏。现在只有 2xx 才采用。
  // cache:'no-cache'：必须绕过 HTTP 缓存拿最新 HTML（2026-11-04）——默认 cache 模式会命中
  // 浏览器/CDN（EdgeOne 等静态托管）缓存的旧 index.html，部署新版后页面永远停在旧版。
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const resp = await fetch(req, { cache: 'no-cache' })
        if (resp && resp.ok) return resp
      } catch (_) { /* 断网/拒绝连接 → 落兜底 */ }
      return shellFallback()
    })())
    return
  }

  // 其余同域资源：缓存优先，未命中则网络并写回缓存；网络失败/失败响应时再查缓存，
  // 最终兜底空响应（不白屏壳，单文件构建下资源几乎全部内联，此路径极少走到）
  if (new URL(req.url).origin === self.location.origin) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE)
      const cached = await cache.match(req)
      if (cached) return cached
      try {
        const resp = await fetch(req)
        if (resp && resp.ok) {
          cache.put(req, resp.clone())
          return resp
        }
      } catch (_) { /* 落兜底 */ }
      return (await cache.match(req))
        || new Response('', { status: 504, statusText: 'offline' })
    })())
  }
})
