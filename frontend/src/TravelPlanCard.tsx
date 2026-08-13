import { useEffect, useState } from 'react'
import type { GoalPlan } from './PlanCard'

export type TravelActivity = {
  time: string
  title: string
  location: string
  description: string
  estimated_cost: number
}

export type TravelPlan = {
  status: 'needs_input' | 'ready' | 'confirmed'
  title: string
  summary: string
  origin: string | null
  destination: string | null
  start_date: string | null
  end_date: string | null
  travelers: number | null
  budget: number | null
  preferences: string[]
  missing_fields: string[]
  clarification_questions: string[]
  days: Array<{
    day_number: number
    date: string
    title: string
    activities: TravelActivity[]
  }>
}

type TravelPlanCardProps = {
  plan: TravelPlan
  travelPlanId: number
  onConfirm: () => Promise<void>
  onGeneratePreparations: () => Promise<GoalPlan>
  onSyncPreparations: (steps: GoalPlan['steps']) => Promise<number>
  onLoadWorkflow: () => Promise<TravelWorkflow>
}

export type TravelWorkflow = {
  travel_plan_id: number
  completed_count: number
  total_count: number
  resumable: boolean
  stages: Array<{
    key: 'requirements' | 'itinerary' | 'confirmation' | 'preparations' | 'todo_sync'
    title: string
    status: 'pending' | 'waiting' | 'completed'
    detail: string
    completed_at: string | null
  }>
}

const money = new Intl.NumberFormat('zh-CN')

