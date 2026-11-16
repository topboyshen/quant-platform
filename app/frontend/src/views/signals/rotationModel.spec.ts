import { describe, expect, it } from 'vitest'
import {
  FORWARD_START, INITIAL_CAPITAL, currency, dayLabel, finite, indexedSeries, indexText, isRotationEmpty,
  navScale, normalizeRotation, numberText, percent, phaseLabel, recentRuns, runReason, sideLabel,
  statusLabel, statusTone, weight,
} from './rotationModel'

describe('rotation snapshot and phase boundaries', () => {
  it.each([undefined, null, {}])('safely normalizes an empty response %s', (payload) => {
    const snapshot = normalizeRotation(payload, 'forward')
    expect(snapshot).toMatchObject({ phase: 'forward', series: [], positions: [], runs: [] })
    expect(isRotationEmpty(snapshot)).toBe(true)
    expect(snapshot.nav).toBeUndefined()
    expect(snapshot.days).toBeUndefined()
  })

  it('keeps missing money as null, not a fabricated cash-only portfolio', () => {
    const snapshot = normalizeRotation({ nav: null, cash: null, days: null, series: null, runs: null, positions: null }, 'replay')
    expect(snapshot.nav).toBeNull()
    expect(snapshot.cash).toBeNull()
    expect(isRotationEmpty(snapshot)).toBe(true)
  })

  it('shows a zero-day forward baseline and pending plans rather than an empty state', () => {
    const snapshot = normalizeRotation({
      phase: 'forward', days: 0, nav: INITIAL_CAPITAL, forward_start: FORWARD_START,
      series: [{ date: '2026-11-13', nav: INITIAL_CAPITAL }],
      pending: { id: 1, status: 'pending', signal_date: '2026-11-13', execute_date: FORWARD_START },
    }, 'forward')
    expect(snapshot.days).toBe(0)
    expect(isRotationEmpty(snapshot)).toBe(false)
    expect(isRotationEmpty(normalizeRotation({ pending: { status: 'pending' } }, 'forward'))).toBe(false)
    expect(isRotationEmpty(normalizeRotation({ nav: 0 }, 'forward'))).toBe(false)
  })

  it('does not count replay days as forward days or infer dates from its series', () => {
    const snapshot = normalizeRotation({ phase: 'replay', days: 250, series: [{ date: '2016-01-04', nav: 980_000 }] }, 'replay')
    expect(snapshot.days).toBe(250)
    expect(snapshot.start).toBeUndefined()
    expect(dayLabel(snapshot.phase)).toBe('回放交易日（不计前向）')
    expect(phaseLabel('replay')).toBe('历史回放 · 仅对照')
    expect(phaseLabel('forward')).toBe('前向观察')
    expect(dayLabel('forward')).toBe('前向交易日')
    expect(FORWARD_START).toBe('2026-11-16')
  })

  it('rejects a replay response under the forward label (and the reverse)', () => {
    expect(() => normalizeRotation({ phase: 'replay' }, 'forward')).toThrow('区间不一致')
    expect(() => normalizeRotation({ phase: 'forward' }, 'replay')).toThrow('区间不一致')
  })
})

