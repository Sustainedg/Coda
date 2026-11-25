/**
 * 智能体头像的统一取值逻辑（两端共用）。
 *
 * 头像**只来自头像包**，显示优先级从高到低：
 *
 * 1. `avatar_file` —— 自定义上传（`/api/agents/{id}/avatar`，需带令牌取 blob）；
 * 2. `avatar_preset` —— 内置头像包（`/avatars/pack-NNN.webp`，随前端构建发布的静态图）；
 * 3. 兜底 —— 两者都没有时，按智能体 id 稳定地选一张头像包图片（`fallbackAvatarPreset`）。
 *
 * 老的 `avatar` emoji 字段**已下线**，只在兜底挑选时当种子用，界面永不渲染 emoji。
 * 头像包清单由 `scripts/gen_avatar_pack.py` 生成（`lib/avatarPack.ts`），图片与清单
 * 必须同步，所以随机生成只会抽到真实存在的图。
 */
import { useEffect, useState } from 'react'
import { getAccessToken } from './auth'
import { AVATAR_GROUPS, AVATAR_PRESETS } from './avatarPack'

const BASE = import.meta.env.VITE_API_BASE ?? ''

/** 是不是内置头像包编号（`pack-001` ~）；后端按同一规则校验（`app/avatars.py`）。 */
export function isAvatarPreset(value: string | null | undefined): value is string {
  return /^pack-\d{3}$/.test(value ?? '')
}

/**
 * 头像包内容版本：头像包整体重处理时 +1（白底 → 透明 v2；修复浅色人物被误抠 → v3；
 * 追加卡通分类 → v4）。地址带它当查询串，浏览器缓存随之整体失效 —— 否则文件名没变，
 * 页面会一直用旧的白底缓存。
 */
const AVATAR_PACK_REV = '4'

/** 内置头像包图片地址（public 目录，随构建发布，无需鉴权）。 */
export function avatarPresetUrl(preset: string): string {
  return `${BASE}/avatars/${preset}.webp?v=${AVATAR_PACK_REV}`
}

/** 随机抽一个内置头像包编号；排除当前正在用的那张，让「随机生成」次次都有新面孔。 */
export function randomAvatarPreset(exclude?: string | null): string {
  const pool = AVATAR_PRESETS.filter((item) => item !== exclude)
  const source = pool.length > 0 ? pool : [...AVATAR_PRESETS]
  return source[Math.floor(Math.random() * source.length)]
}

/** emoji 头像已全面下线：没有任何头像来源时，界面统一退到这张头像包兜底。 */
export const DEFAULT_AVATAR_PRESET: string = AVATAR_PRESETS[0]

/**
 * 没有明确头像时的兜底头像包：按种子（智能体 id）稳定地挑一张。
 *
 * 「稳定」很关键 —— 同一个智能体每次渲染都必须是同一张脸，不能刷新一次换一个。
 * 老智能体只存了 emoji（`avatar`）没有 `avatar_preset`，用 id 兜底就能直接长出
 * 一张固定的头像包人物图，不必改库。
 */
export function fallbackAvatarPreset(seed?: string | null): string {
  const text = (seed ?? '').trim()
  if (!text) return DEFAULT_AVATAR_PRESET
  let hash = 0
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0
  }
  return AVATAR_PRESETS[hash % AVATAR_PRESETS.length]
}

/** 内置头像包的编号列表（画廊挑选用）。 */
export const ALL_AVATAR_PRESETS: readonly string[] = AVATAR_PRESETS

/**
 * 内置头像包按标签分组（人物 / 卡通……，顺序由生成脚本的 `CATEGORIES` 决定）。
 * 画廊照这个结构铺开：同一类的放一起，别让人物和卡通混成一锅粥。
 */
export const AVATAR_PRESET_GROUPS: readonly { key: string; label: string; presets: string[] }[] =
  AVATAR_GROUPS

/* ------------------------------------------------------------------ */
/* 自定义上传头像（avatar_file）：带令牌取 blob，与用户头像同一条路（见 lib/avatar.ts） */

/** 已取到的智能体头像 object URL：`<id>:<avatar_file>` → URL。 */
const cache = new Map<string, string>()
/** 正在取的请求：同一张头像被多个组件同时要时只发一次。 */
const inflight = new Map<string, Promise<string | null>>()

function cacheKey(agentId: string, file: string): string {
  return `${agentId}:${file}`
}

async function loadAgentAvatar(agentId: string, file: string): Promise<string | null> {
  const key = cacheKey(agentId, file)
  const cached = cache.get(key)
  if (cached) return cached
  const pending = inflight.get(key)
  if (pending) return pending

  const task = (async () => {
    const token = getAccessToken()
    const response = await fetch(
      `${BASE}/api/agents/${encodeURIComponent(agentId)}/avatar?v=${encodeURIComponent(file)}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    )
    if (!response.ok) return null
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    // 换头像后旧标识的图不会再被用到：顺手回收，别让缓存无限长大。
    const prefix = `${agentId}:`
    for (const [staleKey, staleUrl] of cache) {
      if (staleKey.startsWith(prefix) && staleKey !== key) {
        URL.revokeObjectURL(staleUrl)
        cache.delete(staleKey)
      }
    }
    cache.set(key, url)
    return url
  })()

  inflight.set(key, task)
  try {
    return await task
  } finally {
    inflight.delete(key)
  }
}

/**
 * 智能体自定义头像的 blob 地址；没有自定义图 / 取图失败时为 `null`（调用方回退）。
 *
 * 头像接口要鉴权，`<img src>` 带不了 Authorization 头，所以这里先 fetch 成 blob
 * 再渲染 —— 与用户头像（`lib/avatar.ts`）同一个原因。
 */
export function useAgentAvatarFileUrl(
  agentId: string | null | undefined,
  file: string | null | undefined,
): string | null {
  const key = agentId && file ? cacheKey(agentId, file) : ''
  const [url, setUrl] = useState<string | null>(() => (key ? cache.get(key) ?? null : null))

  useEffect(() => {
    if (!agentId || !file) {
      setUrl(null)
      return
    }
    let alive = true
    void loadAgentAvatar(agentId, file).then((next) => {
      if (alive) setUrl(next)
    })
    return () => {
      alive = false
    }
  }, [agentId, file])

  return url
}
