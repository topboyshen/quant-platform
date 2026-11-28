import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import IndexClose from './IndexClose.vue'

vi.mock('../../api/client', () => ({ api: vi.fn() }))

// Response from the authenticated index-close API for the two-day regression fixture.
const API_ROWS = [
  { origin: 'legacy', symbol: 'sh000001', trade_date: '2026-11-12', close: 3890.0, source: 'qlib', fetched_at: null },
  { origin: 'qp', symbol: 'sh000001', trade_date: '2026-11-12', close: 3890.0, source: 'tencent', fetched_at: null },
  { origin: 'qp', symbol: 'sz399001', trade_date: '2026-11-12', close: 12980.0, source: 'tencent', fetched_at: null },
  { origin: 'legacy', symbol: 'sz399006', trade_date: '2026-11-12', close: 2480.0, source: 'qlib', fetched_at: null },
  { origin: 'legacy', symbol: 'sh000001', trade_date: '2026-11-13', close: 3900.1234, source: 'qlib', fetched_at: null },
  { origin: 'qp', symbol: 'sh000001', trade_date: '2026-11-13', close: 3900.1234, source: 'tencent', fetched_at: null },
  { origin: 'legacy', symbol: 'sh000300', trade_date: '2026-11-13', close: 4702.0, source: 'qlib', fetched_at: null },
  { origin: 'qp', symbol: 'sh000300', trade_date: '2026-11-13', close: 4700.5, source: 'tencent', fetched_at: null },
  { origin: 'qp', symbol: 'sz399001', trade_date: '2026-11-13', close: 13000.0, source: 'tencent', fetched_at: null },
  { origin: 'legacy', symbol: 'sz399006', trade_date: '2026-11-13', close: 2494.397, source: 'qlib', fetched_at: null },
]

async function renderRows(input: typeof API_ROWS) {
  vi.mocked(api).mockResolvedValueOnce(input)
  const wrapper = mount(IndexClose, { global: { stubs: { EChart: true } } })
  try {
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith('/market/index-close?days=60')
    return wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))
  } finally {
    wrapper.unmount()
  }
}

it('shows both origins once per date and symbol, including both kinds of one-sided close', async () => {
  const rows = await renderRows(API_ROWS)
  expect(rows).toHaveLength(new Set(API_ROWS.map(row => row.symbol + '|' + row.trade_date)).size)
  expect(rows).toEqual([
    ['2026-11-13', 'sh000001', '3900.1234', 'tencent', '3900.1234', '一致'],
    ['2026-11-13', 'sh000300', '4700.5', 'tencent', '4702', '差 -1.5000'],
    ['2026-11-13', 'sz399001', '13000', 'tencent', '-', '-'],
    ['2026-11-13', 'sz399006', '-', '-', '2494.397', '-'],
    ['2026-11-12', 'sh000001', '3890', 'tencent', '3890', '一致'],
    ['2026-11-12', 'sz399001', '12980', 'tencent', '-', '-'],
    ['2026-11-12', 'sz399006', '-', '-', '2480', '-'],
  ])
})

it('includes a difference of 0.011 in the tolerance and reports the next price increment', async () => {
  const pair = (symbol: string, close: number) => [
    { ...API_ROWS[1], symbol, close },
    { ...API_ROWS[0], symbol, close: 4690 },
  ]
  const rows = await renderRows([...pair('sh000001', 4690.011), ...pair('sh000300', 4690.0111)])
  expect(rows.map(row => row[5])).toEqual(['一致', '差 0.0111'])
})
