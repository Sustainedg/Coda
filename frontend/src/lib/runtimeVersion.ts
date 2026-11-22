import type { RuntimeLatest } from './types'

/**
 * 内核版本的小工具（桌面端与移动端的内核卡片共用）。
 *
 * 「检查更新」是每个内核一次 `npm view` 的远端查询，慢且可能失败；
 * 卡片不能等着它才给结论 —— 没有「查到更新」就默认按已是最新显示。
 */

/** 从版本字符串里抽出可比较的核心版本号（`2.1.316 (Claude Code)` → `2.1.316`）。 */
export function versionCore(value: string): string {
  return value.match(/\d+\.\d+(?:\.\d+)?/)?.[0] ?? value.trim()
}

/** 把核心版本号拆成数字段（`2.185.0` → `[2, 185, 0]`）；不是纯数字版本时返回 null。 */
function versionSegments(value: string): number[] | null {
  const core = versionCore(value)
  return /^\d+(?:\.\d+)+$/.test(core) ? core.split('.').map(Number) : null
}

/** 远端版本是否**确实比**本机新：逐段比数字（`2.10` > `2.9`）；解析不了时退化为「不相等」。 */
export function isNewerVersion(remote: string, current: string): boolean {
  const remoteSegments = versionSegments(remote)
  const currentSegments = versionSegments(current)
  if (!remoteSegments || !currentSegments) {
    return versionCore(remote) !== versionCore(current)
  }
  const length = Math.max(remoteSegments.length, currentSegments.length)
  for (let i = 0; i < length; i++) {
    const a = remoteSegments[i] ?? 0
    const b = currentSegments[i] ?? 0
    if (a !== b) return a > b
  }
  return false
}

/**
 * 查到的「更新目标」：远端版本确实比本机新才返回版本号；
 * 没查到 / 查询失败 / 已经是最新时返回 null（卡片按「已是最新」默认显示）。
 */
export function updateTargetOf(
  currentVersion: string | null | undefined,
  latest?: RuntimeLatest | null,
): string | null {
  if (!currentVersion || !latest?.latest) return null
  const current = versionCore(currentVersion)
  const remote = versionCore(latest.latest)
  return isNewerVersion(remote, current) ? remote : null
}
