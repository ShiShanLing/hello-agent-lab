import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { FormEvent } from 'react'
import './App.css'
import projectIcon from './assets/project-icon-v1-optimized.png'
import { AuthScreen } from './AuthScreen'
import type { AuthUser } from './AuthScreen'
import { MarkdownMessage } from './MarkdownMessage'
import type { CitationSource } from './MarkdownMessage'
import { PlanCard } from './PlanCard'
import type { GoalPlan } from './PlanCard'
import { TravelPlanCard } from './TravelPlanCard'
import type { TravelPlan, TravelWorkflow } from './TravelPlanCard'
import { consumeSafeNextRedirect } from './postLoginRedirect'

const WorkflowStudio = lazy(() =>
  import('./WorkflowStudio').then((module) => ({ default: module.WorkflowStudio })),
)
const MetricsDashboard = lazy(() =>
  import('./MetricsDashboard').then((module) => ({ default: module.MetricsDashboard })),
)

type ChatMessage = {
  id: number
  role: 'user' | 'assistant' | 'notice'
  content: string
  plan?: GoalPlan
  travelPlan?: TravelPlan
  travelPlanId?: number
  activities?: ToolActivity[]
  operation?: string
  sources?: CitationSource[]
  confidence?: string
  grounding?: string
  capturedRegression?: boolean
}

type ToolActivity = {
  call_id: string
  tool_name: string
  source: 'local' | 'mcp' | 'skill' | 'openapi'
  status: 'calling' | 'completed' | 'failed'
  message: string
}

type StreamEvent =
  | { type: 'session'; session_id: string }
  | { type: 'delta'; content: string }
  | ({ type: 'tool_activity' } & ToolActivity)
  | {
      type: 'done'
      approval_required: boolean
      sources?: CitationSource[]
      confidence?: string
      grounding?: string
      captured_regression?: boolean
    }
  | { type: 'error'; message: string }

type HistoryResponse = {
  session_id: string
  messages: Array<
    Pick<ChatMessage, 'role' | 'content'> & {
      operation_type?: string | null
      sources?: CitationSource[]
      confidence?: string | null
      grounding?: string | null
    }
  >
}

type SessionSummary = {
  session_id: string
  title: string
  preview: string
  message_count: number
}

type SessionListResponse = {
  sessions: SessionSummary[]
}

type TravelPlanSummary = {
  id: number
  session_id: string
  status: TravelPlan['status']
  title: string
  summary: string
  origin: string | null
  destination: string | null
  start_date: string | null
  end_date: string | null
  updated_at: string
  version: number
  workflow_completed_count: number
  workflow_total_count: number
}

type SavedTravelPlan = TravelPlanSummary & TravelPlan

type SlashCommand = {
  command: string
  label: string
  description: string
  prompt: string
  mode?: 'chat' | 'plan' | 'travel' | 'collaboration'
  requiresInput?: boolean
}

const SLASH_COMMANDS: SlashCommand[] = [
  {
    command: '/计算',
    label: '精确计算',
    description: '调用计算器工具处理数学表达式',
    prompt: '请精确计算：',
  },
  {
    command: '/天气',
    label: '查询天气',
    description: '查询中国省、市、区县的实时天气和未来预报',
    prompt: '请查询中国地区天气：',
  },
  {
    command: '/添加任务',
    label: '添加 Todo',
    description: '创建一条新的待办事项',
    prompt: '帮我添加一个任务：',
  },
  {
    command: '/查看任务',
    label: '查看 Todo',
    description: '列出当前会话中的待办事项',
    prompt: '查看我的待办事项',
    requiresInput: false,
  },
  {
    command: '/完成任务',
    label: '完成 Todo',
    description: '标记指定任务，执行前会请求确认',
    prompt: '完成任务 ',
  },
  {
    command: '/制定计划',
    label: '制定计划',
    description: '把目标拆成结构化的可执行步骤',
    prompt: '我想完成的目标是：',
    mode: 'plan',
  },
  {
    command: '/旅行规划',
    label: '规划旅行',
    description: '收集旅行需求并生成可保存的逐日行程',
    prompt: '请为我规划旅行：',
    mode: 'travel',
  },
  {
    command: '/多智能体',
    label: '多 Agent 协作',
    description: '也可不选命令：复杂目标会自动走协作；简单题仍单 Agent',
    prompt: '请通过多 Agent 协作完成：',
    mode: 'collaboration',
  },
  {
    command: '/知识库',
    label: '查询本地资料',
    description: '从 knowledge 目录中的 Markdown/TXT 搜索答案',
    prompt: '请根据本地知识库回答：',
  },
  {
    command: '/知识文件',
    label: '查看知识文件',
    description: '列出当前知识库可以搜索的资料',
    prompt: '请列出本地知识库中的文件。',
    requiresInput: false,
  },
  {
    command: '/记忆',
    label: '长期记忆',
    description: '查看跨会话保存的个人偏好、事实和约束',
    prompt: '请列出你记住的关于我的长期记忆。',
    requiresInput: false,
  },
  {
    command: '/解释代码',
    label: '解释代码',
    description: '分析代码的作用和执行流程',
    prompt: '请解释下面这段代码：\n\n',
  },
  {
    command: '/帮助',
    label: '查看帮助',
    description: '了解当前 Agent 可以完成的操作',
    prompt: '请介绍你目前可以帮我完成哪些操作。',
    requiresInput: false,
  },
]

type PlanResponse = {
  session_id: string
  plan: GoalPlan
}

type TravelPlanResponse = {
  session_id: string
  travel_plan_id: number
  version: number
  plan: TravelPlan
}

type TravelPreparationResponse = {
  travel_plan_id: number
  plan: GoalPlan
}

type PendingTravelTask = {
  id: number
  version: number
  plan: TravelPlan
}

type PlanTodoSyncResponse = {
  session_id: string
  plan: {
    id: number
    title: string
    todos: Array<{ id: number; title: string; completed: boolean }>
  }
}

type MCPTool = {
  name: string
  description: string
  parameters: {
    properties?: Record<string, { type?: string; description?: string }>
    required?: string[]
  }
}

type MCPServer = {
  id: string | null
  name: string
  transport: string
  builtin: boolean
  enabled: boolean
  url: string | null
  has_auth: boolean
  status: 'ready' | 'error' | 'disabled'
  error_message: string | null
  tools: MCPTool[]
}

type MCPToolsResponse = {
  servers: MCPServer[]
}

type OpenApiOperation = {
  operation_id: string
  tool_name: string
  method: string
  path: string
  summary: string
  unsafe: boolean
  write: boolean
}

type OpenApiTool = {
  name: string
  description: string
  parameters: {
    properties?: Record<string, { type?: string; description?: string }>
    required?: string[]
  }
  method?: string | null
  path?: string | null
  operation_id?: string | null
  unsafe?: boolean
}

type OpenApiSource = {
  id: string
  name: string
  base_url: string
  enabled: boolean
  has_auth: boolean
  auth_header: string
  selected_operations: string[]
  status: 'ready' | 'error' | 'disabled'
  error_message: string | null
  tools: OpenApiTool[]
}

type OpenApiSourcesResponse = {
  sources: OpenApiSource[]
}

type OpenApiPreviewResponse = {
  name: string
  title: string
  openapi_version: string
  base_url: string
  operation_count: number
  operations: OpenApiOperation[]
}

type AgentSkill = {
  name: string
  description: string
  body?: string
}

type SkillsResponse = {
  skills: AgentSkill[]
}

type KnowledgeDocument = {
  id: string
  original_name: string
  category: string
  file_type: string
  size_bytes: number
  status: string
  chunk_count: number
  embedding_progress: number
  embedding_model: string | null
  indexed_at: string | null
  error_message: string | null
  created_at: string
}

type KnowledgeDocumentListResponse = { documents: KnowledgeDocument[] }

type ChatAttachment = {
  id: string
  session_id: string
  original_name: string
  file_type: string
  size_bytes: number
  char_count: number
  preview: string
  extraction_method?: string
  created_at: string
}

type ChatAttachmentListResponse = {
  session_id: string
  attachments: ChatAttachment[]
  max_attachments: number
}

type KnowledgeSearchResult = {
  source: string
  chunk: number
  content: string
  score: number
  keyword_score: number | null
  semantic_score: number | null
  retrieval: string
}

type MemoryCategory = 'preference' | 'fact' | 'goal' | 'constraint'

type UserMemory = {
  id: string
  category: MemoryCategory
  content: string
  source_session_id: string | null
  created_at: string
  updated_at: string
}

type MemoryListResponse = {
  memories: UserMemory[]
  total: number
}

type KnowledgeSearchResponse = {
  query: string
  confidence: 'high' | 'medium' | 'low'
  retrieval_mode: 'hybrid' | 'keyword'
  message: string | null
  results: KnowledgeSearchResult[]
}

type BackgroundTask = {
  id: string
  task_type: string
  title: string
  status: 'queued' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  payload: Record<string, unknown>
  error_message: string | null
  retry_count: number
  max_retries: number
  created_at: string
  started_at: string | null
  finished_at: string | null
}

type TaskNotification = {
  id: string
  task_id: string | null
  level: 'info' | 'success' | 'warning' | 'error'
  title: string
  message: string
  is_read: boolean
  created_at: string
}

type TaskCenterResponse = {
  tasks: BackgroundTask[]
  notifications: TaskNotification[]
  unread_count: number
}

type AutomationSettings = {
  daily_todo_briefing_enabled: boolean
  daily_todo_briefing_hour: number
  last_daily_todo_briefing_on: string | null
  timezone: string
  scheduler_enabled: boolean
}

type AgentRunSummary = {
  id: string
  session_id: string | null
  run_type: string
  title: string
  status: 'running' | 'success' | 'failed'
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  estimated_cost: number | null
  duration_ms: number | null
  error_message: string | null
  started_at: string
  finished_at: string | null
}

type AgentRunDetail = AgentRunSummary & {
  steps: Array<{
    id: number
    step_type: string
    name: string
    status: string
    duration_ms: number
    detail: string
    created_at: string
  }>
}

type EvaluationRunSummary = {
  id: string
  model: string
  prompt_version_id: string | null
  prompt_version_name: string | null
  dataset_id: string | null
  dataset_name: string | null
  scorer: 'keyword' | 'exact' | 'llm_judge'
  judge_enabled: boolean
  confidence: 'high' | 'medium' | 'low'
  baseline_run_id: string | null
  regression_summary: Record<string, number>
  export_metadata: Record<string, unknown>
  status: 'queued' | 'running' | 'completed' | 'failed'
  total_cases: number
  passed_cases: number
  score: number
  total_tokens: number
  estimated_cost: number | null
  duration_ms: number | null
  started_at: string
  finished_at: string | null
}

type PromptVersion = {
  id: string; version: number; name: string; content: string; change_note: string
  status: 'draft' | 'active' | 'archived'; created_at: string; activated_at: string | null
}

type EvaluationTestCase = {
  id: string; dataset_id: string; name: string; category: string; input_text: string
  scoring_method: 'keyword' | 'exact' | 'llm_judge'
  expected_answer: string | null
  judge_rubric: string | null
  expected_keywords: string[]; enabled: boolean; created_at: string; updated_at: string
}

type EvaluationDataset = {
  id: string; name: string; description: string; is_default: boolean
  created_at: string; updated_at: string; cases: EvaluationTestCase[]
}

type EvaluationRunDetail = EvaluationRunSummary & {
  cases: Array<{
    id: number
    case_id: string
    name: string
    category: string
    status: 'passed' | 'failed'
    duration_ms: number
    score: number
    scorer: string
    confidence: 'high' | 'medium' | 'low'
    failure_type: string | null
    failure_reason: string | null
    judge_score: number | null
    judge_summary: string | null
    judge_reasoning: string | null
    signals: Record<string, unknown>
    baseline_status: 'passed' | 'failed' | null
    regression_label: 'new_failure' | 'fixed' | 'persistent_failure' | 'persistent_pass' | null
    expected: string
    actual: string
    error_message: string | null
  }>
}

type EvaluationComparison = {
  current_run_id: string
  baseline_run_id: string
  score_delta: number
  confidence_delta: number
  summary: Record<string, number>
  cases: Array<{
    case_id: string
    name: string
    current_status: 'passed' | 'failed' | null
    baseline_status: 'passed' | 'failed' | null
    current_score: number | null
    baseline_score: number | null
    regression_label: 'new_failure' | 'fixed' | 'persistent_failure' | 'persistent_pass' | 'added' | null
    failure_reason: string | null
  }>
}

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ??
  (import.meta.env.PROD ? '/agent/api' : '/api')
const TODO_APP_URL =
  import.meta.env.VITE_TODO_APP_URL ??
  (import.meta.env.PROD
    ? '/agent/todo/'
    : `${window.location.protocol}//${window.location.hostname}:5174`)
const SESSION_STORAGE_KEY = 'hello-agent-session-id'
const TRAVEL_COMMAND = SLASH_COMMANDS.find(
  (command) => command.mode === 'travel',
)!
const KNOWLEDGE_CATEGORIES = ['未分类', '技术资料', '产品资料', '项目资料', '个人笔记']
const MEMORY_CATEGORIES: MemoryCategory[] = ['preference', 'fact', 'goal', 'constraint']
const MEMORY_CATEGORY_LABELS: Record<MemoryCategory, string> = {
  preference: '偏好',
  fact: '事实',
  goal: '目标',
  constraint: '约束',
}

const OPERATION_TYPES: Record<string, { type: string; label: string }> = {
  '/计算': { type: 'calculator', label: '精确计算' },
  '/天气': { type: 'weather', label: '查询天气' },
  '/添加任务': { type: 'todo_add', label: '添加 Todo' },
  '/查看任务': { type: 'todo_list', label: '查看 Todo' },
  '/完成任务': { type: 'todo_complete', label: '完成 Todo' },
  '/制定计划': { type: 'plan', label: '制定计划' },
  '/旅行规划': { type: 'travel', label: '旅行规划' },
  '/多智能体': { type: 'collaboration', label: '多 Agent 协作' },
  '/知识库': { type: 'knowledge_search', label: '查询本地资料' },
  '/知识文件': { type: 'knowledge_files', label: '查看知识文件' },
  '/记忆': { type: 'memory_list', label: '查看长期记忆' },
  '/解释代码': { type: 'code_explain', label: '解释代码' },
  '/帮助': { type: 'help', label: '查看帮助' },
}

const OPERATION_LABELS = Object.fromEntries(
  Object.values(OPERATION_TYPES).map(({ type, label }) => [type, label]),
) as Record<string, string>

const LEGACY_OPERATION_PREFIXES = [
  { prefix: '请为我制定计划：', label: '制定计划' },
  ...SLASH_COMMANDS.map((command) => ({
    prefix: command.prompt,
    label: command.label,
  })),
].sort((left, right) => right.prefix.length - left.prefix.length)

function restoreHistoryMessage(
  message: Pick<ChatMessage, 'role' | 'content'> & {
    operation_type?: string | null
    sources?: CitationSource[]
    confidence?: string | null
    grounding?: string | null
  },
  id: number,
): ChatMessage {
  if (message.role !== 'user') {
    return {
      id,
      role: message.role,
      content: message.content,
      sources: message.sources ?? [],
      confidence: message.confidence ?? undefined,
      grounding: message.grounding ?? undefined,
    }
  }
  if (message.operation_type) {
    return {
      id,
      role: message.role,
      content: message.content,
      operation: OPERATION_LABELS[message.operation_type] ?? message.operation_type,
    }
  }
  const matched = LEGACY_OPERATION_PREFIXES.find(({ prefix }) =>
    message.content === prefix || message.content.startsWith(prefix),
  )
  if (!matched) return { id, role: message.role, content: message.content }
  const content = message.content
    .slice(matched.prefix.length)
    .replace(/^\s*用户补充：\s*/, '')
    .trim()
  return {
    id,
    role: message.role,
    content: content || matched.label,
    operation: matched.label,
  }
}

function formatKnowledgeBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function formatTaskTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function evaluationConfidenceLabel(value: 'high' | 'medium' | 'low') {
  return value === 'high' ? '高' : value === 'medium' ? '中' : '低'
}

function evaluationScorerLabel(value: 'keyword' | 'exact' | 'llm_judge') {
  if (value === 'exact') return '精确匹配'
  if (value === 'llm_judge') return '大模型裁判'
  return '关键词'
}

function regressionLabelText(value: string | null) {
  if (value === 'new_failure') return '新退化'
  if (value === 'fixed') return '已修复'
  if (value === 'persistent_failure') return '持续失败'
  if (value === 'persistent_pass') return '持续通过'
  if (value === 'added') return '新增用例'
  return '未对比'
}

function ToolActivityTrace({ activities }: { activities: ToolActivity[] }) {
  return (
    <div className="tool-trace" aria-label="Agent 执行过程">
      <div className="tool-trace-heading">
        <span>执行过程</span>
        <small>Agent Tool Calling</small>
      </div>
      {activities.map((activity) => (
        <div
          className={`tool-trace-item tool-trace-${activity.status}`}
          key={activity.call_id}
        >
          <span className="tool-trace-status" aria-hidden="true">
            {activity.status === 'completed'
              ? '✓'
              : activity.status === 'failed'
                ? '!'
                : ''}
          </span>
          <div>
            <div className="tool-trace-name">
              <code>{activity.tool_name}</code>
              <span>
                {activity.source === 'mcp'
                  ? 'MCP'
                  : activity.source === 'skill'
                    ? 'Skill'
                    : activity.source === 'openapi'
                      ? 'OpenAPI'
                      : '本地'}
              </span>
            </div>
            <p>{activity.message}</p>
          </div>
        </div>
      ))}
    </div>
  )
}

