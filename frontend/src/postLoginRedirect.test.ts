import { describe, expect, it } from 'vitest'
import { isSafeNextPath } from './postLoginRedirect'

describe('isSafeNextPath', () => {
  it('allows angular20 paths', () => {
    expect(isSafeNextPath('/angular20')).toBe(true)
    expect(isSafeNextPath('/angular20/')).toBe(true)
    expect(isSafeNextPath('/angular20/#/tools/mortgage')).toBe(true)
  })

  it('rejects open redirects', () => {
    expect(isSafeNextPath('https://evil.example/angular20')).toBe(false)
    expect(isSafeNextPath('//evil.example')).toBe(false)
    expect(isSafeNextPath('/agent/')).toBe(false)
    expect(isSafeNextPath(null)).toBe(false)
  })
})
