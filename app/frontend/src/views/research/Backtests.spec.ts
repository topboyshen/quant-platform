import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import Backtests from './Backtests.vue'
import latest from './fixtures/backtest-latest-501.json'

vi.mock('../../api/client', async (original) => ({
  ...await original<typeof import('../../api/client')>(),
  api: vi.fn(),
}))

beforeEach(() => { vi.mocked(api).mockReset() })

const hit = { total: 1, rows: [{
  run_id: 42, strategy_name: 'MA 双均线', symbol: '000001.SZ',
  timeframe: '1d', status: 'finished', created_at: '2026-11-27T15:00:00+08:00',
}] }

it('lets the user clear an unmatched search and find the run again', async () => {
  vi.mocked(api).mockResolvedValueOnce(hit).mockResolvedValueOnce({ total: 0, rows: [] }).mockResolvedValueOnce(hit)
  const wrapper = mount(Backtests)
  try {
    await flushPromises()
    expect(wrapper.get('tbody tr').text()).toContain('MA 双均线')
    const query = 'ZZZ-无匹配'
    await wrapper.get('input').setValue(query)
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith(`/research/backtests?limit=100&q=${encodeURIComponent(query)}`)
    expect(wrapper.text()).toContain('暂无回测')
    expect(wrapper.find('tbody tr').exists()).toBe(false)
    expect(wrapper.get('button').text()).toBe('查询')
    await wrapper.get('input').setValue('')
    await wrapper.get('input').trigger('keyup.enter')
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith('/research/backtests?limit=100&q=')
    expect(wrapper.get('tbody tr').text()).toContain('MA 双均线')
    expect(wrapper.text()).not.toContain('暂无回测')
  } finally {
    wrapper.unmount()
  }
})

it('lets the user retry a failed list request and removes the old error', async () => {
  vi.mocked(api).mockRejectedValueOnce(new ApiError(500, '服务不可用')).mockResolvedValueOnce(hit)
  const wrapper = mount(Backtests)
  try {
    await flushPromises()
    expect(wrapper.text()).toContain('服务不可用')
    expect(wrapper.get('input').element.value).toBe('')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api).toHaveBeenCalledTimes(2)
    expect(api).toHaveBeenLastCalledWith('/research/backtests?limit=100&q=')
    expect(wrapper.text()).not.toContain('服务不可用')
    expect(wrapper.get('tbody tr').text()).toContain('MA 双均线')
  } finally {
    wrapper.unmount()
  }
})

it('disables editing and submitting a query until its response arrives', async () => {
  let finishSearch!: (result: typeof hit) => void
  const slowSearch = new Promise<typeof hit>(resolve => { finishSearch = resolve })
  vi.mocked(api).mockResolvedValueOnce(hit).mockReturnValueOnce(slowSearch).mockResolvedValueOnce({ total: 0, rows: [] })
  const wrapper = mount(Backtests)
  try {
    await flushPromises()
    const input = wrapper.get('input')
    const button = wrapper.get('button')
    await input.setValue('MA')
    await input.trigger('keyup.enter')
    expect(api).toHaveBeenLastCalledWith('/research/backtests?limit=100&q=MA')
    expect(input.element.disabled).toBe(true)
    expect(button.element.disabled).toBe(true)
    expect(input.element.value).toBe('MA')

    finishSearch(hit)
    await flushPromises()
    expect(input.element.disabled).toBe(false)
    expect(button.element.disabled).toBe(false)
    expect(wrapper.get('tbody tr').text()).toContain('MA 双均线')
    await input.setValue('ZZZ')
    await button.trigger('click')
    await flushPromises()
    expect(api).toHaveBeenLastCalledWith('/research/backtests?limit=100&q=ZZZ')
    expect(wrapper.text()).toContain('暂无回测')
    expect(wrapper.find('tbody tr').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('requests the latest trades for the selected run and shows the full trade count', async () => {
  // Captured from the detail API for a run with indices 1..501 and profit equal to index.
  vi.mocked(api).mockResolvedValueOnce({ total: 1, rows: [latest.run] }).mockResolvedValueOnce(latest)
  const wrapper = mount(Backtests, {
    global: {
      stubs: {
        Drawer: { props: ['show'], template: '<section v-if="show"><slot /></section>' },
        DrawerContent: { template: '<div><slot /></div>' },
      },
    },
  })
  try {
    await flushPromises()
    await wrapper.get('tbody tr').trigger('click')
    await flushPromises()

    expect(api).toHaveBeenLastCalledWith(`/research/backtests/${latest.run.run_id}?trades=latest`)
    const drawer = wrapper.get('section')
    const rows = drawer.findAll('.n-data-table tbody tr')
    const indices = rows.map(row => Number(row.get('td:first-child').text()))
    expect(indices).toEqual(Array.from({ length: 500 }, (_, i) => 501 - i))
    expect(rows[0].get('td:last-child').text()).toBe('501')
    expect(rows[499].get('td:last-child').text()).toBe('2')
    const totals = drawer.get('.n-descriptions').findAll('tr').slice(-2)
    expect(totals.map(row => row.findAll('th, td').map(cell => cell.text()))).toEqual([
      ['权益点', '成交'], ['3', '501'],
    ])
  } finally {
    wrapper.unmount()
  }
})
