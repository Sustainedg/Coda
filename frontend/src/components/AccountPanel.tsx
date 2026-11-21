import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { saveSession, saveUser } from '../lib/auth'
import { AvatarUploader } from './AvatarUploader'
import { useAuth } from './AuthGate'

/**
 * 个人资料与口令。
 *
 * 两端共用：桌面端在「设置 → 账号」，移动端是独立的「账号」页。鉴权关闭的部署里
 * 当前用户是虚拟管理员，这里只显示说明，不提供改口令 / 退出。
 */
export function AccountPanel() {
  const { user, authEnabled, logout, refreshUser } = useAuth()
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [profileBusy, setProfileBusy] = useState(false)
  const [profileMessage, setProfileMessage] = useState<string | null>(null)
  const [profileError, setProfileError] = useState<string | null>(null)

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [passwordBusy, setPasswordBusy] = useState(false)
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null)
  const [passwordError, setPasswordError] = useState<string | null>(null)

  // 个人访问令牌（PAT）：给 MCP 服务器等程序化调用用的长效凭据。
  const [patStatus, setPatStatus] = useState<{ has_pat: boolean; issued_at: string | null } | null>(
    null,
  )
  const [patBusy, setPatBusy] = useState(false)
  const [patNew, setPatNew] = useState<string | null>(null) // 刚生成的明文，只显示这一次
  const [patCopied, setPatCopied] = useState(false)
  const [patMessage, setPatMessage] = useState<string | null>(null)
  const [patError, setPatError] = useState<string | null>(null)

  useEffect(() => {
    if (!authEnabled || !user || user.id === '__system__') return
    api
      .patStatus()
      .then(setPatStatus)
      .catch(() => setPatStatus(null))
  }, [authEnabled, user?.id])

  useEffect(() => {
    setDisplayName(user?.display_name ?? '')
    setEmail(user?.email ?? '')
  }, [user?.display_name, user?.email])

  if (!authEnabled || !user || user.id === '__system__') {
    return (
      <section className="card account-card">
        <div className="card-title">单用户模式</div>
        <p className="dim small">
          当前部署关闭了鉴权（<span className="mono-sm">CODA_AUTH_ENABLED=false</span>），
          所有请求都按管理员处理，没有需要维护的账号。
        </p>
      </section>
    )
  }

  const saveProfile = async (event: React.FormEvent) => {
    event.preventDefault()
    setProfileBusy(true)
    setProfileError(null)
    setProfileMessage(null)
    try {
      const next = await api.updateMe({ display_name: displayName.trim(), email: email.trim() })
      saveUser(next)
      refreshUser(next)
      setProfileMessage('已保存')
    } catch (e) {
      setProfileError(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setProfileBusy(false)
    }
  }

  const changePassword = async (event: React.FormEvent) => {
    event.preventDefault()
    setPasswordBusy(true)
    setPasswordError(null)
    setPasswordMessage(null)
    try {
      const result = await api.changePassword({ old_password: oldPassword, new_password: newPassword })
      // 后端会递增 token_version 让旧令牌失效；这里保存它新签发的令牌，避免下一次请求被登出。
      saveSession(result.access_token, user)
      setOldPassword('')
      setNewPassword('')
      setPasswordMessage('口令已更新，下次登录请用新口令')
    } catch (e) {
      setPasswordError(e instanceof ApiError ? e.message : '修改失败')
    } finally {
      setPasswordBusy(false)
    }
  }

  const generatePat = async () => {
    if (
      patStatus?.has_pat &&
      !window.confirm('重新生成会立即吊销旧令牌，正在使用它的 MCP 服务器会开始收到 401。继续吗？')
    )
      return
    setPatBusy(true)
    setPatError(null)
    setPatMessage(null)
    try {
      const result = await api.issuePat()
      setPatNew(result.pat)
      setPatCopied(false)
      setPatStatus({ has_pat: true, issued_at: result.issued_at })
    } catch (e) {
      setPatError(e instanceof ApiError ? e.message : '生成失败')
    } finally {
      setPatBusy(false)
    }
  }

  const revokePat = async () => {
    if (!window.confirm('确定吊销个人访问令牌吗？正在使用它的 MCP 服务器会立即失去访问权限。'))
      return
    setPatBusy(true)
    setPatError(null)
    setPatMessage(null)
    try {
      await api.revokePat()
      setPatNew(null)
      setPatStatus({ has_pat: false, issued_at: null })
      setPatMessage('令牌已吊销')
    } catch (e) {
      setPatError(e instanceof ApiError ? e.message : '吊销失败')
    } finally {
      setPatBusy(false)
    }
  }

  const copyPat = async () => {
    if (!patNew) return
    try {
      await navigator.clipboard.writeText(patNew)
      setPatCopied(true)
    } catch {
      // 剪贴板不可用时（非安全上下文 / 旧浏览器）提示手动复制
      setPatCopied(false)
    }
  }

  return (
    <>
      <section className="card account-card">
        <div className="card-head">
          <div>
            <h2 className="card-title">
              {user.display_name || user.username}
              <span className={`pill ${user.role === 'admin' ? 'ok' : ''}`} style={{ marginLeft: 8 }}>
                {user.role === 'admin' ? '管理员' : '成员'}
              </span>
            </h2>
            <div className="card-sub">
              登录名 <span className="mono-sm">{user.username}</span>
            </div>
          </div>
          <button className="btn sm" onClick={logout}>
            退出登录
          </button>
        </div>

        <AvatarUploader user={user} />

        <form className="stack" onSubmit={saveProfile}>
          <div className="grid two">
            <label className="field">
              <span className="field-label">显示名</span>
              <input
                className="input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={120}
                placeholder={user.username}
              />
            </label>
            <label className="field">
              <span className="field-label">邮箱（可选）</span>
              <input
                className="input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                maxLength={200}
              />
            </label>
          </div>
          {profileError && <div className="banner err">{profileError}</div>}
          {profileMessage && <div className="banner ok">{profileMessage}</div>}
          <div className="row">
            <button className="btn primary" type="submit" disabled={profileBusy}>
              {profileBusy ? '保存中…' : '保存资料'}
            </button>
          </div>
        </form>
      </section>

      <section className="card account-card">
        <h2 className="card-title">个人访问令牌</h2>
        <p className="dim small">
          长效凭据，供 MCP 服务器等脚本以你的身份调用平台接口（请求头带
          <span className="mono-sm"> Authorization: Bearer pat_…</span>）。与登录令牌互不影响，
          修改口令时会被一并吊销。
          重新生成或吊销后，平台内置的「系统管理」MCP 会自动换一份新令牌，不用手动改配置。
        </p>
        {patNew ? (
          <div className="stack">
            <div className="banner ok">令牌已生成 —— 只显示这一次，请立即复制保存：</div>
            <code className="mono-sm pat-value">{patNew}</code>
            <div className="row">
              <button className="btn sm" onClick={copyPat}>
                {patCopied ? '已复制 ✓' : '复制令牌'}
              </button>
              <button
                className="btn sm"
                onClick={() => {
                  setPatNew(null)
                  setPatMessage('已收起。若忘了保存，请重新生成。')
                }}
              >
                我已保存
              </button>
            </div>
          </div>
        ) : (
          <div className="stack">
            {patStatus?.has_pat ? (
              <p className="dim small">
                当前有一份有效令牌（生成于{' '}
                {patStatus.issued_at ? new Date(patStatus.issued_at).toLocaleString() : '未知时间'}）。
                平台只存哈希，令牌本体无法再次查看。
              </p>
            ) : (
              <p className="dim small">还没有生成过令牌。</p>
            )}
            {patError && <div className="banner err">{patError}</div>}
            {patMessage && <div className="banner ok">{patMessage}</div>}
            <div className="row">
              <button className="btn primary" onClick={generatePat} disabled={patBusy}>
                {patBusy ? '生成中…' : patStatus?.has_pat ? '重新生成' : '生成令牌'}
              </button>
              {patStatus?.has_pat && (
                <button className="btn" onClick={revokePat} disabled={patBusy}>
                  吊销
                </button>
              )}
            </div>
          </div>
        )}
      </section>

      <section className="card account-card">
        <h2 className="card-title">修改口令</h2>
        <form className="stack" onSubmit={changePassword}>
          <div className="grid two">
            <label className="field">
              <span className="field-label">当前口令</span>
              <input
                className="input"
                type="password"
                autoComplete="current-password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                required
              />
            </label>
            <label className="field">
              <span className="field-label">新口令</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                minLength={8}
                required
              />
              <span className="field-help">至少 8 个字符。</span>
            </label>
          </div>
          {passwordError && <div className="banner err">{passwordError}</div>}
          {passwordMessage && <div className="banner ok">{passwordMessage}</div>}
          <div className="row">
            <button className="btn" type="submit" disabled={passwordBusy}>
              {passwordBusy ? '更新中…' : '更新口令'}
            </button>
          </div>
        </form>
      </section>
    </>
  )
}
