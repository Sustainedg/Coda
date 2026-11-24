/**
 * 双向登录的输入框（OAuth 授权码这类），桌面端与移动端共用这一份。
 *
 * 登录进程通过 `provision.input` 声明「需要用户把浏览器里的授权码贴回来」；
 * 提交后由父组件写回登录进程，输入框随之收起。登录结果是异步的，这里不假装成功。
 */
import { useState } from 'react'

export function LoginInput({ prompt, onSubmit }: { prompt: string; onSubmit: (text: string) => void }) {
  const [text, setText] = useState('')
  const submit = () => {
    const value = text.trim()
    if (!value) return
    onSubmit(value)
    setText('')
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="banner info" style={{ whiteSpace: 'pre-line' }}>{prompt}</div>
      <div className="row" style={{ gap: 8 }}>
        <input
          className="input mono"
          style={{ flex: 1 }}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') submit()
          }}
          placeholder="粘贴浏览器页面上显示的授权码"
          autoFocus
        />
        <button className="btn sm primary" onClick={submit} disabled={!text.trim()}>
          提交
        </button>
      </div>
    </div>
  )
}
