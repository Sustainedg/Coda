#!/usr/bin/env node
/**
 * 内核图标加载耗时测量（headless Chromium + CDP，零依赖，不装任何包）。
 *
 * 用法：
 *   node scripts/measure_kernel_icons.mjs <baseUrl> [次数]
 *   # 例：node scripts/measure_kernel_icons.mjs http://127.0.0.1:8010 10
 *
 * 需要环境里有 `chromium`（Debian/Ubuntu：`apt-get install chromium`）。
 * 前端要**先构建**（图标是构建期内嵌进包的）：`cd frontend && npm run build`。
 *
 * 测的是什么：反复打开「设置」页 → 点进「内核管理」标签页，量「页面可见 →
 * 全部图标显示完成」的耗时。口径与页内埋点（`lib/kernelIcons.ts` 的
 * `window.__kernelLogoPerf`）一致，两边数字应该同量级。
 *
 * 顺带统计图标走了多少次网络请求 —— 内嵌之后应该是 0。
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const baseUrl = process.argv[2] ?? 'http://127.0.0.1:8010'
const runs = Number(process.argv[3] ?? 10)
// 两轮之间等多久（毫秒）。管理员的「检查更新」会往后端发 `npm view` 远端查询
// （每个内核一次，各约 2 秒），上一轮没跑完会挤到下一轮的内核列表接口上。
// 默认等 3 秒让它们跑完 —— 这样量到的是「图标这条链路」本身的耗时。
// 想看「连着点十次」的最坏情况（上一轮的查询还堵着），用 CODA_MEASURE_SETTLE=0。
const settle = Number(process.env.CODA_MEASURE_SETTLE ?? 3000)
const PORT = 9222 + Math.floor(Math.random() * 500)

const profile = mkdtempSync(join(tmpdir(), 'coda-measure-'))
const chrome = spawn(
  'chromium',
  [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    '--no-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--window-size=1440,900',
    'about:blank',
  ],
  { stdio: ['ignore', 'pipe', 'pipe'] },
)
chrome.stderr.on('data', () => {})

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitForDevTools() {
  for (let i = 0; i < 150; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (res.ok) return
    } catch {
      /* 还没起来 */
    }
    await sleep(100)
  }
  throw new Error('Chromium DevTools 没起来（chromium 装了吗？）')
}

/** 极简 CDP 客户端：只用到 Runtime / Network / Page 三类命令。 */
class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.handlers = []
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.method) {
        for (const h of this.handlers) h(msg)
        return
      }
      const entry = this.pending.get(msg.id)
      if (!entry) return
      this.pending.delete(msg.id)
      if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)))
      else entry.resolve(msg.result)
    })
  }

  on(fn) {
    this.handlers.push(fn)
  }

  send(method, params = {}) {
    const id = ++this.id
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`${method} 超时`))
      }, 60000)
    })
  }
}

async function connect() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
  const page = list.find((t) => t.type === 'page')
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true })
    ws.addEventListener('error', reject, { once: true })
  })
  return new Cdp(ws)
}

/** P95（最近秩法，与验收口径一致）。 */
function p95(values) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)]
}

/**
 * 页面里跑的计时脚本：点击标签页 → 等图标元素建好 → 等图标资源取完
 * （内嵌方案没有外部资源，这一步直接过）→ 再画完两帧 → 报耗时。
 *
 * 计时全在页面里、用同一个时钟，改动前后代码路径完全一致，公平对比。
 */
const MEASURE = `(async () => {
  const tab = [...document.querySelectorAll('button')].find((el) => el.textContent.trim() === '内核管理');
  if (!tab) return JSON.stringify({ error: 'no-tab' });
  const t0 = Date.now();
  const p0 = performance.now();
  tab.click();
  for (let i = 0; i < 600; i++) {
    if (document.querySelectorAll('.kernel-logo').length > 0) break;
    await new Promise((r) => setTimeout(r, 10));
  }
  const icons = () => performance.getEntriesByType('resource').filter((e) => e.name.includes('/icons/kernels/'));
  for (let i = 0; i < 600; i++) {
    const list = icons();
    if (list.length > 0 && list.every((e) => e.responseEnd > 0)) break;
    if (list.length === 0 && document.querySelectorAll('.kernel-logo').length > 0) {
      await new Promise((r) => setTimeout(r, 30));
      if (icons().length === 0) break;
    }
    await new Promise((r) => setTimeout(r, 10));
  }
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const sample = (window.__kernelLogoPerf && window.__kernelLogoPerf.samples || []).slice(-1)[0] || null;
  return JSON.stringify({
    t0,
    doneAt: Date.now(),
    ms: performance.now() - p0,
    logos: document.querySelectorAll('.kernel-logo').length,
    requests: icons().length,
    sample,
  });
})()`

