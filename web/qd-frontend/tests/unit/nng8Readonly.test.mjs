// Synthetic/explicitly approved local-capture model and real SFC methods.
// No production/network, database, producer, or notification calls.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import axiosLibrary from 'axios'
import {
  NNG8_READONLY_ENABLED, NNG8_SECTION_ROUTE, nng8Enabled, nng8AccountId, nng8PageState,
  nng8Time, nng8Value, nng8Status, normalizeNng8
} from '../../src/utils/nng8Readonly.js'

const source = readFileSync(new URL('../../src/components/Nng8Readonly/index.vue', import.meta.url), 'utf8')
const nativeReports = readFileSync(new URL('../../src/views/personal-reports/index.vue', import.meta.url), 'utf8')
const home = readFileSync(new URL('../../src/views/personal-home/index.vue', import.meta.url), 'utf8')
const API = readFileSync(new URL('../../src/api/workbench.js', import.meta.url), 'utf8')
const stamp = '2026-10-09T11:00:00+08:00'
const hash = 'a'.repeat(64)
const report = () => ({ id: 'nng8:astock:2026-10-09:' + hash, title: '合成晨报', source: 'nng8/astock',
  report_date: '2026-10-09', generated_at: null, data_cutoff: null, evidence_cutoff: '证据观察截止', source_time: stamp,
  status: 'ok', publication_gate: 'passed', review_status: 'not_reviewed', conclusion: ['合成结论'], risks: ['合成风险'],
  blocks: [{ heading: '合成正文', text: '<script>alert(1)</script>\nhttps://example.invalid/指令' }],
  body_block_count: 1, body_truncated: false, content_sha256: hash })
const dto = (overrides = {}) => ({ contract_version: 'workbench.nng8.v1', source: 'nng8', captured_at: stamp,
  observed_at: stamp, snapshot_stale: false, status: 'ok', reports: [report()],
  data_quality: { status: 'ok', source_time: stamp, eligible: 3, valid: 2, invalid: 1,
    counts_scope: 'full_market', full_snapshot: true },
  delivery: [{ id: 'seal', label: '封存', status: 'pass', source_time: null, limitations: [] }],
  services: [{ id: 'synthetic.service', label: '合成服务', status: 'unknown', last_run: null, next_run: null }],
  calibration: [{ id: 'ledger', label: '研究账本', status: 'ok', runtime_mode: 'FROZEN', nominal: 210, observed: 180, pending_review: 180, formal: 0 }],
  issues: [], limitations: [], ...overrides })
const response = overrides => ({ code: 1, msg: 'success', data: dto(overrides) })
function deferred () {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function httpError (status) { return { response: { status, data: { msg: 'sensitive upstream should never render' } } } }
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
let moduleId = 0

async function instance (api, { enabled = true, id = 1, theme = 'light' } = {}) {
  // Extract the actual SFC script; replace only the API transport import.
  // The real component options, methods, computed, watchers and hooks execute.
  const registry = '__nng8_test_transport_' + (++moduleId)
  globalThis[registry] = { getNng8Readonly: api }
  const utility = new URL('../../src/utils/nng8Readonly.js', import.meta.url).href
  const code = source.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace("import Nng8ResearchLink from '@/components/Nng8ResearchLink'", 'const Nng8ResearchLink = {}')
    .replace("import { getNng8Readonly } from '@/api/workbench'", 'const { getNng8Readonly } = globalThis[' + JSON.stringify(registry) + ']')
    .replace("from '@/utils/nng8Readonly'", 'from ' + JSON.stringify(utility))
  let options
  try { options = (await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(code))).default } finally { delete globalThis[registry] }
  const events = { subscriptions: [], unsubscribed: 0, focused: 0, scrolled: 0 }
  const store = { state: { app: { theme } }, getters: { userInfo: { id } },
    subscribe (listener) { events.subscriptions.push(listener); return () => { events.unsubscribed++ } } }
  Object.defineProperty(store.getters, 'token', { get () { throw new Error('test forbids reading token') } })
  const vm = { enabled, $store: store, $route: { query: { section: 'nng8' } },
    $nextTick: fn => Promise.resolve().then(fn),
    $refs: { region: { scrollIntoView () { events.scrolled++ }, focus () { events.focused++ } } } }
  Object.assign(vm, options.data.call(vm))
  for (const [key, method] of Object.entries(options.methods)) vm[key] = method.bind(vm)
  for (const [key, getter] of Object.entries(options.computed)) Object.defineProperty(vm, key, { get: getter.bind(vm) })
  for (const hook of ['created', 'mounted', 'activated', 'deactivated', 'beforeDestroy']) vm[hook] = options[hook].bind(vm)
  return { vm, options, events, store }
}

