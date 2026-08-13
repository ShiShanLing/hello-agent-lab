import { useState } from 'react'
import type { FormEvent } from 'react'
import projectIcon from './assets/project-icon-v1-optimized.png'

export type AuthUser = {
  id: string
  email: string
  display_name: string
  role: 'knowledge_manager' | 'member'
  is_active: boolean
  can_access_admin: boolean
}

type AuthScreenProps = {
  apiBaseUrl: string
  onAuthenticated: (user: AuthUser) => void
}

export function AuthScreen({ apiBaseUrl, onAuthenticated }: AuthScreenProps) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (isSubmitting) return
    setIsSubmitting(true)
    setError(null)
    try {
      const response = await fetch(`${apiBaseUrl}/auth/${mode}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          password,
          ...(mode === 'register' ? { display_name: displayName } : {}),
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '登录失败')
      onAuthenticated(data as AuthUser)
    } catch (requestError) {
      setError(
        requestError instanceof TypeError
          ? '无法连接后端，请确认 FastAPI 服务正在 8000 端口运行。'
          : requestError instanceof Error
            ? requestError.message
            : '登录失败',
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  const switchMode = () => {
    setMode((current) => (current === 'login' ? 'register' : 'login'))
    setError(null)
  }

  return (
    <main className="auth-shell">
      <section className="auth-card">
        <div className="auth-brand">
          <img alt="" aria-hidden="true" className="brand-mark" src={projectIcon} />
          <div>
            <span>HELLO AGENT</span>
            <h1>{mode === 'login' ? '欢迎回来' : '创建你的账号'}</h1>
          </div>
        </div>
        <p className="auth-intro">
          登录后，你的 Agent 对话和计划 Todo 将安全地归属于同一个账号。
        </p>

        <form className="auth-form" onSubmit={submit}>
          {mode === 'register' && (
            <label>
              <span>昵称</span>
              <input
                autoComplete="name"
                maxLength={50}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="例如：小明"
                required
                value={displayName}
              />
            </label>
          )}
          <label>
            <span>邮箱</span>
            <input
              autoComplete="email"
              maxLength={254}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@example.com"
              required
              type="email"
              value={email}
            />
          </label>
          <label>
            <span>密码</span>
            <input
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              minLength={8}
              maxLength={128}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 个字符"
              required
              type="password"
              value={password}
            />
          </label>

          {error && <div className="auth-error" role="alert">{error}</div>}
          <button className="auth-submit" disabled={isSubmitting} type="submit">
            {isSubmitting
              ? '请稍候…'
              : mode === 'login'
                ? '登录'
                : '注册并登录'}
          </button>
        </form>

        <button className="auth-switch" onClick={switchMode} type="button">
          {mode === 'login' ? '还没有账号？立即注册' : '已经有账号？返回登录'}
        </button>
      </section>
    </main>
  )
}
