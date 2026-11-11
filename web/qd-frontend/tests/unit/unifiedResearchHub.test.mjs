// Actual SFC methods with synthetic account-bound GET transports. No network,
// production credentials, business writes or execution endpoints are used.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { validationView, operationalView } from '../../src/utils/unifiedResearch.js'
import { isUnifiedResearchRead } from '../../src/utils/unifiedResearchRead.js'

const source = readFileSync(new URL('../../src/components/UnifiedResearchEvidence/index.vue', import.meta.url), 'utf8')
const utility = new URL('../../src/utils/unifiedResearch.js', import.meta.url).href
const nng8Utility = new URL('../../src/utils/nng8Readonly.js', import.meta.url).href
const stamp = '2026-10-10T12:00:00+08:00'
let moduleCount = 0
const clone = value => structuredClone(value)
const envelope = data => ({ code: 1, data })
const nng8 = () => ({
  contract_version: 'workbench.nng8.v1', source: 'nng8', captured_at: stamp,
  observed_at: stamp, snapshot_stale: false, status: 'ok', reports: [], delivery: [],
  services: [{ id: 'example.service', label: '合成任务', status: 'active', last_run: null, next_run: null }],
  data_quality: { status: 'pass', counts_scope: 'sealed_core_qlib_summary', counts_contract: 'core.v1', counts_target_date: '2026-10-09', market_date: '2026-10-09', eligible: 3, valid: 2, invalid: 1 },
  calibration: [
    { id: 'ledger', label: 'Ledger', status: 'insufficient', runtime_mode: 'FROZEN', nominal: 210, observed: 180, pending_review: 180, formal: 0, limitations: [] },
    { id: 'phase2', label: 'Phase2', status: 'insufficient', runtime_mode: 'FROZEN', nominal: 210, observed: null, pending_review: 180, formal: 0, limitations: [] }
  ], issues: [], limitations: []
})
const overview = (items = []) => ({ contract_version: 'workbench.overview.v1', items })
const radar = () => ({ signals: { effectiveness: { state: 'sample_insufficient', sample_size: 1, minimum_samples: 30, primary_horizon_sessions: 20 } } })
const error = status => ({ response: { status, data: { msg: 'private upstream details must not be rendered' } } })
function deferred () {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function tick () { for (let i = 0; i < 6; i++) await Promise.resolve() }

async function harness (api = {}, { section = 'validation', id = 101 } = {}) {
  const registry = '__unified_hub_transport_' + (++moduleCount)
  const calls = []
  const defaults = { getNng8Readonly: () => envelope(nng8()), getWorkbenchOverview: () => envelope(overview()), getRadarMarketContext: () => envelope(radar()) }
  globalThis[registry] = Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name, (...args) => { calls.push({ name, args }); return (api[name] || fallback)(...args) }]))
  const code = source.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace("import { getNng8Readonly, getWorkbenchOverview, getRadarMarketContext } from '@/api/unifiedResearch'", `const { getNng8Readonly, getWorkbenchOverview, getRadarMarketContext } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import ResearchRunQuality from '@/components/ResearchRunQuality'", 'const ResearchRunQuality = {}')
    .replace("import ResearchValidationProgress from '@/components/ResearchValidationProgress'", 'const ResearchValidationProgress = {}')
    .replace("import ResearchTaskOperations from '@/components/ResearchTaskOperations'", 'const ResearchTaskOperations = {}')
    .replace("import ExternalTaskOperations from '@/components/ExternalTaskOperations'", 'const ExternalTaskOperations = {}')
    .replace("import { NNG8_READONLY_ENABLED,", 'const NNG8_READONLY_ENABLED = true\nimport {')
    .replace("from '@/utils/nng8Readonly'", 'from ' + JSON.stringify(nng8Utility))
    .replace("from '@/utils/unifiedResearch'", 'from ' + JSON.stringify(utility))
  let options
  try { options = (await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(code))).default } finally { delete globalThis[registry] }
  const subscriptions = []
  let unsubscribed = 0
  const store = { getters: { userInfo: { id } }, state: { app: { theme: 'light' } }, subscribe (listener) { subscriptions.push(listener); return () => { unsubscribed++ } } }
  Object.defineProperty(store.getters, 'token', { get () { throw Error('test forbids token reads') } })
  const vm = { section, $store: store, $set: (object, key, value) => { object[key] = value }, $nextTick: fn => Promise.resolve().then(fn) }
  Object.assign(vm, options.data.call(vm))
  for (const [name, method] of Object.entries(options.methods)) vm[name] = method.bind(vm)
  for (const [name, getter] of Object.entries(options.computed)) Object.defineProperty(vm, name, { get: getter.bind(vm) })
  return { vm, options, calls, subscriptions, get unsubscribed () { return unsubscribed } }
}

for (const status of [403, 404]) {
  for (const order of ['first', 'last']) {
    test(`nnG8 ${status} arriving ${order} preserves independent overview and ETF evidence`, async () => {
      const external = deferred(); const local = deferred(); const etf = deferred()
      const h = await harness({ getNng8Readonly: () => external.promise, getWorkbenchOverview: () => local.promise, getRadarMarketContext: () => etf.promise })
      const pending = h.vm.load()
      if (order === 'first') { external.reject(error(status)); await tick() }
      local.resolve(envelope(overview([{ id: 'eod_status', status: 'recovered', detail: '首轮失败后自然恢复' }])))
      etf.resolve(envelope(radar())); await tick()
      if (order === 'last') external.reject(error(status))
      await pending
      assert.equal(h.vm.slots.nng8.status, 'failed')
      assert.equal(h.vm.slots.overview.status, 'ready')
      assert.equal(h.vm.slots.radar.status, 'ready')
      assert.equal(h.vm.validation.etf.sampleSize, 1)
      assert.equal(h.vm.permission, '')
      assert.equal(h.vm.loading, false)
      assert.doesNotMatch(h.vm.slots.nng8.error, /private upstream/)
    })
  }
}

for (const order of ['before', 'after']) {
  test(`401 ${order} local success clears all evidence and rejects late responses`, async () => {
    const external = deferred(); const local = deferred(); const etf = deferred()
    const h = await harness({ getNng8Readonly: () => external.promise, getWorkbenchOverview: () => local.promise, getRadarMarketContext: () => etf.promise })
    const pending = h.vm.load()
    if (order === 'after') { local.resolve(envelope(overview())); await tick(); assert.equal(h.vm.slots.overview.status, 'ready') }
    external.reject(error(401)); await tick()
    assert.deepEqual(h.vm.slots, {})
    assert.match(h.vm.permission, /登录状态已失效/)
    if (order === 'before') local.resolve(envelope(overview()))
    etf.resolve(envelope(radar())); await pending
    assert.deepEqual(h.vm.slots, {})
    assert.equal(h.vm.overview, null)
    assert.equal(h.vm.radar, null)
    assert.equal(h.vm.loading, false)
  })
}

test('account replacement invalidates all old GET responses before new account evidence loads', async () => {
  const old = deferred(); let requests = 0
  const h = await harness({ getNng8Readonly: () => ++requests === 1 ? old.promise : envelope({ ...nng8(), status: 'partial' }) }, { section: 'quality' })
  const pending = h.vm.load()
  h.vm.$store.getters.userInfo = { id: 202 }
  h.options.watch.accountId.call(h.vm)
  await tick()
  old.resolve(envelope(nng8())); await pending
  assert.equal(h.vm.accountId, '202')
  assert.equal(h.vm.nng8.status, 'partial')
  assert.equal(requests, 2)
})

for (const mutation of ['user/SET_TOKEN', 'user/SET_INFO']) {
  test(`${mutation} for the same user invalidates old responses without reading credentials`, async () => {
    const old = deferred(); let requests = 0
    const h = await harness({ getNng8Readonly: () => ++requests === 1 ? old.promise : envelope({ ...nng8(), status: 'partial' }) }, { section: 'quality' })
    h.options.created.call(h.vm)
    assert.equal(h.subscriptions.length, 1)
    h.subscriptions[0]({ type: mutation }); await tick()
    old.resolve(envelope(nng8())); await tick()
    assert.equal(h.vm.accountId, '101')
    assert.equal(h.vm.nng8.status, 'partial')
    assert.equal(requests, 2)
  })
}

test('keep-alive departure rejects late source evidence and permits a fresh read after return', async () => {
  const old = deferred(); let requests = 0
  const h = await harness({ getNng8Readonly: () => ++requests === 1 ? old.promise : envelope({ ...nng8(), status: 'partial' }) }, { section: 'quality' })
  const pending = h.vm.load()
  h.options.deactivated.call(h.vm)
  old.resolve(envelope(nng8())); await pending
  assert.deepEqual(h.vm.slots, {})
  assert.equal(h.vm.active, false)
  h.options.activated.call(h.vm); await tick()
  assert.equal(h.vm.active, true)
  assert.equal(h.vm.nng8.status, 'partial')
})

test('destroyed hub drops pending reads and releases its subscription', async () => {
  const old = deferred()
  const h = await harness({ getNng8Readonly: () => old.promise }, { section: 'quality' })
  h.options.created.call(h.vm)
  h.options.beforeDestroy.call(h.vm)
  old.resolve(envelope(nng8())); await tick()
  assert.deepEqual(h.vm.slots, {})
  assert.equal(h.unsubscribed, 1)
  assert.equal(h.vm.active, false)
})

test('section switch discards prior sources instead of leaking them into the new section', async () => {
  const old = deferred(); let requests = 0
  const h = await harness({ getNng8Readonly: () => ++requests === 1 ? old.promise : envelope({ ...nng8(), status: 'partial' }) }, { section: 'quality' })
  const pending = h.vm.load()
  h.vm.section = 'operations'; h.options.watch.section.call(h.vm)
  await tick(); old.resolve(envelope(nng8())); await pending
  assert.equal(h.vm.nng8.status, 'partial')
  assert.equal(h.vm.slots.overview.status, 'ready')
  assert.equal(h.vm.slots.radar, undefined)
})

test('unknown overview contract does not become ready or clear independent valid sources', async () => {
  const h = await harness({ getWorkbenchOverview: () => envelope({ contract_version: 'unreviewed.v99', items: [{ id: 'profit_radar', status: 'ok' }] }) })
  await h.vm.load()
  assert.equal(h.vm.slots.overview.status, 'failed')
  assert.equal(h.vm.overview, null)
  assert.equal(h.vm.slots.nng8.status, 'ready')
  assert.equal(h.vm.slots.radar.status, 'ready')
})

test('invalid nnG8 and Radar contracts are failed evidence, never empty success', async () => {
  const h = await harness({ getNng8Readonly: () => envelope({ contract_version: 'unknown' }), getRadarMarketContext: () => envelope({}) })
  await h.vm.load()
  assert.equal(h.vm.slots.nng8.status, 'failed')
  assert.equal(h.vm.slots.radar.status, 'failed')
  assert.equal(h.vm.nng8, null)
  assert.equal(h.vm.validation.etf.available, false)
  assert.equal(h.vm.validation.etf.sampleSize, '--')
})

test('four validation protocols preserve their own counts and never manufacture a shared score', async () => {
  const h = await harness(); await h.vm.load()
  const before = clone({ nng8: h.vm.nng8, overview: h.vm.overview, radar: h.vm.radar })
  const view = validationView(before)
  assert.equal(view.ledger.nominal, 210)
  assert.equal(view.ledger.observed, 180)
  assert.equal(view.ledger.pending_review, 180)
  assert.equal(view.ledger.formal, 0)
  assert.equal(view.phase2.nominal, 210)
  assert.equal(view.phase2.observed, null)
  assert.equal(view.phase2.formal, 0)
  assert.equal(view.etf.sampleSize, 1)
  assert.equal(view.etf.minimumSamples, 30)
  assert.deepEqual(view.profit, [])
  assert.equal(Object.keys(view).some(key => /^(?:total|combined|score)/i.test(key)), false)
  assert.deepEqual(before, { nng8: h.vm.nng8, overview: h.vm.overview, radar: h.vm.radar })
  assert.match(source, /Ledger 与 Phase2 可能重叠；不相加，也不计入 ETF 或 Profit 样本/)
  assert.match(source, /待复核包含在已观察中，数字不能相加/)
})

test('readable Profit files do not hide stale parent state or invent missing business dates', () => {
  const parent = { id: 'profit_radar', label: 'Profit', status: 'stale', detail: '同步失败，当前为旧副本', data_cutoff: stamp, value: { files: [
    { file: '026a1083d9e0/latest.md', status: 'ok', conclusion: '表现承压，盈利优势未建立', generated_at: null, market_cutoff_date: null, mtime: stamp, sync_at: stamp },
    { file: 'ec6c86fd856c/latest.md', status: 'ok', conclusion: null, generated_at: '2026-10-09T15:10:00+08:00', market_cutoff_date: '2026-10-09' }
  ] } }
  const view = validationView({ nng8: null, radar: null, overview: overview([parent]) })
  assert.equal(view.profitSource.status, 'stale')
  assert.equal(view.profitSource.detail, '同步失败，当前为旧副本')
  assert.equal(view.profit[0].status, 'ok')
  assert.equal(view.profit[0].generatedAt, '未知')
  assert.equal(view.profit[0].marketCutoff, '未知')
  assert.equal(view.profit[1].conclusion, '未知')
  assert.equal(view.profit[1].marketCutoff, '2026-10-09')
  assert.match(source, /validation\.profitSource\.status/)
  assert.match(source, /子文件可读不代表同步或业务已成功/)
})

test('operations distinguish active processes and readable evidence from task success', () => {
  const view = operationalView(nng8(), overview([{ id: 'hermes_status', status: 'ok', detail: '进程可读；Telegram 未验证', data_cutoff: stamp }]))
  assert.equal(view[0].state.label, '证据可读取')
  assert.equal(view[0].lastRun, null)
  assert.equal(view[1].state.label, '进程运行')
  assert.equal(view[1].cutoff, null)
  assert.match(view[1].detail, /进程运行不代表任务成功/)
  assert.match(source, /<external-task-operations \/>/)
  assert.match(source, /副本时间不代表此刻状态/)
  const capturedCards = source.slice(source.indexOf('<div class="operations-grid">'), source.indexOf('<details v-if="nng8"'))
  assert.doesNotMatch(capturedCards, /@click|<a-button|<button/)
})

test('auth transport exception allows only explicitly flagged exact GET endpoints', () => {
  const urls = ['/api/radar/market-context', '/api/radar/workbench/overview', '/api/radar/workbench/nng8', '/api/research-lab/v2/capabilities', '/api/research-lab/v2/projects', '/api/research-lab/v2/projects/P_1', '/api/research-lab/v2/branches/B-1', '/api/research-lab/v2/branches/B-1/runs', '/api/research-lab/v2/runs/R1', '/api/research-lab/v2/artifacts/A1']
  for (const url of urls) assert.equal(isUnifiedResearchRead({ url, method: 'get', unifiedResearchCallerHandlesAuth: true }), true, url)
  for (const value of [undefined, false, 'true', 1]) assert.equal(isUnifiedResearchRead({ url: urls[0], method: 'get', unifiedResearchCallerHandlesAuth: value }), false)
})

test('auth transport exception accepts only the request interceptor numeric cache-buster', () => {
  const base = { url: '/api/radar/workbench/overview', method: 'get', unifiedResearchCallerHandlesAuth: true }
  assert.equal(isUnifiedResearchRead({ ...base, params: { _t: Date.now() } }), true)
  assert.equal(isUnifiedResearchRead({ ...base, params: {} }), true)
  for (const params of [{ _t: '123' }, { _t: 0 }, { _t: -1 }, { _t: Infinity }, { _t: 1.5 }, { _t: {} }, { _t: Date.now(), replay: 1 }, [], true, 'replay']) {
    assert.equal(isUnifiedResearchRead({ ...base, params }), false, JSON.stringify(params))
  }
})

test('auth transport exception rejects POST, write endpoints, external URLs and query-bearing requests', () => {
  const invalid = [
    { url: '/api/radar/workbench/overview', method: 'post' },
    { url: '/api/research-lab/v2/runs', method: 'post' },
    { url: '/api/research-lab/v2/branches/B1/draft', method: 'get' },
    { url: '/api/research-lab/v2/branches/B1/external-evidence', method: 'get' },
    { url: '/api/research-lab/v2/admin/admission', method: 'get' },
    { url: 'https://example.invalid/api/radar/workbench/overview', method: 'get' },
    { url: '//example.invalid/api/radar/workbench/overview', method: 'get' },
    { url: '/api/radar/workbench/overview?replay=1', method: 'get' },
    { url: '/api/research-lab/v2/projects/P1?source=other', method: 'get' },
    { url: '/api/radar/workbench/overview', method: 'get', baseURL: 'https://example.invalid' },
    { url: '/api/radar/workbench/overview', method: 'get', params: { replay: 1 } }
  ]
  for (const config of invalid) assert.equal(isUnifiedResearchRead({ ...config, unifiedResearchCallerHandlesAuth: true }), false, JSON.stringify(config))
})
