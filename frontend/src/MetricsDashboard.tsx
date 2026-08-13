import { useEffect, useMemo, useState } from 'react'

type PromptVersionOption = { id: string; name: string }

type MetricsPayload = {
  period_days: number
  generated_at: string
  filters: {
    model: string | null
    prompt_version_id: string | null
    models: string[]
    prompt_versions: PromptVersionOption[]
  }
  pricing: {
    currency: string
    model: string
    input_per_million: number
    cache_hit_per_million: number
    output_per_million: number
    source: string
    usd_to_cny: number | null
    note: string
  }
  runs: {
    total: number
    success: number
    failed: number
    success_rate: number
    average_duration_ms: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    estimated_cost_usd: number
  }
  evaluations: {
    total: number
    completed: number
    average_score: number
    low_confidence_run_rate: number
    low_confidence_case_rate: number
    total_tokens: number
    estimated_cost_usd: number
  }
  workflows: {
    total: number
    success: number
    failed: number
    waiting: number
    cancelled: number
    success_rate: number
    node_total: number
    node_failed: number
    node_failure_rate: number
    knowledge_hit_rate: number
  }
  queue: {
    queued: number
    running: number
    waiting: number
    failed: number
    redis_backlog: number | null
  }
  rag: {
    calls: number
    hits: number
    hit_rate: number
    documents: number
    indexed_documents: number
  }
  totals: {
    estimated_cost_usd: number
    estimated_cost_cny: number | null
    total_tokens: number
  }
  daily: Array<{
    date: string
    runs: number
    tokens: number
    cost_usd: number
    workflow_runs: number
    evaluation_runs: number
  }>
  by_model: Array<{ model: string; runs: number; tokens: number; cost_usd: number }>
  by_run_type: Array<{ run_type: string; runs: number; success_rate: number; tokens: number; cost_usd: number }>
  routing?: {
    default: string
    cheap: string
    strong: string
    fallback_chain: string[]
    roles: Record<string, string>
    note: string
  }
}

function money(usd: number, cny: number | null) {
  const dollars = `$${usd.toFixed(4)}`
  return cny == null ? dollars : `${dollars} · ≈¥${cny.toFixed(3)}`
}

