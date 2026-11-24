import { describe, expect, it } from 'vitest'
import { buildStrategyRows, type AttrReport } from './strategyModel'

function group(bucket: string, n: number, excess: number | null, overrides: Record<string, unknown> = {}) {
  return {
    bucket,
    n,
    eligible_n: 1,
    win_rate: 0.5,
    avg_net_return: 0.01,
    avg_excess_return: excess,
    sample_status: 'insufficient',
    sample_status_label: '样本不足',
    ...overrides,
  }
}

const report: AttrReport = {
  as_of: '2026-11-24',
  horizon: 20,
  threshold: 30,
  group_by: ['eligibility', 'sector'],
  overall: group('all', 20, 0.01),
  eligible: group('eligible', 1, 0.02),
  sample: { matured_filled: 20, eligible_matured: 1 },
  warnings: [],
  groups: {
    eligibility: [
      group('no_trigger', 15, -0.01),
      group('eligible', 1, 0.04),
      group('unknown', 10, 0.09),
    ],
    sector: [
      group('宽基', 8, 0.03),
      group('行业', 4, 0.06),
      group('防御', 36, 0.0743, { sample_status: 'baseline_only', sample_status_label: '仅基线' }),
    ],
  },
}

describe('buildStrategyRows', () => {
  it('flattens dimensions, hides unknown, applies sample gate and sorts by excess', () => {
    const rows = buildStrategyRows(report, { minSamples: 5 })
    expect(rows.map((row) => `${row.dimension}:${row.bucket}`)).toEqual([
      'sector:宽基',
      'eligibility:no_trigger',
    ])
    expect(rows[0].rank).toBe(1)
    expect(rows[0].dimension_label).toBe('板块')
  })

  it('hides baseline-only groups by default and includes them on request', () => {
    const hidden = buildStrategyRows(report, { minSamples: 1 })
    expect(hidden.some((row) => row.bucket === '防御')).toBe(false)
    const shown = buildStrategyRows(report, { minSamples: 1, hideBaseline: false })
    expect(shown.some((row) => row.bucket === '防御')).toBe(true)
  })

  it('can include unknown and filter to one dimension', () => {
    const rows = buildStrategyRows(report, {
      minSamples: 1,
      hideUnknown: false,
      dimension: 'eligibility',
      sortBy: 'avg_excess_return',
    })
    expect(rows.map((row) => row.bucket)).toEqual(['unknown', 'eligible', 'no_trigger'])
    expect(rows.every((row) => row.dimension === 'eligibility')).toBe(true)
  })

  it('sorts null metrics last and breaks ties by sample size', () => {
    const rows = buildStrategyRows({
      ...report,
      group_by: ['sector'],
      groups: { sector: [group('A', 10, null), group('B', 12, null), group('C', 5, 0.2)] },
    }, { minSamples: 1 })
    expect(rows.map((row) => row.bucket)).toEqual(['C', 'B', 'A'])
  })
})
