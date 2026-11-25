import { describe, expect, it } from 'vitest'
import { horizonCell, pct, riskLabel, type ShadowSample } from './shadowModel'

const sample = (outcomes: ShadowSample['outcomes']): ShadowSample => ({
  id: 1, decision_date: '2026-11-12', code: '513050', name: '中概互联网ETF',
  risk_level: 'growth_blocked', reason_code: 'macro_risk_blocked', raw_trigger_count: 1, outcomes,
})

describe('riskLabel', () => {
  it.each([
    ['critical', '严重'],
    ['growth_blocked', '成长受限'],
    ['warning', '预警'],
    ['ok', '正常'],
    ['unavailable', '不可用'],
  ])('%s -> %s', (value, expected) => {
    expect(riskLabel(value)).toBe(expected)
  })
  it('keeps an unknown level and shows a dash for null', () => {
    expect(riskLabel('mystery')).toBe('mystery')
    expect(riskLabel(null)).toBe('—')
  })
})

describe('pct', () => {
  it('formats two decimals with a sign-preserving value', () => {
    expect(pct(0.0123)).toBe('1.23%')
    expect(pct(-0.0205)).toBe('-2.05%')
    expect(pct(0)).toBe('0.00%')
  })
  it('shows a dash for missing values', () => {
    expect(pct(null)).toBe('—')
    expect(pct(undefined)).toBe('—')
  })
})

describe('horizonCell', () => {
  it('distinguishes pending, unfilled and filled horizons', () => {
    const row = sample({
      '1': { net_return: 0.0123, excess_return: 0.002, fill_status: 'filled' },
      '3': { net_return: null, excess_return: null, fill_status: 'unfilled' },
    })
    expect(horizonCell(row, 1)).toBe('无依据 1.23%')
    expect(horizonCell(row, 3)).toBe('未成交')
    expect(horizonCell(row, 5)).toBe('待到期')
  })
  it('labels a counterfactual fill and keeps an executable fill unmarked', () => {
    const row = sample({
      '1': { net_return: 0.041, excess_return: 0.01, fill_status: 'filled', return_role: 'counterfactual_ungated' },
      '5': { net_return: 0.02, excess_return: 0.01, fill_status: 'filled', return_role: 'executable' },
    })
    expect(horizonCell(row, 1)).toBe('反事实 4.10%')
    expect(horizonCell(row, 5)).toBe('2.00%')
  })
})
