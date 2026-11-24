import { flushPromises, mount } from '@vue/test-utils'
import { NPagination } from 'naive-ui'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import Outcomes from './Outcomes.vue'

vi.mock('../../api/client', () => ({ api: vi.fn() }))

describe('cross-horizon entry basis', () => {
  it('shows each conflicting field and its recorded values without changing returns', async () => {
    const rows = [{
      id: 1, code: '512000', decision_date: '2026-08-27', horizon_sessions: 1,
      net_return: -0.005381, benchmark_return: 0.01, excess_return: -0.015381, fill_status: 'filled',
      entry_basis: {
        status: 'conflict', horizons: [1, 3, 5], conflicts: [
          { field: 'entry_open', values_by_horizon: { 1: 0.53, 3: 0.528, 5: 0.528 } },
          { field: 'entry_fill', values_by_horizon: { 1: 0.530265, 3: 0.528264, 5: null } },
        ],
      },
    }]
    vi.mocked(api).mockResolvedValue({ total: 1, rows })
    const wrapper = mount(Outcomes)
    try {
      await flushPromises()
      expect(wrapper.findComponent(NPagination).exists()).toBe(false)
      const cells = wrapper.get('tbody tr').findAll('td').map(td => td.text())
      expect(cells.slice(4, 7)).toEqual(['-0.54%', '1.00%', '-1.54%'])
      const evidence = cells[cells.length - 1]
      expect(evidence).toContain('入场依据冲突')
      expect(evidence).toContain('入场开盘价 1日 0.53 · 3日 0.528 · 5日 0.528')
      expect(evidence).toContain('含滑点入场价 1日 0.530265 · 3日 0.528264 · 5日 缺失')
    } finally { wrapper.unmount() }
  })

  it('distinguishes a checked decision from a single horizon and an older response', async () => {
    const rows = [
      { id: 1, entry_basis: { status: 'consistent', horizons: [1, 3], conflicts: [] } },
      { id: 2, entry_basis: { status: 'insufficient', horizons: [1], conflicts: [] } },
      { id: 3 },
    ]
    vi.mocked(api).mockResolvedValue({ total: rows.length, rows })
    const wrapper = mount(Outcomes)
    try {
      await flushPromises()
      expect(wrapper.findAll('tbody tr').map(row => row.get('td:last-child').text())).toEqual([
        '一致 · 已成交持有期 1日、3日', '依据不足 · 已成交持有期 1日', '暂无核对结果',
      ])
    } finally { wrapper.unmount() }
  })

  it('loads older outcomes on demand and ignores a superseded page response', async () => {
    const recent = { total: 201, rows: [{ id: 201, code: '510300' }] }
    const older = { total: 201, rows: [{ id: 1, code: '512000', entry_basis: {
      status: 'conflict', horizons: [1, 3], conflicts: [{ field: 'entry_open', values_by_horizon: { 1: 0.53, 3: 0.528 } }],
    } }] }
    let finishLate!: (data: typeof recent) => void
    const latePage = new Promise<typeof recent>(resolve => { finishLate = resolve })
    const request = vi.mocked(api).mockClear()
      .mockResolvedValueOnce(recent).mockResolvedValueOnce(older)
      .mockReturnValueOnce(latePage).mockResolvedValueOnce(older)
    const wrapper = mount(Outcomes)
    try {
      await flushPromises()
      expect(request.mock.calls).toEqual([['/signals/outcomes?limit=200&offset=0']])
      const pageButtons = wrapper.getComponent(NPagination).findAll('.n-pagination-item--clickable')
      await pageButtons[1].trigger('click')
      await flushPromises()
      expect(request).toHaveBeenLastCalledWith('/signals/outcomes?limit=200&offset=200')
      expect(wrapper.get('tbody').text()).toContain('1日 0.53 · 3日 0.528')
      await pageButtons[0].trigger('click')
      await pageButtons[1].trigger('click')
      await flushPromises()
      finishLate(recent)
      await flushPromises()
      expect(request).toHaveBeenCalledTimes(4)
      expect(wrapper.getComponent(NPagination).props('page')).toBe(2)
      expect(wrapper.get('tbody').text()).toContain('512000')
      expect(wrapper.get('tbody').text()).not.toContain('510300')
    } finally { wrapper.unmount() }
  })
})
