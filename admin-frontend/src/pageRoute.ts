export const ADMIN_PAGE_SLUGS = [
  'dashboard',
  'cost',
  'market',
  'users',
  'admins',
  'releases',
  'audit',
  'security',
] as const

export type AdminPageSlug = (typeof ADMIN_PAGE_SLUGS)[number]

export function appBasePath(baseUrl = import.meta.env.BASE_URL): string {
  const trimmed = (baseUrl || '/').replace(/\/+$/, '')
  return trimmed === '' || trimmed === '.' ? '' : trimmed
}

export function readPageSlug(
  allowed: readonly string[],
  pathname = typeof window === 'undefined' ? '/' : window.location.pathname,
  baseUrl = import.meta.env.BASE_URL,
): string {
  const base = appBasePath(baseUrl)
  let path = pathname
  if (base && (path === base || path.startsWith(`${base}/`))) {
    path = path.slice(base.length)
  }
  const slug = path.replace(/^\/+|\/+$/g, '').split('/')[0] ?? ''
  if (!slug) return ''
  return allowed.includes(slug) ? slug : ''
}

export function pagePath(slug: string, baseUrl = import.meta.env.BASE_URL): string {
  const base = appBasePath(baseUrl)
  if (!slug) return `${base}/` || '/'
  return `${base}/${slug}`
}

export function writePageSlug(slug: string, options?: { replace?: boolean }) {
  const next = pagePath(slug)
  const current = window.location.pathname.replace(/\/+$/, '') || '/'
  const normalizedNext = next.replace(/\/+$/, '') || '/'
  if (current === normalizedNext) return
  const url = `${next}${window.location.search}`
  if (options?.replace) window.history.replaceState(null, '', url)
  else window.history.pushState(null, '', url)
}

export function adminPageFromSlug(slug: string): AdminPageSlug {
  if (slug === '' || slug === 'dashboard') return 'dashboard'
  return ADMIN_PAGE_SLUGS.includes(slug as AdminPageSlug) ? (slug as AdminPageSlug) : 'dashboard'
}