describe('indexed NAV', () => {
  it('uses the same initial capital for C13, EW-20d and 510300 on one axis', () => {
    expect(indexedSeries([
      { date: '2026-11-13', nav: 1_000_000, ew_nav: 1_000_000, benchmark_nav: 1_000_000 },
      { date: '2026-11-16', nav: 1_012_300, ew_nav: 990_000, benchmark_nav: 1_005_000 },
    ])).toEqual([
      { date: '2026-11-13', c13: 1, ew: 1, benchmark: 1 },
      { date: '2026-11-16', c13: 1.0123, ew: 0.99, benchmark: 1.005 },
    ])
  })

  it('does not rebase a visible first point or a benchmark that starts late', () => {
    const rows = indexedSeries([
      { date: '2026-11-16', nav: 980_000, ew_nav: null },
      { date: '2026-11-17', nav: 975_000, ew_nav: 1_050_000, benchmark_nav: 1_010_000 },
    ])
    expect(rows[0]).toEqual({ date: '2026-11-16', c13: 0.98, ew: null, benchmark: null })
    expect(rows[1].ew).toBe(1.05)
    expect(rows[1].benchmark).toBe(1.01)
  })

  it('preserves gaps, zeros and blocked valuations without return repairs', () => {
    expect(indexedSeries([
      { date: '2026-11-16', nav: null, ew_nav: undefined, benchmark_nav: Number.NaN },
      { date: '2026-11-17', nav: 0, ew_nav: Number.POSITIVE_INFINITY, benchmark_nav: 900_000 },
    ])).toEqual([
      { date: '2026-11-16', c13: null, ew: null, benchmark: null },
      { date: '2026-11-17', c13: 0, ew: null, benchmark: 0.9 },
    ])
  })

  it('handles empty series and an explicit initial capital, but never divides by zero', () => {
    expect(indexedSeries(null)).toEqual([])
    expect(indexedSeries(undefined)).toEqual([])
    expect(indexedSeries([{ date: '2026-11-16', nav: 2_200_000 }], 2_000_000)[0].c13).toBe(1.1)
    for (const initial of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(indexedSeries([{ date: '2026-11-16', nav: 1_000_000 }], initial)[0].c13).toBeNull()
    }
  })

  it('sorts dates and uses the last record for a repeated date without mutating input', () => {
    const source = [
      { date: '2026-11-17', nav: 1_020_000 },
      { date: '2026-11-16', nav: 1_000_000 },
      { date: '2026-11-17', nav: 1_010_000 },
    ]
    const rows = indexedSeries(source)
    expect(rows.map((r) => r.date)).toEqual(['2026-11-16', '2026-11-17'])
    expect(rows[1].c13).toBe(1.01)
    expect(source[0].date).toBe('2026-11-17')
  })

  it('provides a shared tidy scale for flat and divergent capital series', () => {
    const flat = navScale(indexedSeries([{ date: '2026-11-13', nav: INITIAL_CAPITAL }]))
    expect(flat.min).toBeLessThan(1)
    expect(flat.max).toBeGreaterThan(1)
    expect(flat.ticks).toContain(1)
    const scale = navScale([{ date: '2026-11-16', c13: 0.83, ew: 1.14, benchmark: 1.02 }])
    expect(scale.min).toBeLessThanOrEqual(0.83)
    expect(scale.max).toBeGreaterThanOrEqual(1.14)
    expect(scale.ticks.length).toBeGreaterThan(2)
    expect(navScale([]).ticks).toEqual([])
  })
})

describe('paper status and run details', () => {
  it.each([
    ['pending', '待模拟执行', 'pending'],
    ['executed', '已模拟执行', 'executed'],
    ['blocked', '已阻断', 'blocked'],
    ['unknown', 'unknown', 'neutral'],
    [null, '—', 'neutral'],
    [undefined, '—', 'neutral'],
  ])('labels %s without implying live trades', (status, label, tone) => {
    expect(statusLabel(status)).toBe(label)
    expect(statusTone(status)).toBe(tone)
  })

  it('labels fills as simulated and preserves unfamiliar sides', () => {
    expect(sideLabel('buy')).toBe('模拟买入')
    expect(sideLabel('SELL')).toBe('模拟卖出')
    expect(sideLabel('other')).toBe('other')
    expect(sideLabel(null)).toBe('—')
  })

  it('merges the last run once and keeps recent signals first', () => {
    const rows = [{ id: 1, signal_date: '2026-11-13' }, { id: 2, signal_date: '2026-12-11' }]
    const snapshot = normalizeRotation({ runs: rows, last_run: { ...rows[1] } }, 'forward')
    expect(recentRuns(snapshot).map((r) => r.id)).toEqual([2, 1])
    snapshot.last_run = { id: 3, signal_date: '2027-01-12' }
    expect(recentRuns(snapshot).map((r) => r.id)).toEqual([3, 2, 1])
    expect(rows[0].id).toBe(1)
    expect(recentRuns(null)).toEqual([])
  })

  it('only displays string reasons rather than rendering objects as text', () => {
    expect(runReason({ detail: { reason: '价格断点，等待核验' } })).toBe('价格断点，等待核验')
    expect(runReason({ detail: { reason: { unsafe: 'object' }, message: '请检查数据' } })).toBe('请检查数据')
    expect(runReason({ detail: null })).toBe('')
    expect(runReason(null)).toBe('')
  })
})

describe('currency, fractions and missing values', () => {
  it.each([null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])('shows dashes for %s, never zero', (value) => {
    expect(finite(value)).toBeNull()
    expect(currency(value)).toBe('—')
    expect(percent(value)).toBe('—')
    expect(weight(value)).toBe('—')
    expect(numberText(value)).toBe('—')
    expect(indexText(value)).toBe('—')
  })

  it('formats currency and share counts explicitly', () => {
    expect(currency(INITIAL_CAPITAL)).toBe('¥1,000,000.00')
    expect(currency(1234.567)).toBe('¥1,234.57')
    expect(currency(0)).toBe('¥0.00')
    expect(currency(-25)).toBe('¥-25.00')
    expect(numberText(123_400)).toBe('123,400')
    expect(numberText(3.251, 3)).toBe('3.251')
    expect(indexText(1)).toBe('1.0000')
  })

  it('converts fractional returns and weights to percentages exactly once', () => {
    expect(percent(0.0123)).toBe('1.23%')
    expect(percent(-0.0205)).toBe('-2.05%')
    expect(weight(0.125)).toBe('12.50%')
    expect(weight(0)).toBe('0.00%')
    expect(weight(1)).toBe('100.00%')
  })
})
