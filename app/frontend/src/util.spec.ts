import { describe, expect, it } from 'vitest'
import { fmt, tagType } from './util'

describe('fmt', () => {
  it('renders a timestamp in CST', () => {
    expect(fmt('2026-11-13T07:30:00Z')).toBe('2026/11/13 15:30:00')
  })
  it('renders a placeholder for empty values', () => {
    expect(fmt(null)).toBe('-')
    expect(fmt(undefined)).toBe('-')
  })
})

describe('tagType', () => {
  it.each([
    ['succeeded', 'success'],
    ['healthy', 'success'],
    ['running', 'success'],
    ['failed', 'error'],
    ['critical', 'error'],
    ['exit-code', 'error'],
    ['warning', 'warning'],
    ['whatever', 'default'],
    [null, 'default'],
  ])('%s -> %s', (value, expected) => {
    expect(tagType(value as string | null)).toBe(expected)
  })
})
