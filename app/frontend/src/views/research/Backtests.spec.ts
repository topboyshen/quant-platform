import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import Backtests from './Backtests.vue'
import latest from './fixtures/backtest-latest-501.json'

vi.mock('../../api/client', () => ({ api: vi.fn() }))

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