function App() {
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null)
  const [isCheckingAuth, setIsCheckingAuth] = useState(true)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 1,
      role: 'assistant',
      content:
        '你好，我是你的 AI Agent。我会优先检索本地知识库，不足时联网搜索兜底；也可以计算、跑短 Python、查中国天气和管理 Todo。',
    },
  ])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(() =>
    localStorage.getItem(SESSION_STORAGE_KEY),
  )
  const [pendingApproval, setPendingApproval] = useState<string | null>(null)
  const [pendingCollaboration, setPendingCollaboration] = useState<{
    id: string
    messageId: number
  } | null>(null)
  const [composerMode, setComposerMode] = useState<'chat' | 'plan' | 'travel' | 'collaboration'>('chat')
  const [activeCommand, setActiveCommand] = useState<SlashCommand | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isWaitingForFirstToken, setIsWaitingForFirstToken] = useState(false)
  const [streamingMessageId, setStreamingMessageId] = useState<number | null>(null)
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0)
  const [isCommandMenuDismissed, setIsCommandMenuDismissed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isFeatureMenuOpen, setIsFeatureMenuOpen] = useState(false)
  const [isMCPPanelOpen, setIsMCPPanelOpen] = useState(false)
  const [isLoadingMCPTools, setIsLoadingMCPTools] = useState(false)
  const [isSavingRemoteMcp, setIsSavingRemoteMcp] = useState(false)
  const [mcpServers, setMCPServers] = useState<MCPServer[]>([])
  const [mcpError, setMCPError] = useState<string | null>(null)
  const [remoteMcpName, setRemoteMcpName] = useState('')
  const [remoteMcpUrl, setRemoteMcpUrl] = useState('')
  const [remoteMcpTransport, setRemoteMcpTransport] = useState<'streamable_http' | 'sse'>('streamable_http')
  const [remoteMcpToken, setRemoteMcpToken] = useState('')
  const [isOpenApiPanelOpen, setIsOpenApiPanelOpen] = useState(false)
  const [isLoadingOpenApi, setIsLoadingOpenApi] = useState(false)
  const [isSavingOpenApi, setIsSavingOpenApi] = useState(false)
  const [isPreviewingOpenApi, setIsPreviewingOpenApi] = useState(false)
  const [openApiSources, setOpenApiSources] = useState<OpenApiSource[]>([])
  const [openApiError, setOpenApiError] = useState<string | null>(null)
  const [openApiName, setOpenApiName] = useState('')
  const [openApiBaseUrl, setOpenApiBaseUrl] = useState('')
  const [openApiSpec, setOpenApiSpec] = useState('')
  const [openApiToken, setOpenApiToken] = useState('')
  const [openApiPreview, setOpenApiPreview] = useState<OpenApiPreviewResponse | null>(null)
  const [openApiSelectedOps, setOpenApiSelectedOps] = useState<string[]>([])
  const [isSkillsPanelOpen, setIsSkillsPanelOpen] = useState(false)
  const [isLoadingSkills, setIsLoadingSkills] = useState(false)
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([])
  const [selectedSkillName, setSelectedSkillName] = useState<string | null>(null)
  const [skillsError, setSkillsError] = useState<string | null>(null)
  const [isMemoryPanelOpen, setIsMemoryPanelOpen] = useState(false)
  const [isLoadingMemories, setIsLoadingMemories] = useState(false)
  const [isSavingMemory, setIsSavingMemory] = useState(false)
  const [userMemories, setUserMemories] = useState<UserMemory[]>([])
  const [memoryFilter, setMemoryFilter] = useState<'全部' | MemoryCategory>('全部')
  const [memoryDraftCategory, setMemoryDraftCategory] = useState<MemoryCategory>('preference')
  const [memoryDraftContent, setMemoryDraftContent] = useState('')
  const [memoryError, setMemoryError] = useState<string | null>(null)
  const [isKnowledgePanelOpen, setIsKnowledgePanelOpen] = useState(false)
  const [isLoadingKnowledge, setIsLoadingKnowledge] = useState(false)
  const [isUploadingKnowledge, setIsUploadingKnowledge] = useState(false)
  const [knowledgeDocuments, setKnowledgeDocuments] = useState<KnowledgeDocument[]>([])
  const [knowledgeCategory, setKnowledgeCategory] = useState('未分类')
  const [knowledgeFilter, setKnowledgeFilter] = useState('全部')
  const [knowledgeError, setKnowledgeError] = useState<string | null>(null)
  const [knowledgeSearchQuery, setKnowledgeSearchQuery] = useState('')
  const [knowledgeSearchResult, setKnowledgeSearchResult] = useState<KnowledgeSearchResponse | null>(null)
  const [isSearchingKnowledge, setIsSearchingKnowledge] = useState(false)
  const [chatAttachments, setChatAttachments] = useState<ChatAttachment[]>([])
  const [isUploadingAttachment, setIsUploadingAttachment] = useState(false)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [isTaskCenterOpen, setIsTaskCenterOpen] = useState(false)
  const [taskCenterTab, setTaskCenterTab] = useState<'tasks' | 'notifications'>('tasks')
  const [backgroundTasks, setBackgroundTasks] = useState<BackgroundTask[]>([])
  const [taskNotifications, setTaskNotifications] = useState<TaskNotification[]>([])
  const [unreadNotificationCount, setUnreadNotificationCount] = useState(0)
  const [taskCenterError, setTaskCenterError] = useState<string | null>(null)
  const [automationSettings, setAutomationSettings] = useState<AutomationSettings | null>(null)
  const [isSavingAutomationSettings, setIsSavingAutomationSettings] = useState(false)
  const [isRunningDailyBriefing, setIsRunningDailyBriefing] = useState(false)
  const [isHistoryPanelOpen, setIsHistoryPanelOpen] = useState(false)
  const [isLoadingSessions, setIsLoadingSessions] = useState(false)
  const [historicalSessions, setHistoricalSessions] = useState<SessionSummary[]>([])
  const [historyPanelError, setHistoryPanelError] = useState<string | null>(null)
  const [isTravelPanelOpen, setIsTravelPanelOpen] = useState(false)
  const [isLoadingTravelPlans, setIsLoadingTravelPlans] = useState(false)
  const [savedTravelPlans, setSavedTravelPlans] = useState<TravelPlanSummary[]>([])
  const [travelPanelError, setTravelPanelError] = useState<string | null>(null)
  const [isObservabilityOpen, setIsObservabilityOpen] = useState(false)
  const [isMetricsOpen, setIsMetricsOpen] = useState(false)
  const [isLoadingRuns, setIsLoadingRuns] = useState(false)
  const [agentRuns, setAgentRuns] = useState<AgentRunSummary[]>([])
  const [selectedAgentRun, setSelectedAgentRun] = useState<AgentRunDetail | null>(null)
  const [observabilityError, setObservabilityError] = useState<string | null>(null)
  const [isEvaluationOpen, setIsEvaluationOpen] = useState(false)
  const [isWorkflowStudioOpen, setIsWorkflowStudioOpen] = useState(false)
  const [isLoadingEvaluations, setIsLoadingEvaluations] = useState(false)
  const [isRunningEvaluation, setIsRunningEvaluation] = useState(false)
  const [evaluationRuns, setEvaluationRuns] = useState<EvaluationRunSummary[]>([])
  const [selectedEvaluation, setSelectedEvaluation] = useState<EvaluationRunDetail | null>(null)
  const [evaluationError, setEvaluationError] = useState<string | null>(null)
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([])
  const [evaluationDatasets, setEvaluationDatasets] = useState<EvaluationDataset[]>([])
  const [selectedPromptId, setSelectedPromptId] = useState('')
  const [selectedDatasetId, setSelectedDatasetId] = useState('')
  const [evaluationModel, setEvaluationModel] = useState('deepseek-v4-flash')
  const [evaluationScorer, setEvaluationScorer] = useState<'keyword' | 'exact' | 'llm_judge'>('llm_judge')
  const [evaluationJudgeEnabled, setEvaluationJudgeEnabled] = useState(true)
  const [selectedBaselineRunId, setSelectedBaselineRunId] = useState('')
  const [evaluationComparison, setEvaluationComparison] = useState<EvaluationComparison | null>(null)
  const [evaluationTab, setEvaluationTab] = useState<'results' | 'prompts' | 'dataset'>('results')
  const [newPromptName, setNewPromptName] = useState('')
  const [newPromptContent, setNewPromptContent] = useState('')
  const [newCaseName, setNewCaseName] = useState('')
  const [newCaseInput, setNewCaseInput] = useState('')
  const [newCaseKeywords, setNewCaseKeywords] = useState('')
  const [newCaseScoringMethod, setNewCaseScoringMethod] = useState<'keyword' | 'exact' | 'llm_judge'>('keyword')
  const [newCaseExpectedAnswer, setNewCaseExpectedAnswer] = useState('')
  const [newCaseJudgeRubric, setNewCaseJudgeRubric] = useState('')
  const [pendingTravelTask, setPendingTravelTask] = useState<PendingTravelTask | null>(null)
  const nextMessageId = useRef(2)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const textarea = inputRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    const nextHeight = Math.min(textarea.scrollHeight, 88)
    textarea.style.height = `${nextHeight}px`
    textarea.style.overflowY = textarea.scrollHeight > 88 ? 'auto' : 'hidden'
  }, [input, activeCommand])

  useEffect(() => {
    const controller = new AbortController()
    const restoreLogin = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/auth/me`, {
          credentials: 'include',
          signal: controller.signal,
        })
        if (response.ok) {
          setCurrentUser((await response.json()) as AuthUser)
        } else if (response.status !== 401) {
          const data = await response.json()
          throw new Error(data.detail ?? '检查登录状态失败')
        }
      } catch (requestError) {
        if (!(requestError instanceof DOMException && requestError.name === 'AbortError')) {
          setError(
            requestError instanceof Error ? requestError.message : '检查登录状态失败',
          )
        }
      } finally {
        setIsCheckingAuth(false)
      }
    }
    void restoreLogin()
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!currentUser) return
    consumeSafeNextRedirect()
  }, [currentUser])

  useEffect(() => {
    if (!currentUser) return
    let active = true
    const applyTaskCenter = (data: TaskCenterResponse) => {
      if (!active) return
      setBackgroundTasks(data.tasks)
      setTaskNotifications(data.notifications)
      setUnreadNotificationCount(data.unread_count)
    }
    void fetch(`${API_BASE_URL}/tasks`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? '读取任务中心失败')
        applyTaskCenter(data as TaskCenterResponse)
      })
      .catch((requestError) => {
        if (active) setTaskCenterError(requestError instanceof Error ? requestError.message : '读取任务中心失败')
      })
    const events = new EventSource(`${API_BASE_URL}/tasks/events`, { withCredentials: true })
    events.onmessage = (event) => {
      try {
        applyTaskCenter(JSON.parse(event.data) as TaskCenterResponse)
        setTaskCenterError(null)
      } catch {
        setTaskCenterError('任务状态推送格式不正确。')
      }
    }
    return () => {
      active = false
      events.close()
    }
  }, [currentUser])

  useEffect(() => {
    if (!currentUser) {
      setAutomationSettings(null)
      return
    }
    let active = true
    void fetch(`${API_BASE_URL}/automations/settings`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? '读取自动化设置失败')
        if (active) setAutomationSettings(data as AutomationSettings)
      })
      .catch((requestError) => {
        if (active) setTaskCenterError(requestError instanceof Error ? requestError.message : '读取自动化设置失败')
      })
    return () => { active = false }
  }, [currentUser])

  useEffect(() => {
    if (!isEvaluationOpen || !selectedEvaluation || !['queued', 'running'].includes(selectedEvaluation.status)) return
    if (!backgroundTasks.some((task) => task.task_type === 'evaluation_run')) return
    const timer = window.setTimeout(() => void loadEvaluation(selectedEvaluation.id), 350)
    return () => window.clearTimeout(timer)
  }, [backgroundTasks, isEvaluationOpen, selectedEvaluation?.id, selectedEvaluation?.status])

  const slashQuery = input.startsWith('/')
    ? input.slice(1).trim().toLocaleLowerCase()
    : ''
  const visibleCommands = input.startsWith('/')
    ? SLASH_COMMANDS.filter((item) => {
        const searchableText = `${item.command.slice(1)} ${item.label} ${item.description}`
          .toLocaleLowerCase()
        return searchableText.includes(slashQuery)
      })
    : []
  const isCommandMenuOpen =
    input.startsWith('/') && !isCommandMenuDismissed && !isLoading
  const highlightedCommandIndex = Math.min(
    selectedCommandIndex,
    Math.max(visibleCommands.length - 1, 0),
  )

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
  }, [messages, isWaitingForFirstToken, pendingApproval])

  useEffect(() => {
    if (!currentUser || !sessionId) {
      setChatAttachments([])
      return
    }
    const controller = new AbortController()
    const loadAttachments = async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/chat/attachments?session_id=${encodeURIComponent(sessionId)}`,
          { credentials: 'include', signal: controller.signal },
        )
        if (!response.ok) {
          const data = await response.json().catch(() => ({}))
          throw new Error(
            typeof data.detail === 'string' ? data.detail : '读取附件失败',
          )
        }
        const data = (await response.json()) as ChatAttachmentListResponse
        setChatAttachments(data.attachments)
        setAttachmentError(null)
      } catch (requestError) {
        if (controller.signal.aborted) return
        setAttachmentError(
          requestError instanceof Error ? requestError.message : '读取附件失败',
        )
      }
    }
    void loadAttachments()
    return () => controller.abort()
  }, [currentUser, sessionId])

  useEffect(() => {
    if (!currentUser) return
    const savedSessionId = localStorage.getItem(SESSION_STORAGE_KEY)
    if (!savedSessionId) return

    const controller = new AbortController()

    const loadHistory = async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/sessions/${savedSessionId}/messages`,
          { credentials: 'include', signal: controller.signal },
        )
        const data = await response.json()
        if (response.status === 403) {
          localStorage.removeItem(SESSION_STORAGE_KEY)
          setSessionId(null)
          return
        }
        if (!response.ok) {
          throw new Error(data.detail ?? '读取历史对话失败')
        }

        const history = data as HistoryResponse
        if (history.messages.length > 0) {
          const restoredMessages = history.messages.map((message) =>
            restoreHistoryMessage(message, nextMessageId.current++),
          )
          setMessages(restoredMessages)
        }
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === 'AbortError') {
          return
        }
        setError(
          requestError instanceof Error
            ? requestError.message
            : '读取历史对话失败',
        )
      }
    }

    void loadHistory()
    return () => controller.abort()
  }, [currentUser])

  useEffect(() => {
    if (!currentUser || !sessionId) {
      setPendingTravelTask(null)
      return
    }
    const controller = new AbortController()
    const restorePendingTravel = async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/travel-plans/pending/${sessionId}`,
          { credentials: 'include', signal: controller.signal },
        )
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? '恢复旅行任务失败')
        if (data === null) {
          setPendingTravelTask(null)
          return
        }
        const result = data as TravelPlanResponse
        setPendingTravelTask({
          id: result.travel_plan_id,
          version: result.version,
          plan: result.plan,
        })
        setActiveCommand(TRAVEL_COMMAND)
        setComposerMode('travel')
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === 'AbortError') return
        setError(
          requestError instanceof Error ? requestError.message : '恢复旅行任务失败',
        )
      }
    }
    void restorePendingTravel()
    return () => controller.abort()
  }, [currentUser, sessionId])

  useEffect(() => {
    setSelectedCommandIndex(0)
  }, [input])

  useEffect(() => {
    if (!isFeatureMenuOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsFeatureMenuOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [isFeatureMenuOpen])

  const appendMessage = (
    role: ChatMessage['role'],
    content: string,
    operation?: string,
  ) => {
    setMessages((current) => [
      ...current,
      { id: nextMessageId.current++, role, content, operation },
    ])
  }

  const selectCommand = (command: SlashCommand) => {
    setInput('')
    setActiveCommand(command)
    setComposerMode(command.mode ?? 'chat')
    setIsCommandMenuDismissed(true)
    requestAnimationFrame(() => {
      const textarea = inputRef.current
      if (!textarea) return
      textarea.focus()
    })
  }

  const callAgent = async (
    message: string,
    approve = false,
    operationType?: string,
  ) => {
    setIsLoading(true)
    setIsWaitingForFirstToken(true)
    setError(null)
    let assistantMessageId: number | null = null

    const appendAssistantDelta = (content: string) => {
      if (assistantMessageId === null) {
        assistantMessageId = nextMessageId.current++
        const id = assistantMessageId
        setStreamingMessageId(id)
        setMessages((current) => [
          ...current,
          { id, role: 'assistant', content },
        ])
        return
      }

      const id = assistantMessageId
      setMessages((current) =>
        current.map((item) =>
          item.id === id
            ? { ...item, content: item.content + content }
            : item,
        ),
      )
    }

    const updateToolActivity = (activity: ToolActivity) => {
      setIsWaitingForFirstToken(false)
      if (assistantMessageId === null) {
        assistantMessageId = nextMessageId.current++
        const id = assistantMessageId
        setStreamingMessageId(id)
        setMessages((current) => [
          ...current,
          { id, role: 'assistant', content: '', activities: [activity] },
        ])
        return
      }

      const id = assistantMessageId
      setMessages((current) =>
        current.map((item) => {
          if (item.id !== id) return item
          const activities = item.activities ?? []
          const exists = activities.some(
            (existing) => existing.call_id === activity.call_id,
          )
          return {
            ...item,
            activities: exists
              ? activities.map((existing) =>
                  existing.call_id === activity.call_id ? activity : existing,
                )
              : [...activities, activity],
          }
        }),
      )
    }

    try {
      const response = await fetch(`${API_BASE_URL}/chat/stream`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          session_id: sessionId,
          approve,
          operation_type: operationType ?? null,
        }),
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail ?? 'Agent 服务调用失败')
      }
      if (!response.body) throw new Error('浏览器不支持流式响应')

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })

        let boundary = buffer.indexOf('\n\n')
        while (boundary !== -1) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const data = block
            .split('\n')
            .filter((line) => line.startsWith('data: '))
            .map((line) => line.slice(6))
            .join('\n')

          if (data) {
            const event = JSON.parse(data) as StreamEvent
            if (event.type === 'session') {
              setSessionId(event.session_id)
              localStorage.setItem(SESSION_STORAGE_KEY, event.session_id)
            } else if (event.type === 'delta') {
              setIsWaitingForFirstToken(false)
              appendAssistantDelta(event.content)
            } else if (event.type === 'tool_activity') {
              updateToolActivity(event)
            } else if (event.type === 'done') {
              setPendingApproval(event.approval_required ? message : null)
              if (assistantMessageId !== null) {
                const id = assistantMessageId
                setMessages((current) =>
                  current.map((item) =>
                    item.id === id
                      ? {
                          ...item,
                          sources: event.sources ?? [],
                          confidence: event.confidence,
                          grounding: event.grounding,
                          capturedRegression: Boolean(event.captured_regression),
                        }
                      : item,
                  ),
                )
              }
            } else if (event.type === 'error') {
              throw new Error(event.message)
            }
          }
          boundary = buffer.indexOf('\n\n')
        }

        if (done) break
      }
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '发生未知错误',
      )
    } finally {
      setIsLoading(false)
      setIsWaitingForFirstToken(false)
      setStreamingMessageId(null)
    }
  }

  const callPlan = async (goal: string) => {
    setIsLoading(true)
    setIsWaitingForFirstToken(true)
    setError(null)

    try {
      const response = await fetch(`${API_BASE_URL}/plans`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal, session_id: sessionId, operation_type: 'plan' }),
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '计划生成失败')
      }

      const result = data as PlanResponse
      setSessionId(result.session_id)
      localStorage.setItem(SESSION_STORAGE_KEY, result.session_id)
      setMessages((current) => [
        ...current,
        {
          id: nextMessageId.current++,
          role: 'assistant',
          content: result.plan.summary,
          plan: result.plan,
        },
      ])
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '计划生成失败',
      )
    } finally {
      setIsLoading(false)
      setIsWaitingForFirstToken(false)
    }
  }

  const callTravelPlan = async (request: string) => {
    setIsLoading(true)
    setIsWaitingForFirstToken(true)
    setError(null)

    try {
      const response = await fetch(`${API_BASE_URL}/travel-plans`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request,
          session_id: sessionId,
          travel_plan_id: pendingTravelTask?.id ?? null,
          operation_type: 'travel',
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '旅行计划生成失败')

      const result = data as TravelPlanResponse
      setSessionId(result.session_id)
      localStorage.setItem(SESSION_STORAGE_KEY, result.session_id)
      if (result.plan.status === 'needs_input') {
        setPendingTravelTask({
          id: result.travel_plan_id,
          version: result.version,
          plan: result.plan,
        })
        setActiveCommand(TRAVEL_COMMAND)
        setComposerMode('travel')
      } else {
        setPendingTravelTask(null)
      }
      setMessages((current) => [
        ...current,
        {
          id: nextMessageId.current++,
          role: 'assistant',
          content: result.plan.summary,
          travelPlan: result.plan,
          travelPlanId: result.travel_plan_id,
        },
      ])
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '旅行计划生成失败',
      )
    } finally {
      setIsLoading(false)
      setIsWaitingForFirstToken(false)
    }
  }

  const confirmTravelPlan = async (travelPlanId: number) => {
    const response = await fetch(
      `${API_BASE_URL}/travel-plans/${travelPlanId}/confirm`,
      { method: 'POST', credentials: 'include' },
    )
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '旅行计划确认失败')

    const result = data as TravelPlanResponse
    setMessages((current) => current.map((message) =>
      message.travelPlanId === travelPlanId && message.travelPlan
        ? { ...message, travelPlan: result.plan }
        : message,
    ))
  }

  const generateTravelPreparations = async (travelPlanId: number) => {
    const response = await fetch(
      `${API_BASE_URL}/travel-plans/${travelPlanId}/preparations`,
      { method: 'POST', credentials: 'include' },
    )
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '准备事项生成失败')
    return (data as TravelPreparationResponse).plan
  }

  const loadTravelWorkflow = async (travelPlanId: number) => {
    const response = await fetch(
      `${API_BASE_URL}/travel-plans/${travelPlanId}/workflow`,
      { credentials: 'include' },
    )
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '工作流状态加载失败')
    return data as TravelWorkflow
  }

  const syncTravelPreparations = async (
    travelPlanId: number,
    selectedSteps: GoalPlan['steps'],
  ) => {
    const response = await fetch(
      `${API_BASE_URL}/travel-plans/${travelPlanId}/todos`,
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_steps: selectedSteps, confirmed: true }),
      },
    )
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '同步 Todo 失败')
    const result = data as PlanTodoSyncResponse
    appendMessage(
      'notice',
      `已创建 Todo 分类“${result.plan.title}”，并同步 ${result.plan.todos.length} 个准备事项`,
    )
    return result.plan.todos.length
  }

  const syncPlanTodos = async (
    plan: GoalPlan,
    selectedSteps: GoalPlan['steps'],
  ) => {
    if (!sessionId) throw new Error('当前计划还没有会话，无法同步 Todo')
    setError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/plans/todos`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          plan,
          selected_steps: selectedSteps,
          confirmed: true,
        }),
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '同步 Todo 失败')
      }

      const result = data as PlanTodoSyncResponse
      appendMessage(
        'notice',
        `已创建计划“${result.plan.title}”，并同步 ${result.plan.todos.length} 个步骤`,
      )
      return result.plan.todos.length
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : '同步 Todo 失败'
      setError(message)
      throw requestError
    }
  }

  const callCollaboration = async (goal: string) => {
    setIsLoading(true)
    setIsWaitingForFirstToken(true)
    setError(null)
    setPendingCollaboration(null)
    const assistantId = nextMessageId.current++
    setStreamingMessageId(assistantId)
    setMessages((current) => [...current, { id: assistantId, role: 'assistant', content: '', activities: [] }])
    const updateActivity = (
      callId: string,
      toolName: string,
      status: 'calling' | 'completed' | 'failed',
      message: string,
      source: ToolActivity['source'] = 'local',
    ) => {
      setIsWaitingForFirstToken(false)
      setMessages((current) => current.map((item) => {
        if (item.id !== assistantId) return item
        const activities = item.activities ?? []
        const exists = activities.some((activity) => activity.call_id === callId)
        const nextActivity = { call_id: callId, tool_name: toolName, source, status, message }
        return {
          ...item,
          activities: exists
            ? activities.map((activity) => activity.call_id === callId ? nextActivity : activity)
            : [...activities, nextActivity],
        }
      }))
    }
    try {
      const response = await fetch(`${API_BASE_URL}/collaborations/stream`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal, session_id: sessionId, operation_type: 'collaboration' }),
      })
      if (!response.ok || !response.body) {
        const data = await response.json()
        throw new Error(data.detail ?? '多 Agent 协作启动失败')
      }
      await consumeCollaborationStream(response.body, assistantId, updateActivity)
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : '多 Agent 协作失败'
      setError(message)
      updateActivity('collaboration-system', '协作流程', 'failed', message)
    } finally {
      setIsLoading(false); setIsWaitingForFirstToken(false); setStreamingMessageId(null)
    }
  }

  const consumeCollaborationStream = async (
    body: ReadableStream<Uint8Array>,
    assistantId: number,
    updateActivity: (
      callId: string,
      toolName: string,
      status: 'calling' | 'completed' | 'failed',
      message: string,
      source?: ToolActivity['source'],
    ) => void,
  ) => {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const events = buffer.split('\n\n')
      buffer = events.pop() ?? ''
      for (const raw of events) {
        const text = raw.replace(/^data:\s*/, '')
        if (!text) continue
        const event = JSON.parse(text) as Record<string, unknown>
        if (event.type === 'session' && typeof event.session_id === 'string') {
          setSessionId(event.session_id)
          localStorage.setItem(SESSION_STORAGE_KEY, event.session_id)
        }
        if (event.type === 'collaboration') {
          const agent = String(event.agent)
          const label = String(event.label)
          const eventStatus = String(event.status)
          const callId = String(event.call_id || event.stage_id || `collaboration-${agent}`)
          if (eventStatus === 'tool_calling' || eventStatus === 'tool_completed' || eventStatus === 'tool_failed') {
            updateActivity(
              callId,
              String(event.tool_name || label),
              eventStatus === 'tool_calling' ? 'calling' : eventStatus === 'tool_failed' ? 'failed' : 'completed',
              `${label} · ${String(event.detail ?? '正在处理')}`,
              event.tool_source === 'mcp'
                ? 'mcp'
                : event.tool_source === 'skill'
                  ? 'skill'
                  : event.tool_source === 'openapi'
                    ? 'openapi'
                    : 'local',
            )
          } else if (eventStatus === 'waiting_approval') {
            updateActivity(
              callId,
              label,
              'completed',
              String(event.detail ?? '计划已就绪，等待确认'),
            )
            if (typeof event.collaboration_id === 'string' && event.collaboration_id) {
              setPendingCollaboration({
                id: event.collaboration_id,
                messageId: assistantId,
              })
            }
          } else {
            updateActivity(
              callId,
              label,
              eventStatus === 'failed' ? 'failed' : eventStatus === 'running' ? 'calling' : 'completed',
              String(event.detail ?? '正在处理'),
            )
          }
          if (
            eventStatus === 'completed'
            && typeof event.content === 'string'
            && event.content.trim()
          ) {
            const sectionTitle =
              agent === 'planner'
                ? '规划'
                : agent === 'executor'
                  ? '执行'
                  : '交付'
            const bodyText = event.content.trim()
            const section = `### ${sectionTitle}\n\n${bodyText}`
            setMessages((current) =>
              current.map((item) => {
                if (item.id !== assistantId) return item
                if (agent === 'reviewer') {
                  return { ...item, content: bodyText }
                }
                const previous = item.content.trim()
                return {
                  ...item,
                  content: previous ? `${previous}\n\n${section}` : section,
                }
              }),
            )
          }
          if (
            eventStatus === 'revision'
            && typeof event.content === 'string'
            && event.content.trim()
          ) {
            const note = `### 审核打回\n\n${event.content.trim()}`
            setMessages((current) =>
              current.map((item) => {
                if (item.id !== assistantId) return item
                const previous = item.content.trim()
                return {
                  ...item,
                  content: previous ? `${previous}\n\n${note}` : note,
                }
              }),
            )
          }
        }
        if (event.type === 'done') {
          if (event.approval_required && typeof event.collaboration_id === 'string') {
            setPendingCollaboration({
              id: event.collaboration_id,
              messageId: assistantId,
            })
          }
        }
        if (event.type === 'error') throw new Error(String(event.message ?? '多 Agent 协作失败'))
      }
      if (done) break
    }
  }

  const approveCollaborationPlan = async () => {
    if (!pendingCollaboration || isLoading) return
    const { id, messageId } = pendingCollaboration
    setPendingCollaboration(null)
    setIsLoading(true)
    setIsWaitingForFirstToken(true)
    setStreamingMessageId(messageId)
    setError(null)
    appendMessage('notice', '你已确认多 Agent 协作计划，开始执行')
    const updateActivity = (
      callId: string,
      toolName: string,
      status: 'calling' | 'completed' | 'failed',
      message: string,
      source: ToolActivity['source'] = 'local',
    ) => {
      setIsWaitingForFirstToken(false)
      setMessages((current) => current.map((item) => {
        if (item.id !== messageId) return item
        const activities = item.activities ?? []
        const exists = activities.some((activity) => activity.call_id === callId)
        const nextActivity = { call_id: callId, tool_name: toolName, source, status, message }
        return {
          ...item,
          activities: exists
            ? activities.map((activity) => activity.call_id === callId ? nextActivity : activity)
            : [...activities, nextActivity],
        }
      }))
    }
    try {
      const response = await fetch(
        `${API_BASE_URL}/collaborations/${id}/approve`,
        { method: 'POST', credentials: 'include' },
      )
      if (!response.ok || !response.body) {
        const data = await response.json().catch(() => ({}))
        throw new Error(
          typeof data.detail === 'string' ? data.detail : '继续协作失败',
        )
      }
      await consumeCollaborationStream(response.body, messageId, updateActivity)
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : '继续协作失败'
      setError(message)
      updateActivity('collaboration-system', '协作流程', 'failed', message)
    } finally {
      setIsLoading(false)
      setIsWaitingForFirstToken(false)
      setStreamingMessageId(null)
    }
  }

  const rejectCollaborationPlan = async () => {
    if (!pendingCollaboration || isLoading) return
    const { id } = pendingCollaboration
    setPendingCollaboration(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/collaborations/${id}/reject`,
        { method: 'POST', credentials: 'include' },
      )
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(
          typeof data.detail === 'string' ? data.detail : '取消协作失败',
        )
      }
      appendMessage('notice', '你取消了多 Agent 协作计划，未继续执行')
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : '取消协作失败',
      )
    }
  }

  const resolveConversationRoute = async (message: string) => {
    const response = await fetch(`${API_BASE_URL}/routing/collaboration`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    })
    const data = await response.json()
    if (!response.ok) {
      throw new Error(data.detail ?? '路由判断失败')
    }
    return data as {
      mode: 'chat' | 'collaboration'
      use_collaboration: boolean
      reason: string
      score: number
    }
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const visibleMessage = input.trim()
    const command = activeCommand
    const needsInput = command?.requiresInput !== false
    if ((!visibleMessage && (!command || needsInput)) || isLoading) return

    const requestMessage = command
      ? command.requiresInput === false
        ? `${command.prompt}${visibleMessage ? `\n用户补充：${visibleMessage}` : ''}`
        : `${command.prompt}${visibleMessage}`
      : visibleMessage
    const displayedMessage = visibleMessage || command?.label || requestMessage
    const operation = command ? OPERATION_TYPES[command.command] : undefined

    setInput('')
    setActiveCommand(null)
    setPendingApproval(null)
    const activeMode = composerMode
    setComposerMode('chat')
    if (activeMode === 'plan') {
      appendMessage('user', displayedMessage, operation?.label)
      await callPlan(visibleMessage)
    } else if (activeMode === 'travel') {
      appendMessage('user', displayedMessage, operation?.label)
      await callTravelPlan(visibleMessage)
    } else if (activeMode === 'collaboration') {
      appendMessage('user', displayedMessage, operation?.label ?? '多 Agent 协作')
      await callCollaboration(visibleMessage)
    } else {
      const userMessageId = nextMessageId.current
      appendMessage('user', displayedMessage, operation?.label)
      try {
        const route = await resolveConversationRoute(requestMessage)
        if (route.use_collaboration) {
          setMessages((current) =>
            current.map((item) =>
              item.id === userMessageId
                ? { ...item, operation: '多 Agent 协作（自动）' }
                : item,
            ),
          )
          await callCollaboration(visibleMessage || requestMessage)
          return
        }
      } catch {
        // 路由失败时回退普通对话，不阻断提问
      }
      await callAgent(requestMessage, false, operation?.type)
    }
  }

  const approvePendingAction = async () => {
    if (!pendingApproval || isLoading) return
    const message = pendingApproval
    setPendingApproval(null)
    appendMessage('notice', '你已确认执行该操作')
    await callAgent(message, true)
  }

  const rejectPendingAction = () => {
    setPendingApproval(null)
    appendMessage('notice', '你保留了数据，操作未执行')
  }

  const startNewSession = () => {
    localStorage.removeItem(SESSION_STORAGE_KEY)
    setSessionId(null)
    setPendingApproval(null)
    setPendingCollaboration(null)
    setInput('')
    setActiveCommand(null)
    setComposerMode('chat')
    setIsCommandMenuDismissed(false)
    setError(null)
    setPendingTravelTask(null)
    setIsHistoryPanelOpen(false)
    setChatAttachments([])
    setAttachmentError(null)
    setMessages([
      {
        id: nextMessageId.current++,
        role: 'assistant',
        content: '新会话已创建。我会优先查询本地资料，今天想了解什么？',
      },
    ])
  }

  const uploadChatAttachments = async (files: FileList | File[]) => {
    if (!currentUser) return
    const selectedFiles = Array.from(files)
    if (selectedFiles.length === 0) return
    setIsUploadingAttachment(true)
    setAttachmentError(null)
    try {
      let activeSessionId = sessionId
      for (const file of selectedFiles) {
        const body = new FormData()
        body.append('file', file)
        if (activeSessionId) {
          body.append('session_id', activeSessionId)
        }
        const response = await fetch(`${API_BASE_URL}/chat/attachments`, {
          method: 'POST',
          credentials: 'include',
          body,
        })
        const data = await response.json()
        if (!response.ok) {
          throw new Error(
            typeof data.detail === 'string'
              ? data.detail
              : `${file.name} 上传失败`,
          )
        }
        const uploaded = data as ChatAttachment
        activeSessionId = uploaded.session_id
        setSessionId(uploaded.session_id)
        localStorage.setItem(SESSION_STORAGE_KEY, uploaded.session_id)
        setChatAttachments((current) => {
          if (current.some((item) => item.id === uploaded.id)) return current
          return [...current, uploaded]
        })
      }
    } catch (requestError) {
      setAttachmentError(
        requestError instanceof Error ? requestError.message : '上传附件失败',
      )
    } finally {
      setIsUploadingAttachment(false)
    }
  }

  const deleteChatAttachment = async (attachmentId: string) => {
    if (!currentUser) return
    setAttachmentError(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/chat/attachments/${attachmentId}`,
        { method: 'DELETE', credentials: 'include' },
      )
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(
          typeof data.detail === 'string' ? data.detail : '删除附件失败',
        )
      }
      setChatAttachments((current) =>
        current.filter((item) => item.id !== attachmentId),
      )
    } catch (requestError) {
      setAttachmentError(
        requestError instanceof Error ? requestError.message : '删除附件失败',
      )
    }
  }

  const openMCPPanel = async () => {
    setIsMCPPanelOpen(true)
    setIsLoadingMCPTools(true)
    setMCPError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/mcp/tools`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '读取 MCP 工具失败')
      }
      setMCPServers((data as MCPToolsResponse).servers)
    } catch (requestError) {
      setMCPError(
        requestError instanceof Error ? requestError.message : '读取 MCP 工具失败',
      )
    } finally {
      setIsLoadingMCPTools(false)
    }
  }

  const createRemoteMcp = async (event: FormEvent) => {
    event.preventDefault()
    if (!remoteMcpName.trim() || !remoteMcpUrl.trim()) return
    setIsSavingRemoteMcp(true)
    setMCPError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/mcp/servers`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: remoteMcpName.trim(),
          url: remoteMcpUrl.trim(),
          transport: remoteMcpTransport,
          auth_token: remoteMcpToken.trim() || null,
          enabled: true,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '添加远程 MCP 失败')
      setRemoteMcpName('')
      setRemoteMcpUrl('')
      setRemoteMcpToken('')
      await openMCPPanel()
    } catch (requestError) {
      setMCPError(requestError instanceof Error ? requestError.message : '添加远程 MCP 失败')
    } finally {
      setIsSavingRemoteMcp(false)
    }
  }

  const toggleRemoteMcp = async (server: MCPServer) => {
    if (!server.id || server.builtin) return
    setMCPError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/mcp/servers/${server.id}`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !server.enabled }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail ?? '更新远程 MCP 失败')
      await openMCPPanel()
    } catch (requestError) {
      setMCPError(requestError instanceof Error ? requestError.message : '更新远程 MCP 失败')
    }
  }

  const deleteRemoteMcp = async (server: MCPServer) => {
    if (!server.id || server.builtin) return
    if (!window.confirm(`确定删除远程 MCP「${server.name}」？`)) return
    setMCPError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/mcp/servers/${server.id}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: '删除失败' }))
        throw new Error(data.detail ?? '删除失败')
      }
      await openMCPPanel()
    } catch (requestError) {
      setMCPError(requestError instanceof Error ? requestError.message : '删除远程 MCP 失败')
    }
  }

  const openOpenApiPanel = async () => {
    setIsOpenApiPanelOpen(true)
    setIsLoadingOpenApi(true)
    setOpenApiError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/openapi/sources`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '读取 OpenAPI 来源失败')
      }
      setOpenApiSources((data as OpenApiSourcesResponse).sources)
    } catch (requestError) {
      setOpenApiError(
        requestError instanceof Error ? requestError.message : '读取 OpenAPI 来源失败',
      )
    } finally {
      setIsLoadingOpenApi(false)
    }
  }

  const previewOpenApi = async (event: FormEvent) => {
    event.preventDefault()
    if (!openApiSpec.trim()) return
    setIsPreviewingOpenApi(true)
    setOpenApiError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/openapi/preview`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: openApiName.trim() || 'OpenAPI',
          spec_text: openApiSpec.trim(),
          base_url: openApiBaseUrl.trim() || null,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '解析 OpenAPI 失败')
      const preview = data as OpenApiPreviewResponse
      setOpenApiPreview(preview)
      if (!openApiName.trim()) setOpenApiName(preview.title)
      if (!openApiBaseUrl.trim()) setOpenApiBaseUrl(preview.base_url)
      setOpenApiSelectedOps(
        preview.operations.filter((item) => !item.unsafe).slice(0, 8).map((item) => item.operation_id),
      )
    } catch (requestError) {
      setOpenApiError(requestError instanceof Error ? requestError.message : '解析 OpenAPI 失败')
    } finally {
      setIsPreviewingOpenApi(false)
    }
  }

  const createOpenApiSource = async (event: FormEvent) => {
    event.preventDefault()
    if (!openApiSpec.trim() || !openApiName.trim()) return
    setIsSavingOpenApi(true)
    setOpenApiError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/openapi/sources`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: openApiName.trim(),
          spec_text: openApiSpec.trim(),
          base_url: openApiBaseUrl.trim() || null,
          selected_operations: openApiSelectedOps,
          auth_token: openApiToken.trim() || null,
          enabled: true,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '导入 OpenAPI 失败')
      setOpenApiName('')
      setOpenApiBaseUrl('')
      setOpenApiSpec('')
      setOpenApiToken('')
      setOpenApiPreview(null)
      setOpenApiSelectedOps([])
      await openOpenApiPanel()
    } catch (requestError) {
      setOpenApiError(requestError instanceof Error ? requestError.message : '导入 OpenAPI 失败')
    } finally {
      setIsSavingOpenApi(false)
    }
  }

  const toggleOpenApiSource = async (source: OpenApiSource) => {
    setOpenApiError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/openapi/sources/${source.id}`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !source.enabled }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '更新 OpenAPI 来源失败')
      await openOpenApiPanel()
    } catch (requestError) {
      setOpenApiError(requestError instanceof Error ? requestError.message : '更新 OpenAPI 来源失败')
    }
  }

  const deleteOpenApiSource = async (source: OpenApiSource) => {
    if (!window.confirm(`确定删除 OpenAPI 来源「${source.name}」？`)) return
    setOpenApiError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/openapi/sources/${source.id}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: '删除失败' }))
        throw new Error(data.detail ?? '删除失败')
      }
      await openOpenApiPanel()
    } catch (requestError) {
      setOpenApiError(requestError instanceof Error ? requestError.message : '删除 OpenAPI 来源失败')
    }
  }

  const openSkillsPanel = async () => {
    setIsSkillsPanelOpen(true)
    setIsLoadingSkills(true)
    setSkillsError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/skills`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '读取 Agent Skills 失败')
      }
      setAgentSkills((data as SkillsResponse).skills)
    } catch (requestError) {
      setSkillsError(
        requestError instanceof Error ? requestError.message : '读取 Agent Skills 失败',
      )
    } finally {
      setIsLoadingSkills(false)
    }
  }

  const loadSkillBody = async (name: string) => {
    setSelectedSkillName(name)
    const existing = agentSkills.find((item) => item.name === name)
    if (existing?.body) {
      return
    }
    try {
      const response = await fetch(`${API_BASE_URL}/skills/${name}`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(data.detail ?? '加载 Skill 正文失败')
      }
      const detail = data as AgentSkill
      setAgentSkills((current) =>
        current.map((item) =>
          item.name === detail.name ? { ...item, body: detail.body } : item,
        ),
      )
    } catch (requestError) {
      setSkillsError(
        requestError instanceof Error ? requestError.message : '加载 Skill 正文失败',
      )
    }
  }

  const loadKnowledgeDocuments = async () => {
    setIsLoadingKnowledge(true)
    setKnowledgeError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/knowledge/documents`, { credentials: 'include' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取知识库失败')
      setKnowledgeDocuments((data as KnowledgeDocumentListResponse).documents)
    } catch (requestError) {
      setKnowledgeError(requestError instanceof Error ? requestError.message : '读取知识库失败')
    } finally {
      setIsLoadingKnowledge(false)
    }
  }

  const openKnowledgePanel = async () => {
    setIsFeatureMenuOpen(false)
    setIsKnowledgePanelOpen(true)
    await loadKnowledgeDocuments()
  }

  const loadMemories = async () => {
    setIsLoadingMemories(true)
    setMemoryError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/memories`, { credentials: 'include' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取长期记忆失败')
      setUserMemories((data as MemoryListResponse).memories)
    } catch (requestError) {
      setMemoryError(requestError instanceof Error ? requestError.message : '读取长期记忆失败')
    } finally {
      setIsLoadingMemories(false)
    }
  }

  const openMemoryPanel = async () => {
    setIsFeatureMenuOpen(false)
    setIsMemoryPanelOpen(true)
    await loadMemories()
  }

  const createMemory = async (event: FormEvent) => {
    event.preventDefault()
    const content = memoryDraftContent.trim()
    if (!content) return
    setIsSavingMemory(true)
    setMemoryError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/memories`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: memoryDraftCategory, content }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '保存记忆失败')
      setMemoryDraftContent('')
      await loadMemories()
    } catch (requestError) {
      setMemoryError(requestError instanceof Error ? requestError.message : '保存记忆失败')
    } finally {
      setIsSavingMemory(false)
    }
  }

  const deleteMemory = async (memoryId: string) => {
    setMemoryError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/memories/${memoryId}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: '删除记忆失败' }))
        throw new Error(data.detail ?? '删除记忆失败')
      }
      setUserMemories((current) => current.filter((item) => item.id !== memoryId))
    } catch (requestError) {
      setMemoryError(requestError instanceof Error ? requestError.message : '删除记忆失败')
    }
  }

  const clearMemories = async () => {
    if (!window.confirm('确定清空全部长期记忆？Agent 之后将不再记得这些偏好和约束。')) return
    setMemoryError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/memories`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: '清空记忆失败' }))
        throw new Error(data.detail ?? '清空记忆失败')
      }
      setUserMemories([])
    } catch (requestError) {
      setMemoryError(requestError instanceof Error ? requestError.message : '清空记忆失败')
    }
  }

  useEffect(() => {
    if (!isKnowledgePanelOpen || !knowledgeDocuments.some((item) => item.status === 'processing')) return
    const timer = window.setInterval(() => {
      void fetch(`${API_BASE_URL}/knowledge/documents`, { credentials: 'include' })
        .then(async (response) => {
          const data = await response.json()
          if (response.ok) setKnowledgeDocuments((data as KnowledgeDocumentListResponse).documents)
        })
        .catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [isKnowledgePanelOpen, knowledgeDocuments])

  const searchKnowledge = async (event: FormEvent) => {
    event.preventDefault()
    const query = knowledgeSearchQuery.trim()
    if (!query) return
    setIsSearchingKnowledge(true)
    setKnowledgeError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/knowledge/search?query=${encodeURIComponent(query)}&limit=5`, { credentials: 'include' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '检索失败')
      setKnowledgeSearchResult(data as KnowledgeSearchResponse)
    } catch (requestError) {
      setKnowledgeError(requestError instanceof Error ? requestError.message : '检索失败')
    } finally {
      setIsSearchingKnowledge(false)
    }
  }

  const reindexKnowledgeDocument = async (documentId: string) => {
    setKnowledgeError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/knowledge/documents/${documentId}/reindex`, { method: 'POST', credentials: 'include' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '重新向量化失败')
      setKnowledgeDocuments((documents) => documents.map((item) => item.id === documentId ? data as KnowledgeDocument : item))
    } catch (requestError) {
      setKnowledgeError(requestError instanceof Error ? requestError.message : '重新向量化失败')
    }
  }

  const retryBackgroundTask = async (taskId: string) => {
    setTaskCenterError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/tasks/${taskId}/retry`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '任务重试失败')
      setBackgroundTasks((items) => items.map((item) => item.id === taskId ? data as BackgroundTask : item))
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '任务重试失败')
    }
  }

  const cancelBackgroundTask = async (taskId: string) => {
    setTaskCenterError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/tasks/${taskId}/cancel`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '取消任务失败')
      setBackgroundTasks((items) => items.map((item) => item.id === taskId ? data as BackgroundTask : item))
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '取消任务失败')
    }
  }

  const continueWorkflowTask = async (task: BackgroundTask) => {
    const runId = String(task.payload?.run_id ?? '')
    if (!runId) return
    setTaskCenterError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/workflow-runs/${runId}/approve`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '继续执行失败')
      setBackgroundTasks((items) => items.map((item) => item.id === task.id ? data.task as BackgroundTask : item))
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '继续执行失败')
    }
  }

  const markNotificationRead = async (notificationId: string) => {
    try {
      const response = await fetch(`${API_BASE_URL}/notifications/${notificationId}/read`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!response.ok) return
      setTaskNotifications((items) => items.map((item) => item.id === notificationId ? { ...item, is_read: true } : item))
      setUnreadNotificationCount((count) => Math.max(0, count - 1))
    } catch {
      return
    }
  }

  const markAllNotificationsRead = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/notifications/read-all`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!response.ok) throw new Error('标记通知失败')
      setTaskNotifications((items) => items.map((item) => ({ ...item, is_read: true })))
      setUnreadNotificationCount(0)
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '标记通知失败')
    }
  }

  const updateAutomationSettings = async (patch: Partial<AutomationSettings>) => {
    setIsSavingAutomationSettings(true)
    setTaskCenterError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/automations/settings`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '保存自动化设置失败')
      setAutomationSettings(data as AutomationSettings)
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '保存自动化设置失败')
    } finally {
      setIsSavingAutomationSettings(false)
    }
  }

  const runDailyTodoBriefingNow = async () => {
    setIsRunningDailyBriefing(true)
    setTaskCenterError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/automations/daily-todo-briefing/run`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '启动每日简报失败')
      const task = (data as { task: BackgroundTask }).task
      setBackgroundTasks((items) => [task, ...items.filter((item) => item.id !== task.id)])
      setTaskCenterTab('tasks')
    } catch (requestError) {
      setTaskCenterError(requestError instanceof Error ? requestError.message : '启动每日简报失败')
    } finally {
      setIsRunningDailyBriefing(false)
    }
  }

  const uploadKnowledgeFiles = async (files: FileList | File[]) => {
    if (!currentUser || currentUser.role !== 'knowledge_manager') return
    const selectedFiles = Array.from(files)
    if (selectedFiles.length === 0) return
    setIsUploadingKnowledge(true)
    setKnowledgeError(null)
    try {
      for (const file of selectedFiles) {
        const body = new FormData()
        body.append('file', file)
        body.append('category', knowledgeCategory)
        const response = await fetch(`${API_BASE_URL}/knowledge/documents`, { method: 'POST', credentials: 'include', body })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? `${file.name} 上传失败`)
      }
      await loadKnowledgeDocuments()
    } catch (requestError) {
      setKnowledgeError(requestError instanceof Error ? requestError.message : '上传失败')
    } finally {
      setIsUploadingKnowledge(false)
    }
  }

  const deleteKnowledgeDocument = async (documentId: string) => {
    if (!currentUser || currentUser.role !== 'knowledge_manager') return
    setKnowledgeError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/knowledge/documents/${documentId}`, { method: 'DELETE', credentials: 'include' })
      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail ?? '删除失败')
      }
      setKnowledgeDocuments((documents) => documents.filter((item) => item.id !== documentId))
    } catch (requestError) {
      setKnowledgeError(requestError instanceof Error ? requestError.message : '删除失败')
    }
  }

  const openHistoryPanel = async () => {
    setIsTravelPanelOpen(false)
    setIsHistoryPanelOpen(true)
    setIsLoadingSessions(true)
    setHistoryPanelError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/sessions`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取历史会话失败')
      setHistoricalSessions((data as SessionListResponse).sessions)
    } catch (requestError) {
      setHistoryPanelError(
        requestError instanceof Error ? requestError.message : '读取历史会话失败',
      )
    } finally {
      setIsLoadingSessions(false)
    }
  }

  const openTravelPanel = async () => {
    setIsHistoryPanelOpen(false)
    setIsTravelPanelOpen(true)
    setIsLoadingTravelPlans(true)
    setTravelPanelError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/travel-plans`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取旅行计划失败')
      setSavedTravelPlans(data as TravelPlanSummary[])
    } catch (requestError) {
      setTravelPanelError(
        requestError instanceof Error ? requestError.message : '读取旅行计划失败',
      )
    } finally {
      setIsLoadingTravelPlans(false)
    }
  }

  const loadAgentRun = async (runId: string) => {
    setObservabilityError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/observability/runs/${runId}`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取运行详情失败')
      setSelectedAgentRun(data as AgentRunDetail)
    } catch (requestError) {
      setObservabilityError(
        requestError instanceof Error ? requestError.message : '读取运行详情失败',
      )
    }
  }

  const openObservabilityPanel = async () => {
    setIsObservabilityOpen(true)
    setIsLoadingRuns(true)
    setObservabilityError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/observability/runs`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取 Agent 运行记录失败')
      const runs = (data as { runs: AgentRunSummary[] }).runs
      setAgentRuns(runs)
      if (runs.length > 0) await loadAgentRun(runs[0].id)
      else setSelectedAgentRun(null)
    } catch (requestError) {
      setObservabilityError(
        requestError instanceof Error ? requestError.message : '读取 Agent 运行记录失败',
      )
    } finally {
      setIsLoadingRuns(false)
    }
  }

  const loadEvaluation = async (runId: string) => {
    setEvaluationError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/evaluations/runs/${runId}`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取评测详情失败')
      const detail = data as EvaluationRunDetail
      setSelectedEvaluation(detail)
      setEvaluationRuns((current) => current.map((run) => run.id === detail.id ? detail : run))
      if (detail.baseline_run_id) {
        setSelectedBaselineRunId(detail.baseline_run_id)
        try {
          const compareResponse = await fetch(
            `${API_BASE_URL}/evaluations/runs/${runId}/compare?baseline_run_id=${detail.baseline_run_id}`,
            { credentials: 'include' },
          )
          const compareData = await compareResponse.json()
          if (compareResponse.ok) setEvaluationComparison(compareData as EvaluationComparison)
          else setEvaluationComparison(null)
        } catch {
          setEvaluationComparison(null)
        }
      } else {
        setSelectedBaselineRunId('')
        setEvaluationComparison(null)
      }
    } catch (requestError) {
      setEvaluationError(
        requestError instanceof Error ? requestError.message : '读取评测详情失败',
      )
    }
  }

  const openEvaluationPanel = async () => {
    setIsEvaluationOpen(true)
    setIsLoadingEvaluations(true)
    setEvaluationError(null)
    try {
      const [runsResponse, configResponse] = await Promise.all([
        fetch(`${API_BASE_URL}/evaluations/runs`, { credentials: 'include' }),
        fetch(`${API_BASE_URL}/agentops/config`, { credentials: 'include' }),
      ])
      const data = await runsResponse.json()
      const config = await configResponse.json()
      if (!runsResponse.ok) throw new Error(data.detail ?? '读取评测记录失败')
      if (!configResponse.ok) throw new Error(config.detail ?? '读取 AgentOps 配置失败')
      const runs = (data as { runs: EvaluationRunSummary[] }).runs
      const prompts = (config as { prompts: PromptVersion[] }).prompts
      const datasets = (config as { datasets: EvaluationDataset[] }).datasets
      setEvaluationRuns(runs)
      setPromptVersions(prompts)
      setEvaluationDatasets(datasets)
      setSelectedPromptId((current) => current || prompts.find((item) => item.status === 'active')?.id || prompts[0]?.id || '')
      setSelectedDatasetId((current) => current || datasets[0]?.id || '')
      if (runs.length > 0) await loadEvaluation(runs[0].id)
      else setSelectedEvaluation(null)
    } catch (requestError) {
      setEvaluationError(
        requestError instanceof Error ? requestError.message : '读取评测记录失败',
      )
    } finally {
      setIsLoadingEvaluations(false)
    }
  }

  const runEvaluation = async () => {
    if (!selectedPromptId || !selectedDatasetId) {
      setEvaluationError('请先选择 Prompt 版本和测试集。')
      return
    }
    setIsRunningEvaluation(true)
    setEvaluationError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/evaluations/runs`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirmed: true,
          prompt_version_id: selectedPromptId,
          dataset_id: selectedDatasetId,
          model: evaluationModel,
          scorer: evaluationScorer,
          judge_enabled: evaluationJudgeEnabled,
          baseline_run_id: selectedBaselineRunId || undefined,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '实时评测失败')
      const result = (data as { run: EvaluationRunDetail }).run
      setSelectedEvaluation(result)
      setEvaluationRuns((current) => [result, ...current.filter((run) => run.id !== result.id)])
      setTaskCenterTab('tasks')
      setIsTaskCenterOpen(true)
    } catch (requestError) {
      setEvaluationError(
        requestError instanceof Error ? requestError.message : '提交后台评测失败',
      )
    } finally {
      setIsRunningEvaluation(false)
    }
  }

  const reloadAgentOpsConfig = async () => {
    const response = await fetch(`${API_BASE_URL}/agentops/config`, { credentials: 'include' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '刷新 AgentOps 配置失败')
    setPromptVersions((data as { prompts: PromptVersion[] }).prompts)
    setEvaluationDatasets((data as { datasets: EvaluationDataset[] }).datasets)
  }

  const createPromptVersion = async () => {
    if (!newPromptName.trim() || !newPromptContent.trim()) return
    setEvaluationError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/agentops/prompts`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newPromptName, content: newPromptContent, change_note: '网页创建' }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '创建 Prompt 版本失败')
      setNewPromptName(''); setNewPromptContent(''); await reloadAgentOpsConfig()
    } catch (error) { setEvaluationError(error instanceof Error ? error.message : '创建 Prompt 版本失败') }
  }

  const activatePromptVersion = async (promptId: string) => {
    setEvaluationError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/agentops/prompts/${promptId}/activate`, {
        method: 'POST', credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '发布 Prompt 版本失败')
      setSelectedPromptId(promptId); await reloadAgentOpsConfig()
    } catch (error) { setEvaluationError(error instanceof Error ? error.message : '发布 Prompt 版本失败') }
  }

  const createEvaluationCase = async () => {
    if (!selectedDatasetId || !newCaseName.trim() || !newCaseInput.trim()) return
    setEvaluationError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/agentops/datasets/${selectedDatasetId}/cases`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newCaseName, category: 'custom', input_text: newCaseInput,
          scoring_method: newCaseScoringMethod,
          expected_keywords: newCaseKeywords.split(',').map((item) => item.trim()).filter(Boolean),
          expected_answer: newCaseExpectedAnswer.trim() || undefined,
          judge_rubric: newCaseJudgeRubric.trim() || undefined,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '添加测试用例失败')
      setNewCaseName('')
      setNewCaseInput('')
      setNewCaseKeywords('')
      setNewCaseExpectedAnswer('')
      setNewCaseJudgeRubric('')
      setNewCaseScoringMethod('keyword')
      await reloadAgentOpsConfig()
    } catch (error) { setEvaluationError(error instanceof Error ? error.message : '添加测试用例失败') }
  }

  const exportEvaluation = async (runId: string, format: 'json' | 'csv') => {
    setEvaluationError(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/evaluations/runs/${runId}/export?format=${format}`,
        { credentials: 'include' },
      )
      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail ?? '导出评测报告失败')
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `evaluation-${runId}.${format}`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (error) {
      setEvaluationError(error instanceof Error ? error.message : '导出评测报告失败')
    }
  }

  
  const compareEvaluationWithBaseline = async (runId: string, baselineRunId: string) => {
    if (!baselineRunId) {
      setEvaluationComparison(null)
      return
    }
    setEvaluationError(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/evaluations/runs/${runId}/compare?baseline_run_id=${baselineRunId}`,
        { credentials: 'include' },
      )
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取评测对比失败')
      setEvaluationComparison(data as EvaluationComparison)
      setSelectedBaselineRunId(baselineRunId)
    } catch (error) {
      setEvaluationError(error instanceof Error ? error.message : '读取评测对比失败')
    }
  }

  const toggleEvaluationCase = async (item: EvaluationTestCase) => {
    const response = await fetch(`${API_BASE_URL}/agentops/cases/${item.id}`, {
      method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !item.enabled }),
    })
    const data = await response.json()
    if (!response.ok) { setEvaluationError(data.detail ?? '更新用例失败'); return }
    await reloadAgentOpsConfig()
  }

  const importEvaluationCases = async (file: File) => {
    if (!selectedDatasetId) return
    setEvaluationError(null)
    try {
      const parsed = JSON.parse(await file.text()) as Array<{
        name: string; input_text: string; expected_keywords: string[]; category?: string
      }>
      if (!Array.isArray(parsed) || parsed.length === 0 || parsed.length > 100) {
        throw new Error('JSON 必须是包含 1～100 条用例的数组。')
      }
      for (const item of parsed) {
        if (!item.name || !item.input_text || !Array.isArray(item.expected_keywords) || item.expected_keywords.length === 0) {
          throw new Error('每条用例都需要 name、input_text 和 expected_keywords。')
        }
        const response = await fetch(`${API_BASE_URL}/agentops/datasets/${selectedDatasetId}/cases`, {
          method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...item, category: item.category || 'imported' }),
        })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? `导入“${item.name}”失败`)
      }
      await reloadAgentOpsConfig()
    } catch (error) { setEvaluationError(error instanceof Error ? error.message : '导入测试集失败') }
  }

  const selectSavedTravelPlan = async (travelPlanId: number) => {
    setTravelPanelError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/travel-plans/${travelPlanId}`, {
        credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取旅行计划详情失败')
      const saved = data as SavedTravelPlan
      setMessages([
        {
          id: nextMessageId.current++,
          role: 'assistant',
          content: saved.summary,
          travelPlan: saved,
          travelPlanId: saved.id,
        },
      ])
      setSessionId(saved.session_id)
      localStorage.setItem(SESSION_STORAGE_KEY, saved.session_id)
      setPendingApproval(null)
      setError(null)
      if (saved.status === 'needs_input') {
        setPendingTravelTask({ id: saved.id, version: saved.version, plan: saved })
        setActiveCommand(TRAVEL_COMMAND)
        setComposerMode('travel')
      } else {
        setPendingTravelTask(null)
        setActiveCommand(null)
        setComposerMode('chat')
      }
      setIsTravelPanelOpen(false)
    } catch (requestError) {
      setTravelPanelError(
        requestError instanceof Error ? requestError.message : '读取旅行计划详情失败',
      )
    }
  }

  const selectHistoricalSession = async (selectedSessionId: string) => {
    setHistoryPanelError(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/sessions/${selectedSessionId}/messages`,
        { credentials: 'include' },
      )
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取会话内容失败')
      const history = data as HistoryResponse
      setMessages(
        history.messages.map((message) =>
          restoreHistoryMessage(message, nextMessageId.current++),
        ),
      )
      setSessionId(selectedSessionId)
      localStorage.setItem(SESSION_STORAGE_KEY, selectedSessionId)
      setPendingApproval(null)
      setError(null)
      setIsHistoryPanelOpen(false)
    } catch (requestError) {
      setHistoryPanelError(
        requestError instanceof Error ? requestError.message : '读取会话内容失败',
      )
    }
  }
  const handleAuthenticated = (user: AuthUser) => {
    localStorage.removeItem(SESSION_STORAGE_KEY)
    setSessionId(null)
    setCurrentUser(user)
    setError(null)
    setPendingTravelTask(null)
    setMessages([
      {
        id: nextMessageId.current++,
        role: 'assistant',
        content: `你好，${user.display_name}。登录成功，今天想完成什么？`,
      },
    ])
  }

  const logout = async () => {
    try {
      await fetch(`${API_BASE_URL}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      })
    } finally {
      localStorage.removeItem(SESSION_STORAGE_KEY)
      setSessionId(null)
      setCurrentUser(null)
      setPendingTravelTask(null)
      setMessages([])
    }
  }

  if (isCheckingAuth) {
    return (
      <main className="auth-shell">
        <div className="auth-loading">
          <div className="typing-dots" aria-label="正在检查登录状态">
            <span /><span /><span />
          </div>
          正在恢复登录状态…
        </div>
      </main>
    )
  }

  if (!currentUser) {
    return <AuthScreen apiBaseUrl={API_BASE_URL} onAuthenticated={handleAuthenticated} />
  }

  return (
    <main className="app-shell">
      <section className="chat-panel" aria-label="AI Agent 聊天窗口">
        <header className="chat-header">
          <div className="brand">
            <img alt="" aria-hidden="true" className="brand-mark" src={projectIcon} />
            <div>
              <h1>Hello Agent</h1>
              <p>
                <span className="status-dot" aria-hidden="true" /> DeepSeek ·
                FastAPI
              </p>
            </div>
          </div>
          <div className="header-actions">
            <button
              aria-label={`任务与通知${unreadNotificationCount ? `，${unreadNotificationCount} 条未读` : ''}`}
              className="task-center-button"
              onClick={() => setIsTaskCenterOpen(true)}
              type="button"
            >
              <span aria-hidden="true">♢</span>
              {unreadNotificationCount > 0 && <b>{unreadNotificationCount > 99 ? '99+' : unreadNotificationCount}</b>}
            </button>
            <button
              className="history-button"
              onClick={() => void openHistoryPanel()}
              type="button"
            >
              <span aria-hidden="true">◷</span>
              <span className="history-button-label">历史会话</span>
            </button>
            <button
              aria-expanded={isFeatureMenuOpen}
              aria-haspopup="dialog"
              className="feature-menu-button"
              onClick={() => setIsFeatureMenuOpen((current) => !current)}
              type="button"
            >
              <span className="feature-menu-grid" aria-hidden="true">
                <i /><i /><i /><i />
              </span>
              <span className="feature-menu-button-label">功能中心</span>
            </button>
            <button
              className="secondary-button new-session-button"
              onClick={startNewSession}
            >
              <span aria-hidden="true">＋</span>
              <span className="new-session-button-label">新会话</span>
            </button>
            <div className="user-menu">
              <img alt="" aria-hidden="true" className="user-avatar" src={projectIcon} />
              <span className="user-name">{currentUser.display_name}</span>
              <button onClick={() => void logout()} type="button">退出</button>
            </div>
          </div>
        </header>

        {isFeatureMenuOpen && (
          <div
            className="feature-menu-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsFeatureMenuOpen(false)
            }}
          >
            <section
              aria-labelledby="feature-menu-title"
              aria-modal="true"
              className="feature-menu-panel"
              role="dialog"
            >
              <div className="feature-menu-header">
                <div>
                  <span>HELLO AGENT</span>
                  <h2 id="feature-menu-title">功能中心</h2>
                </div>
                <button
                  aria-label="关闭功能中心"
                  onClick={() => setIsFeatureMenuOpen(false)}
                  type="button"
                >
                  ×
                </button>
              </div>

              <div className="feature-menu-content">
                <div className="feature-menu-group">
                  <h3>常用功能</h3>
                  <a className="feature-menu-item" href={TODO_APP_URL}>
                    <span className="feature-menu-icon feature-menu-icon-green" aria-hidden="true">✓</span>
                    <span><strong>Todo 应用</strong><small>管理计划分类和待办步骤</small></span>
                    <b aria-hidden="true">›</b>
                  </a>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openTravelPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-blue" aria-hidden="true">⌖</span>
                    <span><strong>旅行计划</strong><small>查看已确认和待完善的旅行方案</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => void openKnowledgePanel()}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-teal" aria-hidden="true">▤</span>
                    <span><strong>我的知识库</strong><small>上传和管理仅属于自己的资料</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => void openMemoryPanel()}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-pink" aria-hidden="true">✦</span>
                    <span><strong>长期记忆</strong><small>跨会话保存偏好、事实和约束，可随时删除</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      setIsTaskCenterOpen(true)
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-orange" aria-hidden="true">♢</span>
                    <span><strong>任务与通知</strong><small>查看后台进度、失败重试和站内提醒</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                </div>

                <div className="feature-menu-group">
                  <h3>Agent 能力</h3>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      setIsWorkflowStudioOpen(true)
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-purple" aria-hidden="true">⌁</span>
                    <span><strong>可视化工作流</strong><small>拖拽节点，保存并运行 Agent 流程</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      selectCommand(SLASH_COMMANDS.find((item) => item.mode === 'collaboration')!)
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-teal" aria-hidden="true">◌</span>
                    <span><strong>多 Agent 协作</strong><small>规划→执行调工具→审核打回后交付</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openMCPPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-purple" aria-hidden="true">⌘</span>
                    <span><strong>MCP 工具</strong><small>查看内置工具，添加远程 SSE/HTTP MCP</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openOpenApiPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-teal" aria-hidden="true">⧉</span>
                    <span><strong>OpenAPI 工具</strong><small>粘贴 JSON 规范，生成可调用 HTTP 工具</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openSkillsPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-orange" aria-hidden="true">☰</span>
                    <span><strong>Agent Skills</strong><small>目录常驻，正文按需加载</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                </div>

                <div className="feature-menu-group">
                  <h3>开发工具</h3>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openObservabilityPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-orange" aria-hidden="true">⌁</span>
                    <span><strong>运行记录与执行链</strong><small>排查模型、工具调用和耗时</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      setIsMetricsOpen(true)
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-teal" aria-hidden="true">▣</span>
                    <span><strong>监控与费用看板</strong><small>按 DeepSeek Token 估算调用费用和成功率</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                  <button
                    className="feature-menu-item"
                    onClick={() => {
                      setIsFeatureMenuOpen(false)
                      void openEvaluationPanel()
                    }}
                    type="button"
                  >
                    <span className="feature-menu-icon feature-menu-icon-pink" aria-hidden="true">◇</span>
                    <span><strong>自动评测中心</strong><small>运行核心能力测试并比较版本</small></span>
                    <b aria-hidden="true">›</b>
                  </button>
                </div>
                {currentUser.can_access_admin && <div className="feature-menu-group">
                  <h3>系统管理</h3>
                  <a className="feature-menu-item" href="/agent/admin/">
                    <span className="feature-menu-icon feature-menu-icon-dark" aria-hidden="true">⚙</span>
                    <span><strong>管理后台</strong><small>使用独立管理员账号和双重认证登录</small></span>
                    <b aria-hidden="true">›</b>
                  </a>
                </div>}
              </div>
            </section>
          </div>
        )}

        {isTaskCenterOpen && (
          <div className="task-center-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsTaskCenterOpen(false) }}>
            <aside aria-labelledby="task-center-title" aria-modal="true" className="task-center-drawer" role="dialog">
              <header className="task-center-header">
                <div><span>ASYNC OPERATIONS</span><h2 id="task-center-title">任务与通知</h2><p>耗时操作会在后台可靠执行，关闭页面也不会中断。</p></div>
                <button aria-label="关闭任务中心" onClick={() => setIsTaskCenterOpen(false)} type="button">×</button>
              </header>
              <section className="automation-card">
                <div>
                  <strong>每日 Todo 简报</strong>
                  <p>先保留开关，演示时再开启。开启后会在设定时间生成一条站内通知。</p>
                </div>
                <div className="automation-controls">
                  <label className="automation-toggle">
                    <input
                      checked={automationSettings?.daily_todo_briefing_enabled ?? false}
                      disabled={!automationSettings || isSavingAutomationSettings}
                      onChange={(event) => void updateAutomationSettings({ daily_todo_briefing_enabled: event.target.checked })}
                      type="checkbox"
                    />
                    <span>{automationSettings?.daily_todo_briefing_enabled ? '已开启' : '已关闭'}</span>
                  </label>
                  <label className="automation-hour">
                    <span>时间</span>
                    <select
                      value={automationSettings?.daily_todo_briefing_hour ?? 9}
                      disabled={!automationSettings || isSavingAutomationSettings}
                      onChange={(event) => void updateAutomationSettings({ daily_todo_briefing_hour: Number(event.target.value) })}
                    >
                      {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{`${hour.toString().padStart(2, '0')}:00`}</option>)}
                    </select>
                  </label>
                  <button disabled={isRunningDailyBriefing} onClick={() => void runDailyTodoBriefingNow()} type="button">
                    {isRunningDailyBriefing ? '正在启动…' : '立即生成'}
                  </button>
                </div>
                <small>
                  时区 {automationSettings?.timezone ?? 'Asia/Shanghai'} · 调度器
                  {automationSettings?.scheduler_enabled ? '已部署' : '未启用'} · 最近发送
                  {automationSettings?.last_daily_todo_briefing_on ?? '暂无'}
                </small>
              </section>
              <nav className="task-center-tabs">
                <button className={taskCenterTab === 'tasks' ? 'is-active' : ''} onClick={() => setTaskCenterTab('tasks')} type="button">后台任务 <span>{backgroundTasks.length}</span></button>
                <button className={taskCenterTab === 'notifications' ? 'is-active' : ''} onClick={() => setTaskCenterTab('notifications')} type="button">通知 <span>{unreadNotificationCount}</span></button>
              </nav>
              {taskCenterError && <div className="task-center-error">{taskCenterError}</div>}
              <div className="task-center-content">
                {taskCenterTab === 'tasks' ? (
                  backgroundTasks.length === 0 ? <div className="task-center-empty"><strong>还没有后台任务</strong><span>上传知识文档后，向量化任务会出现在这里。</span></div> : backgroundTasks.map((task) => (
                    <article className={`background-task task-${task.status}`} key={task.id}>
                      <header><span className="task-state-icon" aria-hidden="true">{task.status === 'succeeded' ? '✓' : task.status === 'failed' ? '!' : task.status === 'cancelled' ? '×' : task.status === 'waiting' ? 'Ⅱ' : task.status === 'running' ? '↻' : '…'}</span><div><strong>{task.title}</strong><p>{task.status === 'queued' ? '等待 Worker 执行' : task.status === 'running' ? `正在执行 ${task.progress}%` : task.status === 'waiting' ? '等待你确认后继续' : task.status === 'succeeded' ? '执行成功' : task.status === 'cancelled' ? '已取消' : '执行失败'}</p></div><time>{formatTaskTime(task.created_at)}</time></header>
                      <div className="task-progress"><i style={{ width: `${task.progress}%` }} /></div>
                      {task.error_message && <p className="task-error-message">{task.error_message}</p>}
                      <footer><span>重试 {task.retry_count}/{task.max_retries}</span><span className="task-actions">{(task.status === 'queued' || task.status === 'running' || task.status === 'waiting') && <button onClick={() => void cancelBackgroundTask(task.id)} type="button">取消</button>}{task.status === 'waiting' && task.task_type === 'workflow_run' && <button onClick={() => void continueWorkflowTask(task)} type="button">继续执行</button>}{task.status === 'failed' && task.retry_count < task.max_retries && <button onClick={() => void retryBackgroundTask(task.id)} type="button">重新执行</button>}</span></footer>
                    </article>
                  ))
                ) : (
                  <>
                    {taskNotifications.some((item) => !item.is_read) && <button className="mark-all-read" onClick={() => void markAllNotificationsRead()} type="button">全部标为已读</button>}
                    {taskNotifications.length === 0 ? <div className="task-center-empty"><strong>暂时没有通知</strong><span>任务完成或失败后会在这里提醒你。</span></div> : taskNotifications.map((notice) => (
                      <button className={`task-notification notice-${notice.level} ${notice.is_read ? 'is-read' : ''}`} key={notice.id} onClick={() => !notice.is_read && void markNotificationRead(notice.id)} type="button"><i aria-hidden="true" /><div><strong>{notice.title}</strong><p>{notice.message}</p><time>{formatTaskTime(notice.created_at)}</time></div>{!notice.is_read && <span>新</span>}</button>
                    ))}
                  </>
                )}
              </div>
            </aside>
          </div>
        )}

        {isWorkflowStudioOpen && (
          <Suspense fallback={<div className="workflow-loading">正在加载工作流画布…</div>}>
            <WorkflowStudio
              apiBaseUrl={API_BASE_URL}
              onClose={() => setIsWorkflowStudioOpen(false)}
            />
          </Suspense>
        )}

        {isMemoryPanelOpen && (
          <div className="knowledge-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsMemoryPanelOpen(false) }}>
            <section aria-labelledby="memory-panel-title" aria-modal="true" className="knowledge-panel memory-panel" role="dialog">
              <header className="knowledge-header">
                <div>
                  <span className="knowledge-eyebrow">LONG-TERM MEMORY</span>
                  <h2 id="memory-panel-title">长期记忆</h2>
                  <p>跨会话保留你的偏好、身份和约束；聊天记录与知识库不会自动变成记忆，内容仅你可见。</p>
                </div>
                <button aria-label="关闭长期记忆" onClick={() => setIsMemoryPanelOpen(false)} type="button">×</button>
              </header>
              <div className="knowledge-body">
                <div className="knowledge-stats">
                  <div><strong>{userMemories.length}</strong><span>条记忆</span></div>
                  <div><strong>{userMemories.filter((item) => item.category === 'preference').length}</strong><span>偏好</span></div>
                  <div><strong>{userMemories.filter((item) => item.category === 'constraint').length}</strong><span>约束</span></div>
                </div>
                <div className="knowledge-permission is-allowed">
                  <span>✓</span>
                  <p><strong>记忆只属于当前账号</strong>对新会话自动生效。说“请记住 / 请忘记”，或在下方手动添加；管理员看不到这些内容。</p>
                </div>
                <form className="memory-composer" onSubmit={(event) => void createMemory(event)}>
                  <label>
                    <span>类型</span>
                    <select onChange={(event) => setMemoryDraftCategory(event.target.value as MemoryCategory)} value={memoryDraftCategory}>
                      {MEMORY_CATEGORIES.map((item) => (
                        <option key={item} value={item}>{MEMORY_CATEGORY_LABELS[item]}</option>
                      ))}
                    </select>
                  </label>
                  <label className="memory-composer-content">
                    <span>一句话记忆</span>
                    <input
                      maxLength={200}
                      onChange={(event) => setMemoryDraftContent(event.target.value)}
                      placeholder="例如：喜欢早起跑步，不要推荐咖啡"
                      value={memoryDraftContent}
                    />
                  </label>
                  <button disabled={isSavingMemory || !memoryDraftContent.trim()} type="submit">
                    {isSavingMemory ? '保存中…' : '添加'}
                  </button>
                </form>
                {memoryError && <div className="knowledge-error">{memoryError}</div>}
                <div className="knowledge-filter">
                  <button className={memoryFilter === '全部' ? 'is-active' : ''} onClick={() => setMemoryFilter('全部')} type="button">全部</button>
                  {MEMORY_CATEGORIES.map((item) => (
                    <button className={memoryFilter === item ? 'is-active' : ''} key={item} onClick={() => setMemoryFilter(item)} type="button">
                      {MEMORY_CATEGORY_LABELS[item]}
                    </button>
                  ))}
                  {userMemories.length > 0 && (
                    <button className="memory-clear" onClick={() => void clearMemories()} type="button">清空全部</button>
                  )}
                </div>
                <div className="memory-list">
                  {isLoadingMemories ? (
                    <div className="knowledge-empty">正在读取记忆…</div>
                  ) : userMemories.filter((item) => memoryFilter === '全部' || item.category === memoryFilter).length === 0 ? (
                    <div className="knowledge-empty">
                      <strong>{userMemories.length === 0 ? '还没有长期记忆' : '这一类还没有记忆'}</strong>
                      <span>{userMemories.length === 0 ? '在对话里说“请记住我喜欢早起”，或在这里手动添加。' : '切换到其他分类，或在上方添加一条。'}</span>
                    </div>
                  ) : userMemories
                    .filter((item) => memoryFilter === '全部' || item.category === memoryFilter)
                    .map((item) => (
                      <article className={`memory-item category-${item.category}`} key={item.id}>
                        <span>{MEMORY_CATEGORY_LABELS[item.category]}</span>
                        <p>{item.content}</p>
                        <button onClick={() => void deleteMemory(item.id)} type="button">删除</button>
                      </article>
                    ))}
                </div>
              </div>
            </section>
          </div>
        )}

        {isKnowledgePanelOpen && (
          <div className="knowledge-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsKnowledgePanelOpen(false) }}>
            <section aria-labelledby="knowledge-panel-title" aria-modal="true" className="knowledge-panel" role="dialog">
              <header className="knowledge-header">
                <div><span className="knowledge-eyebrow">PRIVATE KNOWLEDGE BASE</span><h2 id="knowledge-panel-title">我的知识库</h2><p>资料仅供你的 Agent 检索，其他用户和后台管理员均无法查看。</p></div>
                <button aria-label="关闭知识库" onClick={() => setIsKnowledgePanelOpen(false)} type="button">×</button>
              </header>
              <div className="knowledge-body">
                <div className="knowledge-stats"><div><strong>{knowledgeDocuments.length}</strong><span>份资料</span></div><div><strong>{knowledgeDocuments.reduce((total, item) => total + item.chunk_count, 0)}</strong><span>检索片段</span></div><div><strong>{formatKnowledgeBytes(knowledgeDocuments.reduce((total, item) => total + item.size_bytes, 0))}</strong><span>占用空间</span></div></div>
                <div className={`knowledge-permission ${currentUser.role === 'knowledge_manager' ? 'is-allowed' : ''}`}><span>{currentUser.role === 'knowledge_manager' ? '✓' : 'i'}</span><p><strong>{currentUser.role === 'knowledge_manager' ? '你可以管理自己的知识库' : '当前账号只有查看权限'}</strong>{currentUser.role === 'knowledge_manager' ? '上传后自动分段并生成语义向量，聊天时会融合关键词与语义检索。' : '你仍然可以检索已有资料；如需上传，请联系管理员调整权限。'}</p></div>
                <form className="knowledge-search" onSubmit={(event) => void searchKnowledge(event)}><div><strong>测试混合检索</strong><span>输入一种与原文不完全相同的问法，查看语义命中、引用和可信度。</span></div><label><input onChange={(event) => setKnowledgeSearchQuery(event.target.value)} placeholder="例如：怎样在修改待办前获得用户确认？" value={knowledgeSearchQuery} /><button disabled={isSearchingKnowledge || !knowledgeSearchQuery.trim()} type="submit">{isSearchingKnowledge ? '检索中…' : '开始检索'}</button></label></form>
                {knowledgeSearchResult && <section className="knowledge-search-results"><header><strong>{knowledgeSearchResult.retrieval_mode === 'hybrid' ? '关键词 + 语义混合检索' : '关键词检索'}</strong><span className={`confidence-${knowledgeSearchResult.confidence}`}>可信度：{knowledgeSearchResult.confidence === 'high' ? '高' : knowledgeSearchResult.confidence === 'medium' ? '中' : '低'}</span></header>{knowledgeSearchResult.message && <p className="knowledge-search-message">{knowledgeSearchResult.message}</p>}{knowledgeSearchResult.results.map((item) => <article key={`${item.source}-${item.chunk}`}><div><strong>{item.source}</strong><span>{item.retrieval} · 命中 {item.score} 分</span></div><p>{item.content}</p><small>引用片段 #{item.chunk}{item.semantic_score !== null ? ` · 语义 ${Math.round(item.semantic_score * 100)}%` : ''}{item.keyword_score !== null ? ` · 关键词 ${Math.round(item.keyword_score * 100)}%` : ''}</small></article>)}</section>}
                {currentUser.role === 'knowledge_manager' && <><div className="knowledge-upload-toolbar"><label><span>上传到分类</span><select value={knowledgeCategory} onChange={(event) => setKnowledgeCategory(event.target.value)}>{KNOWLEDGE_CATEGORIES.map((item) => <option key={item}>{item}</option>)}</select></label><button disabled={isUploadingKnowledge} onClick={() => document.getElementById('knowledge-file-picker')?.click()} type="button">{isUploadingKnowledge ? '正在上传…' : '选择文件上传'}</button></div><input accept=".md,.txt,.pdf,.docx" hidden id="knowledge-file-picker" multiple onChange={(event) => event.target.files && void uploadKnowledgeFiles(event.target.files)} type="file" /><label className="knowledge-dropzone"><input accept=".md,.txt,.pdf,.docx" hidden multiple onChange={(event) => event.target.files && void uploadKnowledgeFiles(event.target.files)} type="file" /><span>↑</span><strong>点击选择资料</strong><small>Markdown、TXT、PDF、Word（.docx）</small></label></>}
                {knowledgeError && <div className="knowledge-error">{knowledgeError}</div>}
                <div className="knowledge-filter"><button className={knowledgeFilter === '全部' ? 'is-active' : ''} onClick={() => setKnowledgeFilter('全部')} type="button">全部</button>{Array.from(new Set(knowledgeDocuments.map((item) => item.category))).map((item) => <button className={knowledgeFilter === item ? 'is-active' : ''} key={item} onClick={() => setKnowledgeFilter(item)} type="button">{item}</button>)}</div>
                <div className="knowledge-documents">{isLoadingKnowledge ? <div className="knowledge-empty">正在读取资料…</div> : knowledgeDocuments.filter((item) => knowledgeFilter === '全部' || item.category === knowledgeFilter).map((item) => <article className="knowledge-document" key={item.id}><span className={`knowledge-file-icon type-${item.file_type}`}>{item.file_type.toUpperCase()}</span><div><strong>{item.original_name}</strong><p><span>{item.category}</span><span>{item.chunk_count} 个片段</span><span>{formatKnowledgeBytes(item.size_bytes)}</span></p>{item.status === 'processing' && <div className="knowledge-progress"><i style={{ width: `${item.embedding_progress}%` }} /><span>正在分段与向量化 {item.embedding_progress}%</span></div>}{item.status === 'failed' && <small>{item.error_message ?? '向量化失败，可点击重试'}</small>}</div><span className={`knowledge-status status-${item.status}`}>{item.status === 'ready' ? '语义索引完成' : item.status === 'processing' ? `${item.embedding_progress}%` : '索引失败'}</span>{currentUser.role === 'knowledge_manager' && <div className="knowledge-actions">{item.status === 'failed' && <button onClick={() => void reindexKnowledgeDocument(item.id)} type="button">重试</button>}<button className="knowledge-delete" onClick={() => void deleteKnowledgeDocument(item.id)} type="button">删除</button></div>}</article>)}</div>
                {!isLoadingKnowledge && knowledgeDocuments.length === 0 && <div className="knowledge-empty"><strong>还没有个人资料</strong><span>上传后，直接在聊天中提问即可优先检索。</span></div>}
              </div>
            </section>
          </div>
        )}

        {isHistoryPanelOpen && (
          <div
            className="history-drawer-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsHistoryPanelOpen(false)
            }}
          >
            <aside
              aria-labelledby="history-panel-title"
              aria-modal="true"
              className="history-drawer"
              role="dialog"
            >
              <div className="history-drawer-header">
                <div className="history-drawer-brand">
                  <img alt="" aria-hidden="true" className="brand-mark" src={projectIcon} />
                  <strong id="history-panel-title">Hello Agent</strong>
                </div>
                <button
                  aria-label="关闭历史会话"
                  className="history-close-button"
                  onClick={() => setIsHistoryPanelOpen(false)}
                  type="button"
                >‹</button>
              </div>
              <button className="history-new-chat" onClick={startNewSession} type="button">
                <span aria-hidden="true">＋</span>
                新建会话
              </button>
              <div className="history-section-label">最近</div>
              <div className="history-panel-content">
                {isLoadingSessions ? (
                  <div className="mcp-loading">
                    <div className="typing-dots"><span /><span /><span /></div>
                    正在读取历史会话…
                  </div>
                ) : historyPanelError ? (
                  <div className="error-message">{historyPanelError}</div>
                ) : historicalSessions.length === 0 ? (
                  <div className="history-empty">还没有可以恢复的旧会话</div>
                ) : historicalSessions.map((item) => (
                  <button
                    className={`history-session-item ${item.session_id === sessionId ? 'is-current' : ''}`}
                    key={item.session_id}
                    onClick={() => void selectHistoricalSession(item.session_id)}
                    type="button"
                  >
                    <span className="history-session-copy">
                      <strong>{item.title}</strong>
                      <small>{item.message_count} 条消息</small>
                    </span>
                    {item.session_id === sessionId && (
                      <span className="history-current-dot" aria-label="当前会话" />
                    )}
                  </button>
                ))}
              </div>
              <div className="history-drawer-footer">
                <img alt="" aria-hidden="true" className="user-avatar" src={projectIcon} />
                <span>
                  <strong>{currentUser.display_name}</strong>
                  <small>{currentUser.email}</small>
                </span>
              </div>
            </aside>
          </div>
        )}

        {isTravelPanelOpen && (
          <div
            className="history-drawer-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsTravelPanelOpen(false)
            }}
          >
            <aside
              aria-labelledby="travel-panel-title"
              aria-modal="true"
              className="history-drawer travel-plans-drawer"
              role="dialog"
            >
              <div className="history-drawer-header">
                <div className="history-drawer-brand">
                  <span className="brand-mark travel-drawer-mark" aria-hidden="true">⌖</span>
                  <strong id="travel-panel-title">我的旅行计划</strong>
                </div>
                <button
                  aria-label="关闭旅行计划"
                  className="history-close-button"
                  onClick={() => setIsTravelPanelOpen(false)}
                  type="button"
                >‹</button>
              </div>
              <button
                className="history-new-chat"
                onClick={() => {
                  setIsTravelPanelOpen(false)
                  selectCommand(TRAVEL_COMMAND)
                }}
                type="button"
              >
                <span aria-hidden="true">＋</span>
                新建旅行计划
              </button>
              <div className="history-section-label">最近更新</div>
              <div className="history-panel-content">
                {isLoadingTravelPlans ? (
                  <div className="mcp-loading">
                    <div className="typing-dots"><span /><span /><span /></div>
                    正在读取旅行计划…
                  </div>
                ) : travelPanelError ? (
                  <div className="error-message">{travelPanelError}</div>
                ) : savedTravelPlans.length === 0 ? (
                  <div className="history-empty">还没有旅行计划</div>
                ) : savedTravelPlans.map((item) => (
                  <button
                    className="travel-plan-list-item"
                    key={item.id}
                    onClick={() => void selectSavedTravelPlan(item.id)}
                    type="button"
                  >
                    <span className="travel-plan-list-heading">
                      <strong>{item.title}</strong>
                      <em className={`travel-list-status status-${item.status}`}>
                        {item.status === 'needs_input' ? '待补充' : item.status === 'ready' ? '待确认' : '已确认'}
                      </em>
                    </span>
                    <small>
                      {item.origin && item.destination ? `${item.origin} → ${item.destination}` : item.summary}
                    </small>
                    <span className="travel-list-progress">
                      <i><b style={{ width: `${item.workflow_completed_count / item.workflow_total_count * 100}%` }} /></i>
                      {item.workflow_completed_count}/{item.workflow_total_count}
                    </span>
                  </button>
                ))}
              </div>
              <div className="history-drawer-footer travel-drawer-footer">
                <span>选择计划后，将在聊天区恢复结构化卡片和工作流状态。</span>
              </div>
            </aside>
          </div>
        )}

        {isMetricsOpen && (
          <Suspense fallback={<div className="workflow-loading">正在加载监控看板…</div>}>
            <MetricsDashboard apiBaseUrl={API_BASE_URL} onClose={() => setIsMetricsOpen(false)} />
          </Suspense>
        )}

        {isObservabilityOpen && (
          <div
            className="mcp-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsObservabilityOpen(false)
            }}
          >
            <section
              aria-labelledby="observability-panel-title"
              aria-modal="true"
              className="mcp-panel observability-panel"
              role="dialog"
            >
              <div className="mcp-panel-header">
                <div>
                  <span className="mcp-eyebrow">Agent Observability</span>
                  <h2 id="observability-panel-title">运行记录与执行链</h2>
                  <p>查看模型、工具、数据库步骤的耗时、Token 和执行结果。</p>
                </div>
                <button
                  aria-label="关闭运行记录"
                  className="mcp-close-button"
                  onClick={() => setIsObservabilityOpen(false)}
                  type="button"
                >×</button>
              </div>
              <div className="observability-content">
                <aside className="run-list">
                  {isLoadingRuns ? (
                    <div className="mcp-loading">
                      <div className="typing-dots"><span /><span /><span /></div>
                      正在读取运行记录…
                    </div>
                  ) : agentRuns.length === 0 ? (
                    <div className="history-empty">完成一次 Agent 操作后，这里会出现记录</div>
                  ) : agentRuns.map((run) => (
                    <button
                      className={`run-list-item ${selectedAgentRun?.id === run.id ? 'is-selected' : ''}`}
                      key={run.id}
                      onClick={() => void loadAgentRun(run.id)}
                      type="button"
                    >
                      <span><strong>{run.title}</strong><em className={`run-status status-${run.status}`}>{run.status === 'success' ? '成功' : run.status === 'failed' ? '失败' : '运行中'}</em></span>
                      <small>{run.run_type} · {run.duration_ms === null ? '—' : `${run.duration_ms} ms`}</small>
                    </button>
                  ))}
                </aside>
                <div className="run-detail">
                  {observabilityError ? (
                    <div className="error-message">{observabilityError}</div>
                  ) : selectedAgentRun ? (
                    <>
                      <div className="run-detail-heading">
                        <div><span>TRACE</span><strong>{selectedAgentRun.id.slice(0, 8)}</strong></div>
                        <em className={`run-status status-${selectedAgentRun.status}`}>{selectedAgentRun.status === 'success' ? '执行成功' : selectedAgentRun.status === 'failed' ? '执行失败' : '运行中'}</em>
                      </div>
                      <h3>{selectedAgentRun.title}</h3>
                      <div className="run-metrics">
                        <span><small>总耗时</small><strong>{selectedAgentRun.duration_ms ?? 0} ms</strong></span>
                        <span><small>Token</small><strong>{selectedAgentRun.total_tokens}</strong></span>
                        <span><small>模型</small><strong>{selectedAgentRun.model ?? '无模型调用'}</strong></span>
                        <span><small>预估费用</small><strong>{selectedAgentRun.estimated_cost === null ? '无 Token' : `$${selectedAgentRun.estimated_cost.toFixed(6)}`}</strong></span>
                      </div>
                      <div className="run-token-breakdown">输入 {selectedAgentRun.prompt_tokens} · 输出 {selectedAgentRun.completion_tokens}</div>
                      <div className="run-step-list">
                        {selectedAgentRun.steps.length === 0 ? (
                          <div className="history-empty">本次运行没有可展示的执行步骤</div>
                        ) : selectedAgentRun.steps.map((step, index) => (
                          <article className={`run-step status-${step.status}`} key={step.id}>
                            <span>{step.status === 'success' ? '✓' : step.status === 'failed' ? '!' : index + 1}</span>
                            <div><strong>{step.name}</strong><small>{step.step_type} · {step.detail}</small></div>
                            <time>{step.duration_ms} ms</time>
                          </article>
                        ))}
                      </div>
                      {selectedAgentRun.error_message && <div className="error-message">{selectedAgentRun.error_message}</div>}
                    </>
                  ) : (
                    <div className="history-empty">请选择一条运行记录</div>
                  )}
                </div>
              </div>
            </section>
          </div>
        )}

        {isEvaluationOpen && createPortal(
          <div
            className="mcp-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget && !isRunningEvaluation) setIsEvaluationOpen(false)
            }}
          >
            <section
              aria-labelledby="evaluation-panel-title"
              aria-modal="true"
              className="mcp-panel evaluation-panel"
              role="dialog"
            >
              <div className="mcp-panel-header">
                <div>
                  <span className="mcp-eyebrow">Agent Evals</span>
                  <h2 id="evaluation-panel-title">自动评测中心</h2>
                  <p>量化工具路由、结构化输出、安全隔离和工作流幂等性。</p>
                </div>
                <button
                  aria-label="关闭评测中心"
                  className="mcp-close-button"
                  disabled={isRunningEvaluation}
                  onClick={() => setIsEvaluationOpen(false)}
                  type="button"
                >×</button>
              </div>
              <div className="evaluation-toolbar">
                <label><small>Prompt 版本</small><select value={selectedPromptId} onChange={(event) => setSelectedPromptId(event.target.value)}>{promptVersions.map((item) => <option key={item.id} value={item.id}>v{item.version} · {item.name}{item.status === 'active' ? '（线上）' : ''}</option>)}</select></label>
                <label><small>测试集</small><select value={selectedDatasetId} onChange={(event) => setSelectedDatasetId(event.target.value)}>{evaluationDatasets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.cases.filter((entry) => entry.enabled).length} 条</option>)}</select></label>
                <label><small>模型</small><input value={evaluationModel} onChange={(event) => setEvaluationModel(event.target.value)} /></label>
                <label><small>评分方式</small><select value={evaluationScorer} onChange={(event) => setEvaluationScorer(event.target.value as 'keyword' | 'exact' | 'llm_judge')}><option value="llm_judge">大模型裁判</option><option value="keyword">关键词</option><option value="exact">精确匹配</option></select></label>
                <label><small>基线版本</small><select value={selectedBaselineRunId} onChange={(event) => setSelectedBaselineRunId(event.target.value)}><option value="">自动选择最近完成版本</option>{evaluationRuns.filter((run) => run.status === 'completed').map((run) => <option key={run.id} value={run.id}>{run.prompt_version_name || run.model} · {run.score}%</option>)}</select></label>
                <label className="evaluation-judge-toggle"><small>裁判开关</small><span><input checked={evaluationJudgeEnabled} onChange={(event) => setEvaluationJudgeEnabled(event.target.checked)} type="checkbox" />启用 LLM Judge</span></label>
                <button disabled={isRunningEvaluation} onClick={() => void runEvaluation()} type="button">
                  {isRunningEvaluation ? '正在提交…' : '后台运行评测'}
                </button>
              </div>
              <div className="evaluation-tabs">
                <button className={evaluationTab === 'results' ? 'is-active' : ''} onClick={() => setEvaluationTab('results')} type="button">结果与对比</button>
                <button className={evaluationTab === 'prompts' ? 'is-active' : ''} onClick={() => setEvaluationTab('prompts')} type="button">Prompt 版本</button>
                <button className={evaluationTab === 'dataset' ? 'is-active' : ''} onClick={() => setEvaluationTab('dataset')} type="button">测试集</button>
              </div>
              <div className="evaluation-body">
              {evaluationError && evaluationTab !== 'results' && <div className="agentops-inline-error error-message">{evaluationError}</div>}
              {evaluationTab === 'results' && <div className="evaluation-content">
                <aside className="evaluation-history">
                  {isLoadingEvaluations ? (
                    <div className="mcp-loading"><div className="typing-dots"><span /><span /><span /></div>读取评测记录…</div>
                  ) : evaluationRuns.length === 0 ? (
                    <div className="history-empty">还没有评测版本</div>
                  ) : evaluationRuns.map((run, index) => (
                    <button
                      className={selectedEvaluation?.id === run.id ? 'is-selected' : ''}
                      key={run.id}
                      onClick={() => void loadEvaluation(run.id)}
                      type="button"
                    >
                      <span><strong>{run.prompt_version_name || `评测 #${evaluationRuns.length - index}`}</strong><em>{run.status === 'completed' ? `${run.score}%` : run.status}</em></span>
                      <small>{run.dataset_name || '核心回归集'} · {run.model} · {run.passed_cases}/{run.total_cases}</small>
                    </button>
                  ))}
                </aside>
                <div className="evaluation-detail">
                  {evaluationError ? (
                    <div className="error-message">{evaluationError}</div>
                  ) : selectedEvaluation ? (
                    <>
                      <div className="evaluation-summary">
                        <div className="evaluation-score"><strong>{selectedEvaluation.score}%</strong><small>总通过率</small></div>
                        <div><span><small>通过</small><strong>{selectedEvaluation.passed_cases}/{selectedEvaluation.total_cases}</strong></span><span><small>耗时</small><strong>{selectedEvaluation.duration_ms ?? 0} ms</strong></span><span><small>Token / 费用</small><strong>{selectedEvaluation.total_tokens} / {selectedEvaluation.estimated_cost == null ? '无 Token' : `$${selectedEvaluation.estimated_cost.toFixed(4)}`}</strong></span><span><small>评测器</small><strong>{evaluationScorerLabel(selectedEvaluation.scorer)}</strong></span><span><small>可信度</small><strong className={`confidence-${selectedEvaluation.confidence}`}>{evaluationConfidenceLabel(selectedEvaluation.confidence)}</strong></span><span><small>裁判</small><strong>{selectedEvaluation.judge_enabled ? '开启' : '关闭'}</strong></span></div>
                      </div>
                      <div className="evaluation-actions">
                        <button onClick={() => void exportEvaluation(selectedEvaluation.id, 'json')} type="button">导出 JSON</button>
                        <button onClick={() => void exportEvaluation(selectedEvaluation.id, 'csv')} type="button">导出 CSV</button>
                        {selectedEvaluation.baseline_run_id && <button onClick={() => void compareEvaluationWithBaseline(selectedEvaluation.id, selectedEvaluation.baseline_run_id!)} type="button">刷新退化对比</button>}
                      </div>
                      {(() => {
                        const previous = evaluationRuns.find((run) => run.id !== selectedEvaluation.id && run.status === 'completed')
                        if (!previous || selectedEvaluation.status !== 'completed') return null
                        return <div className="evaluation-compare"><strong>与上次完成版本对比</strong><span className={selectedEvaluation.score >= previous.score ? 'is-better' : 'is-worse'}>成功率 {(selectedEvaluation.score - previous.score) >= 0 ? '+' : ''}{(selectedEvaluation.score - previous.score).toFixed(1)}%</span><span>耗时 {((selectedEvaluation.duration_ms ?? 0) - (previous.duration_ms ?? 0)) >= 0 ? '+' : ''}{(selectedEvaluation.duration_ms ?? 0) - (previous.duration_ms ?? 0)} ms</span><span>Token {selectedEvaluation.total_tokens - previous.total_tokens >= 0 ? '+' : ''}{selectedEvaluation.total_tokens - previous.total_tokens}</span></div>
                      })()}
                      {evaluationComparison && <div className="evaluation-regressions"><strong>退化分析</strong><div><span>新退化 {evaluationComparison.summary.new_failures ?? 0}</span><span>已修复 {evaluationComparison.summary.fixed ?? 0}</span><span>持续失败 {evaluationComparison.summary.persistent_failures ?? 0}</span></div>{evaluationComparison.cases.filter((item) => item.regression_label && item.regression_label !== 'persistent_pass').map((item) => <article key={item.case_id}><strong>{item.name}</strong><small>{regressionLabelText(item.regression_label)}</small><p>{item.failure_reason || '本次与基线差异已记录。'}</p></article>)}</div>}
                      <div className="evaluation-trends"><strong>最近版本趋势</strong>{evaluationRuns.filter((run) => run.status === 'completed').slice(0, 8).reverse().map((run) => <div key={run.id}><small>{run.prompt_version_name || run.model}</small><span><i style={{ width: `${Math.max(2, run.score)}%` }} /></span><em>{run.score}% · {run.duration_ms ?? 0}ms · {run.total_tokens} Token{run.estimated_cost == null ? '' : ` · $${run.estimated_cost.toFixed(4)}`}</em></div>)}</div>
                      <div className="evaluation-case-list">
                        {selectedEvaluation.cases.map((item) => (
                          <article className={`evaluation-case status-${item.status}`} key={item.id}>
                            <span>{item.status === 'passed' ? '✓' : '!'}</span>
                            <div><strong>{item.name}</strong><small>{item.category} · {item.duration_ms} ms · {evaluationScorerLabel(item.scorer as 'keyword' | 'exact' | 'llm_judge')}</small><p><b>预期：</b>{item.expected}</p><p><b>实际：</b>{item.actual}</p><p><b>得分 / 可信度：</b>{item.score} / <span className={`confidence-${item.confidence}`}>{evaluationConfidenceLabel(item.confidence)}</span></p>{item.regression_label && <p><b>与基线对比：</b>{regressionLabelText(item.regression_label)}</p>}{item.failure_reason && <p className="evaluation-case-error"><b>失败原因：</b>{item.failure_reason}</p>}{item.judge_summary && <p><b>裁判结论：</b>{item.judge_summary}</p>}{item.judge_reasoning && <p><b>裁判说明：</b>{item.judge_reasoning}</p>}{item.error_message && <p className="evaluation-case-error">{item.error_message}</p>}</div>
                          </article>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div className="history-empty">运行一次评测后查看结果</div>
                  )}
                </div>
              </div>}
              {evaluationTab === 'prompts' && <div className="agentops-manage">
                <div className="agentops-list">{promptVersions.map((item) => <article key={item.id}><div><strong>v{item.version} · {item.name}</strong><small>{item.status === 'active' ? '当前线上版本' : item.status === 'draft' ? '草稿' : '历史版本'}</small></div><p>{item.content}</p><button disabled={item.status === 'active'} onClick={() => void activatePromptVersion(item.id)} type="button">{item.status === 'archived' ? '回滚到此版本' : '发布此版本'}</button></article>)}</div>
                <div className="agentops-form"><h3>新建 Prompt 版本</h3><input placeholder="版本名称，例如：更严格的知识边界" value={newPromptName} onChange={(event) => setNewPromptName(event.target.value)} /><textarea placeholder="输入补充规则；系统基础安全规则会继续保留" value={newPromptContent} onChange={(event) => setNewPromptContent(event.target.value)} /><button onClick={() => void createPromptVersion()} type="button">保存为草稿</button></div>
              </div>}
              {evaluationTab === 'dataset' && <div className="agentops-manage">
                <div className="agentops-list">{evaluationDatasets.find((item) => item.id === selectedDatasetId)?.cases.map((item) => <article className={!item.enabled ? 'is-disabled' : ''} key={item.id}><div><strong>{item.name}</strong><small>{item.category} · {evaluationScorerLabel(item.scoring_method)}{item.expected_keywords.length > 0 ? ` · 关键词：${item.expected_keywords.join('、')}` : ''}{item.expected_answer ? ' · 有标准答案' : ''}</small></div><p>{item.input_text}</p>{item.judge_rubric && <p>裁判规则：{item.judge_rubric}</p>}<button onClick={() => void toggleEvaluationCase(item)} type="button">{item.enabled ? '停用' : '启用'}</button></article>)}</div>
                <div className="agentops-form"><h3>添加测试用例</h3><input placeholder="用例名称" value={newCaseName} onChange={(event) => setNewCaseName(event.target.value)} /><textarea placeholder="用户会问什么？" value={newCaseInput} onChange={(event) => setNewCaseInput(event.target.value)} /><select value={newCaseScoringMethod} onChange={(event) => setNewCaseScoringMethod(event.target.value as 'keyword' | 'exact' | 'llm_judge')}><option value="keyword">关键词评分</option><option value="exact">精确匹配</option><option value="llm_judge">大模型裁判</option></select><input placeholder="期望关键词，多个用英文逗号分隔" value={newCaseKeywords} onChange={(event) => setNewCaseKeywords(event.target.value)} /><textarea placeholder="可选：标准答案（精确匹配或裁判参考）" value={newCaseExpectedAnswer} onChange={(event) => setNewCaseExpectedAnswer(event.target.value)} /><textarea placeholder="可选：裁判规则，例如必须拒绝编造、必须说明依据" value={newCaseJudgeRubric} onChange={(event) => setNewCaseJudgeRubric(event.target.value)} /><button onClick={() => void createEvaluationCase()} type="button">添加用例</button><label className="agentops-import">或导入 JSON 测试集<input accept="application/json,.json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void importEvaluationCases(file); event.currentTarget.value = '' }} type="file" /><small>格式：name、input_text、expected_keywords 数组</small></label></div>
              </div>}
              </div>
            </section>
          </div>,
          document.body,
        )}

        {isSkillsPanelOpen && (
          <div
            className="mcp-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsSkillsPanelOpen(false)
            }}
          >
            <section
              aria-labelledby="skills-panel-title"
              aria-modal="true"
              className="mcp-panel skills-panel"
              role="dialog"
            >
              <div className="mcp-panel-header">
                <div>
                  <span className="mcp-eyebrow">Agent Skills</span>
                  <h2 id="skills-panel-title">按需加载的工作说明书</h2>
                  <p>目录只占很少上下文；点开后才会读取完整 SKILL.md，这和 MCP 工具常驻 schema 不同。</p>
                </div>
                <button
                  aria-label="关闭 Agent Skills 面板"
                  className="mcp-close-button"
                  onClick={() => setIsSkillsPanelOpen(false)}
                  type="button"
                >
                  ×
                </button>
              </div>
              <div className="mcp-panel-content">
                {isLoadingSkills ? (
                  <div className="mcp-loading">
                    <div className="typing-dots" aria-label="正在读取 Skills 目录">
                      <span />
                      <span />
                      <span />
                    </div>
                    正在读取 Skill 目录…
                  </div>
                ) : skillsError ? (
                  <div className="error-message" role="alert">{skillsError}</div>
                ) : (
                  <div className="skill-list">
                    {agentSkills.map((skill) => {
                      const isOpen = selectedSkillName === skill.name
                      return (
                        <article className={`skill-card${isOpen ? ' is-open' : ''}`} key={skill.name}>
                          <button onClick={() => void loadSkillBody(skill.name)} type="button">
                            <code>{skill.name}</code>
                            <small>{skill.body ? '正文已加载' : '目录项 · 点击加载正文'}</small>
                            <p>{skill.description}</p>
                          </button>
                          {isOpen && skill.body && <pre className="skill-body">{skill.body}</pre>}
                        </article>
                      )
                    })}
                  </div>
                )}
              </div>
            </section>
          </div>
        )}

        {isMCPPanelOpen && (
          <div
            className="mcp-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsMCPPanelOpen(false)
            }}
          >
            <section
              aria-labelledby="mcp-panel-title"
              aria-modal="true"
              className="mcp-panel"
              role="dialog"
            >
              <div className="mcp-panel-header">
                <div>
                  <span className="mcp-eyebrow">Agent 能力</span>
                  <h2 id="mcp-panel-title">MCP 工具</h2>
                  <p>内置天气/知识库始终可用；也可添加远程 SSE 或 HTTP MCP，供对话与工作流调用。</p>
                </div>
                <button
                  aria-label="关闭 MCP 工具面板"
                  className="mcp-close-button"
                  onClick={() => setIsMCPPanelOpen(false)}
                  type="button"
                >
                  ×
                </button>
              </div>

              <div className="mcp-panel-content">
                <form className="mcp-remote-form" onSubmit={(event) => void createRemoteMcp(event)}>
                  <div>
                    <strong>添加远程 MCP</strong>
                    <span>填写已部署的 MCP 地址。Token 仅保存在你的账号下，不会回显。</span>
                  </div>
                  <label>
                    <span>名称</span>
                    <input onChange={(event) => setRemoteMcpName(event.target.value)} placeholder="例如：公司内部工具" value={remoteMcpName} />
                  </label>
                  <label>
                    <span>地址</span>
                    <input onChange={(event) => setRemoteMcpUrl(event.target.value)} placeholder="https://example.com/mcp" value={remoteMcpUrl} />
                  </label>
                  <label>
                    <span>传输</span>
                    <select onChange={(event) => setRemoteMcpTransport(event.target.value as 'streamable_http' | 'sse')} value={remoteMcpTransport}>
                      <option value="streamable_http">Streamable HTTP</option>
                      <option value="sse">SSE</option>
                    </select>
                  </label>
                  <label>
                    <span>Bearer Token（可选）</span>
                    <input onChange={(event) => setRemoteMcpToken(event.target.value)} placeholder="留空表示无需鉴权" type="password" value={remoteMcpToken} />
                  </label>
                  <button disabled={isSavingRemoteMcp || !remoteMcpName.trim() || !remoteMcpUrl.trim()} type="submit">
                    {isSavingRemoteMcp ? '连接中…' : '添加并探测'}
                  </button>
                </form>
                {mcpError && <div className="error-message" role="alert">{mcpError}</div>}
                {isLoadingMCPTools ? (
                  <div className="mcp-loading">
                    <div className="typing-dots" aria-label="正在发现 MCP 工具">
                      <span />
                      <span />
                      <span />
                    </div>
                    正在连接 MCP Server…
                  </div>
                ) : (
                  mcpServers.map((server) => (
                    <div className={`mcp-server ${server.builtin ? 'is-builtin' : ''} status-${server.status}`} key={`${server.builtin ? 'builtin' : 'remote'}-${server.id ?? server.name}`}>
                      <div className="mcp-server-heading">
                        <div>
                          <strong>{server.name}</strong>
                          <span>
                            {server.builtin ? '内置' : '远程'}
                            {server.url ? ` · ${server.url}` : ''}
                            {server.has_auth ? ' · 已配置 Token' : ''}
                            {` · ${server.tools.length} 个工具`}
                          </span>
                        </div>
                        <div className="mcp-server-actions">
                          <span className="transport-badge">{server.transport}</span>
                          {!server.builtin && (
                            <>
                              <button onClick={() => void toggleRemoteMcp(server)} type="button">
                                {server.enabled ? '停用' : '启用'}
                              </button>
                              <button className="mcp-delete" onClick={() => void deleteRemoteMcp(server)} type="button">删除</button>
                            </>
                          )}
                        </div>
                      </div>
                      {server.error_message && <div className="mcp-server-error">{server.error_message}</div>}
                      {server.status === 'disabled' ? (
                        <div className="mcp-disabled-hint">已停用，对话不会加载这些工具。</div>
                      ) : (
                        <div className="mcp-tool-list">
                          {server.tools.map((tool) => (
                            <article className="mcp-tool-card" key={tool.name}>
                              <div className="mcp-tool-icon" aria-hidden="true">⌁</div>
                              <div>
                                <code>{tool.name}</code>
                                <p>{tool.description}</p>
                                <div className="mcp-parameters">
                                  {Object.entries(tool.parameters.properties ?? {}).map(
                                    ([name, details]) => (
                                      <span key={name}>
                                        {name}
                                        {tool.parameters.required?.includes(name) ? ' *' : ''}
                                        <small>{details.type ?? 'any'}</small>
                                      </span>
                                    ),
                                  )}
                                </div>
                              </div>
                            </article>
                          ))}
                          {server.tools.length === 0 && !server.error_message && (
                            <div className="history-empty">暂未发现工具</div>
                          )}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </section>
          </div>
        )}

        {isOpenApiPanelOpen && (
          <div
            className="mcp-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setIsOpenApiPanelOpen(false)
            }}
          >
            <section
              aria-labelledby="openapi-panel-title"
              aria-modal="true"
              className="mcp-panel"
              role="dialog"
            >
              <div className="mcp-panel-header">
                <div>
                  <span className="mcp-eyebrow">Agent 能力</span>
                  <h2 id="openapi-panel-title">OpenAPI 工具</h2>
                  <p>粘贴 OpenAPI JSON，勾选接口后即可在对话中调用；写操作会要求确认，内网地址会被拒绝。</p>
                </div>
                <button
                  aria-label="关闭 OpenAPI 工具面板"
                  className="mcp-close-button"
                  onClick={() => setIsOpenApiPanelOpen(false)}
                  type="button"
                >
                  ×
                </button>
              </div>

              <div className="mcp-panel-content">
                <form className="mcp-remote-form openapi-import-form" onSubmit={(event) => void (openApiPreview ? createOpenApiSource(event) : previewOpenApi(event))}>
                  <div>
                    <strong>导入 OpenAPI</strong>
                    <span>目前仅支持 JSON。先解析预览，再勾选要启用的接口。</span>
                  </div>
                  <label>
                    <span>名称</span>
                    <input onChange={(event) => setOpenApiName(event.target.value)} placeholder="例如：宠物商店 API" value={openApiName} />
                  </label>
                  <label>
                    <span>API 根地址</span>
                    <input onChange={(event) => setOpenApiBaseUrl(event.target.value)} placeholder="可留空，使用文档 servers.url" value={openApiBaseUrl} />
                  </label>
                  <label className="openapi-spec-field">
                    <span>OpenAPI JSON</span>
                    <textarea
                      onChange={(event) => {
                        setOpenApiSpec(event.target.value)
                        setOpenApiPreview(null)
                      }}
                      placeholder='{"openapi":"3.0.0","info":{"title":"Demo"},"servers":[{"url":"https://example.com"}],"paths":{...}}'
                      rows={8}
                      value={openApiSpec}
                    />
                  </label>
                  <label>
                    <span>Bearer Token（可选）</span>
                    <input onChange={(event) => setOpenApiToken(event.target.value)} placeholder="留空表示无需鉴权" type="password" value={openApiToken} />
                  </label>
                  {openApiPreview && (
                    <div className="openapi-ops-picker">
                      <strong>勾选要启用的接口（{openApiSelectedOps.length}/{openApiPreview.operations.length}）</strong>
                      <div className="openapi-ops-list">
                        {openApiPreview.operations.map((operation) => {
                          const checked = openApiSelectedOps.includes(operation.operation_id)
                          return (
                            <label className="openapi-op-item" key={operation.operation_id}>
                              <input
                                checked={checked}
                                onChange={() => {
                                  setOpenApiSelectedOps((current) =>
                                    checked
                                      ? current.filter((id) => id !== operation.operation_id)
                                      : [...current, operation.operation_id],
                                  )
                                }}
                                type="checkbox"
                              />
                              <span>
                                <code>{operation.method}</code> {operation.path}
                                <small>{operation.summary}{operation.unsafe ? ' · 高风险' : ''}</small>
                              </span>
                            </label>
                          )
                        })}
                      </div>
                    </div>
                  )}
                  <button
                    disabled={
                      openApiPreview
                        ? isSavingOpenApi || !openApiName.trim() || openApiSelectedOps.length === 0
                        : isPreviewingOpenApi || !openApiSpec.trim()
                    }
                    type="submit"
                  >
                    {openApiPreview
                      ? isSavingOpenApi
                        ? '导入中…'
                        : '确认导入'
                      : isPreviewingOpenApi
                        ? '解析中…'
                        : '解析并预览'}
                  </button>
                </form>
                {openApiError && <div className="error-message" role="alert">{openApiError}</div>}
                {isLoadingOpenApi ? (
                  <div className="mcp-loading">
                    <div className="typing-dots" aria-label="正在读取 OpenAPI 来源">
                      <span />
                      <span />
                      <span />
                    </div>
                    正在读取已导入来源…
                  </div>
                ) : openApiSources.length === 0 ? (
                  <div className="history-empty">还没有导入 OpenAPI 来源</div>
                ) : (
                  openApiSources.map((source) => (
                    <div className={`mcp-server status-${source.status}`} key={source.id}>
                      <div className="mcp-server-heading">
                        <div>
                          <strong>{source.name}</strong>
                          <span>
                            {source.base_url}
                            {source.has_auth ? ' · 已配置 Token' : ''}
                            {` · ${source.tools.length} 个工具`}
                          </span>
                        </div>
                        <div className="mcp-server-actions">
                          <span className="transport-badge">OpenAPI</span>
                          <button onClick={() => void toggleOpenApiSource(source)} type="button">
                            {source.enabled ? '停用' : '启用'}
                          </button>
                          <button className="mcp-delete" onClick={() => void deleteOpenApiSource(source)} type="button">删除</button>
                        </div>
                      </div>
                      {source.error_message && <div className="mcp-server-error">{source.error_message}</div>}
                      {source.status === 'disabled' ? (
                        <div className="mcp-disabled-hint">已停用，对话不会加载这些工具。</div>
                      ) : (
                        <div className="mcp-tool-list">
                          {source.tools.map((tool) => (
                            <article className="mcp-tool-card" key={tool.name}>
                              <div className="mcp-tool-icon" aria-hidden="true">⧉</div>
                              <div>
                                <code>{tool.name}</code>
                                <p>{tool.description}</p>
                                {(tool.method || tool.path) && (
                                  <div className="mcp-parameters">
                                    <span>{tool.method} {tool.path}</span>
                                  </div>
                                )}
                              </div>
                            </article>
                          ))}
                          {source.tools.length === 0 && !source.error_message && (
                            <div className="history-empty">暂未启用工具</div>
                          )}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </section>
          </div>
        )}

        <div className="message-list" aria-live="polite">
          {messages.map((message) => (
            <article
              className={`message message-${message.role}`}
              key={message.id}
            >
              {message.role !== 'notice' && (
                <span className="message-label">
                  {message.role === 'user' ? '你' : 'Agent'}
                </span>
              )}
              {message.travelPlan && message.travelPlanId !== undefined ? (
                <TravelPlanCard
                  plan={message.travelPlan}
                  travelPlanId={message.travelPlanId}
                  onConfirm={() => confirmTravelPlan(message.travelPlanId!)}
                  onGeneratePreparations={() => generateTravelPreparations(message.travelPlanId!)}
                  onSyncPreparations={(steps) => syncTravelPreparations(message.travelPlanId!, steps)}
                  onLoadWorkflow={() => loadTravelWorkflow(message.travelPlanId!)}
                />
              ) : message.plan ? (
                <PlanCard
                  plan={message.plan}
                  onSyncTodos={(steps) => syncPlanTodos(message.plan!, steps)}
                />
              ) : message.role === 'assistant' ? (
                <div className="assistant-response">
                  {message.activities && message.activities.length > 0 && (
                    <ToolActivityTrace activities={message.activities} />
                  )}
                  {message.content && (
                    <MarkdownMessage
                      content={message.content}
                      isStreaming={message.id === streamingMessageId}
                      sources={message.sources}
                    />
                  )}
                  {message.capturedRegression && (
                    <p className="citation-regression-note">
                      已自动收录到「可信问答回归集」
                    </p>
                  )}
                </div>
              ) : (
                <div className="message-content">
                  {message.role === 'user' && message.operation && (
                    <span className="message-operation">
                      <span aria-hidden="true">⌁</span>
                      {message.operation}
                    </span>
                  )}
                  <span>{message.content}</span>
                </div>
              )}
            </article>
          ))}

          {isWaitingForFirstToken && (
            <article className="message message-assistant loading-message">
              <span className="message-label">Agent</span>
              <div className="typing-dots" aria-label="Agent 正在思考">
                <span />
                <span />
                <span />
              </div>
            </article>
          )}

          {pendingCollaboration && !isLoading && (
            <aside className="approval-card">
              <div>
                <strong>多 Agent 计划待确认</strong>
                <p>规划已完成。是否按此计划继续执行与审核？</p>
              </div>
              <div className="approval-actions">
                <button className="secondary-button" onClick={() => void rejectCollaborationPlan()}>
                  取消
                </button>
                <button className="primary-button" onClick={() => void approveCollaborationPlan()}>
                  按此计划执行
                </button>
              </div>
            </aside>
          )}

          {pendingApproval && !isLoading && (
            <aside className="approval-card">
              <div>
                <strong>需要你的确认</strong>
                <p>这个操作会修改 Todo 数据，是否继续？</p>
              </div>
              <div className="approval-actions">
                <button className="secondary-button" onClick={rejectPendingAction}>
                  取消
                </button>
                <button className="primary-button" onClick={approvePendingAction}>
                  确认执行
                </button>
              </div>
            </aside>
          )}

          {error && (
            <div className="error-message" role="alert">
              {error}
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <footer className="composer-area">
          {(chatAttachments.length > 0 || attachmentError) && (
            <div className="chat-attachment-bar">
              {chatAttachments.map((item) => (
                <span className="chat-attachment-chip" key={item.id} title={item.preview || item.original_name}>
                  <strong>{item.original_name}</strong>
                  <small>
                    {item.extraction_method === 'ocr' ? 'OCR · ' : ''}
                    {item.char_count} 字
                  </small>
                  <button
                    aria-label={`移除 ${item.original_name}`}
                    disabled={isUploadingAttachment || isLoading}
                    onClick={() => void deleteChatAttachment(item.id)}
                    type="button"
                  >
                    ×
                  </button>
                </span>
              ))}
              {attachmentError && (
                <span className="chat-attachment-error" role="alert">
                  {attachmentError}
                </span>
              )}
            </div>
          )}
          {pendingTravelTask && (
            <aside className="travel-resume-banner">
              <div>
                <span>旅行任务 #{pendingTravelTask.id} · V{pendingTravelTask.version}</span>
                <strong>等待你补充信息</strong>
                <small>{pendingTravelTask.plan.clarification_questions.join(' ')}</small>
              </div>
              <button
                onClick={() => {
                  setActiveCommand(TRAVEL_COMMAND)
                  setComposerMode('travel')
                  requestAnimationFrame(() => inputRef.current?.focus())
                }}
                type="button"
              >
                继续补充
              </button>
            </aside>
          )}
          {isCommandMenuOpen && (
            <div
              aria-label="可用操作"
              className="command-menu"
              id="slash-command-menu"
              role="listbox"
            >
              <div className="command-menu-title">
                <span>可用操作</span>
                <kbd>↑↓ 选择 · Enter 确认</kbd>
              </div>
              {visibleCommands.length > 0 ? (
                visibleCommands.map((command, index) => (
                  <button
                    aria-selected={index === highlightedCommandIndex}
                    className={index === highlightedCommandIndex ? 'is-selected' : ''}
                    id={`slash-command-${index}`}
                    key={command.command}
                    onClick={() => selectCommand(command)}
                    onMouseDown={(event) => event.preventDefault()}
                    role="option"
                    type="button"
                  >
                    <span className="command-name">{command.command}</span>
                    <span className="command-details">
                      <strong>{command.label}</strong>
                      <small>{command.description}</small>
                    </span>
                  </button>
                ))
              ) : (
                <p className="command-empty">没有匹配的操作</p>
              )}
            </div>
          )}
          <form className="composer" onSubmit={handleSubmit}>
            {activeCommand && (
              <span className="composer-mode">
                {activeCommand.label}
                <button
                  aria-label={`退出${activeCommand.label}模式`}
                  onClick={() => {
                    setActiveCommand(null)
                    setComposerMode('chat')
                  }}
                  type="button"
                >
                  ×
                </button>
              </span>
            )}
            <textarea
              aria-activedescendant={
                isCommandMenuOpen && visibleCommands.length > 0
                  ? `slash-command-${highlightedCommandIndex}`
                  : undefined
              }
              aria-controls="slash-command-menu"
              aria-expanded={isCommandMenuOpen}
              aria-haspopup="listbox"
              aria-label="给 Agent 发送消息"
              disabled={isLoading}
              onChange={(event) => {
                const nextInput = event.target.value
                setInput(nextInput)
                if (nextInput.startsWith('/') && activeCommand) {
                  setActiveCommand(null)
                  setComposerMode('chat')
                }
                setIsCommandMenuDismissed(false)
              }}
              onKeyDown={(event) => {
                if (isCommandMenuOpen) {
                  if (event.key === 'ArrowDown' && visibleCommands.length > 0) {
                    event.preventDefault()
                    setSelectedCommandIndex(
                      (current) => (current + 1) % visibleCommands.length,
                    )
                    return
                  }
                  if (event.key === 'ArrowUp' && visibleCommands.length > 0) {
                    event.preventDefault()
                    setSelectedCommandIndex(
                      (current) =>
                        (current - 1 + visibleCommands.length) %
                        visibleCommands.length,
                    )
                    return
                  }
                  if (event.key === 'Enter' && visibleCommands.length > 0) {
                    event.preventDefault()
                    selectCommand(visibleCommands[highlightedCommandIndex])
                    return
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    setIsCommandMenuDismissed(true)
                    return
                  }
                }
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
              placeholder={
                activeCommand
                  ? activeCommand.requiresInput === false
                    ? `按 Enter 执行“${activeCommand.label}”`
                    : activeCommand.description
                  : '输入消息，或输入 / 查看可用操作'
              }
              ref={inputRef}
              rows={1}
              value={input}
            />
            <input
              accept=".md,.txt,.pdf,.docx,.png,.jpg,.jpeg,.webp"
              hidden
              id="chat-attachment-picker"
              multiple
              onChange={(event) => {
                if (event.target.files) {
                  void uploadChatAttachments(event.target.files)
                  event.target.value = ''
                }
              }}
              type="file"
            />
            <button
              aria-label="上传文本或图片附件"
              className="attach-button"
              disabled={isLoading || isUploadingAttachment || !currentUser}
              onClick={() =>
                document.getElementById('chat-attachment-picker')?.click()
              }
              title="上传 Markdown / TXT / PDF / Word / 图片（扫描件与图片走本地 OCR）"
              type="button"
            >
              {isUploadingAttachment ? (
                '…'
              ) : (
                <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18">
                  <path
                    d="M14.5 7.5 8.2 13.8a3 3 0 1 0 4.2 4.2l7.1-7.1a4.5 4.5 0 0 0-6.4-6.4L6 11.6"
                    stroke="currentColor"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="1.8"
                  />
                </svg>
              )}
            </button>
            <button
              aria-label="发送消息"
              className="send-button"
              disabled={
                isLoading ||
                (!input.trim() && (!activeCommand || activeCommand.requiresInput !== false))
              }
              type="submit"
            >
              <span aria-hidden="true">↑</span>
            </button>
          </form>
          <div className="composer-meta">
            <span>Enter 发送 · Shift + Enter 换行 · 可附带文本/图片（OCR）</span>
            <span>{sessionId ? `会话 ${sessionId.slice(0, 8)}` : '新会话'}</span>
          </div>
        </footer>
      </section>
    </main>
  )
}

export default App
