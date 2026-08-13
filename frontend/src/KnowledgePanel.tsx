import { useEffect, useMemo, useRef, useState } from 'react'

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

type KnowledgePanelProps = {
  apiBaseUrl: string
  canManage: boolean
  onClose: () => void
}

const CATEGORIES = [
  '未分类',
  '公司制度',
  '产品资料',
  '技术运维',
  '客户服务',
  '项目管理',
  '学习资料',
]

async function responseData(response: Response) {
  const text = await response.text()
  try {
    return text ? JSON.parse(text) : null
  } catch {
    throw new Error(`服务器返回了无法识别的内容（HTTP ${response.status}）`)
  }
}

function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function KnowledgePanel({ apiBaseUrl, canManage, onClose }: KnowledgePanelProps) {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [category, setCategory] = useState('未分类')
  const [activeCategory, setActiveCategory] = useState('全部')
  const [isLoading, setIsLoading] = useState(true)
  const [isUploading, setIsUploading] = useState(false)
  const [isSeeding, setIsSeeding] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const loadDocuments = async () => {
    setError(null)
    try {
      const response = await fetch(`${apiBaseUrl}/knowledge/documents`, {
        credentials: 'include',
      })
      const data = await responseData(response)
      if (!response.ok) throw new Error(data?.detail ?? '读取知识库失败')
      setDocuments(data.documents)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取知识库失败')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    void loadDocuments()
  }, [])

  const uploadFiles = async (files: FileList | File[]) => {
    if (!canManage || !files.length) return
    setIsUploading(true)
    setError(null)
    try {
      for (const file of Array.from(files)) {
        const form = new FormData()
        form.append('file', file)
        form.append('category', category)
        const response = await fetch(`${apiBaseUrl}/knowledge/documents`, {
          method: 'POST',
          credentials: 'include',
          body: form,
        })
        const data = await responseData(response)
        if (!response.ok) throw new Error(`${file.name}：${data?.detail ?? '上传失败'}`)
      }
      await loadDocuments()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '上传知识文档失败')
    } finally {
      setIsUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const addDemoKnowledge = async () => {
    setIsSeeding(true)
    setError(null)
    try {
      const response = await fetch(`${apiBaseUrl}/knowledge/demo`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await responseData(response)
      if (!response.ok) throw new Error(data?.detail ?? '添加演示资料失败')
      setDocuments(data.documents)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '添加演示资料失败')
    } finally {
      setIsSeeding(false)
    }
  }

  const removeDocument = async (document: KnowledgeDocument) => {
    if (!window.confirm(`确定删除“${document.original_name}”吗？`)) return
    setError(null)
    try {
      const response = await fetch(
        `${apiBaseUrl}/knowledge/documents/${document.id}`,
        { method: 'DELETE', credentials: 'include' },
      )
      if (!response.ok) {
        const data = await responseData(response)
        throw new Error(data?.detail ?? '删除失败')
      }
      setDocuments((current) => current.filter((item) => item.id !== document.id))
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '删除知识文档失败')
    }
  }

  const categories = useMemo(
    () => ['全部', ...Array.from(new Set(documents.map((item) => item.category)))],
    [documents],
  )
  const visibleDocuments = activeCategory === '全部'
    ? documents
    : documents.filter((item) => item.category === activeCategory)
  const totalChunks = documents.reduce((sum, item) => sum + item.chunk_count, 0)

  return (
    <div
      className="knowledge-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isUploading) onClose()
      }}
    >
      <section
        aria-labelledby="knowledge-panel-title"
        aria-modal="true"
        className="knowledge-panel"
        role="dialog"
      >
        <header className="knowledge-header">
          <div>
            <span className="knowledge-eyebrow">RAG KNOWLEDGE BASE</span>
            <h2 id="knowledge-panel-title">知识库</h2>
            <p>上传资料后，Agent 会在回答前自动检索并标注来源。</p>
          </div>
          <button aria-label="关闭知识库" onClick={onClose} type="button">×</button>
        </header>

        <div className="knowledge-body">
          <div className="knowledge-stats">
            <div><strong>{documents.length}</strong><span>文档</span></div>
            <div><strong>{totalChunks}</strong><span>检索片段</span></div>
            <div><strong>{Math.max(0, categories.length - 1)}</strong><span>分类</span></div>
          </div>

          {canManage ? <>
            <div className="knowledge-permission is-allowed"><span>✓</span><p><strong>你可以管理知识库</strong>系统管理员和知识库管理员可以上传、导入及删除资料。</p></div>
            <div className="knowledge-upload-toolbar">
              <label>
                <span>上传到分类</span>
                <select value={category} onChange={(event) => setCategory(event.target.value)}>
                  {CATEGORIES.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
              <button disabled={isSeeding || isUploading} onClick={() => void addDemoKnowledge()} type="button">
                {isSeeding ? '正在添加…' : '＋ 添加演示资料'}
              </button>
            </div>

            <button
              className={`knowledge-dropzone ${isDragging ? 'is-dragging' : ''}`}
              disabled={isUploading}
              onClick={() => fileInputRef.current?.click()}
              onDragEnter={(event) => { event.preventDefault(); setIsDragging(true) }}
              onDragLeave={(event) => { event.preventDefault(); setIsDragging(false) }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                setIsDragging(false)
                void uploadFiles(event.dataTransfer.files)
              }}
              type="button"
            >
              <span aria-hidden="true">⇧</span>
              <strong>{isUploading ? '正在解析并建立索引…' : '点击选择或拖入文档'}</strong>
              <small>支持 Markdown、TXT、PDF、Word，单个文件不超过 8 MB</small>
            </button>
            <input
              accept=".md,.txt,.pdf,.docx"
              hidden
              multiple
              onChange={(event) => event.target.files && void uploadFiles(event.target.files)}
              ref={fileInputRef}
              type="file"
            />
          </> : <div className="knowledge-permission"><span>i</span><p><strong>当前为只读权限</strong>你仍可查看并使用已有资料问答；如需上传或删除，请联系管理员授予“知识库管理员”角色。</p></div>}

          {error && <div className="knowledge-error" role="alert">{error}</div>}

          <div className="knowledge-filter" aria-label="知识库分类">
            {categories.map((item) => (
              <button
                className={activeCategory === item ? 'is-active' : ''}
                key={item}
                onClick={() => setActiveCategory(item)}
                type="button"
              >
                {item}
              </button>
            ))}
          </div>

          <div className="knowledge-documents">
            {isLoading ? (
              <div className="knowledge-empty">正在读取知识库…</div>
            ) : visibleDocuments.length === 0 ? (
              <div className="knowledge-empty">
                <strong>还没有知识文档</strong>
                <span>可以上传自己的资料，或先添加 10 份虚构演示资料。</span>
              </div>
            ) : visibleDocuments.map((document) => (
              <article className="knowledge-document" key={document.id}>
                <span className={`knowledge-file-icon type-${document.file_type}`} aria-hidden="true">
                  {document.file_type.toUpperCase()}
                </span>
                <div>
                  <strong title={document.original_name}>{document.original_name}</strong>
                  <p>
                    <span>{document.category}</span>
                    <span>{document.chunk_count} 个片段</span>
                    <span>{fileSize(document.size_bytes)}</span>
                  </p>
                  {document.error_message && <small>{document.error_message}</small>}
                </div>
                <span className={`knowledge-status status-${document.status}`}>
                  {document.status === 'ready' ? '已完成' : document.status === 'failed' ? '失败' : '解析中'}
                </span>
                {canManage && <button
                  aria-label={`删除 ${document.original_name}`}
                  className="knowledge-delete"
                  onClick={() => void removeDocument(document)}
                  type="button"
                >
                  删除
                </button>}
              </article>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
