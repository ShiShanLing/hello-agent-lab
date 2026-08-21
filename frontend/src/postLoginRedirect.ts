export function isSafeNextPath(next: string | null): next is string {
  if (!next) {
    return false
  }
  if (!next.startsWith('/') || next.startsWith('//') || next.includes('://')) {
    return false
  }
  return (
    next === '/angular20' ||
    next.startsWith('/angular20/') ||
    next.startsWith('/angular20#') ||
    next.startsWith('/angular20/#')
  )
}

export function consumeSafeNextRedirect(): boolean {
  const next = new URLSearchParams(window.location.search).get('next')
  if (!isSafeNextPath(next)) {
    return false
  }
  window.location.assign(next)
  return true
}
