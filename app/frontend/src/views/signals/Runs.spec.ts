import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { NDrawer, NDrawerContent } from 'naive-ui'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import Runs from './Runs.vue'

vi.mock('../../api/client', () => ({ api: vi.fn() }))
enableAutoUnmount(afterEach)

const a = {
  run_id: 101, decision_date: '2026-11-26', attempt_no: 1, status: 'success', outcome: '已生成目标',
  signal_count: 9, allowed: 6, blocked: 3,
}
const b = {
  run_id: 102, decision_date: '2026-11-27', attempt_no: 2, status: 'partial', outcome: '部分评估',
  signal_count: 4, allowed: 1, blocked: 3,
}
const decisionsA = [
  { id: 1, code: '510300', name: '沪深300ETF', decision_state: '买入', risk_action: 'allow', reason_code: 'TREND_UP' },
  { id: 2, code: '159915', name: '创业板ETF', decision_state: '持有', risk_action: 'allow', reason_code: 'HOLD' },
]
const decisionsB = [
  { id: 3, code: '512000', name: '券商ETF', decision_state: '卖出', risk_action: 'block', reason_code: 'RISK_OFF' },
]

function pendingDetail() {
  let resolve!: (data: { decisions: typeof decisionsA }) => void
  let reject!: (error: Error) => void
  const promise = new Promise<{ decisions: typeof decisionsA }>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

async function setup() {
  const request = vi.mocked(api).mockReset().mockResolvedValueOnce({ total: 2, rows: [a, b] })
  const wrapper = mount(Runs, { attachTo: document.body })
  await flushPromises()
  const rows = wrapper.findAll('tbody tr')
  const drawer = wrapper.getComponent(NDrawer)
  const content = () => wrapper.getComponent(NDrawerContent)

  async function select(run: typeof a) {
    const detail = pendingDetail()
    request.mockReturnValueOnce(detail.promise)
    await rows.find(row => row.get('td').text() === String(run.run_id))!.trigger('click')
    expect(request).toHaveBeenLastCalledWith(`/signals/runs/${run.run_id}`)
    return detail
  }

  function decisionCodes() {
    return content().findAll('.n-data-table tbody tr').map(row => row.get('td').text())
  }

  function expectRun(run: typeof a, decisions: typeof decisionsA) {
    expect(drawer.props('show')).toBe(true)
    expect(content().props('title')).toBe(`运行 #${run.run_id}`)
    expect(content().findAll('.n-descriptions-table-content').map(cell => cell.text())).toEqual([
      run.decision_date, run.status, run.outcome, String(run.signal_count), String(run.allowed), String(run.blocked),
    ])
    expect(content().find('.n-data-table').exists()).toBe(true)
    expect(decisionCodes()).toEqual(decisions.map(row => row.code))
    expect(content().find('[role="alert"]').exists()).toBe(false)
  }

  return { request, drawer, content, select, decisionCodes, expectRun }
}

describe('EOD run details', () => {
  it('ignores an older success after the selected run has loaded', async () => {
    const page = await setup()
    const first = await page.select(a)
    const latest = await page.select(b)
    latest.resolve({ decisions: decisionsB })
    await flushPromises()
    page.expectRun(b, decisionsB)
    first.resolve({ decisions: decisionsA })
    await flushPromises()
    page.expectRun(b, decisionsB)
  })

  it('does not show older decisions while waiting for the selected run', async () => {
    const page = await setup()
    const first = await page.select(a)
    const latest = await page.select(b)
    first.resolve({ decisions: decisionsA })
    await flushPromises()
    if (page.drawer.props('show')) expect(page.decisionCodes()).toEqual([])
    latest.resolve({ decisions: decisionsB })
    await flushPromises()
    page.expectRun(b, decisionsB)
  })

  it('clears old decisions, reports a current failure and retries that run', async () => {
    const page = await setup()
    const first = await page.select(a)
    first.resolve({ decisions: decisionsA })
    await flushPromises()
    const latest = await page.select(b)
    expect(page.decisionCodes()).toEqual([])
    latest.reject(new Error('运行 102 请求失败'))
    await flushPromises()
    expect(page.drawer.props('show')).toBe(true)
    expect(page.content().props('title')).toBe('运行 #102')
    expect(page.decisionCodes()).toEqual([])
    expect(page.content().text()).toContain('运行 102 请求失败')
    page.request.mockResolvedValueOnce({ decisions: decisionsB })
    const retry = page.content().get('button')
    expect(retry.text()).toContain('重试')
    await retry.trigger('click')
    await flushPromises()
    expect(page.request.mock.calls.map(([path]) => path)).toEqual([
      '/signals/runs?limit=100', '/signals/runs/101', '/signals/runs/102', '/signals/runs/102',
    ])
    page.expectRun(b, decisionsB)
  })

  it('shows the error and retry even when the first selection fails', async () => {
    const page = await setup()
    const detail = await page.select(b)
    detail.reject(new Error('运行 102 请求失败'))
    await flushPromises()
    expect(page.drawer.props('show')).toBe(true)
    expect(page.content().get('[role="alert"]').text()).toContain('运行 102 请求失败')
    expect(page.content().get('button').text()).toContain('重试')
    expect(page.decisionCodes()).toEqual([])
  })

  it('ignores a late error after the selected run succeeded', async () => {
    const page = await setup()
    const first = await page.select(a)
    const latest = await page.select(b)
    latest.resolve({ decisions: decisionsB })
    await flushPromises()
    first.reject(new Error('过期请求失败'))
    await flushPromises()
    page.expectRun(b, decisionsB)
    expect(page.content().text()).not.toContain('过期请求失败')
  })

  it('keeps the drawer closed after a late response until the run is selected again', async () => {
    const page = await setup()
    const first = await page.select(a)
    first.resolve({ decisions: decisionsA })
    await flushPromises()
    const pending = await page.select(b)
    page.drawer.vm.$emit('update:show', false)
    await flushPromises()
    expect(page.drawer.props('show')).toBe(false)
    pending.resolve({ decisions: decisionsB })
    await flushPromises()
    expect(page.drawer.props('show')).toBe(false)
    const reopened = await page.select(b)
    reopened.resolve({ decisions: decisionsB })
    await flushPromises()
    page.expectRun(b, decisionsB)
  })

  it.each(['012', '021', '102', '120', '201', '210'])('accepts only the newest A in A → B → A, resolution order %s', async order => {
    const page = await setup()
    const requests = [await page.select(a), await page.select(b), await page.select(a)]
    const replies = [decisionsA.slice(1), decisionsB, decisionsA]
    let latestLoaded = false
    for (const index of Array.from(order, Number)) {
      requests[index].resolve({ decisions: replies[index] })
      await flushPromises()
      latestLoaded ||= index === 2
      if (latestLoaded) page.expectRun(a, decisionsA)
      else if (page.drawer.props('show')) expect(page.decisionCodes()).toEqual([])
    }
  })

  it('opens a single run with its summary and decisions', async () => {
    const page = await setup()
    const first = await page.select(a)
    first.resolve({ decisions: decisionsA })
    await flushPromises()
    page.expectRun(a, decisionsA)
  })

  it('opens an empty decision table for a run with no decisions', async () => {
    const page = await setup()
    const detail = await page.select(b)
    detail.resolve({ decisions: [] })
    await flushPromises()
    page.expectRun(b, [])
  })
})
