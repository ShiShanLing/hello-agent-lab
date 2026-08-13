import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { FormEvent } from 'react'
import QRCode from 'qrcode'
import projectIcon from './assets/project-icon-v1-optimized.png'
import './App.css'

type Role = 'knowledge_manager' | 'member'

type CurrentUser = {
  id: string
  email: string
  display_name: string
  role: 'admin'
  mfa_rebind_required: boolean
  can_manage_knowledge: boolean
}

type ManagedUser = {
  id: string
  email: string
  display_name: string
  role: Role
  is_active: boolean
  created_at: string
  last_login_at: string | null
}

type ManagedAdminUser = {
  id: string
  email: string
  display_name: string
  is_active: boolean
  can_manage_knowledge: boolean
  created_at: string
  last_login_at: string | null
}

type Overview = {
  users: {
    total: number
    active: number
    roles: Partial<Record<Role, number>>
    active_sessions: number
  }
  runs_24h: {
    total: number
    success: number
    success_rate: number
    total_tokens: number
    average_duration_ms: number
    estimated_cost: number
  }
  knowledge: { documents: number; chunks: number; size_bytes: number }
  evaluations_7d: { runs: number; average_score: number }
  activity_7d: Array<{ date: string; runs: number }>
  generated_at: string
  privacy_notice: string
}

type AuditUser = Pick<CurrentUser, 'id' | 'email' | 'display_name'>
type AuditLog = {
  id: number
  action: string
  actor: AuditUser | null
  target: AuditUser | null
  changes: {
    before?: { role?: Role; is_active?: boolean; can_manage_knowledge?: boolean }
    after?: { role?: Role; is_active?: boolean; can_manage_knowledge?: boolean }
  }
  ip_address: string | null
  created_at: string
}

type SecurityStatus = {
  authenticator_enabled: boolean
  email_otp_available: boolean
  email_verified: boolean
  email_recovery_enabled: boolean
  masked_email: string
  email: string
  recovery_email: string | null
  recovery_codes_remaining: number
  active_sessions: number
  idle_timeout_minutes: number
  absolute_timeout_hours: number
}

type KnowledgeDocument = {
  id: string
  original_name: string
  category: string
  file_type: string
  size_bytes: number
  status: 'ready' | 'processing' | 'failed'
  chunk_count: number
  error_message: string | null
  created_at: string
  updated_at: string
}

type KnowledgePreview = {
  document: KnowledgeDocument
  content: string
  truncated: boolean
}

type KnowledgeSearchMatch = {
  source: string
  chunk: number
  content: string
  score: number
}

type CostPeriod = 'day' | 'week' | 'month'

type PlatformCost = {
  period: CostPeriod
  generated_at: string
  privacy_notice: string
  pricing: {
    currency: string
    source: string
    usd_to_cny: number | null
    note: string
  }
  labels: { current: string; previous: string; compare: string }
  current: {
    key: string
    cost_usd: number
    cost_cny: number | null
    tokens: number
    runs: number
    evaluation_runs: number
    active_users: number
  }
  previous: {
    key: string
    cost_usd: number
    cost_cny: number | null
    tokens: number
    runs: number
    evaluation_runs: number
    active_users: number
    cost_delta_pct: number | null
  }
  lookback: {
    buckets: number
    cost_usd: number
    cost_cny: number | null
    tokens: number
    runs: number
    active_users: number
  }
  series: Array<{
    key: string
    label: string
    cost_usd: number
    tokens: number
    runs: number
    evaluation_runs: number
    active_users: number
  }>
  by_user: Array<{
    user_id: string
    email: string
    display_name: string
    runs: number
    evaluation_runs: number
    tokens: number
    cost_usd: number
  }>
  by_model: Array<{ model: string; runs: number; tokens: number; cost_usd: number }>
}

type ReleaseRecord = {
  id: string
  created_at: string
  status: 'success' | 'rolled_back' | 'failed' | 'dry_run' | 'running' | string
  targets: string[]
  git_sha?: string
  git_branch?: string
  operator?: string
  duration_seconds?: number
  error?: string | null
  dry_run?: boolean
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ??
  (import.meta.env.PROD ? '/agent/api' : '/api')

const KNOWLEDGE_CATEGORIES = [
  '未分类',
  '公司制度',
  '产品资料',
  '技术运维',
  '客户服务',
  '项目管理',
  '学习资料',
]

const ROLE_LABELS: Record<Role, string> = {
  knowledge_manager: '知识库管理员',
  member: '普通成员',
}

async function readResponse(response: Response) {
  const text = await response.text()
  try {
    return text ? JSON.parse(text) : null
  } catch {
    throw new Error(`服务器返回了无法识别的内容（HTTP ${response.status}）`)
  }
}

function apiError(data: unknown, fallback: string) {
  if (!data || typeof data !== 'object') return fallback
  const detail = (data as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== 'object') return ''
        const issue = item as { type?: string; loc?: unknown[]; msg?: string; ctx?: { min_length?: number } }
        const field = issue.loc?.at(-1)
        if (issue.type === 'string_too_short' && field === 'new_password') {
          return `新后台密码至少需要 ${issue.ctx?.min_length ?? 12} 个字符。`
        }
        if (issue.type === 'string_too_short' && field === 'password') {
          return '请输入后台密码。'
        }
        return issue.msg ?? ''
      })
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  return fallback
}

async function copyText(text: string) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 继续使用兼容 HTTP 的复制方式。
    }
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  const copied = document.execCommand('copy')
  textarea.remove()
  return copied
}