async function main() {
  await waitForDevTools()
  const cdp = await connect()
  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Network.enable')

  const results = []
  let iconRequests = 0
  /** 本轮图标请求：requestId → { url, offset }（offset 把单调时钟换算成 epoch 秒） */
  const iconReqs = new Map()
  /** 本轮最后一个图标资源完成的 epoch 毫秒 */
  let iconReadyAt = 0
  let pendingIcons = 0

  cdp.on((msg) => {
    if (msg.method === 'Network.requestWillBeSent') {
      if (msg.params.request.url.includes('/icons/kernels/')) {
        iconRequests++
        pendingIcons++
        iconReqs.set(msg.params.requestId, {
          offset: msg.params.wallTime - msg.params.timestamp,
        })
      }
      return
    }
    if (msg.method !== 'Network.loadingFinished' && msg.method !== 'Network.loadingFailed') return
    const info = iconReqs.get(msg.params.requestId)
    if (!info) return
    iconReqs.delete(msg.params.requestId)
    pendingIcons--
    // `loadingFinished` 只有单调时钟，用请求事件里的 wallTime/timestamp 差值换算成 epoch。
    iconReadyAt = Math.max(iconReadyAt, (msg.params.timestamp + info.offset) * 1000)
  })

  for (let i = 0; i < runs; i++) {
    // 每次都从「设置」页重新点进「内核管理」标签页：与真实用户路径一致。
    await cdp.send('Page.navigate', { url: `${baseUrl}/settings` })
    await sleep(1500)

    // 页面可能还没渲染出标签栏：等它出现（最多 10 秒）
    let ready = false
    for (let k = 0; k < 100 && !ready; k++) {
      const res = await cdp.send('Runtime.evaluate', {
        expression: `!![...document.querySelectorAll('button')].find((el) => el.textContent.trim() === '内核管理')`,
      })
      ready = res.result.value === true
      if (!ready) await sleep(100)
    }
    if (!ready) {
      console.log(`#${i + 1} 失败：等不到「内核管理」标签页`)
      continue
    }

    iconReadyAt = 0
    pendingIcons = 0
    const res = await cdp.send('Runtime.evaluate', {
      expression: MEASURE,
      awaitPromise: true,
    })
    const parsed = JSON.parse(res.result.value || '{}')
    if (parsed.error) {
      console.log(`#${i + 1} 失败：${parsed.error}`)
      continue
    }

    // 图标资源还在路上就等它（内嵌方案没有请求，直接跳过）。
    let waited = 0
    while (pendingIcons > 0 && waited < 5000) {
      await sleep(20)
      waited += 20
    }
    // 资源到齐后再画两帧，确认图标真的上屏。
    const painted = await cdp.send('Runtime.evaluate', {
      expression: `(async () => {
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        return Date.now();
      })()`,
      awaitPromise: true,
    })
    // 结束时刻取「图标资源全到齐」与「画完两帧」里较晚的那个。
    const doneAt = Math.max(painted.result.value ?? 0, iconReadyAt, parsed.doneAt ?? 0)

    const entry = {
      ms: doneAt - parsed.t0,
      logos: parsed.logos,
      requests: iconRequests,
      sample: parsed.sample,
    }
    results.push(entry)
    console.log(
      `#${i + 1} 耗时=${entry.ms.toFixed(1)}ms  图标元素=${entry.logos}  图标请求=${parsed.requests}` +
        (entry.sample ? `  页内埋点=${entry.sample.durationMs.toFixed(1)}ms(接口 ${entry.sample.apiMs.toFixed(1)})` : ''),
    )
    // 管理员的「检查更新」会往后端发 `npm view` 远端查询（每个内核一次），
    // 上一轮没跑完会挤到下一轮的内核列表接口上，把接口耗时搅浑。
    // 等它们跑完再测下一轮 —— 真人也不会一秒连点十次。
    if (settle > 0) await sleep(settle)
  }

  console.log('\n--- 汇总 ---')
  console.log(`图标网络请求总数：${iconRequests}（内嵌后应为 0）`)
  if (results.length) {
    const ms = results.map((r) => r.ms)
    console.log(`每次耗时：${ms.map((d) => d.toFixed(1)).join(', ')}`)
    console.log(`  max=${Math.max(...ms).toFixed(1)}ms  p95=${p95(ms).toFixed(1)}ms`)
    const samples = results.map((r) => r.sample).filter(Boolean)
    if (samples.length) {
      const d = samples.map((s) => s.durationMs)
      console.log(`页内埋点（页面可见→图标显示）：${d.map((x) => x.toFixed(1)).join(', ')}`)
      console.log(`  max=${Math.max(...d).toFixed(1)}ms  p95=${p95(d).toFixed(1)}ms`)
    }
  }
  chrome.kill()
  try {
    rmSync(profile, { recursive: true, force: true })
  } catch {
    /* 临时目录清理失败不影响结论 */
  }
}

main().catch((e) => {
  console.error(e)
  chrome.kill()
  process.exit(1)
})