async function actualTransport () {
  // Execute the real shared Axios module and API helper, replacing only their
  // unrelated browser/import dependencies. Its real interceptors and Axios
  // request/response pipeline stay intact; the adapter has no network capability.
  const registry = '__nng8_actual_transport_' + (++moduleId)
  const events = { removed: [], notifications: [], navigated: [] }
  const values = new Map([['Access-Token', 'synthetic-account-a'], ['User-Info', { id: 1 }],
    ['User-Roles', ['synthetic']], ['PHPSESSID', 'synthetic-session']])
  const storage = { get: key => values.get(key), set: (key, value) => values.set(key, value),
    remove: key => { events.removed.push(key); values.delete(key) } }
  const browser = { location: { hostname: 'example.invalid', hash: '#/personal-reports',
    assign: url => events.navigated.push(url) } }
  globalThis[registry] = { axios: axiosLibrary, storage, notification: { error: item => events.notifications.push(item) },
    VueAxios: {}, i18n: { t: key => key }, window: browser, document: { cookie: '' } }
  let code = readFileSync(new URL('../../src/utils/request.js', import.meta.url), 'utf8')
    .replace("import axios from 'axios'", `const { axios } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import storage from 'store'", `const { storage } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import notification from 'ant-design-vue/es/notification'", `const { notification } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import { VueAxios } from './axios'", `const { VueAxios } = globalThis[${JSON.stringify(registry)}]`)
    .replace("from '@/store/mutation-types'", 'from ' + JSON.stringify(new URL('../../src/store/mutation-types.js', import.meta.url).href))
    .replace("import i18n from '@/locales'", `const { i18n } = globalThis[${JSON.stringify(registry)}]`)
    .replace("from './unifiedResearchRead'", 'from ' + JSON.stringify(new URL('../../src/utils/unifiedResearchRead.js', import.meta.url).href))
  code = `const { window, document } = globalThis[${JSON.stringify(registry)}]\n` + code
  const requestUrl = 'data:text/javascript;charset=utf-8,' + encodeURIComponent(code)
  let request
  try { request = (await import(requestUrl)).default } finally { delete globalThis[registry] }
  const api = await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(
    API.replace("from '@/utils/request'", 'from ' + JSON.stringify(requestUrl))))
  const pending = []
  request.defaults.adapter = config => new Promise((resolve, reject) => {
    pending.push({ config,
      succeed: overrides => resolve({ data: response(overrides), status: 200, statusText: 'OK', headers: {}, config }),
      fail: status => reject(new axiosLibrary.AxiosError('synthetic failure', 'ERR_BAD_REQUEST', config, null,
        { status, data: { msg: 'synthetic private diagnostic' }, headers: {}, config })) })
  })
  const drain = () => new Promise(resolve => setImmediate(resolve))
  return { api, request, pending, values, events, drain }
}

let strictProjection
function actualStrictProjection () {
  if (strictProjection) return strictProjection
  const root = fileURLToPath(new URL('../../../../', import.meta.url))
  const program = [
    'import importlib,importlib.util,json,sys,types,hashlib',
    'from pathlib import Path',
    'from datetime import datetime',
    'root=Path(sys.argv[1])',
    'spec=importlib.util.spec_from_file_location("strict_frontend_normalizer",root/"candidate/integration/dashboard/nng8_readonly.py")',
    'normalizer=importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(normalizer)',
    'for name,path in (("app",root/"candidate/backend/app"),("app.services",root/"candidate/backend/app/services")):',
    '    package=types.ModuleType(name)',
    '    package.__path__=[str(path)]',
    '    sys.modules[name]=package',
    'projector=importlib.import_module("app.services.nng8_context")',
    'source=root/"fixtures/strict-source-bundle.json"',
    'bundle=normalizer.read_bundle(source)',
    'capture_clock=datetime.fromisoformat(bundle["captured_at"])',
    'dto,status=normalizer.normalize_bundle(bundle,capture_clock)',
    'projected=projector.project_dto(dto)',
    'report=bundle["documents"]["today"]["payload"]["report"]',
    'text_hashes=[hashlib.sha256(block["text"].encode("utf-8")).hexdigest() for block in report["blocks"]]',
    'result={"dto":projected,"status":status,"source_text_sha256s":text_hashes,"source_block_count":len(report["blocks"]),"bundle_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}',
    'print(json.dumps(result,ensure_ascii=False,allow_nan=False))'
  ].join('\n')
  // Capture payload is consumed internally, never emitted in test logs.
  strictProjection = JSON.parse(execFileSync('python3', ['-B', '-c', program, root], { encoding: 'utf8' }))
  return strictProjection
}

