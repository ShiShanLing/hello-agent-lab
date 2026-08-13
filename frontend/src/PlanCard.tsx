import { useState } from 'react'

export type GoalPlan = {
  title: string
  summary: string
  priority: 'low' | 'medium' | 'high'
  steps: Array<{
    title: string
    description: string
    minutes: number
  }>
}

type PlanCardProps = {
  plan: GoalPlan
  onSyncTodos: (steps: GoalPlan['steps']) => Promise<number>
}

const PRIORITY_LABELS = {
  low: '低优先级',
  medium: '中优先级',
  high: '高优先级',
} as const

export function PlanCard({ plan, onSyncTodos }: PlanCardProps) {
  const totalMinutes = plan.steps.reduce((total, step) => total + step.minutes, 0)
  const [selectedSteps, setSelectedSteps] = useState(() =>
    plan.steps.map((_step, index) => index),
  )
  const [isSyncing, setIsSyncing] = useState(false)
  const [syncedCount, setSyncedCount] = useState<number | null>(null)

  const toggleStep = (index: number) => {
    setSelectedSteps((current) =>
      current.includes(index)
        ? current.filter((item) => item !== index)
        : [...current, index].sort((left, right) => left - right),
    )
  }

  const syncSelectedSteps = async () => {
    if (selectedSteps.length === 0 || isSyncing || syncedCount !== null) return
    setIsSyncing(true)
    try {
      const count = await onSyncTodos(
        selectedSteps.map((index) => plan.steps[index]),
      )
      setSyncedCount(count)
    } catch {
      // 错误信息由聊天页统一展示，计划保留为可重试状态。
    } finally {
      setIsSyncing(false)
    }
  }

  return (
    <div className="message-content plan-card">
      <div className="plan-card-heading">
        <div>
          <span className="plan-eyebrow">结构化计划</span>
          <h2>{plan.title}</h2>
        </div>
        <span className={`priority-badge priority-${plan.priority}`}>
          {PRIORITY_LABELS[plan.priority]}
        </span>
      </div>
      <p className="plan-summary">{plan.summary}</p>
      <div className="plan-total">预计总用时 {totalMinutes} 分钟</div>
      <ol className="plan-steps">
        {plan.steps.map((step, index) => (
          <li
            className={selectedSteps.includes(index) ? 'is-selected' : ''}
            key={`${step.title}-${index}`}
          >
            <label className="plan-step-selector">
              <input
                checked={selectedSteps.includes(index)}
                disabled={syncedCount !== null}
                onChange={() => toggleStep(index)}
                type="checkbox"
              />
              <span className="plan-step-number">{index + 1}</span>
            </label>
            <div>
              <div className="plan-step-title">
                <strong>{step.title}</strong>
                <span>{step.minutes} 分钟</span>
              </div>
              <p>{step.description}</p>
            </div>
          </li>
        ))}
      </ol>
      <div className="plan-actions">
        <span>
          {syncedCount === null
            ? `已选择 ${selectedSteps.length} 项，确认后才会修改 Todo`
            : `已同步为 1 个计划，共 ${syncedCount} 个步骤`}
        </span>
        <button
          className={syncedCount === null ? 'plan-sync-button' : 'plan-synced-button'}
          disabled={selectedSteps.length === 0 || isSyncing || syncedCount !== null}
          onClick={() => void syncSelectedSteps()}
          type="button"
        >
          {isSyncing ? '正在同步…' : syncedCount === null ? '确认同步到 Todo' : '同步完成 ✓'}
        </button>
      </div>
    </div>
  )
}