export function TravelPlanCard({
  plan,
  travelPlanId,
  onConfirm,
  onGeneratePreparations,
  onSyncPreparations,
  onLoadWorkflow,
}: TravelPlanCardProps) {
  const [isConfirming, setIsConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [preparationPlan, setPreparationPlan] = useState<GoalPlan | null>(null)
  const [selectedPreparations, setSelectedPreparations] = useState<number[]>([])
  const [isGeneratingPreparations, setIsGeneratingPreparations] = useState(false)
  const [isSyncingPreparations, setIsSyncingPreparations] = useState(false)
  const [syncedPreparationCount, setSyncedPreparationCount] = useState<number | null>(null)
  const [preparationError, setPreparationError] = useState<string | null>(null)
  const [workflow, setWorkflow] = useState<TravelWorkflow | null>(null)

  const refreshWorkflow = async () => {
    try {
      setWorkflow(await onLoadWorkflow())
    } catch {
      // 工作流状态不影响旅行计划主体的使用，接口错误由具体操作反馈。
    }
  }

  useEffect(() => {
    void refreshWorkflow()
  }, [travelPlanId, plan.status])

  const workflowTimeline = workflow && (
    <section className="travel-workflow" aria-label="旅行计划执行进度">
      <div className="travel-workflow-heading">
        <strong>Agent 工作流</strong>
        <span>{workflow.completed_count}/{workflow.total_count} 已完成</span>
      </div>
      <ol>
        {workflow.stages.map((stage) => (
          <li className={`workflow-${stage.status}`} key={stage.key}>
            <span className="travel-workflow-dot" aria-hidden="true">
              {stage.status === 'completed' ? '✓' : stage.status === 'waiting' ? '•' : ''}
            </span>
            <div><strong>{stage.title}</strong><small>{stage.detail}</small></div>
          </li>
        ))}
      </ol>
    </section>
  )
  const estimatedCost = plan.days.reduce(
    (total, day) =>
      total + day.activities.reduce(
        (dayTotal, activity) => dayTotal + activity.estimated_cost,
        0,
      ),
    0,
  )

  if (plan.status === 'needs_input') {
    return (
      <div className="message-content travel-card travel-needs-input">
        <div className="travel-card-heading">
          <div>
            <span className="travel-eyebrow">旅行任务 #{travelPlanId}</span>
            <h2>{plan.title}</h2>
          </div>
          <span className="travel-status-badge">待补充</span>
        </div>
        <p className="travel-summary">{plan.summary}</p>
        {workflowTimeline}
        <div className="travel-question-box">
          <strong>生成行程前，还需要确认</strong>
          <ol>
            {plan.clarification_questions.map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ol>
        </div>
        <p className="travel-card-note">任务已暂停并保存。直接在下方输入框补充，Agent 会继续当前任务。</p>
      </div>
    )
  }

  return (
    <div className="message-content travel-card">
      <div className="travel-card-heading">
        <div>
          <span className="travel-eyebrow">旅行任务 #{travelPlanId}</span>
          <h2>{plan.title}</h2>
        </div>
        <span className={plan.status === 'confirmed' ? 'travel-confirmed-badge' : 'travel-ready-badge'}>
          {plan.status === 'confirmed' ? '已确认' : '草稿'}
        </span>
      </div>
      <p className="travel-summary">{plan.summary}</p>
      {workflowTimeline}
      <div className="travel-route">
        <span>{plan.origin}</span>
        <span aria-hidden="true">→</span>
        <span>{plan.destination}</span>
      </div>
      <div className="travel-facts">
        <span><small>日期</small>{plan.start_date} 至 {plan.end_date}</span>
        <span><small>人数</small>{plan.travelers} 人</span>
        <span><small>总预算</small>¥{money.format(plan.budget ?? 0)}</span>
        <span><small>当前项目估算</small>¥{money.format(estimatedCost)}</span>
      </div>
      {plan.preferences.length > 0 && (
        <div className="travel-preferences">
          {plan.preferences.map((preference) => <span key={preference}>{preference}</span>)}
        </div>
      )}
      <div className="travel-days">
        {plan.days.map((day) => (
          <section className="travel-day" key={`${day.day_number}-${day.date}`}>
            <div className="travel-day-heading">
              <span>DAY {day.day_number}</span>
              <div><strong>{day.title}</strong><small>{day.date}</small></div>
            </div>
            <div className="travel-activities">
              {day.activities.map((activity, index) => (
                <article className="travel-activity" key={`${activity.time}-${activity.title}-${index}`}>
                  <time>{activity.time}</time>
                  <div>
                    <div className="travel-activity-title">
                      <strong>{activity.title}</strong>
                      <span>约 ¥{money.format(activity.estimated_cost)}</span>
                    </div>
                    <small>{activity.location}</small>
                    <p>{activity.description}</p>
                  </div>
                </article>
              ))}
            </div>
          </section>
        ))}
      </div>
      <div className="travel-confirm-area">
        <div>
          <strong>{plan.status === 'confirmed' ? '这份旅行计划已确认' : '确认采用这份旅行计划？'}</strong>
          <small>{plan.status === 'confirmed' ? 'Agent 之后可以随时查询到这份正式计划。' : '确认后会转为正式旅行计划，仍然不会自动创建 Todo。'}</small>
          {confirmError && <span className="travel-confirm-error">{confirmError}</span>}
        </div>
        <button
          type="button"
          className="travel-confirm-button"
          disabled={plan.status === 'confirmed' || isConfirming}
          onClick={async () => {
            setIsConfirming(true)
            setConfirmError(null)
            try {
              await onConfirm()
              await refreshWorkflow()
            } catch (error) {
              setConfirmError(error instanceof Error ? error.message : '确认失败，请稍后重试')
            } finally {
              setIsConfirming(false)
            }
          }}
        >
          {plan.status === 'confirmed' ? '已确认 ✓' : isConfirming ? '确认中…' : '确认旅行计划'}
        </button>
      </div>
      {plan.status === 'confirmed' && (
        <section className="travel-preparation-panel">
          <div className="travel-preparation-heading">
            <div>
              <span className="travel-eyebrow">下一步</span>
              <strong>把行程变成可以执行的准备事项</strong>
              <small>Agent 会生成出发前任务，由你选择后同步到 Todo 分类。</small>
            </div>
            {!preparationPlan && (
              <button
                type="button"
                className="travel-generate-todos-button"
                disabled={isGeneratingPreparations}
                onClick={async () => {
                  setIsGeneratingPreparations(true)
                  setPreparationError(null)
                  try {
                    const generated = await onGeneratePreparations()
                    setPreparationPlan(generated)
                    setSelectedPreparations(generated.steps.map((_step, index) => index))
                    await refreshWorkflow()
                  } catch (error) {
                    setPreparationError(error instanceof Error ? error.message : '准备事项生成失败')
                  } finally {
                    setIsGeneratingPreparations(false)
                  }
                }}
              >
                {isGeneratingPreparations
                  ? 'Agent 正在生成…'
                  : workflow?.stages.find((stage) => stage.key === 'preparations')?.status === 'completed'
                    ? '恢复准备事项'
                    : '生成准备事项'}
              </button>
            )}
          </div>
          {preparationPlan && (
            <div className="travel-preparation-content">
              <div className="travel-preparation-list">
                {preparationPlan.steps.map((step, index) => (
                  <label
                    className={selectedPreparations.includes(index) ? 'is-selected' : ''}
                    key={`${step.title}-${index}`}
                  >
                    <input
                      type="checkbox"
                      checked={selectedPreparations.includes(index)}
                      disabled={syncedPreparationCount !== null}
                      onChange={() => setSelectedPreparations((current) =>
                        current.includes(index)
                          ? current.filter((item) => item !== index)
                          : [...current, index].sort((left, right) => left - right),
                      )}
                    />
                    <span>
                      <strong>{step.title}</strong>
                      <small>{step.description} · 约 {step.minutes} 分钟</small>
                    </span>
                  </label>
                ))}
              </div>
              <div className="travel-preparation-actions">
                <span>{syncedPreparationCount === null ? `已选择 ${selectedPreparations.length} 项` : `已同步 ${syncedPreparationCount} 项到 Todo`}</span>
                <button
                  type="button"
                  disabled={selectedPreparations.length === 0 || isSyncingPreparations || syncedPreparationCount !== null}
                  onClick={async () => {
                    setIsSyncingPreparations(true)
                    setPreparationError(null)
                    try {
                      const count = await onSyncPreparations(
                        selectedPreparations.map((index) => preparationPlan.steps[index]),
                      )
                      setSyncedPreparationCount(count)
                      await refreshWorkflow()
                    } catch (error) {
                      setPreparationError(error instanceof Error ? error.message : '同步 Todo 失败')
                    } finally {
                      setIsSyncingPreparations(false)
                    }
                  }}
                >
                  {syncedPreparationCount !== null ? '同步完成 ✓' : isSyncingPreparations ? '同步中…' : '同步到 Todo'}
                </button>
              </div>
            </div>
          )}
          {preparationError && <span className="travel-confirm-error">{preparationError}</span>}
        </section>
      )}
      <p className="travel-card-note">计划已保存。价格、开放时间和交通信息请在出行前再次确认。</p>
    </div>
  )
}