test('feature flag defaults off and accepts only explicit true/1', () => {
  assert.equal(NNG8_READONLY_ENABLED, false)
  for (const value of [undefined, null, false, '', 'false', '0', 1, 'TRUE', {}]) assert.equal(nng8Enabled(value), false)
  for (const value of [true, 'true', '1']) assert.equal(nng8Enabled(value), true)
})
test('source contract rejects foreign top source and unknown contract', () => {
  assert.equal(normalizeNng8(dto({ source: 'native' })), null)
  assert.equal(normalizeNng8(dto({ contract_version: 'unknown' })), null)
  assert.equal(normalizeNng8(dto({ reports: {} })), null)
})
test('report source allowlist rejects foreign content and strips unknown fields', () => {
  const view = normalizeNng8(dto({ reports: [{ ...report(), source: 'profit/native', blocks: [{ text: 'PRIVATE' }] }],
    server_captured: true, user_id: 99, result: { private: true } }))
  assert.equal(view.reports.length, 0)
  assert.equal(view.issues[0].code, 'report_source_rejected')
  for (const key of ['server_captured', 'user_id', 'result']) assert.equal(key in view, false)
})
test('128 body blocks remain intact; over-limit body and text truncation are explicit, not silently complete', () => {
  const blocks = Array.from({ length: 128 }, (_, index) => ({ heading: '', text: '合成段落' + index }))
  const keep = normalizeNng8(dto({ reports: [{ ...report(), blocks, body_block_count: 128 }] }))
  assert.equal(keep.reports[0].blocks.length, 128)
  assert.equal(keep.reports[0].body_block_count, 128)
  assert.equal(keep.reports[0].body_truncated, false)
  assert.ok(!keep.issues.some(row => row.code === 'content_truncated'))
  const unicode = normalizeNng8(dto({ reports: [{ ...report(), blocks: [{ heading: '🧪'.repeat(160), text: '🧪'.repeat(2000) }] }] })).reports[0]
  assert.equal(Array.from(unicode.blocks[0].text).length, 2000)
  assert.equal(Array.from(unicode.blocks[0].heading).length, 160)
  assert.equal(unicode.body_truncated, false)
  for (const changed of [
    { blocks: [...blocks, { text: '第129段' }], body_block_count: 129 },
    { blocks: [{ heading: '', text: '长'.repeat(2001) }], body_block_count: 1 },
    { blocks: [{ heading: '题'.repeat(161), text: '短正文' }], body_block_count: 1 },
    { blocks: [{ heading: '不支持的嵌套正文', text: { html: '<iframe>' } }], body_block_count: 1 }
  ]) {
    const view = normalizeNng8(dto({ reports: [{ ...report(), ...changed }] }))
    const row = view.reports[0]
    assert.ok(row.blocks.length <= 128)
    assert.ok(row.blocks.every(block => block.text.length <= 2000 && (!block.heading || block.heading.length <= 160)))
    assert.equal(row.body_block_count, changed.body_block_count)
    assert.equal(row.body_truncated, true)
    assert.ok(view.issues.some(issue => issue.source === 'today' && issue.code === 'content_truncated'))
    assert.equal(nng8PageState({ enabled: true, snapshot: view }), 'partial')
  }
})
test('body original-count and upstream truncation metadata remain independent of shown count', () => {
  const row = { ...report(), body_block_count: 180, body_truncated: true }
  const view = normalizeNng8(dto({ reports: [row], issues: [{ source: 'today', code: 'content_truncated', message: '上游已标记截断' }] }))
  assert.equal(view.reports[0].body_block_count, 180)
  assert.equal(view.reports[0].blocks.length, 1)
  assert.equal(view.reports[0].body_truncated, true)
  assert.equal(view.issues.filter(issue => issue.code === 'content_truncated').length, 1)
  delete row.body_block_count; delete row.body_truncated
  const unknown = normalizeNng8(dto({ reports: [row] })).reports[0]
  assert.equal(unknown.body_block_count, null)
  assert.equal(unknown.body_truncated, null)
  for (const invalid of [true, -1, '107']) {
    assert.equal(normalizeNng8(dto({ reports: [{ ...report(), body_block_count: invalid }] })).reports[0].body_block_count, null)
  }
})
test('legacy strict capture retains all 107 paragraphs but lacks the v2 complete-content contract', () => {
  const actual = actualStrictProjection()
  const expectedHash = '64a1a9cdf781bc57e66906d38547bdf886270573199f26d3ccb93aefb25f13dd'
  assert.equal(actual.bundle_sha256, expectedHash)
  assert.equal(actual.status, 200)
  const view = normalizeNng8(actual.dto)
  const row = view.reports[0]
  assert.equal(actual.source_block_count, 107)
  assert.equal(row.blocks.length, 107)
  assert.equal(row.body_block_count, 107)
  assert.equal(row.body_truncated, true)
  assert.equal(row.body_complete, false)
  assert.ok(row.truncation_reasons.includes('content_contract_unverified'))
  const shownHashes = row.blocks.map(block => createHash('sha256').update(block.text).digest('hex'))
  // Compare fingerprints, never report bodies, if a future regression fails.
  assert.deepEqual(shownHashes, actual.source_text_sha256s)
  assert.equal(row.generated_at, null); assert.equal(row.data_cutoff, null)
  assert.ok(row.source_time); assert.ok(row.evidence_cutoff)
  const sourceFile = new URL('../../../../fixtures/strict-source-bundle.json', import.meta.url)
  assert.equal(createHash('sha256').update(readFileSync(sourceFile)).digest('hex'), expectedHash)
})
test('strict source quality and frozen calibration keep exact scopes and distinct semantic statuses', () => {
  const view = normalizeNng8(actualStrictProjection().dto)
  assert.equal(view.data_quality.counts_scope, 'sealed_core_qlib_summary')
  assert.deepEqual([view.data_quality.eligible, view.data_quality.valid, view.data_quality.invalid], [5575, 5562, 13])
  assert.ok(view.data_quality.limitations.some(line => /不是完整全市场/.test(line)))
  const ledger = view.calibration.find(row => row.id === 'ledger')
  const phase2 = view.calibration.find(row => row.id === 'phase2')
  assert.equal(ledger.status, 'insufficient'); assert.equal(nng8Status(ledger.status).label, '样本不足')
  assert.equal(phase2.status, 'unknown'); assert.equal(nng8Status(phase2.status).label, '未知')
  assert.equal(ledger.runtime_mode, 'FROZEN'); assert.equal(phase2.runtime_mode, 'FROZEN')
  assert.equal(phase2.observed, null)
  assert.equal(nng8PageState({ enabled: true, snapshot: view }), 'partial')
})
test('only explicitly verified quality scopes retain counts; unknown or absent scope never expands to full market', () => {
  for (const counts_scope of ['csi_members', 'full_market', 'sealed_core_qlib_summary']) {
    const view = normalizeNng8(dto({ data_quality: { ...dto().data_quality, counts_scope } }))
    assert.equal(view.data_quality.counts_scope, counts_scope)
    assert.equal(view.data_quality.eligible, 3)
  }
  for (const counts_scope of [null, undefined, 'all-market', 'SEALED_CORE_QLIB_SUMMARY', {}, true]) {
    const view = normalizeNng8(dto({ data_quality: { ...dto().data_quality, counts_scope } }))
    assert.equal(view.data_quality.counts_scope, null)
    assert.deepEqual([view.data_quality.eligible, view.data_quality.valid, view.data_quality.invalid], [null, null, null])
    assert.ok(view.issues.some(issue => issue.code === 'quality_counts_unknown'))
  }
})
test('timestamps are separate; absent generated/cutoff do not borrow observed/source/capture', () => {
  const view = normalizeNng8(dto())
  assert.equal(view.reports[0].generated_at, null)
  assert.equal(view.reports[0].data_cutoff, null)
  assert.equal(view.reports[0].source_time, stamp)
  assert.equal(view.reports[0].evidence_cutoff, '证据观察截止')
  assert.equal(nng8Time('2026-10-09T11:00:00'), null)
  assert.equal(nng8Time('not-time'), null)
})
test('quality requires a coherent same-scope triple, rejects bool and does not fill zero', () => {
  for (const quality of [{ eligible: 10, valid: 2, invalid: 1 }, { eligible: true, valid: 0, invalid: 1 }, {}]) {
    const view = normalizeNng8(dto({ data_quality: quality }))
    assert.equal(view.data_quality.eligible, null)
    assert.equal(view.data_quality.valid, null)
    assert.equal(view.data_quality.invalid, null)
  }
  const zero = normalizeNng8(dto({ data_quality: { eligible: 0, valid: 0, invalid: 0, counts_scope: 'full_market' } }))
  assert.equal(zero.data_quality.eligible, 0)
  assert.equal(nng8Value(0), 0)
  assert.equal(nng8Value(null), '未知')
})
test('Ledger/Phase2 counts stay separate, observed is not manufactured from pending', () => {
  const view = normalizeNng8(dto({ calibration: [...dto().calibration, { id: 'phase2', pending_review: 180, formal: 0 }] }))
  assert.equal(view.calibration[0].observed, 180)
  assert.equal(view.calibration[0].formal, 0)
  assert.equal(view.calibration[1].observed, null)
  assert.equal(view.calibration[1].runtime_mode, null)
  assert.equal('total' in view, false)
})
test('quality retains scope/contract/date provenance and explicit conflicts erase the full triple', () => {
  const quality = { status: 'ok', eligible: 3, valid: 2, invalid: 1, active_contract: 'full-market-v1', market_date: '2026-10-09',
    counts_scope: 'full_market', counts_contract: 'full-market-v1', counts_target_date: '2026-10-09' }
  const accepted = normalizeNng8(dto({ data_quality: quality })).data_quality
  assert.equal(accepted.counts_scope, 'full_market')
  assert.equal(accepted.counts_contract, 'full-market-v1')
  assert.equal(accepted.counts_target_date, '2026-10-09')
  assert.equal(accepted.eligible, 3)
  for (const conflict of [{ counts_contract: 'csi-members-v1' }, { counts_target_date: '2026-10-08' }]) {
    const view = normalizeNng8(dto({ data_quality: { ...quality, ...conflict } }))
    assert.equal(view.data_quality.eligible, null)
    assert.equal(view.data_quality.valid, null)
    assert.equal(view.data_quality.invalid, null)
    assert.ok(view.issues.some(issue => issue.code === 'quality_counts_unknown'))
  }
})
test('stale and partial routes cannot become available even if raw aggregate says ok', () => {
  const state = snapshot => nng8PageState({ enabled: true, snapshot })
  assert.equal(state(normalizeNng8(dto({ snapshot_stale: true }))), 'partial')
  assert.equal(state(normalizeNng8(dto({ data_quality: { status: 'unavailable' } }))), 'partial')
  assert.equal(state(normalizeNng8(dto({ reports: [{ ...report(), status: 'stale' }] }))), 'partial')
  assert.equal(state(normalizeNng8(dto())), 'partial', 'unknown service evidence cannot become fully readable')
})
test('actual normalizer preserves legacy partial content admission independently of quality pass', () => {
  const root = fileURLToPath(new URL('../../../../', import.meta.url))
  const program = [
    'import importlib.util,json,sys',
    'from pathlib import Path',
    'from datetime import datetime,timezone',
    'root=Path(sys.argv[1])',
    'sys.path.insert(0,str(root/"tests"))',
    'from nng8_fixtures import make_all_pass_bundle,rehash',
    'spec=importlib.util.spec_from_file_location("actual_nng8_normalizer",root/"candidate/integration/dashboard/nng8_readonly.py")',
    'normalizer=importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(normalizer)',
    'now=datetime.now(timezone.utc)',
    'rows={}',
    'for mode in ("all-pass","unknown","stale","missing"):',
    '    bundle=make_all_pass_bundle(now)',
    '    if mode=="unknown":',
    '        bundle["documents"]["today"]["payload"]["report"]["dataStatus"]="unknown"',
    '        rehash(bundle,"today")',
    '    elif mode=="stale":',
    '        bundle["documents"]["today"]["payload"]["stale"]=True',
    '        rehash(bundle,"today")',
    '    elif mode=="missing":',
    '        del bundle["documents"]["progress"]',
    '    rows[mode]=normalizer.normalize_bundle(bundle,now)',
    'print(json.dumps(rows,ensure_ascii=False,allow_nan=False))'
  ].join('\n')
  const actual = JSON.parse(execFileSync('python3', ['-B', '-c', program, root], { encoding: 'utf8' }))
  const [passed, status] = actual['all-pass']
  assert.equal(status, 200)
  assert.equal(passed.status, 'partial')
  assert.equal(passed.data_quality.status, 'pass')
  assert.ok(passed.reports.every(row => row.status === 'partial' && row.body_complete === false && row.truncation_reasons.includes('content_contract_unverified')))
  assert.ok(passed.calibration.every(row => row.status === 'pass'))
  const withoutSync = normalizeNng8(passed)
  assert.equal(withoutSync.sync.status, 'unknown')
  assert.equal(nng8PageState({ enabled: true, snapshot: withoutSync }), 'partial', 'business pass without verified sync cannot make the whole page green')
  for (const mode of ['unknown', 'stale', 'missing']) {
    const [payload] = actual[mode]
    assert.notEqual(payload.status, 'ok', mode)
    assert.equal(nng8PageState({ enabled: true, snapshot: normalizeNng8(payload) }), 'partial', mode)
  }
})
test('loading/failure/empty/partial/denial are independent states', () => {
  const empty = normalizeNng8(dto({ reports: [], delivery: [], services: [], calibration: [], data_quality: null }))
  assert.equal(nng8PageState({ enabled: true, snapshot: empty }), 'empty')
  assert.equal(nng8PageState({ enabled: true, loading: true }), 'loading')
  assert.equal(nng8PageState({ enabled: true, error: 'failed' }), 'unavailable')
  assert.equal(nng8PageState({ enabled: true, permission: 'denied', loading: true }), 'denied')
  assert.equal(nng8PageState({ enabled: false, snapshot: normalizeNng8(dto()) }), 'disabled')
})
test('only a known getter profile id binds a request; no fallback user 1', () => {
  assert.equal(nng8AccountId({ id: 12 }), '12')
  for (const value of [{}, { id: null }, { id: true }, { id: 0 }, { id: -1 }]) assert.equal(nng8AccountId(value), '')
})
test('disabled component hooks/load produce zero requests and no data', async () => {
  let calls = 0
  const { vm } = await instance(() => { calls++; return Promise.resolve(response()) }, { enabled: false })
  vm.created(); vm.activated(); await vm.load(); await tick()
  assert.equal(calls, 0); assert.equal(vm.snapshot, null)
})
test('missing login identity produces zero requests', async () => {
  let calls = 0
  const { vm } = await instance(() => { calls++ }, { id: null })
  await vm.load()
  assert.equal(calls, 0); assert.equal(vm.permission, 'login')
})
test('real component success uses DTO observed time, not browser clock', async () => {
  const { vm } = await instance(async () => response())
  await vm.load()
  assert.equal(vm.snapshot.source, 'nng8'); assert.equal(vm.lastSuccessAt, stamp)
  assert.equal(vm.cached, false); assert.equal(vm.loading, false)
})
test('real component method retains approved source DTO with every body block and completeness metadata', async () => {
  const { vm } = await instance(async () => ({ code: 1, msg: 'success', data: actualStrictProjection().dto }))
  await vm.load()
  assert.equal(vm.snapshot.reports[0].blocks.length, 107)
  assert.equal(vm.snapshot.reports[0].body_block_count, 107)
  assert.equal(vm.snapshot.reports[0].body_truncated, true)
  assert.equal(vm.snapshot.reports[0].body_complete, false)
  assert.equal(vm.pageState, 'partial'); assert.equal(vm.cached, false)
  assert.match(source, /report\.body_truncated === true[\s\S]*data-testid="nng8-body-truncated"/)
  assert.match(source, /正文显示 \{\{ report\.blocks\.length \}\} 段 \/ 来源原始 \{\{ value\(report\.body_block_count\) \}\}/)
  assert.match(source, /sealed_core_qlib_summary[\s\S]*不是完整全市场价格档案/)
})
test('real component newest response wins request races', async () => {
  const first = deferred(), second = deferred()
  let calls = 0
  const { vm } = await instance(() => (++calls === 1 ? first : second).promise)
  const a = vm.load(), b = vm.load()
  second.resolve(response({ observed_at: '2026-10-09T11:02:00+08:00' })); await b
  first.resolve(response()); await a
  assert.equal(vm.lastSuccessAt, '2026-10-09T11:02:00+08:00')
})
test('superseded denial cannot erase a newer successful response', async () => {
  const first = deferred(), second = deferred()
  let calls = 0
  const { vm } = await instance(() => (++calls === 1 ? first : second).promise)
  const a = vm.load(), b = vm.load()
  second.resolve(response()); await b; first.reject(httpError(403)); await a
  assert.equal(vm.permission, ''); assert.ok(vm.snapshot)
})
test('actual Axios chain: account A late 401 cannot erase account B session or successful body', async () => {
  const transport = await actualTransport()
  const { vm, options, store } = await instance(transport.api.getNng8Readonly)
  const a = vm.load(); await transport.drain()
  assert.equal(transport.pending.length, 1)
  assert.equal(transport.pending[0].config.nng8CallerHandlesAuth, true)
  store.getters.userInfo = { id: 2 }
  transport.values.set('Access-Token', 'synthetic-account-b')
  transport.values.set('User-Info', { id: 2 })
  options.watch.accountId.call(vm); await transport.drain()
  assert.equal(transport.pending.length, 2)
  transport.pending[1].succeed({ observed_at: '2026-10-09T11:02:00+08:00' }); await transport.drain()
  assert.equal(vm.snapshotAccount, '2'); assert.ok(vm.snapshot)
  transport.pending[0].fail(401); await a; await transport.drain()
  assert.equal(vm.snapshotAccount, '2'); assert.ok(vm.snapshot)
  assert.equal(vm.lastSuccessAt, '2026-10-09T11:02:00+08:00'); assert.equal(vm.permission, '')
  assert.equal(transport.values.get('Access-Token'), 'synthetic-account-b')
  assert.deepEqual(transport.values.get('User-Info'), { id: 2 })
  assert.deepEqual(transport.events.removed, []); assert.deepEqual(transport.events.navigated, [])
  assert.deepEqual(transport.events.notifications, [])
})
test('actual Axios chain: current nnG8 401 clears body and prompts login without mutating global session', async () => {
  const transport = await actualTransport()
  const { vm } = await instance(transport.api.getNng8Readonly)
  const first = vm.load(); await transport.drain(); transport.pending[0].succeed(); await first
  assert.ok(vm.snapshot)
  const refresh = vm.load(); await transport.drain(); transport.pending[1].fail(401); await refresh
  assert.equal(vm.payload, null); assert.equal(vm.snapshot, null); assert.equal(vm.lastSuccessAt, null)
  assert.equal(vm.permission, 'login'); assert.match(vm.stateText, /重新登录/)
  assert.equal(vm.cached, false); assert.equal(vm.loading, false)
  assert.deepEqual(transport.events.removed, []); assert.deepEqual(transport.events.navigated, [])
})
test('actual Axios chain: native overview 401 retains existing global auth cleanup/redirect behavior', async () => {
  const transport = await actualTransport()
  const failed = transport.api.getWorkbenchOverview()
  const rejection = assert.rejects(failed, error => error.response.status === 401)
  await transport.drain(); transport.pending[0].fail(401); await rejection
  assert.equal(transport.pending[0].config.nng8CallerHandlesAuth, undefined)
  assert.deepEqual(transport.events.removed, ['Access-Token', 'User-Info', 'User-Roles', 'PHPSESSID'])
  assert.equal(transport.events.navigated.length, 1)
  assert.match(transport.events.navigated[0], /^\/#\/user\/login\?redirect=/)
})
test('actual Axios chain: auth exception cannot apply to a different endpoint or non-GET request', async () => {
  for (const config of [{ url: '/api/radar/workbench/overview', method: 'get' },
    { url: '/api/radar/workbench/nng8', method: 'post' },
    { url: '/api/radar/workbench/nng8', method: 'get' }]) {
    const transport = await actualTransport()
    const failed = transport.request({ ...config, nng8CallerHandlesAuth: config.method === 'get' && config.url.endsWith('/nng8') ? false : true })
    const rejection = assert.rejects(failed, error => error.response.status === 401)
    await transport.drain(); transport.pending[0].fail(401); await rejection
    assert.equal(transport.events.removed.length, 4)
    assert.equal(transport.events.navigated.length, 1)
  }
})
test('network/503 failure retains a clearly old snapshot and does not refresh timestamps', async () => {
  let fail = false
  const { vm } = await instance(async () => { if (fail) throw httpError(503); return response() })
  await vm.load(); fail = true; await vm.load()
  assert.equal(vm.cached, true); assert.equal(vm.lastSuccessAt, stamp); assert.equal(vm.snapshot.observed_at, stamp)
  assert.equal(vm.pageState, 'unavailable'); assert.match(vm.error, /本次有效证据/)
})
for (const status of [401, 403, 404]) {
  test('HTTP ' + status + ' clears cached body and identity immediately', async () => {
    let failure = false
    const { vm } = await instance(async () => { if (failure) throw httpError(status); return response() })
    await vm.load(); failure = true; await vm.load()
    assert.equal(vm.snapshot, null); assert.equal(vm.payload, null); assert.equal(vm.lastSuccessAt, null)
    assert.equal(vm.permission, status === 401 ? 'login' : 'denied'); assert.equal(vm.cached, false)
  })
}
test('account change prevents late response even before watcher runs', async () => {
  const request = deferred()
  const { vm, store } = await instance(() => request.promise)
  const loading = vm.load(); store.getters.userInfo = { id: 2 }
  request.resolve(response()); await loading
  assert.equal(vm.snapshot, null); assert.equal(vm.payload, null)
})
test('account watcher clears old content synchronously and refreshes under new account', async () => {
  const next = deferred()
  let calls = 0
  const { vm, options, store } = await instance(() => (++calls === 1 ? Promise.resolve(response()) : next.promise))
  await vm.load(); store.getters.userInfo = { id: 2 }; options.watch.accountId.call(vm)
  assert.equal(vm.payload, null); await tick()
  next.resolve(response()); await tick()
  assert.equal(vm.snapshotAccount, '2')
})
test('logout/session mutation clears content without reading token and late request cannot revive', async () => {
  const late = deferred()
  const { vm, store, events } = await instance(() => late.promise)
  vm.created(); events.subscriptions[0]({ type: 'SET_TOKEN' }); store.getters.userInfo = {}
  late.resolve(response()); await tick()
  assert.equal(vm.payload, null); assert.equal(vm.snapshot, null)
})
test('feature switch off invalidates in-flight request and clears old content', async () => {
  const late = deferred()
  const { vm, options } = await instance(() => late.promise)
  const loading = vm.load(); vm.enabled = false; options.watch.enabled.call(vm, false)
  late.resolve(response()); await loading
  assert.equal(vm.payload, null); assert.equal(vm.pageState, 'disabled')
})
test('keep-alive deactivation invalidates in-flight response', async () => {
  const late = deferred()
  const { vm } = await instance(() => late.promise)
  const loading = vm.load(); vm.deactivated(); late.resolve(response()); await loading
  assert.equal(vm.payload, null); assert.equal(vm.loading, false)
})
test('keep-alive first activation does not duplicate created request', async () => {
  const late = deferred(); let calls = 0
  const { vm } = await instance(() => { calls++; return late.promise })
  vm.created(); vm.activated(); assert.equal(calls, 1)
  late.resolve(response()); await tick()
})
test('destroy unsubscribes session listener and rejects late body', async () => {
  const late = deferred()
  const { vm, events } = await instance(() => late.promise)
  vm.created(); vm.beforeDestroy(); late.resolve(response()); await tick()
  assert.equal(events.unsubscribed, 1); assert.equal(vm.payload, null)
})
test('both dark/realdark modes use component dark tokens', async () => {
  for (const theme of ['dark', 'realdark', 'light']) {
    const { vm } = await instance(async () => response(), { theme })
    assert.equal(vm.dark, theme !== 'light')
  }
})
test('section route focuses only active enabled component', async () => {
  const { vm, events } = await instance(async () => response())
  vm.focusSection(); await tick(); assert.equal(events.focused, 1)
  vm.active = false; vm.focusSection(); await tick(); assert.equal(events.focused, 1)
  assert.deepEqual(NNG8_SECTION_ROUTE, { path: '/personal-reports', query: { section: 'nng8' } })
})
test('plain-text body, exact GET endpoint, internal landing and default-off gating structures', () => {
  assert.doesNotMatch(source, /v-html|<iframe|v-for=.*href=|Date\.now\(|getters\.token/)
  assert.match(source, /\{\{ block\.text|value\(block\.text\)/)
  assert.match(source, /snapshot\.snapshot_stale/)
  assert.match(source, /上次成功读取/)
  assert.match(API, /url: '\/api\/radar\/workbench\/nng8',[\s\S]*method: 'get'/)
  assert.match(nativeReports, /getNng8Readonly/)
  assert.match(nativeReports, /unifiedReportRows/)
  assert.doesNotMatch(nativeReports, /<nng8-readonly|<controlled-research-evidence/)
  assert.match(home, /v-if="nng8Enabled"[^>]*:to="nng8SectionRoute"/)
  assert.match(source, /repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(source, /--ng-ink: #e3ede7/)
})
