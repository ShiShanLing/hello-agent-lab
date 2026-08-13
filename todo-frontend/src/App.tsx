import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
import projectIcon from './assets/project-icon-v1-optimized.png'

type User = { id: string; email: string; display_name: string }
type Todo = {
  id: number
  title: string
  completed: boolean
  plan_id: number | null
  description: string | null
  minutes: number | null
}
type TodoPlan = {
  id: number
  title: string
  summary: string
  priority: 'low' | 'medium' | 'high'
  created_at: string
  todos: Todo[]
}
type TodoWorkspace = { plans: TodoPlan[]; inbox: Todo[] }
type Filter = 'all' | 'active' | 'completed'

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ??
  (import.meta.env.PROD ? '/agent/api' : '/api')
const AGENT_APP_URL =
  import.meta.env.VITE_AGENT_APP_URL ??
  (import.meta.env.PROD ? '/agent/' : '/')

function AuthView({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsSubmitting(true)
    setError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/auth/${mode}`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, ...(mode === 'register' ? { display_name: displayName } : {}) }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '登录失败')
      onAuthenticated(data as User)
    } catch (requestError) {
      setError(requestError instanceof TypeError
        ? '无法连接后端，请确认 FastAPI 服务正在 8000 端口运行。'
        : requestError instanceof Error ? requestError.message : '登录失败')
    } finally { setIsSubmitting(false) }
  }

  return (
    <main className="auth-shell"><section className="auth-card">
      <img alt="" aria-hidden="true" className="logo-mark" src={projectIcon} /><span className="eyebrow">TASKFLOW</span>
      <h1>{mode === 'login' ? '管理每一个成长计划' : '创建 Todo 账号'}</h1>
      <p>与 Hello Agent 使用同一个账号，Agent 制定的计划会完整同步到这里。</p>
      <form className="auth-form" onSubmit={submit}>
        {mode === 'register' && <label><span>昵称</span><input autoComplete="name" maxLength={50} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如：小明" required value={displayName} /></label>}
        <label><span>邮箱</span><input autoComplete="email" maxLength={254} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" required type="email" value={email} /></label>
        <label><span>密码</span><input autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength={8} maxLength={128} onChange={(event) => setPassword(event.target.value)} placeholder="至少 8 个字符" required type="password" value={password} /></label>
        {error && <div className="inline-error" role="alert">{error}</div>}
        <button className="submit-button" disabled={isSubmitting} type="submit">{isSubmitting ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}</button>
      </form>
      <button className="text-button" onClick={() => { setMode((current) => current === 'login' ? 'register' : 'login'); setError(null) }} type="button">{mode === 'login' ? '没有账号？立即注册' : '已有账号？返回登录'}</button>
    </section></main>
  )
}

function TaskRow({ todo, pendingDelete, onToggle, onAskDelete, onDelete, onCancelDelete }: {
  todo: Todo; pendingDelete: boolean; onToggle: () => void; onAskDelete: () => void; onDelete: () => void; onCancelDelete: () => void
}) {
  return <article className={`todo-item ${todo.completed ? 'completed' : ''}`}>
    <button aria-label={todo.completed ? `恢复任务：${todo.title}` : `完成任务：${todo.title}`} className="check-button" onClick={onToggle} type="button">{todo.completed ? '✓' : ''}</button>
    <div className="todo-title"><strong>{todo.title}</strong>{todo.description && <p>{todo.description}</p>}<span>{todo.minutes ? `${todo.minutes} 分钟` : `任务 #${todo.id}`}</span></div>
    {pendingDelete ? <div className="delete-confirm"><button onClick={onCancelDelete} type="button">取消</button><button className="danger" onClick={onDelete} type="button">确认</button></div>
      : <button aria-label={`删除任务：${todo.title}`} className="delete-button" onClick={onAskDelete} type="button">×</button>}
  </article>
}

function PlanSection({ plan, filter, pendingDeleteId, onToggle, onAskDelete, onDelete, onCancelDelete }: {
  plan: TodoPlan; filter: Filter; pendingDeleteId: number | null; onToggle: (todo: Todo) => void; onAskDelete: (id: number) => void; onDelete: (id: number) => void; onCancelDelete: () => void
}) {
  const completed = plan.todos.filter((todo) => todo.completed).length
  const visible = plan.todos.filter((todo) => filter === 'all' || (filter === 'active' ? !todo.completed : todo.completed))
  const totalMinutes = plan.todos.reduce((total, todo) => total + (todo.minutes ?? 0), 0)
  const percent = plan.todos.length ? Math.round(completed / plan.todos.length * 100) : 0
  return <section className={`plan-group priority-${plan.priority}`}>
    <header className="plan-group-header">
      <div><span className="plan-label">计划</span><h2>{plan.title}</h2><p>{plan.summary || '这个计划还没有说明。'}</p></div>
      <div className="plan-progress"><strong>{percent}%</strong><span>{completed}/{plan.todos.length}</span></div>
    </header>
    <div className="plan-meta"><span>共 {plan.todos.length} 个步骤</span>{totalMinutes > 0 && <span>预计 {totalMinutes} 分钟</span>}<div><i style={{ width: `${percent}%` }} /></div></div>
    <div className="plan-tasks">
      {visible.length ? visible.map((todo) => <TaskRow key={todo.id} todo={todo} pendingDelete={pendingDeleteId === todo.id} onToggle={() => onToggle(todo)} onAskDelete={() => onAskDelete(todo.id)} onDelete={() => onDelete(todo.id)} onCancelDelete={onCancelDelete} />)
        : <p className="filtered-empty">这个筛选条件下没有任务</p>}
    </div>
  </section>
}

function App() {
  const [user, setUser] = useState<User | null>(null)
  const [workspace, setWorkspace] = useState<TodoWorkspace>({ plans: [], inbox: [] })
  const [filter, setFilter] = useState<Filter>('all')
  const [newTitle, setNewTitle] = useState('')
  const [selectedPlanId, setSelectedPlanId] = useState('inbox')
  const [isCheckingAuth, setIsCheckingAuth] = useState(true)
  const [isLoading, setIsLoading] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const [isPlanFormOpen, setIsPlanFormOpen] = useState(false)
  const [planTitle, setPlanTitle] = useState('')
  const [planSummary, setPlanSummary] = useState('')
  const [pendingDeleteId, setPendingDeleteId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const allTodos = useMemo(() => [...workspace.plans.flatMap((plan) => plan.todos), ...workspace.inbox], [workspace])
  const completedCount = allTodos.filter((todo) => todo.completed).length

  const request = async (path: string, options?: RequestInit) => {
    const response = await fetch(`${API_BASE_URL}${path}`, { ...options, credentials: 'include' })
    if (response.status === 401) { setUser(null); throw new Error('登录已失效，请重新登录。') }
    return response
  }
  const loadWorkspace = async () => {
    setIsLoading(true); setError(null)
    try {
      const response = await request('/todo-plans'); const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取计划失败')
      setWorkspace(data as TodoWorkspace)
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '读取计划失败') }
    finally { setIsLoading(false) }
  }

  useEffect(() => { void (async () => { try { const response = await fetch(`${API_BASE_URL}/auth/me`, { credentials: 'include' }); if (response.ok) setUser(await response.json() as User) } finally { setIsCheckingAuth(false) } })() }, [])
  useEffect(() => { if (user) void loadWorkspace() }, [user])

  const createPlan = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!planTitle.trim()) return; setIsCreating(true); setError(null)
    try {
      const response = await request('/todo-plans', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: planTitle, summary: planSummary }) })
      const data = await response.json(); if (!response.ok) throw new Error(data.detail ?? '创建计划失败')
      setPlanTitle(''); setPlanSummary(''); setIsPlanFormOpen(false); setSelectedPlanId(String((data as TodoPlan).id)); await loadWorkspace()
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '创建计划失败') }
    finally { setIsCreating(false) }
  }
  const createTodo = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const title = newTitle.trim(); if (!title) return; setIsCreating(true); setError(null)
    try {
      const response = await request('/todos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, plan_id: selectedPlanId === 'inbox' ? null : Number(selectedPlanId) }) })
      const data = await response.json(); if (!response.ok) throw new Error(data.detail ?? '创建任务失败')
      setNewTitle(''); await loadWorkspace()
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '创建任务失败') }
    finally { setIsCreating(false) }
  }
  const toggleTodo = async (todo: Todo) => { setError(null); try { const response = await request(`/todos/${todo.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ completed: !todo.completed }) }); if (!response.ok) { const data = await response.json(); throw new Error(data.detail ?? '更新任务失败') }; await loadWorkspace() } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '更新任务失败') } }
  const deleteTodo = async (id: number) => { setError(null); try { const response = await request(`/todos/${id}`, { method: 'DELETE' }); if (!response.ok) { const data = await response.json(); throw new Error(data.detail ?? '删除任务失败') }; setPendingDeleteId(null); await loadWorkspace() } catch (requestError) { setError(requestError instanceof Error ? requestError.message : '删除任务失败') } }
  const logout = async () => { await fetch(`${API_BASE_URL}/auth/logout`, { method: 'POST', credentials: 'include' }); setUser(null); setWorkspace({ plans: [], inbox: [] }) }

  if (isCheckingAuth) return <main className="loading-screen"><span className="spinner" />正在恢复登录状态…</main>
  if (!user) return <AuthView onAuthenticated={setUser} />

  const visibleInbox = workspace.inbox.filter((todo) => filter === 'all' || (filter === 'active' ? !todo.completed : todo.completed))
  return <main className="todo-shell">
    <header className="app-header"><div className="app-brand"><img alt="" aria-hidden="true" className="logo-mark" src={projectIcon} /><div><strong>TaskFlow</strong><small>计划与任务</small></div></div><div className="account-actions"><a href={AGENT_APP_URL}>打开 Agent</a><img alt="" aria-hidden="true" className="avatar" src={projectIcon} /><button onClick={() => void logout()} type="button">退出</button></div></header>
    <section className="todo-content">
      <div className="welcome-row"><div><span className="eyebrow">MY PLANS</span><h1>你好，{user.display_name}</h1><p>{workspace.plans.length} 个计划 · {allTodos.length - completedCount} 项待完成</p></div><button className="new-plan-button" onClick={() => setIsPlanFormOpen((open) => !open)} type="button">{isPlanFormOpen ? '取消' : '＋ 新建计划'}</button></div>
      {isPlanFormOpen && <form className="new-plan-form" onSubmit={createPlan}><input aria-label="计划名称" maxLength={100} onChange={(event) => setPlanTitle(event.target.value)} placeholder="计划名称，例如：旅行准备" required value={planTitle} /><input aria-label="计划说明" maxLength={300} onChange={(event) => setPlanSummary(event.target.value)} placeholder="一句话说明这个计划" value={planSummary} /><button disabled={isCreating} type="submit">创建计划</button></form>}
      <form className="add-todo" onSubmit={createTodo}><span>＋</span><input aria-label="新任务" maxLength={100} onChange={(event) => setNewTitle(event.target.value)} placeholder="添加一个计划步骤…" value={newTitle} /><select aria-label="所属计划" onChange={(event) => setSelectedPlanId(event.target.value)} value={selectedPlanId}><option value="inbox">未分类</option>{workspace.plans.map((plan) => <option key={plan.id} value={plan.id}>{plan.title}</option>)}</select><button disabled={!newTitle.trim() || isCreating} type="submit">添加</button></form>
      <div className="list-toolbar"><div className="filters">{([['all','全部'],['active','未完成'],['completed','已完成']] as Array<[Filter,string]>).map(([value,label]) => <button aria-pressed={filter === value} className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)} type="button">{label}</button>)}</div><button className="refresh-button" disabled={isLoading} onClick={() => void loadWorkspace()} type="button">{isLoading ? '同步中…' : '刷新同步'}</button></div>
      {error && <div className="inline-error">{error}</div>}
      <div className="plans-list">{workspace.plans.map((plan) => <PlanSection key={plan.id} plan={plan} filter={filter} pendingDeleteId={pendingDeleteId} onToggle={(todo) => void toggleTodo(todo)} onAskDelete={setPendingDeleteId} onDelete={(id) => void deleteTodo(id)} onCancelDelete={() => setPendingDeleteId(null)} />)}</div>
      {workspace.inbox.length > 0 && <section className="inbox-group"><header><div><span className="plan-label">收集箱</span><h2>未分类任务</h2></div><span>{workspace.inbox.length} 项</span></header><div className="plan-tasks">{visibleInbox.map((todo) => <TaskRow key={todo.id} todo={todo} pendingDelete={pendingDeleteId === todo.id} onToggle={() => void toggleTodo(todo)} onAskDelete={() => setPendingDeleteId(todo.id)} onDelete={() => void deleteTodo(todo.id)} onCancelDelete={() => setPendingDeleteId(null)} />)}</div></section>}
      {!isLoading && workspace.plans.length === 0 && workspace.inbox.length === 0 && <div className="empty-state"><div>＋</div><strong>还没有计划</strong><p>从 Agent 同步一个计划，或者在这里新建。</p></div>}
    </section>
  </main>
}

export default App
