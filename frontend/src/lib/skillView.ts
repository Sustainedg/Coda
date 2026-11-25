/**
 * 技能列表的视图偏好：平铺（默认）或按内核分组。
 *
 * 技能中心化之后，「按内核分组」不再是技能的自然形态（同一实体的多内核行
 * 会分散在各组里），所以默认平铺；分组保留给「分配时同内核核对 / 排错定位」
 * 的场景。两端（桌面 / 移动）共用这一份实现与 localStorage 键（`coda.skillView`），
 * 切换偏好两端同步记忆。
 */
export type SkillViewMode = 'flat' | 'grouped'

const SKILL_VIEW_STORAGE_KEY = 'coda.skillView'

/** 读出视图偏好；没设置过或值不认识时回落「平铺」。 */
export function skillViewMode(): SkillViewMode {
  try {
    const raw = localStorage.getItem(SKILL_VIEW_STORAGE_KEY)
    return raw === 'grouped' ? 'grouped' : 'flat'
  } catch {
    return 'flat'
  }
}

/** 切换偏好并记忆；返回切换后的值。 */
export function toggleSkillViewMode(current: SkillViewMode): SkillViewMode {
  const next: SkillViewMode = current === 'flat' ? 'grouped' : 'flat'
  try {
    localStorage.setItem(SKILL_VIEW_STORAGE_KEY, next)
  } catch {
    // 无痕模式等场景写不进去：偏好不记忆，但本轮切换照常生效。
  }
  return next
}
