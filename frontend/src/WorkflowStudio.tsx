import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from '@xyflow/react'
import type { Connection, Edge, Node, NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './WorkflowStudio.css'

type WorkflowNodeType = 'input' | 'agent' | 'knowledge' | 'llm' | 'mcp' | 'todo' | 'http' | 'transform' | 'foreach' | 'subworkflow' | 'condition' | 'approval' | 'output'
type WorkflowNodeData = {
  label: string
  nodeType: WorkflowNodeType
  config: Record<string, unknown>
  runStatus?: 'running' | 'success' | 'failed' | 'skipped' | 'waiting'
  runSummary?: string
}
type FlowNode = Node<WorkflowNodeData, 'workflow'>

type SavedWorkflow = {
  id: string
  name: string
  description: string
  definition: {
    nodes: Array<{
      id: string
      type: WorkflowNodeType
      label: string
      position: { x: number; y: number }
      config: Record<string, unknown>
    }>
    edges: Array<{ id: string; source: string; target: string; source_handle?: string | null }>
  }
  updated_at: string
}

type AgentSkillOption = {
  name: string
  description: string
}

type McpToolOption = {
  name: string
  description: string
}

type WorkflowRun = {
  id: string
  status: 'queued' | 'running' | 'waiting_approval' | 'success' | 'failed' | 'cancelled'
  output_text: string
  error_message: string | null
  duration_ms: number | null
  steps: Array<{
    node_id: string
    node_type: WorkflowNodeType
    label: string
    status: 'running' | 'success' | 'failed' | 'skipped' | 'waiting'
    duration_ms: number
    summary: string
    input?: unknown
    output?: unknown
  }>
}

type WorkflowLaunch = {
  run: WorkflowRun
  task: { id: string; status: string; progress: number }
}

const NODE_META: Record<WorkflowNodeType, { label: string; icon: string; hint: string }> = {
  input: { label: '输入', icon: '↳', hint: '接收用户问题' },
  agent: { label: 'Agent', icon: '✦', hint: '分析并动态调用工具' },
  knowledge: { label: '知识库', icon: '▤', hint: '检索相关资料' },
  llm: { label: '大模型', icon: '✦', hint: 'DeepSeek 推理' },
  mcp: { label: 'MCP 工具', icon: '⌘', hint: '调用已发现的 MCP 工具' },
  todo: { label: '添加 Todo', icon: '☑', hint: '写入一条待办任务' },
  http: { label: 'HTTP 请求', icon: '↗', hint: '调用外部 HTTP API' },
  transform: { label: '数据转换', icon: '⇄', hint: '模板、拆分或提取字段' },
  foreach: { label: '循环批处理', icon: '↻', hint: '逐项处理列表或子工作流' },
  subworkflow: { label: '子工作流', icon: '⧉', hint: '调用另一条已保存工作流' },
  condition: { label: '条件分支', icon: '◇', hint: '根据规则选择路线' },
  approval: { label: '人工确认', icon: '✓', hint: '暂停并等待确认' },
  output: { label: '输出', icon: '→', hint: '返回最终结果' },
}

const TEMPLATE_NODES: FlowNode[] = (['input', 'agent', 'approval', 'output'] as WorkflowNodeType[]).map((nodeType, index) => ({
  id: `${nodeType}-1`, type: 'workflow', position: { x: 80 + index * 270, y: 190 },
  data: { label: NODE_META[nodeType].label, nodeType, config: defaultConfig(nodeType) },
}))

const TEMPLATE_EDGES: Edge[] = TEMPLATE_NODES.slice(0, -1).map((node, index) => ({
  id: `edge-${node.id}-${TEMPLATE_NODES[index + 1].id}`,
  source: node.id,
  target: TEMPLATE_NODES[index + 1].id,
  animated: true,
}))

function defaultConfig(type: WorkflowNodeType): Record<string, unknown> {
  if (type === 'agent') return { prompt: '理解用户任务，按需调用知识库或天气工具，然后给出完整回答。', tools: ['search_knowledge', 'get_weather'], skills: [], max_rounds: 4 }
  if (type === 'knowledge') return { limit: 3 }
  if (type === 'llm') return { prompt: '请结合知识库与工具结果，给出准确、简洁的回答。' }
  if (type === 'mcp') return { tool: 'get_weather', days: 3, location: '', arguments_json: '' }
  if (type === 'todo') return { title: '' }
  if (type === 'http') return { method: 'GET', url: '', headers: '', body: '', timeout: 8 }
  if (type === 'transform') return { mode: 'template', template: '{{upstream}}', path: '', separator: '\n' }
  if (type === 'foreach') return { path: '', separator: '\n', template: '{{item}}', max_items: 10, workflow_id: '', join: '' }
  if (type === 'subworkflow') return { workflow_id: '', input_template: '{{upstream}}' }
  if (type === 'condition') return { contains: '需要确认' }
  if (type === 'approval') return { message: 'Agent 已生成结果，确认后继续输出。' }
  return {}
}

function WorkflowNode({ data, selected }: NodeProps<FlowNode>) {
  const meta = NODE_META[data.nodeType]
  return (
    <article className={`workflow-node type-${data.nodeType} ${selected ? 'is-selected' : ''} ${data.runStatus ? `is-${data.runStatus}` : ''}`}>
      {data.nodeType !== 'input' && <Handle type="target" position={Position.Left} />}
      <span className="workflow-node-icon">{meta.icon}</span>
      <div>
        <strong>{data.label}</strong>
        <small>{data.runSummary ?? meta.hint}</small>
      </div>
      {data.runStatus && <span className="workflow-node-state">{data.runStatus === 'success' ? '✓' : data.runStatus === 'running' ? '…' : data.runStatus === 'waiting' ? 'Ⅱ' : data.runStatus === 'skipped' ? '–' : '!'}</span>}
      {data.nodeType === 'condition' ? <><Handle id="true" type="source" position={Position.Right} style={{ top: 24 }} /><Handle id="false" type="source" position={Position.Right} style={{ top: 52 }} /></> : data.nodeType !== 'output' && <Handle type="source" position={Position.Right} />}
    </article>
  )
}

const nodeTypes = { workflow: WorkflowNode }

export function WorkflowStudio({ apiBaseUrl, onClose }: { apiBaseUrl: string; onClose: () => void }) {
  return createPortal(
    <ReactFlowProvider>
      <WorkflowStudioCanvas apiBaseUrl={apiBaseUrl} onClose={onClose} />
    </ReactFlowProvider>,
    document.body,
  )
}

function WorkflowStudioCanvas({ apiBaseUrl, onClose }: { apiBaseUrl: string; onClose: () => void }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(TEMPLATE_NODES)
  const [edges, setEdges, onEdgesChange] = useEdgesState(TEMPLATE_EDGES)
  const [workflows, setWorkflows] = useState<SavedWorkflow[]>([])
  const [workflowId, setWorkflowId] = useState<string | null>(null)
  const [name, setName] = useState('智能问答工作流')
  const [description, setDescription] = useState('检索企业知识，并结合天气 MCP 生成回答')
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [runInput, setRunInput] = useState('上海明天天气怎么样？请结合知识库给出出行建议。')
  const [latestRun, setLatestRun] = useState<WorkflowRun | null>(null)
  const [pendingRunId, setPendingRunId] = useState<string | null>(null)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [isRunning, setIsRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [availableSkills, setAvailableSkills] = useState<AgentSkillOption[]>([])
  const [mcpTools, setMcpTools] = useState<McpToolOption[]>([
    { name: 'get_weather', description: '天气' },
    { name: 'search_knowledge', description: '知识库' },
  ])
  const canvasRef = useRef<HTMLDivElement>(null)
  const { screenToFlowPosition, fitView } = useReactFlow()

  const selectedNode = useMemo(
    () => nodes.find((node) => node.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId],
  )
  const selectedStep = useMemo(
    () => latestRun?.steps.find((step) => step.node_id === selectedNodeId) ?? null,
    [latestRun, selectedNodeId],
  )

  const loadWorkflows = useCallback(async () => {
    const response = await fetch(`${apiBaseUrl}/workflows`, { credentials: 'include' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '读取工作流失败')
    setWorkflows(data.workflows as SavedWorkflow[])
  }, [apiBaseUrl])

  useEffect(() => { void loadWorkflows().catch((requestError) => setError(String(requestError))) }, [loadWorkflows])

  useEffect(() => {
    void fetch(`${apiBaseUrl}/skills`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? '读取 Agent Skills 失败')
        setAvailableSkills((data.skills ?? []) as AgentSkillOption[])
      })
      .catch((requestError) => setError(String(requestError)))
    void fetch(`${apiBaseUrl}/mcp/tools`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? '读取 MCP 工具失败')
        const tools = ((data.servers ?? []) as Array<{ tools?: McpToolOption[] }>)
          .flatMap((server) => server.tools ?? [])
          .filter((tool, index, list) => list.findIndex((item) => item.name === tool.name) === index)
        if (tools.length) setMcpTools(tools)
      })
      .catch(() => undefined)
  }, [apiBaseUrl])

  const onConnect = useCallback(
    (connection: Connection) => setEdges((current) => addEdge({ ...connection, animated: true }, current)),
    [setEdges],
  )

  const onDrop = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    const nodeType = event.dataTransfer.getData('application/workflow-node') as WorkflowNodeType
    if (!NODE_META[nodeType]) return
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
    const id = `${nodeType}-${Date.now()}`
    setNodes((current) => [...current, {
      id,
      type: 'workflow',
      position,
      data: { label: NODE_META[nodeType].label, nodeType, config: defaultConfig(nodeType) },
    }])
    setSelectedNodeId(id)
  }, [screenToFlowPosition, setNodes])

  const serialize = () => ({
    nodes: nodes.map((node) => ({
      id: node.id,
      type: node.data.nodeType,
      label: node.data.label,
      position: node.position,
      config: node.data.config,
    })),
    edges: edges.map((edge) => ({ id: edge.id, source: edge.source, target: edge.target, source_handle: edge.sourceHandle })),
  })

  const save = async () => {
    setIsSaving(true)
    setError(null)
    try {
      const response = await fetch(`${apiBaseUrl}/workflows`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: workflowId, name, description, definition: serialize() }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail?.[0]?.msg ?? data.detail ?? '保存失败')
      const saved = data as SavedWorkflow
      setWorkflowId(saved.id)
      await loadWorkflows()
      return saved.id
    } finally {
      setIsSaving(false)
    }
  }

  const applyRunStep = (step: WorkflowRun['steps'][number]) => {
    setNodes((current) => current.map((node) => node.id === step.node_id ? {
      ...node,
      data: { ...node.data, runStatus: step.status, runSummary: `${step.summary}${step.duration_ms ? ` · ${step.duration_ms}ms` : ''}` },
    } : node))
    const color = step.status === 'running' ? '#6947e2' : step.status === 'success' ? '#2daa79' : step.status === 'waiting' ? '#e59335' : step.status === 'skipped' ? '#aaa2b2' : '#d95269'
    setEdges((current) => current.map((edge) => edge.target === step.node_id
      ? { ...edge, animated: step.status === 'running', style: { ...edge.style, stroke: color, strokeWidth: 2.2 } }
      : edge))
  }

  const applyRun = (run: WorkflowRun) => {
    setLatestRun(run)
    run.steps.forEach(applyRunStep)
  }

  const pollRun = async (runId: string) => {
    while (true) {
      const response = await fetch(`${apiBaseUrl}/workflow-runs/${runId}`, { credentials: 'include' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '读取运行状态失败')
      const run = data as WorkflowRun
      applyRun(run)
      if (run.status === 'queued' || run.status === 'running') {
        await new Promise((resolve) => window.setTimeout(resolve, 700))
        continue
      }
      return run
    }
  }

  const launchAndWatch = async (response: Response) => {
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '运行失败')
    const launched = data as WorkflowLaunch
    setPendingRunId(launched.run.id)
    setActiveTaskId(launched.task.id)
    applyRun(launched.run)
    const run = await pollRun(launched.run.id)
    if (run.status === 'failed') setError(run.error_message ?? '运行失败')
    if (run.status === 'cancelled') setError(run.error_message ?? '运行已取消')
    setIsRunning(false)
    return run
  }

  const run = async () => {
    setIsRunning(true)
    setLatestRun(null)
    setPendingRunId(null)
    setActiveTaskId(null)
    setError(null)
    setNodes((current) => current.map((node) => ({ ...node, data: { ...node.data, runStatus: undefined, runSummary: undefined } })))
    setEdges((current) => current.map((edge) => ({ ...edge, animated: false, style: undefined })))
    try {
      const activeId = await save()
      await launchAndWatch(await fetch(`${apiBaseUrl}/workflows/${activeId}/runs`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: runInput }),
      }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '运行失败')
      setIsRunning(false)
    }
  }

  const approveRun = async () => {
    if (!pendingRunId) return
    setIsRunning(true)
    setError(null)
    try {
      await launchAndWatch(await fetch(`${apiBaseUrl}/workflow-runs/${pendingRunId}/approve`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true }),
      }))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '继续运行失败')
      setIsRunning(false)
    }
  }

  const cancelRun = async () => {
    if (!activeTaskId) return
    setError(null)
    try {
      const response = await fetch(`${apiBaseUrl}/tasks/${activeTaskId}/cancel`, {
        method: 'POST', credentials: 'include',
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '取消失败')
      if (pendingRunId) await pollRun(pendingRunId)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '取消失败')
    } finally {
      setIsRunning(false)
    }
  }

  const openWorkflow = (workflow: SavedWorkflow) => {
    setWorkflowId(workflow.id)
    setName(workflow.name)
    setDescription(workflow.description)
    setNodes(workflow.definition.nodes.map((node) => ({
      id: node.id,
      type: 'workflow',
      position: node.position,
      data: { label: node.label, nodeType: node.type, config: node.config },
    })))
    setEdges(workflow.definition.edges.map((edge) => ({ id: edge.id, source: edge.source, target: edge.target, sourceHandle: edge.source_handle ?? undefined, animated: false })))
    setLatestRun(null)
    setSelectedNodeId(null)
    requestAnimationFrame(() => void fitView({ padding: 0.18 }))
  }

  const updateSelectedNode = (patch: Partial<WorkflowNodeData>, config?: Record<string, unknown>) => {
    if (!selectedNodeId) return
    setNodes((current) => current.map((node) => node.id === selectedNodeId
      ? { ...node, data: { ...node.data, ...patch, config: config ?? node.data.config } }
      : node))
  }

  return (
    <div className="workflow-studio" role="dialog" aria-modal="true" aria-label="可视化工作流编排">
      <header className="workflow-topbar">
        <div className="workflow-brand"><span>⌁</span><div><small>AGENT BUILDER</small><strong>可视化工作流</strong></div></div>
        <div className="workflow-title-fields">
          <input aria-label="工作流名称" value={name} onChange={(event) => setName(event.target.value)} />
          <input aria-label="工作流描述" value={description} onChange={(event) => setDescription(event.target.value)} />
        </div>
        <div className="workflow-actions">
          <span className="workflow-save-state">{isSaving ? '保存中…' : workflowId ? '已保存' : '未保存'}</span>
          <button className="workflow-save" disabled={isSaving || isRunning} onClick={() => void save().catch((requestError) => setError(String(requestError)))} type="button">保存</button>
          <button className="workflow-run" disabled={isRunning} onClick={() => void run()} type="button">{isRunning ? '后台运行中…' : '▶ 运行'}</button>
          {(isRunning || latestRun?.status === 'waiting_approval') && activeTaskId && <button className="workflow-cancel" onClick={() => void cancelRun()} type="button">取消</button>}
          <button className="workflow-close" onClick={onClose} type="button" aria-label="关闭">×</button>
        </div>
      </header>

      <div className="workflow-layout">
        <aside className="workflow-sidebar">
          <div className="workflow-sidebar-heading"><strong>节点</strong><small>拖到画布中使用</small></div>
          <div className="workflow-palette">
            {(Object.keys(NODE_META) as WorkflowNodeType[]).map((type) => (
              <div className={`workflow-palette-item type-${type}`} draggable key={type} onDragStart={(event) => { event.dataTransfer.setData('application/workflow-node', type); event.dataTransfer.effectAllowed = 'move' }}>
                <span>{NODE_META[type].icon}</span><div><strong>{NODE_META[type].label}</strong><small>{NODE_META[type].hint}</small></div><b>⋮⋮</b>
              </div>
            ))}
          </div>
          <div className="workflow-saved-heading"><strong>已保存</strong><button onClick={() => { setWorkflowId(null); setName('新工作流'); setNodes([]); setEdges([]) }} type="button">＋</button></div>
          <div className="workflow-saved-list">
            {workflows.map((workflow) => <button className={workflow.id === workflowId ? 'is-active' : ''} key={workflow.id} onClick={() => openWorkflow(workflow)} type="button"><span>⌁</span><div><strong>{workflow.name}</strong><small>{workflow.definition.nodes.length} 个节点</small></div></button>)}
            {!workflows.length && <small className="workflow-empty">保存后会显示在这里</small>}
          </div>
        </aside>

        <div className="workflow-canvas" ref={canvasRef} onDrop={onDrop} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }}>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={(_, node) => setSelectedNodeId(node.id)} onPaneClick={() => setSelectedNodeId(null)} deleteKeyCode={['Backspace', 'Delete']} fitView>
            <Background gap={22} size={1.4} color="#ddd7eb" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor={(node) => ({ input: '#5f6ee9', agent: '#6947e2', knowledge: '#19a981', llm: '#8a5cf6', mcp: '#e59335', todo: '#2f9e7a', http: '#3d7ea6', transform: '#6b6f8a', foreach: '#c45f8a', subworkflow: '#5a6fd6', condition: '#2c91c9', approval: '#dc8a2e', output: '#e35f88' }[(node.data as WorkflowNodeData).nodeType])} />
          </ReactFlow>
          <div className="workflow-canvas-tip">拖拽节点 · 连线端点 · Delete 删除</div>
        </div>

        <aside className="workflow-inspector">
          {selectedNode ? <>
            <div className="workflow-inspector-title"><span className={`type-${selectedNode.data.nodeType}`}>{NODE_META[selectedNode.data.nodeType].icon}</span><div><small>节点配置</small><strong>{selectedNode.data.label}</strong></div></div>
            <label>节点名称<input value={selectedNode.data.label} onChange={(event) => updateSelectedNode({ label: event.target.value })} /></label>
            {selectedNode.data.nodeType === 'agent' && <>
              <label>Agent 任务提示词<textarea rows={5} value={String(selectedNode.data.config.prompt ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, prompt: event.target.value })} /></label>
              <label>可用工具<div className="workflow-tool-toggles">{[['search_knowledge', '知识库'], ['get_weather', '天气 MCP'], ['add_todo', '添加 Todo'], ['list_todos', '查看 Todo'], ['calculate', '计算器']].map(([tool, label]) => { const active = Array.isArray(selectedNode.data.config.tools) && selectedNode.data.config.tools.includes(tool); return <button className={active ? 'is-active' : ''} key={tool} onClick={() => { const current = Array.isArray(selectedNode.data.config.tools) ? selectedNode.data.config.tools as string[] : []; updateSelectedNode({}, { ...selectedNode.data.config, tools: active ? current.filter((item) => item !== tool) : [...current, tool] }) }} type="button">{active ? '✓ ' : ''}{label}</button> })}</div></label>
              <label>Agent Skills<small className="workflow-field-hint">勾选后运行时注入说明书，不会新增 MCP 工具</small><div className="workflow-skill-toggles">{availableSkills.length ? availableSkills.map((skill) => { const current = Array.isArray(selectedNode.data.config.skills) ? selectedNode.data.config.skills as string[] : []; const active = current.includes(skill.name); return <button className={active ? 'is-active' : ''} key={skill.name} onClick={() => updateSelectedNode({}, { ...selectedNode.data.config, skills: active ? current.filter((item) => item !== skill.name) : [...current, skill.name] })} title={skill.description} type="button">{active ? '✓ ' : ''}{skill.name}</button> }) : <small>暂无可用 Skills</small>}</div></label>
              <label>最大工具轮次<input min="1" max="8" type="number" value={Number(selectedNode.data.config.max_rounds ?? 4)} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, max_rounds: Number(event.target.value) })} /></label>
            </>}
            {selectedNode.data.nodeType === 'knowledge' && <label>返回片段数<input min="1" max="10" type="number" value={Number(selectedNode.data.config.limit ?? 3)} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, limit: Number(event.target.value) })} /></label>}
            {selectedNode.data.nodeType === 'llm' && <label>任务提示词<textarea rows={6} value={String(selectedNode.data.config.prompt ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, prompt: event.target.value })} /></label>}
            {selectedNode.data.nodeType === 'mcp' && <><label>MCP 工具<select value={String(selectedNode.data.config.tool ?? 'get_weather')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, tool: event.target.value })}>{mcpTools.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}{tool.description ? ` · ${tool.description}` : ''}</option>)}</select></label>{String(selectedNode.data.config.tool ?? '') === 'get_weather' && <><label>固定地点（可选）<input placeholder="留空则使用 {{input}}" value={String(selectedNode.data.config.location ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, location: event.target.value })} /></label><label>预报天数<input min="1" max="7" type="number" value={Number(selectedNode.data.config.days ?? 3)} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, days: Number(event.target.value) })} /></label></>}<label>自定义参数 JSON<small className="workflow-field-hint">填写后优先生效，可用 {'{{input}}'} / {'{{upstream}}'} / {'{{vars.x}}'}</small><textarea rows={4} placeholder='{"query":"{{upstream}}","limit":3}' value={String(selectedNode.data.config.arguments_json ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, arguments_json: event.target.value })} /></label></>}
            {selectedNode.data.nodeType === 'http' && <><label>方法<select value={String(selectedNode.data.config.method ?? 'GET')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, method: event.target.value })}><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select></label><label>URL<input placeholder="https://api.example.com/search?q={{upstream}}" value={String(selectedNode.data.config.url ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, url: event.target.value })} /></label><label>请求头<textarea rows={3} placeholder={'Content-Type: application/json'} value={String(selectedNode.data.config.headers ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, headers: event.target.value })} /></label><label>请求体<textarea rows={4} placeholder={'{"q":"{{upstream}}"}'} value={String(selectedNode.data.config.body ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, body: event.target.value })} /></label><label>超时（秒）<input min="2" max="15" type="number" value={Number(selectedNode.data.config.timeout ?? 8)} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, timeout: Number(event.target.value) })} /></label><small className="workflow-field-hint">只允许公网 http/https，禁止访问本机和内网。</small></>}
            {selectedNode.data.nodeType === 'transform' && <><label>转换方式<select value={String(selectedNode.data.config.mode ?? 'template')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, mode: event.target.value })}><option value="template">模板</option><option value="json_path">JSON 路径</option><option value="split">按分隔符拆分</option><option value="join">合并列表</option><option value="json_parse">解析 JSON</option></select></label>{String(selectedNode.data.config.mode ?? 'template') === 'template' && <label>模板<textarea rows={4} value={String(selectedNode.data.config.template ?? '{{upstream}}')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, template: event.target.value })} /></label>}{String(selectedNode.data.config.mode ?? '') === 'json_path' && <label>取值路径<input placeholder="例如 matches.0.content" value={String(selectedNode.data.config.path ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, path: event.target.value })} /></label>}{(String(selectedNode.data.config.mode ?? '') === 'split' || String(selectedNode.data.config.mode ?? '') === 'join') && <label>分隔符<input value={String(selectedNode.data.config.separator ?? '\n')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, separator: event.target.value })} /></label>}</>}
            {selectedNode.data.nodeType === 'foreach' && <><label>列表路径<input placeholder="留空则自动识别列表或按行拆分" value={String(selectedNode.data.config.path ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, path: event.target.value })} /></label><label>项模板<textarea rows={3} value={String(selectedNode.data.config.template ?? '{{item}}')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, template: event.target.value })} /></label><label>最多处理条数<input min="1" max="20" type="number" value={Number(selectedNode.data.config.max_items ?? 10)} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, max_items: Number(event.target.value) })} /></label><label>逐项调用子工作流<select value={String(selectedNode.data.config.workflow_id ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, workflow_id: event.target.value })}><option value="">不调用，仅转换每一项</option>{workflows.filter((item) => item.id !== workflowId).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>合并分隔符<input placeholder="留空则输出列表" value={String(selectedNode.data.config.join ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, join: event.target.value })} /></label></>}
            {selectedNode.data.nodeType === 'subworkflow' && <><label>子工作流<select value={String(selectedNode.data.config.workflow_id ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, workflow_id: event.target.value })}><option value="">请选择</option>{workflows.filter((item) => item.id !== workflowId).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>传入输入<textarea rows={3} value={String(selectedNode.data.config.input_template ?? '{{upstream}}')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, input_template: event.target.value })} /></label><small className="workflow-field-hint">子工作流会自动通过审批节点，最多嵌套 3 层。</small></>}
            {selectedNode.data.nodeType === 'todo' && <label>任务标题<input placeholder="留空则使用上游输入" value={String(selectedNode.data.config.title ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, title: event.target.value })} /><small className="workflow-field-hint">会调用本地 add_todo，写入当前账号的 Todo</small></label>}
            {selectedNode.data.nodeType === 'condition' && <label>上游内容包含<input placeholder="例如：需要人工确认" value={String(selectedNode.data.config.contains ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, contains: event.target.value })} /><small>上方出口为“是”，下方出口为“否”</small></label>}
            {selectedNode.data.nodeType === 'approval' && <label>确认提示<textarea rows={3} value={String(selectedNode.data.config.message ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, message: event.target.value })} /></label>}
            {selectedNode.data.nodeType !== 'input' && <label>从上游取值路径<input placeholder="例如 matches.0.content，留空则使用全部上游" value={String(selectedNode.data.config.map_from ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, map_from: event.target.value })} /></label>}
            <label>保存为变量<input placeholder="例如 city，后续用 {{vars.city}}" value={String(selectedNode.data.config.save_as ?? '')} onChange={(event) => updateSelectedNode({}, { ...selectedNode.data.config, save_as: event.target.value })} /></label>
            <small className="workflow-field-hint">模板变量：{'{{input}}'} 运行输入 · {'{{upstream}}'} 上游 · {'{{vars.x}}'} 已存变量 · {'{{item}}'} 循环项</small>
            {selectedStep && <div className="workflow-node-debug"><div><strong>本次运行数据</strong><span>{selectedStep.status} · {selectedStep.duration_ms}ms</span></div><label>输入<pre>{JSON.stringify(selectedStep.input, null, 2)}</pre></label><label>输出<pre>{JSON.stringify(selectedStep.output, null, 2)}</pre></label></div>}
            <button className="workflow-delete-node" onClick={() => { setNodes((current) => current.filter((node) => node.id !== selectedNode.id)); setEdges((current) => current.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id)); setSelectedNodeId(null) }} type="button">删除节点</button>
          </> : <div className="workflow-run-panel">
            <div><small>TEST RUN</small><strong>运行工作流</strong></div>
            <label>测试输入<textarea rows={5} value={runInput} onChange={(event) => setRunInput(event.target.value)} /></label>
            <button disabled={isRunning} onClick={() => void run()} type="button">{isRunning ? '已提交到后台…' : '▶ 开始运行'}</button>
            {(isRunning || latestRun?.status === 'waiting_approval') && activeTaskId && <button className="workflow-cancel-inline" onClick={() => void cancelRun()} type="button">取消本次运行</button>}
            {error && <div className="workflow-error">{error}</div>}
            {latestRun && <div className={`workflow-result is-${latestRun.status}`}><div><strong>{latestRun.status === 'success' ? '运行成功' : latestRun.status === 'waiting_approval' ? '等待你的确认' : latestRun.status === 'queued' ? '已排队，等待 Worker' : latestRun.status === 'running' ? '后台运行中' : latestRun.status === 'cancelled' ? '已取消' : '运行失败'}</strong><span>{latestRun.steps.length} 个节点</span></div>{latestRun.status === 'waiting_approval' && <button className="workflow-approve" disabled={isRunning} onClick={() => void approveRun()} type="button">✓ 确认并继续执行</button>}{latestRun.output_text && <p>{latestRun.output_text}</p>}{latestRun.error_message && <p>{latestRun.error_message}</p>}<ol>{latestRun.steps.map((step) => <li key={step.node_id}><span>{step.status === 'success' ? '✓' : step.status === 'running' ? '…' : step.status === 'waiting' ? 'Ⅱ' : step.status === 'skipped' ? '–' : '!'}</span><div><strong>{step.label} · {step.summary}</strong><small>{step.duration_ms} ms</small></div></li>)}</ol></div>}
          </div>}
        </aside>
      </div>
    </div>
  )
}