export function MetricsDashboard({
  apiBaseUrl,
  onClose,
}: {
  apiBaseUrl: string
  onClose: () => void
}) {
  const [days, setDays] = useState(7)
  const [model, setModel] = useState('')
  const [promptVersionId, setPromptVersionId] = useState('')
  const [data, setData] = useState<MetricsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const load = async (nextDays = days, nextModel = model, nextPrompt = promptVersionId) => {
    setIsLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ days: String(nextDays) })
      if (nextModel) params.set('model', nextModel)
      if (nextPrompt) params.set('prompt_version_id', nextPrompt)
      const response = await fetch(`${apiBaseUrl}/metrics?${params}`, { credentials: 'include' })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? '读取监控数据失败')
      setData(payload as MetricsPayload)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取监控数据失败')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const maxDaily = useMemo(() => Math.max(1, ...(data?.daily.map((item) => item.runs + item.workflow_runs + item.evaluation_runs) ?? [1])), [data])
  const maxCost = useMemo(() => Math.max(0.000001, ...(data?.daily.map((item) => item.cost_usd) ?? [0])), [data])

  return (
    <div className="metrics-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section aria-labelledby="metrics-panel-title" aria-modal="true" className="mcp-panel metrics-panel" role="dialog">
        <div className="mcp-panel-header">
          <div>
            <span className="mcp-eyebrow">PLATFORM METRICS</span>
            <h2 id="metrics-panel-title">监控与费用看板</h2>
            <p>统计本账号的调用、耗时、Token 和估算费用。DeepSeek API Key 不能读取账户账单，这里按官方单价估算。</p>
          </div>
          <button aria-label="关闭监控看板" className="mcp-close-button" onClick={onClose} type="button">×</button>
        </div>

        <div className="metrics-toolbar">
          <label>时间范围
            <select value={days} onChange={(event) => { const value = Number(event.target.value); setDays(value); void load(value, model, promptVersionId) }}>
              <option value={1}>最近 1 天</option>
              <option value={7}>最近 7 天</option>
              <option value={30}>最近 30 天</option>
            </select>
          </label>
          <label>模型
            <select value={model} onChange={(event) => { setModel(event.target.value); void load(days, event.target.value, promptVersionId) }}>
              <option value="">全部模型</option>
              {(data?.filters.models ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label>Prompt 版本
            <select value={promptVersionId} onChange={(event) => { setPromptVersionId(event.target.value); void load(days, model, event.target.value) }}>
              <option value="">全部版本</option>
              {(data?.filters.prompt_versions ?? []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
        </div>

        {error && <div className="error-message metrics-error">{error}</div>}
        {isLoading && !data ? (
          <div className="mcp-loading"><div className="typing-dots"><span /><span /><span /></div>正在汇总监控数据…</div>
        ) : data ? (
          <div className="metrics-body">
            <div className="metrics-cards">
              <article><small>模型调用</small><strong>{data.runs.total}</strong><span>成功率 {data.runs.success_rate}% · 平均 {data.runs.average_duration_ms} ms</span></article>
              <article><small>估算费用</small><strong>{money(data.totals.estimated_cost_usd, data.totals.estimated_cost_cny)}</strong><span>{data.totals.total_tokens.toLocaleString()} Token · {data.pricing.source === 'env' ? '环境单价' : '官方公开价'}</span></article>
              <article><small>输入 / 输出</small><strong>{data.runs.prompt_tokens.toLocaleString()} / {data.runs.completion_tokens.toLocaleString()}</strong><span>${data.pricing.input_per_million} / ${data.pricing.output_per_million} / 1M</span></article>
              <article><small>队列积压</small><strong>{data.queue.queued + data.queue.running + data.queue.waiting}</strong><span>排队 {data.queue.queued} · 执行 {data.queue.running} · 等待 {data.queue.waiting}{data.queue.redis_backlog == null ? '' : ` · Redis ${data.queue.redis_backlog}`}</span></article>
              <article><small>工作流节点失败</small><strong>{data.workflows.node_failure_rate}%</strong><span>{data.workflows.node_failed}/{data.workflows.node_total} 节点 · 运行成功率 {data.workflows.success_rate}%</span></article>
              <article><small>RAG 命中</small><strong>{data.rag.hit_rate}%</strong><span>{data.rag.hits}/{data.rag.calls} 次检索 · 已向量化 {data.rag.indexed_documents}/{data.rag.documents}</span></article>
              <article><small>低可信回答</small><strong>{data.evaluations.low_confidence_case_rate}%</strong><span>评测用例 · 运行级 {data.evaluations.low_confidence_run_rate}%</span></article>
              <article><small>评测</small><strong>{data.evaluations.average_score}%</strong><span>{data.evaluations.completed} 次完成 · {money(data.evaluations.estimated_cost_usd, data.pricing.usd_to_cny == null ? null : data.evaluations.estimated_cost_usd * data.pricing.usd_to_cny)}</span></article>
            </div>

            <section className="metrics-chart">
              <header><strong>每日调用与费用</strong><span>柱状为调用次数，横条为估算费用</span></header>
              <div className="metrics-bars">
                {data.daily.map((item) => {
                  const calls = item.runs + item.workflow_runs + item.evaluation_runs
                  return (
                    <div key={item.date}>
                      <small>{item.date.slice(5)}</small>
                      <span><i style={{ height: `${Math.max(6, calls / maxDaily * 100)}%` }} /></span>
                      <em>{calls}</em>
                      <b style={{ width: `${Math.max(4, item.cost_usd / maxCost * 100)}%` }} />
                      <p>${item.cost_usd.toFixed(4)}</p>
                    </div>
                  )
                })}
              </div>
            </section>

            <div className="metrics-tables">
              <section>
                <strong>按模型</strong>
                {data.by_model.length === 0 ? <p>这段时间还没有模型调用。</p> : data.by_model.map((item) => (
                  <div key={item.model}><span>{item.model}</span><em>{item.runs} 次 · {item.tokens.toLocaleString()} Token · ${item.cost_usd.toFixed(4)}</em></div>
                ))}
              </section>
              <section>
                <strong>按运行类型</strong>
                {data.by_run_type.length === 0 ? <p>完成一次对话或工具调用后会出现在这里。</p> : data.by_run_type.map((item) => (
                  <div key={item.run_type}><span>{item.run_type}</span><em>{item.runs} 次 · 成功 {item.success_rate}% · ${item.cost_usd.toFixed(4)}</em></div>
                ))}
              </section>
            </div>

            {data.routing ? (
              <section className="metrics-chart">
                <header>
                  <strong>多模型路由</strong>
                  <span>{data.routing.note}</span>
                </header>
                <div className="metrics-tables">
                  <section>
                    <strong>角色模型</strong>
                    <div><span>便宜 (cheap)</span><em>{data.routing.cheap}</em></div>
                    <div><span>强模型 (strong)</span><em>{data.routing.strong}</em></div>
                    <div><span>默认</span><em>{data.routing.default}</em></div>
                    <div><span>降级链</span><em>{data.routing.fallback_chain.join(' → ')}</em></div>
                  </section>
                  <section>
                    <strong>任务分配</strong>
                    {Object.entries(data.routing.roles).map(([task, role]) => (
                      <div key={task}><span>{task}</span><em>{role}</em></div>
                    ))}
                  </section>
                </div>
              </section>
            ) : null}

            <p className="metrics-note">{data.pricing.note} 当前按 {data.pricing.model} 计：输入 ${data.pricing.input_per_million} / 缓存命中 ${data.pricing.cache_hit_per_million} / 输出 ${data.pricing.output_per_million}（每百万 Token，美元）。</p>
          </div>
        ) : null}
      </section>
    </div>
  )
}
