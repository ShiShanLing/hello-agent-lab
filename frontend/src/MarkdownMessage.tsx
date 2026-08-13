import { useRef, useState } from 'react'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Highlight, themes } from 'prism-react-renderer'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'

export type CitationSource = {
  kind?: 'knowledge' | 'web' | string
  title?: string
  source?: string
  chunk?: number | string | null
  score?: number | null
  snippet?: string
  body?: string
  url?: string | null
}

type MarkdownMessageProps = {
  content: string
  isStreaming?: boolean
  sources?: CitationSource[]
}

const LANGUAGE_ALIASES: Record<string, string> = {
  js: 'javascript',
  jsx: 'jsx',
  py: 'python',
  sh: 'bash',
  shell: 'bash',
  ts: 'typescript',
  tsx: 'tsx',
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function normalizeName(name: string) {
  return name.trim().replace(/\\/g, '/').toLowerCase()
}

function nameCandidates(name: string) {
  const normalized = normalizeName(name)
  if (!normalized) return []
  const base = normalized.split('/').pop() || normalized
  return [...new Set([normalized, base])]
}

function namesMatch(left: string, right: string) {
  const a = nameCandidates(left)
  const b = nameCandidates(right)
  return a.some((item) =>
    b.some(
      (other) =>
        item === other ||
        item.endsWith(`/${other}`) ||
        other.endsWith(`/${item}`),
    ),
  )
}

function findKnowledgeSourceIndex(
  sources: CitationSource[],
  file: string,
  chunk: string | null,
) {
  let loose = -1
  for (let index = 0; index < sources.length; index += 1) {
    const source = sources[index]
    if (source.kind === 'web') continue
    const sourceName = String(source.source || source.title || '')
    if (!namesMatch(file, sourceName)) continue
    if (chunk != null && String(source.chunk ?? '') === String(chunk)) {
      return index
    }
    if (loose < 0) loose = index
  }
  return loose
}

function allowCitationUrl(url: string) {
  if (url.startsWith('cite://')) return url
  return defaultUrlTransform(url)
}

/** 把模型输出的 [文件#片段] / 来源链接改成可渲染的 cite:// 标记。 */
export function injectCitationMarkers(
  content: string,
  sources: CitationSource[],
): string {
  let result = content

  // 1) 通用：任意 [文件名#片段N]，按文件名（含路径后缀）模糊匹配来源
  result = result.replace(
    /\[([^\]\n]+?)#片段\s*(\d+)\]/g,
    (_full, file: string, chunk: string) => {
      const index = findKnowledgeSourceIndex(sources, file, chunk)
      if (index < 0) {
        // 没有命中来源时仍做成可点链接，弹框提示暂无正文
        return `[${file}#片段${chunk}](cite://orphan/${encodeURIComponent(file)}/${chunk})`
      }
      return `[${file}#片段${chunk}](cite://k/${index})`
    },
  )

  if (!sources.length) return result

  sources.forEach((source, index) => {
    if (source.kind === 'web' && source.url) {
      const url = escapeRegExp(source.url)
      const title = escapeRegExp(source.title || source.source || '')
      if (title) {
        result = result.replace(
          new RegExp(`\\[${title}\\]\\(${url}\\)`, 'gi'),
          `[†](cite://w/${index})`,
        )
      }
      result = result.replace(
        new RegExp(`\\[[^\\]]+\\]\\(${url}\\)`, 'gi'),
        `[†](cite://w/${index})`,
      )
      return
    }

    const name = String(source.source || source.title || '').trim()
    if (!name) return
    for (const alias of nameCandidates(name)) {
      // alias 已小写；原文可能大小写不同，用原 name 的 basename 再替一次
      const originalBase = name.split(/[/\\]/).pop() || name
      for (const label of [...new Set([name, originalBase])]) {
        const escaped = escapeRegExp(label)
        result = result.replace(
          new RegExp(`\\[${escaped}\\](?!\\()`, 'g'),
          `[${label}](cite://k/${index})`,
        )
      }
      void alias
    }
  })

  return result
}

function Code({ className, children, ...props }: ComponentPropsWithoutRef<'code'>) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'error'>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const languageMatch = /language-([\w-]+)/.exec(className ?? '')
  const rawLanguage = languageMatch?.[1] ?? 'text'
  const language = LANGUAGE_ALIASES[rawLanguage] ?? rawLanguage
  const code = String(children).replace(/\n$/, '')
  const isBlock = Boolean(languageMatch) || String(children).includes('\n')

  if (!isBlock) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  }

  const copyCode = async () => {
    if (resetTimer.current) clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(code)
      setCopyStatus('copied')
    } catch {
      setCopyStatus('error')
    }
    resetTimer.current = setTimeout(() => setCopyStatus('idle'), 1800)
  }

  const buttonLabel =
    copyStatus === 'copied'
      ? '已复制'
      : copyStatus === 'error'
        ? '复制失败'
        : '复制代码'

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>{rawLanguage}</span>
        <button onClick={() => void copyCode()} type="button">
          {buttonLabel}
        </button>
      </div>
      <Highlight code={code} language={language} theme={themes.oneDark}>
        {({ className: highlightClass, getLineProps, getTokenProps, style, tokens }) => (
          <pre className={highlightClass} style={style}>
            {tokens.map((line, lineIndex) => (
              <span
                {...getLineProps({ line })}
                className="code-line"
                key={lineIndex}
              >
                {line.map((token, tokenIndex) => (
                  <span {...getTokenProps({ token })} key={tokenIndex} />
                ))}
                {'\n'}
              </span>
            ))}
          </pre>
        )}
      </Highlight>
    </div>
  )
}