function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function formatDate(value: string | null) {
  if (!value) return '从未登录'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function formatUsd(value: number) {
  if (value === 0) return '$0'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function formatMoney(usd: number, cny: number | null) {
  return cny == null ? formatUsd(usd) : `${formatUsd(usd)} · ≈¥${cny.toFixed(3)}`
}

function formatDelta(value: number | null) {
  if (value == null) return '暂无对比'
  if (value === 0) return '持平'
  return `${value > 0 ? '+' : ''}${value}%`
}

function Login({ onLogin }: { onLogin: (user: CurrentUser) => void }) {
  const [stage, setStage] = useState<'password' | 'mfa' | 'email' | 'setup' | 'recovery'>('password')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [code, setCode] = useState('')
  const [challengeToken, setChallengeToken] = useState('')
  const [setupSecret, setSetupSecret] = useState('')
  const [qrCode, setQrCode] = useState('')
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([])
  const [activatedUser, setActivatedUser] = useState<CurrentUser | null>(null)
  const [emailRecoveryAvailable, setEmailRecoveryAvailable] = useState(false)
  const [maskedEmail, setMaskedEmail] = useState('')
  const [recoveryMessage, setRecoveryMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const submitPassword = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '登录失败'))
      setChallengeToken(data.challenge_token)
      setEmailRecoveryAvailable(Boolean(data.email_recovery_available))
      if (data.requires_setup) {
        const setupResponse = await fetch(`${API_BASE}/admin-auth/mfa/setup`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ challenge_token: data.challenge_token }),
        })
        const setupData = await readResponse(setupResponse)
        if (!setupResponse.ok) throw new Error(apiError(setupData, '生成绑定信息失败'))
        setSetupSecret(setupData.secret)
        setQrCode(await QRCode.toDataURL(setupData.otpauth_uri, { margin: 1, width: 190 }))
        setStage('setup')
      } else {
        setStage('mfa')
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  const verifyMfa = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/mfa/verify`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge_token: challengeToken, code }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '验证失败'))
      onLogin(data as CurrentUser)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '验证失败')
    } finally { setLoading(false) }
  }

  const sendEmailRecovery = async () => {
    setLoading(true); setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/email/login/send`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge_token: challengeToken }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '发送邮箱验证码失败'))
      setMaskedEmail(data.masked_email)
      setCode('')
      setStage('email')
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '发送邮箱验证码失败') }
    finally { setLoading(false) }
  }

  const verifyEmailRecovery = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/email/login/verify`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge_token: challengeToken, code }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '邮箱验证失败'))
      onLogin(data as CurrentUser)
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '邮箱验证失败') }
    finally { setLoading(false) }
  }

  const activateMfa = async (event: FormEvent) => {
    event.preventDefault()
    if (newPassword !== confirmPassword) { setError('两次输入的新密码不一致。'); return }
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/mfa/activate`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge_token: challengeToken, code, new_password: newPassword }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '启用双重认证失败'))
      setActivatedUser(data.user as CurrentUser)
      setRecoveryCodes(data.recovery_codes)
      setStage('recovery')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '启用双重认证失败')
    } finally { setLoading(false) }
  }

  const resetLogin = () => {
    setStage('password'); setPassword(''); setCode(''); setChallengeToken(''); setError(null)
  }

  return (
    <main className="login-page">
      <section className="login-card">
        <div className="login-brand"><img alt="石山岭科技项目图标" src={projectIcon} /><div><strong>石山岭科技</strong><small>智能体管理后台</small></div></div>
        <div className="login-heading">
          <span>安全管理控制台</span>
          <h1>{stage === 'password' ? '后台管理系统' : stage === 'mfa' ? '双重认证' : stage === 'email' ? '邮箱验证码登录' : stage === 'setup' ? '绑定身份验证器' : '保存恢复码'}</h1>
          <p>{stage === 'password' ? '使用独立管理员账号登录，普通智能体账号无法进入后台。' : stage === 'mfa' ? '可使用身份验证器动态码、恢复码，或切换到邮箱验证码完成二次验证。' : stage === 'email' ? `验证码已发送至 ${maskedEmail}，5 分钟内有效，60 秒内不能重复发送。` : stage === 'setup' ? '首次登录必须绑定身份验证器，并设置新的独立后台密码。' : '恢复码只显示一次，请保存到安全的位置。'}</p>
        </div>
        {stage === 'password' && <form onSubmit={submitPassword}>
          <label><span>管理员邮箱</span><input autoComplete="email" onChange={(event) => setEmail(event.target.value)} required type="email" value={email} /></label>
          <label><span>独立后台密码</span><input autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} required type="password" value={password} /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button disabled={loading} type="submit">{loading ? '正在验证…' : '继续验证'}</button>
        </form>}
        {stage === 'mfa' && <form onSubmit={verifyMfa}>
          <label><span>动态验证码或恢复码</span><input autoComplete="one-time-code" autoFocus onChange={(event) => setCode(event.target.value)} placeholder="6 位动态码" required value={code} /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button disabled={loading} type="submit">{loading ? '正在验证…' : '完成登录'}</button>
          <div className="other-auth-methods">
            <button aria-haspopup="menu" className="other-auth-trigger" type="button">其他验证方式 <span>⌄</span></button>
            <div className="other-auth-menu" role="menu">
              <button disabled={!emailRecoveryAvailable || loading} onClick={() => void sendEmailRecovery()} role="menuitem" type="button"><span>✉</span><div><strong>邮箱验证</strong><small>{emailRecoveryAvailable ? '向已验证的邮箱发送 6 位验证码' : '请先在安全设置中启用'}</small></div></button>
            </div>
          </div>
          <button className="login-secondary" onClick={resetLogin} type="button">返回账号密码</button>
        </form>}
        {stage === 'email' && <form onSubmit={verifyEmailRecovery}>
          <label><span>邮箱验证码</span><input autoComplete="one-time-code" autoFocus inputMode="numeric" maxLength={6} onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))} placeholder="6 位验证码" required value={code} /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button disabled={loading} type="submit">{loading ? '正在验证…' : '使用邮箱验证登录'}</button>
          <button className="login-secondary" disabled={loading} onClick={() => void sendEmailRecovery()} type="button">重新发送验证码</button>
          <button className="login-secondary" onClick={() => { setCode(''); setError(null); setStage('mfa') }} type="button">返回身份验证器验证</button>
        </form>}
        {stage === 'setup' && <form onSubmit={activateMfa}>
          <div className="mfa-setup"><img alt="身份验证器绑定二维码" src={qrCode} /><p>使用微软身份验证器、谷歌身份验证器或其他认证器扫描二维码。</p><details><summary>无法扫码？查看密钥</summary><code>{setupSecret}</code></details></div>
          <label><span>设置新的后台密码</span><input autoComplete="new-password" minLength={12} onChange={(event) => setNewPassword(event.target.value)} required type="password" value={newPassword} /></label>
          <label><span>确认新密码</span><input autoComplete="new-password" minLength={12} onChange={(event) => setConfirmPassword(event.target.value)} required type="password" value={confirmPassword} /></label>
          <label><span>认证器中的 6 位动态码</span><input autoComplete="one-time-code" inputMode="numeric" maxLength={6} onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))} required value={code} /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button disabled={loading} type="submit">{loading ? '正在启用…' : '启用双重认证'}</button>
        </form>}
        {stage === 'recovery' && <div className="recovery-box"><div>{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div><div className="recovery-actions"><button onClick={() => void copyText(recoveryCodes.join('\n')).then((copied) => setRecoveryMessage(copied ? '恢复码已复制。' : '浏览器禁止复制，请使用下载功能。'))} type="button">复制全部恢复码</button><button onClick={() => downloadText('agent-admin-recovery-codes.txt', recoveryCodes.join('\n'))} type="button">下载恢复码</button></div>{recoveryMessage && <p className={recoveryMessage.includes('已复制') ? 'success' : 'error'} role="status">{recoveryMessage}</p>}<button className="primary" onClick={() => activatedUser && onLogin(activatedUser)} type="button">我已安全保存，进入后台</button></div>}
      </section>
    </main>
  )
}

