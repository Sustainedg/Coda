/**
 * 浏览器本地下载：`Blob` → `<a download>` 只有这一份实现。
 *
 * 对话导出（图片 / Markdown）都走这里；不经过后端，也不引第三方依赖。
 */

/** 触发一次本地下载。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 立刻 revoke 在部分浏览器上会让下载拿不到内容，挪到下一轮事件循环再回收。
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/** 下载纯文本（Markdown 导出等）。 */
export function downloadText(
  text: string,
  filename: string,
  mime = 'text/markdown;charset=utf-8',
): void {
  downloadBlob(new Blob([text], { type: mime }), filename)
}
