import { describe, expect, it } from 'vitest'
import { snapshotAge, snapshotNote } from './tasksModel'

const NOW = new Date('2026-11-15T07:30:00Z') // 15:30 CST
const ago = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString()

describe('snapshotAge', () => {
  it('is empty without a snapshot', () => {
    expect(snapshotAge(null, NOW)).toBe('')
  })
  it('shows 刚刚 under a minute and minutes otherwise', () => {
    expect(snapshotAge(ago(0.4), NOW)).toBe('刚刚')
    expect(snapshotAge(ago(5), NOW)).toBe('5 分钟前')
  })
})

describe('snapshotNote', () => {
  it('stays silent for a fresh snapshot', () => {
    expect(snapshotNote(ago(5), NOW)).toBe('')
  })
  it('explains the closing window on a stale snapshot at 15:00–16:25', () => {
    expect(snapshotNote(ago(30), NOW)).toContain('收盘窗口 15:00–16:25')
  })
  it('treats 16:25 as the last closing-window minute', () => {
    expect(snapshotNote(ago(30), new Date('2026-11-15T08:25:00Z'))).toContain('收盘窗口')
  })
  it('asks to check the timer after 16:25', () => {
    expect(snapshotNote(ago(30), new Date('2026-11-15T08:26:00Z'))).toContain('qp-collect.timer')
    expect(snapshotNote(ago(30), new Date('2026-11-15T09:00:00Z'))).toContain('qp-collect.timer')
  })
  it('is silent without a snapshot', () => {
    expect(snapshotNote(null, NOW)).toBe('')
  })
})
