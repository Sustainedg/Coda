/* Coda 移动端 Service Worker。
 *
 * 目标：把 /m/ 这条链接变成可安装、可离线的应用外壳（app shell）。
 * 只接管本作用域（/m/）下的页面，且**永不缓存 /api/**：对话走 SSE 长连接，
 * 必须实时直连后端，缓存会导致消息延迟或错乱。
 */
/* 缓存名带版本号：改了外壳（入口 HTML / 样式表 / 构建产物）就把它 +1 —— 老客户端下次
 * 打开会装上新版 SW，activate 时清掉旧缓存；用户也可以在设置页「更新到最新版本」自助清。 */
const SHELL_CACHE = 'coda-m-shell-v139'
const ASSET_CACHE = 'coda-m-assets-v138'
const ENTRY = '/m/'
const STATIC_SHELL = ['/m/manifest.webmanifest', '/icons/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png']

self.addEventListener('install', (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, ASSET_CACHE])
      for (const key of await caches.keys()) {
        if (!keep.has(key)) await caches.delete(key)
      }
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return

  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return

  if (request.mode === 'navigate') {
    event.respondWith(networkFirstNavigation())
    return
  }
  if (isStaticAsset(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request))
  }
})

/** 安装期预缓存外壳：入口 HTML + 它引用的构建产物（文件名带哈希，无法静态列举）。 */
async function precache() {
  try {
    const cache = await caches.open(SHELL_CACHE)
    const entry = await fetch(ENTRY, { cache: 'reload' })
    const html = await entry.clone().text()
    await cache.put(ENTRY, entry)

    const assets = new Set(STATIC_SHELL)
    for (const match of html.matchAll(/(?:src|href)="(\/[^"]+)"/g)) {
      const path = match[1]
      if (path.startsWith('/assets/') || path.startsWith('/icons/')) assets.add(path)
    }
    await Promise.all([...assets].map((path) => cache.add(path).catch(() => undefined)))
  } catch {
    /* 首次安装时网络不可用：保留空缓存，等联网后再由运行时缓存补齐 */
  }
}

function isStaticAsset(pathname) {
  return (
    pathname.startsWith('/assets/') ||
    pathname.startsWith('/icons/') ||
    // 内置头像包：选中的头像要能在断网时也画出来（走 stale-while-revalidate 边用边更新）
    pathname.startsWith('/avatars/') ||
    pathname === '/m/manifest.webmanifest' ||
    pathname === '/m/sw.js'
  )
}

/** 离线时回落到缓存的外壳，保证任何 /m/ 深链都能打开应用。 */
async function networkFirstNavigation() {
  try {
    const fresh = await fetch(ENTRY, { cache: 'no-cache' })
    if (fresh.ok) {
      const cache = await caches.open(SHELL_CACHE)
      await cache.put(ENTRY, fresh.clone())
    }
    return fresh
  } catch {
    const cache = await caches.open(SHELL_CACHE)
    const cached = await cache.match(ENTRY)
    if (cached) return cached
    return new Response('离线：请连上网络后再打开 Coda。', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    })
  }
}

/** 构建产物不可变（文件名带内容哈希）：先用缓存，后台再校正。 */
async function staleWhileRevalidate(request) {
  const cache = await caches.open(ASSET_CACHE)
  const cached = await cache.match(request)
  const network = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone())
      return response
    })
    .catch(() => cached ?? Response.error())
  return cached ?? network
}