function KnowledgeCiteMark({
  source,
  label,
}: {
  source: CitationSource
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const title = source.title || source.source || '内部资料'
  const linkText =
    label?.trim() ||
    (source.chunk != null
      ? `[${title}#片段${source.chunk}]`
      : `[${title}]`)

  return (
    <>
      <button
        aria-label={`查看内部资料：${title}`}
        className="inline-cite-link is-knowledge"
        onClick={() => setOpen(true)}
        title="点击查看资料内容"
        type="button"
      >
        {linkText}
      </button>
      {open &&
        createPortal(
          <div
            className="citation-preview-overlay"
            onClick={() => setOpen(false)}
            role="presentation"
          >
            <div
              aria-labelledby="citation-preview-title"
              aria-modal="true"
              className="citation-preview-panel"
              onClick={(event) => event.stopPropagation()}
              role="dialog"
            >
              <header>
                <div>
                  <strong id="citation-preview-title">{title}</strong>
                  <small>
                    {source.chunk != null
                      ? `知识库片段 ${source.chunk}`
                      : '本地知识库'}
                    {source.score != null ? ` · 命中 ${source.score} 分` : ''}
                  </small>
                </div>
                <button
                  aria-label="关闭资料预览"
                  className="secondary-button"
                  onClick={() => setOpen(false)}
                  type="button"
                >
                  关闭
                </button>
              </header>
              <div className="citation-preview-body">
                <div className="citation-preview-markdown message-content message-markdown">
                  <ReactMarkdown
                    components={{
                      a: ({ href, children }) => (
                        <a href={href} rel="noreferrer" target="_blank">
                          {children}
                        </a>
                      ),
                    }}
                    disallowedElements={['img']}
                    remarkPlugins={[remarkGfm]}
                    skipHtml
                    urlTransform={allowCitationUrl}
                  >
                    {source.body || source.snippet || '暂无可用资料正文。'}
                  </ReactMarkdown>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}

function WebCiteMark({ source }: { source: CitationSource }) {
  const [copied, setCopied] = useState(false)
  const url = source.url || ''
  const title = source.title || source.source || url

  const copyUrl = async () => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  if (!url) return null

  return (
    <span className="inline-cite-wrap">
      <button
        aria-label={`打开或复制网页来源：${title}`}
        className="inline-cite-mark is-web"
        type="button"
      >
        链
      </button>
      <span className="inline-cite-menu" role="menu">
        <span className="citation-link-tip">打开新标签，或复制链接</span>
        <a href={url} rel="noreferrer" role="menuitem" target="_blank">
          新标签打开
        </a>
        <button onClick={() => void copyUrl()} role="menuitem" type="button">
          {copied ? '已复制' : '复制链接'}
        </button>
      </span>
    </span>
  )
}

function childrenToLabel(children: ReactNode) {
  if (Array.isArray(children)) return children.map(String).join('')
  if (typeof children === 'string' || typeof children === 'number') {
    return String(children)
  }
  return ''
}

function CitationLink({
  href,
  children,
  sources,
}: {
  href?: string
  children?: ReactNode
  sources: CitationSource[]
}) {
  const citeMatch = href?.match(/^cite:\/\/([kw])\/(\d+)$/)
  if (citeMatch) {
    const index = Number(citeMatch[2])
    const source = sources[index]
    if (!source) return <>{children}</>
    const rawLabel = childrenToLabel(children)
    if (citeMatch[1] === 'k') {
      const label = rawLabel
        ? rawLabel.startsWith('[')
          ? rawLabel
          : `[${rawLabel}]`
        : undefined
      return <KnowledgeCiteMark label={label} source={source} />
    }
    return <WebCiteMark source={source} />
  }

  const orphanMatch = href?.match(/^cite:\/\/orphan\/([^/]+)\/(\d+)$/)
  if (orphanMatch) {
    const file = decodeURIComponent(orphanMatch[1])
    const chunk = orphanMatch[2]
    const index = findKnowledgeSourceIndex(sources, file, chunk)
    const source =
      index >= 0
        ? sources[index]
        : {
            kind: 'knowledge',
            title: file,
            source: file,
            chunk,
            snippet: '',
            body: '本轮回答未附带该片段正文，请重新提问以加载资料。',
          }
    const rawLabel = childrenToLabel(children)
    const label = rawLabel
      ? rawLabel.startsWith('[')
        ? rawLabel
        : `[${rawLabel}]`
      : `[${file}#片段${chunk}]`
    return <KnowledgeCiteMark label={label} source={source} />
  }

  if (href && /^https?:\/\//i.test(href)) {
    const matched = sources.find((item) => item.kind === 'web' && item.url === href)
    if (matched) {
      return <WebCiteMark source={matched} />
    }
  }

  return (
    <a href={href} rel="noreferrer" target="_blank">
      {children}
    </a>
  )
}

export function MarkdownMessage({
  content,
  isStreaming = false,
  sources = [],
}: MarkdownMessageProps) {
  const rendered = injectCitationMarkers(content, sources)

  return (
    <div
      className={`message-content message-markdown${isStreaming ? ' is-streaming' : ''}`}
    >
      <ReactMarkdown
        components={{
          a: ({ href, children }) => (
            <CitationLink href={href} sources={sources}>
              {children}
            </CitationLink>
          ),
          code: Code,
        }}
        disallowedElements={['img']}
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={allowCitationUrl}
      >
        {rendered}
      </ReactMarkdown>
      {isStreaming && (
        <span aria-label="Agent 正在继续生成" className="streaming-indicator">
          <span />
          <span />
          <span />
        </span>
      )}
    </div>
  )
}