function App() {
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null)
  const [checking, setChecking] = useState(true)
  const [activePage, setActivePage] = useState<'dashboard' | 'cost' | 'releases' | 'users' | 'admins' | 'audit' | 'security' | 'knowledge'>('dashboard')
  const [costRefresh, setCostRefresh] = useState(0)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [adminUsers, setAdminUsers] = useState<ManagedAdminUser[]>([])
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([])
  const [releases, setReleases] = useState<ReleaseRecord[]>([])
  const [security, setSecurity] = useState<SecurityStatus | null>(null)
  const [knowledgeDocuments, setKnowledgeDocuments] = useState<KnowledgeDocument[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        const response = await fetch(`${API_BASE}/admin-auth/me`, { credentials: 'include' })
        if (response.ok) setCurrentUser(await readResponse(response))
      } finally {
        setChecking(false)
      }
    })()
  }, [])

  const loadPage = async (page = activePage) => {
    if (!currentUser || currentUser.role !== 'admin') return
    if (page === 'cost') {
      setLoading(false)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const path = page === 'dashboard'
        ? '/admin/overview'
        : page === 'users'
          ? '/admin/users'
          : page === 'admins'
            ? '/admin/admin-users'
          : page === 'audit'
            ? '/admin/audit-logs'
            : page === 'releases'
              ? '/admin/releases'
            : page === 'knowledge'
              ? '/admin/knowledge/documents'
              : '/admin-auth/security'
      const response = await fetch(`${API_BASE}${path}`, { credentials: 'include' })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '读取后台数据失败'))
      if (page === 'dashboard') setOverview(data as Overview)
      else if (page === 'users') setUsers((data as { users: ManagedUser[] }).users)
      else if (page === 'admins') setAdminUsers((data as { admins: ManagedAdminUser[] }).admins)
      else if (page === 'audit') setAuditLogs((data as { logs: AuditLog[] }).logs)
      else if (page === 'releases') setReleases((data as { releases: ReleaseRecord[] }).releases)
      else if (page === 'knowledge') setKnowledgeDocuments((data as { documents: KnowledgeDocument[] }).documents)
      else setSecurity(data as SecurityStatus)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取后台数据失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void loadPage() }, [activePage, currentUser?.id])

  const logout = async () => {
    await fetch(`${API_BASE}/admin-auth/logout`, { method: 'POST', credentials: 'include' })
    setCurrentUser(null)
  }

  const updateUser = async (user: ManagedUser, role: Role, isActive: boolean) => {
    const description = `${user.display_name}：${ROLE_LABELS[user.role]} → ${ROLE_LABELS[role]}，账号${isActive ? '启用' : '停用'}`
    if (!window.confirm(`确认修改权限？\n\n${description}\n\n修改后该用户的现有登录会话会失效。`)) return
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin/users/${user.id}`, {
        method: 'PATCH', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role, is_active: isActive }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '修改权限失败'))
      setUsers((current) => current.map((item) => item.id === user.id ? data : item))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '修改权限失败')
    }
  }

  const updateAdminUser = async (
    adminUser: ManagedAdminUser,
    canManageKnowledge: boolean,
    isActive: boolean,
  ) => {
    const description = `${adminUser.display_name}：知识库权限${canManageKnowledge ? '开启' : '关闭'}，后台账号${isActive ? '启用' : '停用'}`
    if (!window.confirm(`确认修改后台管理员权限？\n\n${description}\n\n修改后该账号当前后台会话会失效，需要重新登录。`)) return
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin/admin-users/${adminUser.id}`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ can_manage_knowledge: canManageKnowledge, is_active: isActive }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '修改后台管理员权限失败'))
      setAdminUsers((current) => current.map((item) => item.id === adminUser.id ? data : item))
      if (currentUser?.id === adminUser.id) setCurrentUser((current) => current ? { ...current, can_manage_knowledge: (data as ManagedAdminUser).can_manage_knowledge } : current)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '修改后台管理员权限失败')
    }
  }

  if (checking) return <main className="center-state">正在检查管理员身份…</main>
  if (!currentUser) return <Login onLogin={setCurrentUser} />
  return (
    <div className="admin-shell">
      <aside className="sidebar">
        <div className="sidebar-brand"><img alt="石山岭科技项目图标" src={projectIcon} /><div><strong>石山岭科技</strong><small>智能体管理后台</small></div></div>
        <nav>
          <button className={activePage === 'dashboard' ? 'active' : ''} onClick={() => setActivePage('dashboard')}><i><DashboardMenuIcon /></i><span>系统概览</span></button>
          <button className={activePage === 'cost' ? 'active' : ''} onClick={() => setActivePage('cost')}><i><CostMenuIcon /></i><span>总费用</span></button>
          <button className={activePage === 'users' ? 'active' : ''} onClick={() => setActivePage('users')}><i><UsersMenuIcon /></i><span>用户与权限</span></button>
          <button className={activePage === 'admins' ? 'active' : ''} onClick={() => setActivePage('admins')}><i><AdminMenuIcon /></i><span>后台管理员</span></button>
          <button className={activePage === 'releases' ? 'active' : ''} onClick={() => setActivePage('releases')}><i><ReleaseMenuIcon /></i><span>发布记录</span></button>
          <button className={activePage === 'audit' ? 'active' : ''} onClick={() => setActivePage('audit')}><i><AuditMenuIcon /></i><span>审计日志</span></button>
          <button className={activePage === 'security' ? 'active' : ''} onClick={() => setActivePage('security')}><i><SecurityMenuIcon /></i><span>安全设置</span></button>
        </nav>
        <div className="sidebar-privacy"><span>隐私边界</span><p>后台只读取聚合指标和账号权限，不读取任何用户业务内容。</p></div>
        <div className="sidebar-user"><img alt="" src={projectIcon} /><div><strong>{currentUser.display_name}</strong><small>系统管理员</small></div><button aria-label="退出登录" className="logout-button" data-tooltip="退出登录" onClick={() => void logout()} title="退出登录">↪</button></div>
      </aside>

      <main className="admin-main">
        <header className="topbar"><div><span>后台管理控制台</span><h1>{activePage === 'dashboard' ? '系统概览' : activePage === 'cost' ? '总费用' : activePage === 'releases' ? '发布记录' : activePage === 'users' ? '用户与权限' : activePage === 'admins' ? '后台管理员' : activePage === 'audit' ? '审计日志' : '安全设置'}</h1></div><div><button disabled={loading} onClick={() => { if (activePage === 'cost') setCostRefresh((tick) => tick + 1); else void loadPage() }}>↻ 刷新</button></div></header>
        {error && <div className="page-error" role="alert">{error}</div>}
        {loading && !overview && users.length === 0 && adminUsers.length === 0 && auditLogs.length === 0 && knowledgeDocuments.length === 0 ? <div className="page-loading">正在加载安全数据…</div> : null}

        {activePage === 'dashboard' && overview && <Dashboard overview={overview} />}
        {activePage === 'cost' && <CostPage onError={setError} refreshKey={costRefresh} />}
        {activePage === 'releases' && <ReleasesPage releases={releases} />}
        {activePage === 'users' && <UsersPage onUpdate={updateUser} users={users} />}
        {activePage === 'admins' && <AdminUsersPage currentAdminId={currentUser.id} onUpdate={updateAdminUser} admins={adminUsers} />}
        {activePage === 'audit' && <AuditPage logs={auditLogs} />}
        {activePage === 'security' && security && <SecurityPage onRefresh={() => void loadPage('security')} status={security} />}
      </main>
    </div>
  )
}

function MenuIconFrame({ children }: { children: ReactNode }) {
  return <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 18 18" width="18" xmlns="http://www.w3.org/2000/svg">{children}</svg>
}

function DashboardMenuIcon() {
  return <MenuIconFrame>
    <rect height="9" rx="1.8" stroke="currentColor" strokeWidth="1.7" width="11" x="3.5" y="4" />
    <path d="M6.5 10.2V7.7M9 10.2V6.5M11.5 10.2V8.6" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function ReleaseMenuIcon() {
  return <MenuIconFrame>
    <path d="M5 12.5 9 4.5l4 8" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" />
    <path d="M6.2 12.5h5.6M7.4 14.6h3.2" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function CostMenuIcon() {
  return <MenuIconFrame>
    <path d="M9 3.8v10.4M6.4 6.2c.7-1 2-1.6 3.4-1.6 2 0 3.2 1 3.2 2.4s-1.2 2.2-3.4 2.6c-2.1.4-3.4 1.2-3.4 2.6s1.3 2.5 3.5 2.5c1.5 0 2.8-.6 3.5-1.7" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function UsersMenuIcon() {
  return <MenuIconFrame>
    <circle cx="7" cy="6.5" r="2.1" stroke="currentColor" strokeWidth="1.7" />
    <path d="M3.9 12.8c.7-1.8 2.1-2.8 4.1-2.8s3.3 1 4 2.8" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
    <path d="M12.1 5.1a1.9 1.9 0 0 1 2 1.9 1.9 1.9 0 0 1-1.4 1.8M12.7 10.3c1.2.2 2.1 1 2.8 2.2" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function AdminMenuIcon() {
  return <MenuIconFrame>
    <rect height="8.8" rx="2" stroke="currentColor" strokeWidth="1.7" width="8.8" x="4.6" y="4.6" />
    <path d="M7 8.2h4M7 10.5h2.7" stroke="currentColor" strokeLinecap="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function AuditMenuIcon() {
  return <MenuIconFrame>
    <circle cx="9" cy="9" r="5.2" stroke="currentColor" strokeWidth="1.7" />
    <circle cx="9" cy="9" r="1.7" fill="currentColor" />
  </MenuIconFrame>
}

function SecurityMenuIcon() {
  return <MenuIconFrame>
    <path d="M9 3.6 13 5v3.1c0 2.4-1.4 4.5-4 5.9-2.6-1.4-4-3.5-4-5.9V5l4-1.4Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.7" />
    <path d="m7.2 9 1.2 1.2 2.5-2.5" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" />
  </MenuIconFrame>
}

function Dashboard({ overview }: { overview: Overview }) {
  const maxRuns = Math.max(1, ...overview.activity_7d.map((item) => item.runs))
  return <div className="dashboard-page">
    <section className="privacy-banner"><span>✓</span><div><strong>隐私安全统计</strong><p>{overview.privacy_notice}</p></div></section>
    <section className="metric-grid">
      <Metric label="注册用户" value={formatNumber(overview.users.total)} detail={`${overview.users.active} 个启用账号`} tone="purple" />
      <Metric label="24 小时调用" value={formatNumber(overview.runs_24h.total)} detail={`成功率 ${overview.runs_24h.success_rate}%`} tone="blue" />
      <Metric label="Token 消耗" value={formatNumber(overview.runs_24h.total_tokens)} detail={`平均 ${overview.runs_24h.average_duration_ms} ms`} tone="orange" />
      <Metric label="知识文档" value={formatNumber(overview.knowledge.documents)} detail={`${overview.knowledge.chunks} 个检索片段`} tone="green" />
    </section>
    <section className="dashboard-grid">
      <article className="panel activity-panel"><div className="panel-heading"><div><span>AGENT ACTIVITY</span><h2>最近 7 天调用趋势</h2></div><small>仅统计次数</small></div><div className="bar-chart">{overview.activity_7d.map((item) => <div className="bar-column" key={item.date}><span>{item.runs}</span><div style={{ height: `${Math.max(5, item.runs / maxRuns * 100)}%` }} /><small>{item.date.slice(5)}</small></div>)}</div></article>
      <article className="panel role-panel"><div className="panel-heading"><div><span>AGENT ACCESS</span><h2>Agent 用户角色</h2></div></div><div className="role-list"><RoleRow count={overview.users.roles.knowledge_manager ?? 0} label="知识库管理员" tone="blue" /><RoleRow count={overview.users.roles.member ?? 0} label="普通成员" tone="gray" /></div><div className="session-count"><span>Agent 有效登录会话</span><strong>{overview.users.active_sessions}</strong></div></article>
    </section>
    <section className="secondary-metrics"><div><span>7 天评测次数</span><strong>{overview.evaluations_7d.runs}</strong></div><div><span>平均评测分数</span><strong>{overview.evaluations_7d.average_score}%</strong></div><div><span>知识库存储</span><strong>{formatBytes(overview.knowledge.size_bytes)}</strong></div><div><span>24 小时费用估算</span><strong>{overview.runs_24h.estimated_cost || '未配置'}</strong></div></section>
  </div>
}

function Metric({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: string }) { return <article className={`metric-card ${tone}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article> }

const RELEASE_STATUS: Record<string, string> = {
  success: '成功',
  rolled_back: '已回滚',
  failed: '失败',
  dry_run: '演练',
  running: '进行中',
}

function ReleasesPage({ releases }: { releases: ReleaseRecord[] }) {
  return <section className="panel audit-page">
    <div className="audit-heading"><div><span>DEPLOY HISTORY</span><h2>自动发布记录</h2><p>由 <code>deploy/publish.py</code> 写入服务器，不包含聊天内容、密钥或数据库正文。</p></div><strong>{releases.length} 条</strong></div>
    {releases.length === 0 ? <div className="empty-state">还没有发布记录。使用 <code>./deploy/publish.sh publish --targets frontend</code> 发布后会出现在这里。</div> : <div className="audit-list">{releases.map((item) => <article key={item.id}><i className={`audit-dot release-${item.status}`} /><div><strong>{(item.targets || []).join('、') || '未指定目标'} · {RELEASE_STATUS[item.status] || item.status}</strong><p>{item.git_branch ? `${item.git_branch} @ ${item.git_sha || 'unknown'}` : item.git_sha || item.id}{item.operator ? ` · ${item.operator}` : ''}{item.duration_seconds != null ? ` · ${item.duration_seconds}s` : ''}{item.error ? ` · ${item.error}` : ''}</p></div><aside><time>{formatDate(item.created_at)}</time><small>{item.id}</small></aside></article>)}</div>}
  </section>
}

function CostPage({ onError, refreshKey }: { onError: (message: string | null) => void; refreshKey: number }) {
  const [period, setPeriod] = useState<CostPeriod>('day')
  const [data, setData] = useState<PlatformCost | null>(null)
  const [loading, setLoading] = useState(true)
  const maxCost = Math.max(0.000001, ...(data?.series.map((item) => item.cost_usd) ?? [0]))

  useEffect(() => {
    let cancelled = false
    void (async () => {
      setLoading(true)
      onError(null)
      try {
        const response = await fetch(`${API_BASE}/admin/metrics?period=${period}`, { credentials: 'include' })
        const payload = await readResponse(response)
        if (!response.ok) throw new Error(apiError(payload, '读取费用统计失败'))
        if (!cancelled) setData(payload as PlatformCost)
      } catch (requestError) {
        if (!cancelled) onError(requestError instanceof Error ? requestError.message : '读取费用统计失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [period, refreshKey, onError])

  if (loading && !data) return <div className="page-loading">正在汇总全部用户费用…</div>
  if (!data) return null
  const delta = data.previous.cost_delta_pct
  return <div className="cost-page">
    <section className="privacy-banner"><span>✓</span><div><strong>全站费用估算</strong><p>{data.privacy_notice} 费用按 Token × DeepSeek 官方单价估算，不是账单接口实扣金额。</p></div></section>
    <section className="panel cost-toolbar">
      <div><span>PLATFORM COST</span><h2>{data.labels.current}全站消耗</h2><p>{data.pricing.note}</p></div>
      <div className="cost-periods" role="tablist" aria-label="统计周期">
        {([['day', '每天'], ['week', '每周'], ['month', '每月']] as const).map(([value, label]) => (
          <button aria-selected={period === value} className={period === value ? 'active' : ''} key={value} onClick={() => setPeriod(value)} type="button">{label}</button>
        ))}
      </div>
    </section>
    <section className="metric-grid">
      <Metric detail={`${data.labels.compare} ${formatDelta(delta)}`} label={`${data.labels.current}费用`} tone="purple" value={formatMoney(data.current.cost_usd, data.current.cost_cny)} />
      <Metric detail={`评测 ${formatNumber(data.current.evaluation_runs)} 次`} label={`${data.labels.current} Token`} tone="orange" value={formatNumber(data.current.tokens)} />
      <Metric detail={`成功率统计见系统概览`} label={`${data.labels.current}调用`} tone="blue" value={formatNumber(data.current.runs)} />
      <Metric detail={`窗口内 ${formatNumber(data.lookback.active_users)} 人有消耗`} label="消耗用户" tone="green" value={formatNumber(data.current.active_users)} />
    </section>
    <section className="dashboard-grid">
      <article className="panel activity-panel">
        <div className="panel-heading"><div><span>COST TREND</span><h2>费用趋势</h2></div><small>近 {data.lookback.buckets} 个{period === 'day' ? '自然日' : period === 'week' ? '自然周' : '自然月'} · {formatMoney(data.lookback.cost_usd, data.lookback.cost_cny)}</small></div>
        <div className="bar-chart cost-chart">{data.series.map((item) => <div className={`bar-column${item.key === data.current.key ? ' is-current' : ''}`} key={item.key}><span>{formatUsd(item.cost_usd)}</span><div style={{ height: `${Math.max(5, item.cost_usd / maxCost * 100)}%` }} /><small>{item.label}</small></div>)}</div>
      </article>
      <article className="panel role-panel">
        <div className="panel-heading"><div><span>BY MODEL</span><h2>按模型</h2></div></div>
        {data.by_model.length === 0 ? <div className="empty-state">该周期还没有模型消耗。</div> : <div className="role-list cost-model-list">{data.by_model.map((item) => <div key={item.model}><i className="purple" /><span>{item.model}<small>{formatNumber(item.tokens)} Token · {formatNumber(item.runs)} 次</small></span><strong>{formatUsd(item.cost_usd)}</strong></div>)}</div>}
      </article>
    </section>
    <section className="panel users-page cost-users">
      <div className="users-toolbar"><div><span>{data.labels.current}用户消耗</span><p>只展示{data.labels.current}产生 Token 费用的账号，不含聊天内容和任务标题。</p></div><strong>{formatMoney(data.current.cost_usd, data.current.cost_cny)}</strong></div>
      {data.by_user.length === 0 ? <div className="empty-state">{data.labels.current}还没有用户产生费用。</div> : <div className="user-table cost-table"><div className="user-table-head cost-table-head"><span>用户</span><span>费用</span><span>Token</span><span>调用</span><span>评测</span></div>{data.by_user.map((user) => <div className="user-row cost-row" key={user.user_id}><div className="user-identity"><img alt="" src={projectIcon} /><div><strong>{user.display_name}</strong><p>{user.email}</p></div></div><strong>{formatUsd(user.cost_usd)}</strong><span>{formatNumber(user.tokens)}</span><span>{formatNumber(user.runs)}</span><span>{formatNumber(user.evaluation_runs)}</span></div>)}</div>}
    </section>
  </div>
}
function RoleRow({ label, count, tone }: { label: string; count: number; tone: string }) { return <div><i className={tone} /><span>{label}</span><strong>{count}</strong></div> }

function UsersPage({ users, onUpdate }: { users: ManagedUser[]; onUpdate: (user: ManagedUser, role: Role, active: boolean) => void }) {
  const [query, setQuery] = useState('')
  const visible = useMemo(() => users.filter((user) => `${user.display_name} ${user.email}`.toLowerCase().includes(query.toLowerCase())), [users, query])
  return <section className="users-page panel"><div className="users-toolbar"><div><span>共 {users.length} 个 Agent 账号</span><p>这些账号与后台管理员完全独立；知识库管理员可管理自己的知识文档。</p></div><input aria-label="搜索用户" onChange={(event) => setQuery(event.target.value)} placeholder="搜索昵称或邮箱" value={query} /></div><div className="user-table"><div className="user-table-head"><span>用户</span><span>角色</span><span>账号状态</span><span>最近登录</span><span>操作</span></div>{visible.map((user) => <UserRow key={user.id} onUpdate={onUpdate} user={user} />)}</div></section>
}

function UserRow({ user, onUpdate }: { user: ManagedUser; onUpdate: (user: ManagedUser, role: Role, active: boolean) => void }) {
  const [role, setRole] = useState<Role>(user.role)
  const [active, setActive] = useState(user.is_active)
  useEffect(() => { setRole(user.role); setActive(user.is_active) }, [user.role, user.is_active])
  const changed = role !== user.role || active !== user.is_active
  return <div className={`user-row ${!user.is_active ? 'disabled-user' : ''}`}><div className="user-identity"><img alt="" src={projectIcon} /><div><strong>{user.display_name}</strong><p>{user.email}</p></div></div><select aria-label={`${user.display_name}的角色`} onChange={(event) => setRole(event.target.value as Role)} value={role}>{Object.entries(ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><label className="status-switch"><input checked={active} onChange={(event) => setActive(event.target.checked)} type="checkbox" /><span /><b>{active ? '已启用' : '已停用'}</b></label><time>{formatDate(user.last_login_at)}</time><button disabled={!changed} onClick={() => onUpdate(user, role, active)}>保存</button></div>
}

function AdminUsersPage({
  admins,
  currentAdminId,
  onUpdate,
}: {
  admins: ManagedAdminUser[]
  currentAdminId: string
  onUpdate: (admin: ManagedAdminUser, canManageKnowledge: boolean, isActive: boolean) => void
}) {
  const [query, setQuery] = useState('')
  const visible = useMemo(
    () => admins.filter((admin) => `${admin.display_name} ${admin.email}`.toLowerCase().includes(query.toLowerCase())),
    [admins, query],
  )
  return <section className="users-page panel"><div className="users-toolbar"><div><span>共 {admins.length} 个后台管理员</span><p>这里管理独立后台账号本身，可配置是否允许其管理公共知识库。修改后，该后台账号需要重新登录。</p></div><input aria-label="搜索后台管理员" onChange={(event) => setQuery(event.target.value)} placeholder="搜索昵称或邮箱" value={query} /></div><div className="admin-table"><div className="user-table-head admin-user-table-head"><span>后台管理员</span><span>知识库权限</span><span>账号状态</span><span>最近登录</span><span>操作</span></div>{visible.map((admin) => <AdminUserRow currentAdminId={currentAdminId} key={admin.id} onUpdate={onUpdate} admin={admin} />)}</div></section>
}

function AdminUserRow({
  admin,
  currentAdminId,
  onUpdate,
}: {
  admin: ManagedAdminUser
  currentAdminId: string
  onUpdate: (admin: ManagedAdminUser, canManageKnowledge: boolean, isActive: boolean) => void
}) {
  const [canManageKnowledge, setCanManageKnowledge] = useState(admin.can_manage_knowledge)
  const [active, setActive] = useState(admin.is_active)
  useEffect(() => {
    setCanManageKnowledge(admin.can_manage_knowledge)
    setActive(admin.is_active)
  }, [admin.can_manage_knowledge, admin.is_active])
  const changed = canManageKnowledge !== admin.can_manage_knowledge || active !== admin.is_active
  const isCurrent = admin.id === currentAdminId
  return <div className={`user-row admin-user-row ${!admin.is_active ? 'disabled-user' : ''}`}><div className="user-identity"><img alt="" src={projectIcon} /><div><strong>{admin.display_name}{isCurrent ? <small>当前账号</small> : null}</strong><p>{admin.email}</p></div></div><label className="status-switch permission-switch"><input checked={canManageKnowledge} onChange={(event) => setCanManageKnowledge(event.target.checked)} type="checkbox" /><span /><b>{canManageKnowledge ? '可管理知识库' : '只读查看'}</b></label><label className="status-switch"><input checked={active} onChange={(event) => setActive(event.target.checked)} type="checkbox" /><span /><b>{active ? '已启用' : '已停用'}</b></label><time>{formatDate(admin.last_login_at)}</time><button disabled={!changed} onClick={() => onUpdate(admin, canManageKnowledge, active)}>保存</button></div>
}

function KnowledgePage({
  admin,
  documents,
  onChange,
  onRefresh,
}: {
  admin: CurrentUser
  documents: KnowledgeDocument[]
  onChange: (documents: KnowledgeDocument[]) => void
  onRefresh: () => void
}) {
  const [category, setCategory] = useState('未分类')
  const [activeCategory, setActiveCategory] = useState('全部')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<KnowledgeSearchMatch[]>([])
  const [searchMessage, setSearchMessage] = useState<string | null>(null)
  const [preview, setPreview] = useState<KnowledgePreview | null>(null)
  const [previewTargetId, setPreviewTargetId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isUploading, setIsUploading] = useState(false)
  const [isSeeding, setIsSeeding] = useState(false)
  const [isSearching, setIsSearching] = useState(false)
  const [isPreviewLoading, setIsPreviewLoading] = useState(false)

  const visibleDocuments = activeCategory === '全部'
    ? documents
    : documents.filter((item) => item.category === activeCategory)
  const categories = ['全部', ...Array.from(new Set(documents.map((item) => item.category)))]
  const totalChunks = documents.reduce((sum, item) => sum + item.chunk_count, 0)

  const uploadFiles = async (files: FileList | File[]) => {
    if (!admin.can_manage_knowledge) {
      setError('当前后台账号只有查看权限，不能上传知识库资料。')
      return
    }
    if (!files.length) return
    setError(null)
    setIsUploading(true)
    try {
      for (const file of Array.from(files)) {
        const form = new FormData()
        form.append('file', file)
        form.append('category', category)
        const response = await fetch(`${API_BASE}/admin/knowledge/documents`, {
          method: 'POST',
          credentials: 'include',
          body: form,
        })
        const data = await readResponse(response)
        if (!response.ok) throw new Error(apiError(data, `${file.name} 上传失败`))
      }
      onRefresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '上传知识文档失败')
    } finally {
      setIsUploading(false)
    }
  }

  const addDemoKnowledge = async () => {
    if (!admin.can_manage_knowledge) {
      setError('当前后台账号只有查看权限，不能导入演示资料。')
      return
    }
    setError(null)
    setIsSeeding(true)
    try {
      const response = await fetch(`${API_BASE}/admin/knowledge/demo`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '添加演示资料失败'))
      onChange((data as { documents: KnowledgeDocument[] }).documents)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '添加演示资料失败')
    } finally {
      setIsSeeding(false)
    }
  }

  const removeDocument = async (document: KnowledgeDocument) => {
    if (!admin.can_manage_knowledge) {
      setError('当前后台账号只有查看权限，不能删除知识库资料。')
      return
    }
    if (!window.confirm(`确定删除“${document.original_name}”吗？`)) return
    setError(null)
    try {
      const response = await fetch(`${API_BASE}/admin/knowledge/documents/${document.id}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      const data = response.status === 204 ? null : await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '删除失败'))
      onChange(documents.filter((item) => item.id !== document.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '删除知识文档失败')
    }
  }

  const runSearch = async () => {
    const normalized = searchQuery.trim()
    if (!normalized) {
      setSearchMessage('请输入关键词后再测试搜索。')
      setSearchResults([])
      return
    }
    setError(null)
    setSearchMessage(null)
    setIsSearching(true)
    try {
      const response = await fetch(`${API_BASE}/admin/knowledge/search?query=${encodeURIComponent(normalized)}&limit=5`, {
        credentials: 'include',
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '搜索失败'))
      const payload = data as { results: KnowledgeSearchMatch[]; message: string | null }
      setSearchResults(payload.results)
      setSearchMessage(payload.message)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '搜索失败')
    } finally {
      setIsSearching(false)
    }
  }

  const openPreview = async (document: KnowledgeDocument) => {
    setError(null)
    setIsPreviewLoading(true)
    setPreviewTargetId(document.id)
    try {
      const response = await fetch(`${API_BASE}/admin/knowledge/documents/${document.id}/preview`, {
        credentials: 'include',
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '读取预览失败'))
      setPreview(data as KnowledgePreview)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取预览失败')
    } finally {
      setIsPreviewLoading(false)
      setPreviewTargetId(null)
    }
  }

  return <section className="knowledge-page">
    <div className="knowledge-banner panel">
      <div>
        <span>PUBLIC KNOWLEDGE BASE</span>
        <h2>公共知识库</h2>
        <p>这里上传的是系统级资料，会作为公共知识提供给 Agent 检索。个人账号上传的私有资料不会出现在这里，也不会被后台管理员查看。</p>
        <small className="knowledge-permission">{admin.can_manage_knowledge ? '当前账号：可上传、删除、导入资料' : '当前账号：只读，可预览和搜索测试'}</small>
      </div>
      <div className="knowledge-summary">
        <strong>{documents.length}</strong>
        <small>文档</small>
        <strong>{totalChunks}</strong>
        <small>检索片段</small>
      </div>
    </div>

    <div className="knowledge-toolbar panel">
      <label>
        <span>上传到分类</span>
        <select disabled={!admin.can_manage_knowledge} onChange={(event) => setCategory(event.target.value)} value={category}>
          {KNOWLEDGE_CATEGORIES.map((item) => <option key={item}>{item}</option>)}
        </select>
      </label>
      <label className="knowledge-upload-button">
        <input accept=".md,.txt,.pdf,.docx" disabled={!admin.can_manage_knowledge} hidden multiple onChange={(event) => event.target.files && void uploadFiles(event.target.files)} type="file" />
        {isUploading ? '正在上传…' : admin.can_manage_knowledge ? '上传资料' : '只读模式'}
      </label>
      <button className="knowledge-secondary-button" disabled={!admin.can_manage_knowledge || isUploading || isSeeding} onClick={() => void addDemoKnowledge()} type="button">
        {isSeeding ? '正在导入…' : '导入演示资料'}
      </button>
    </div>

    <div className="knowledge-search panel">
      <div className="knowledge-search-head">
        <div>
          <span>SEARCH SANDBOX</span>
          <h3>搜索测试</h3>
          <p>输入一个问题，看看公共知识库会返回哪些片段，方便你验证资料是否真的可检索。</p>
        </div>
      </div>
      <div className="knowledge-search-bar">
        <input onChange={(event) => setSearchQuery(event.target.value)} placeholder="例如：报销标准、VPN、请假制度、RAG 是什么" value={searchQuery} />
        <button disabled={isSearching} onClick={() => void runSearch()} type="button">{isSearching ? '搜索中…' : '测试搜索'}</button>
      </div>
      {searchMessage ? <p className="knowledge-search-message">{searchMessage}</p> : null}
      {searchResults.length > 0 ? <div className="knowledge-search-results">{searchResults.map((item) => <article key={`${item.source}-${item.chunk}`}><div><strong>{item.source}</strong><span>片段 {item.chunk} · 分数 {item.score}</span></div><p>{item.content}</p></article>)}</div> : null}
    </div>

    <div className="knowledge-filter-row">
      {categories.map((item) => (
        <button
          className={activeCategory === item ? 'active' : ''}
          key={item}
          onClick={() => setActiveCategory(item)}
          type="button"
        >
          {item}
        </button>
      ))}
    </div>

    {error && <div className="page-error" role="alert">{error}</div>}

    <div className="knowledge-doc-list">
      {visibleDocuments.length === 0 ? <div className="empty-state panel">当前分类还没有资料，可以先上传公司制度、产品说明或 FAQ。</div> : visibleDocuments.map((document) => (
        <article className="knowledge-doc-card panel" key={document.id}>
          <div className="knowledge-doc-main">
            <div className="knowledge-doc-meta">
              <strong>{document.original_name}</strong>
              <span>{document.category}</span>
            </div>
            <p>{document.file_type.toUpperCase()} · {formatBytes(document.size_bytes)} · {document.chunk_count} 个片段 · {formatDate(document.updated_at)}</p>
          </div>
          <div className="knowledge-doc-actions">
            <button className="knowledge-secondary-button" disabled={isPreviewLoading} onClick={() => void openPreview(document)} type="button">{isPreviewLoading && previewTargetId === document.id ? '读取中…' : '预览'}</button>
            <button className="knowledge-delete-button" disabled={!admin.can_manage_knowledge} onClick={() => void removeDocument(document)} type="button">删除</button>
          </div>
        </article>
      ))}
    </div>

    {preview ? <div className="knowledge-preview-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreview(null) }}>
      <section className="knowledge-preview-panel" aria-modal="true" role="dialog">
        <header><div><span>DOCUMENT PREVIEW</span><h3>{preview.document.original_name}</h3><p>{preview.document.category} · {preview.document.file_type.toUpperCase()} · {preview.truncated ? '已截取前 12000 字符' : '完整预览'}</p></div><button onClick={() => setPreview(null)} type="button">×</button></header>
        <pre>{preview.content}</pre>
      </section>
    </div> : null}
  </section>
}

// 后台不再提供入口；暂保留实现和接口，避免升级时影响已有部署数据。
void KnowledgePage

function SecurityPage({ status, onRefresh }: { status: SecurityStatus; onRefresh: () => void }) {
  const [message, setMessage] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const [emailCode, setEmailCode] = useState('')
  const [recoveryEmail, setRecoveryEmail] = useState(status.recovery_email ?? '')
  const [emailStep, setEmailStep] = useState<'idle' | 'verify'>('idle')
  useEffect(() => {
    setRecoveryEmail(status.recovery_email ?? '')
  }, [status.recovery_email])

  const revokeOthers = async () => {
    if (!window.confirm('确定退出其他设备上的全部后台会话吗？')) return
    setWorking(true); setMessage(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/sessions/others`, { method: 'DELETE', credentials: 'include' })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '操作失败'))
      setMessage(`已退出 ${data.revoked_count} 个其他后台会话。`)
      onRefresh()
    } catch (requestError) { setMessage(requestError instanceof Error ? requestError.message : '操作失败') }
    finally { setWorking(false) }
  }
  const sendEmailCode = async () => {
    setWorking(true); setMessage(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/security/email/send`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: recoveryEmail }),
      })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '发送验证码失败'))
      setEmailStep('verify')
      setMessage(`验证码已发送至 ${data.masked_email}。`)
    } catch (requestError) { setMessage(requestError instanceof Error ? requestError.message : '发送验证码失败') }
    finally { setWorking(false) }
  }
  const enableEmail = async () => {
    setWorking(true); setMessage(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/security/email/enable`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: emailCode }) })
      const data = await readResponse(response)
      if (!response.ok) throw new Error(apiError(data, '启用邮箱恢复失败'))
      setMessage('邮箱验证码登录已启用。')
      setEmailStep('idle'); setEmailCode(''); onRefresh()
    } catch (requestError) { setMessage(requestError instanceof Error ? requestError.message : '启用邮箱恢复失败') }
    finally { setWorking(false) }
  }
  const disableEmail = async () => {
    if (!window.confirm('停用后，这个邮箱将不能再作为二次验证方式登录后台。确定继续吗？')) return
    setWorking(true); setMessage(null)
    try {
      const response = await fetch(`${API_BASE}/admin-auth/security/email`, { method: 'DELETE', credentials: 'include' })
      if (!response.ok) throw new Error(apiError(await readResponse(response), '停用失败'))
      setMessage('邮箱验证码登录已停用。'); onRefresh()
    } catch (requestError) { setMessage(requestError instanceof Error ? requestError.message : '停用失败') }
    finally { setWorking(false) }
  }
  return <div className="security-page"><section className="security-summary"><span>✓</span><div><strong>双重认证已启用</strong><p>后台账号与 Agent 账号相互独立，所有后台会话均已完成 MFA 验证。</p></div></section><section className="security-grid"><article className="security-card enabled"><div><span>APP</span><small>方式一</small></div><h2>Authenticator</h2><p>使用每 30 秒变化的 6 位动态验证码完成二次验证。</p><strong>已启用</strong></article><article className={`security-card ${status.email_recovery_enabled ? 'enabled' : ''}`}><div><span>MAIL</span><small>方式二</small></div><h2>邮箱验证码</h2><p>{status.email_otp_available ? (status.recovery_email ? `当前绑定邮箱：${status.masked_email}` : '请先绑定你自己的邮箱验证码方式。') : 'SMTP 邮件服务尚未配置完整。'}</p><div className="recovery-email-field"><input aria-label="恢复邮箱" autoComplete="email" disabled={working} onChange={(event) => setRecoveryEmail(event.target.value)} placeholder="输入你自己的验证码邮箱" type="email" value={recoveryEmail} /><button className="security-card-action" disabled={working || !status.email_otp_available || !recoveryEmail.trim()} onClick={() => void sendEmailCode()}>{emailStep === 'verify' ? '重新发送验证码' : status.recovery_email ? '更换并发送验证码' : '发送验证邮件'}</button></div>{emailStep === 'verify' ? <div className="email-enable-form"><input aria-label="邮箱验证码" inputMode="numeric" maxLength={6} onChange={(event) => setEmailCode(event.target.value.replace(/\D/g, ''))} placeholder="输入 6 位验证码" value={emailCode} /><button disabled={working || emailCode.length !== 6} onClick={() => void enableEmail()}>确认启用</button></div> : null}{status.email_recovery_enabled ? <><strong>已验证并启用，可与身份验证器二选一登录。</strong><button className="security-card-action danger" disabled={working} onClick={() => void disableEmail()}>停用邮箱验证</button></> : <strong>验证通过后，这个邮箱可以作为独立的二次验证方式。</strong>}</article></section><section className="security-details panel"><h2>账号安全状态</h2><div><span>后台登录邮箱<strong>{status.email}</strong></span><span>验证码频率<strong>同一功能 60 秒一次</strong></span><span>恢复码余量<strong>{status.recovery_codes_remaining} / 8</strong></span><span>有效后台会话<strong>{status.active_sessions}</strong></span><span>无操作超时<strong>{status.idle_timeout_minutes} 分钟</strong></span><span>最长会话<strong>{status.absolute_timeout_hours} 小时</strong></span></div><button disabled={working} onClick={() => void revokeOthers()}>{working ? '正在处理…' : '退出其他设备'}</button>{message && <p className="security-message">{message}</p>}</section></div>
}

function AuditPage({ logs }: { logs: AuditLog[] }) {
  const descriptions: Record<string, string> = {
    admin_email_recovery_enabled: '启用了邮箱验证码登录',
    admin_email_recovery_disabled: '停用了邮箱验证码登录',
    admin_email_otp_login: '使用邮箱验证码完成了二次验证登录',
    admin_authenticator_rebound: '重新绑定了 Authenticator 并更新恢复码',
  }
  return <section className="audit-page panel"><div className="audit-heading"><div><span>SECURITY AUDIT</span><h2>权限与认证记录</h2><p>审计日志只记录权限和认证安全事件，不包含用户业务数据或验证码。</p></div><strong>{logs.length} 条</strong></div><div className="audit-list">{logs.length === 0 ? <div className="empty-state">暂无安全操作记录</div> : logs.map((log) => <article key={log.id}><span className="audit-dot" /><div>{log.action === 'user_permission_updated' ? <><strong>{log.actor?.display_name ?? '未知管理员'} 修改了 {log.target?.display_name ?? '未知账号'} 的权限</strong><p>{log.changes.before?.role && ROLE_LABELS[log.changes.before.role]} → {log.changes.after?.role && ROLE_LABELS[log.changes.after.role]} · {log.changes.after?.is_active ? '账号启用' : '账号停用'}</p></> : log.action === 'admin_permission_updated' ? <><strong>{log.actor?.display_name ?? '未知管理员'} 修改了后台管理员 {log.target?.display_name ?? '未知账号'} 的权限</strong><p>{log.changes.after?.can_manage_knowledge ? '可管理知识库' : '只读查看'} · {log.changes.after?.is_active ? '账号启用' : '账号停用'}</p></> : <><strong>{log.actor?.display_name ?? '系统管理员'}{descriptions[log.action] ?? '执行了安全操作'}</strong><p>后台身份安全事件</p></>}</div><aside><time>{formatDate(log.created_at)}</time><small>{log.ip_address ?? '系统记录'}</small></aside></article>)}</div></section>
}

export default App
