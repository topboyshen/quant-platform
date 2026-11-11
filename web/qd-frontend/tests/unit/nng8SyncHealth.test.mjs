// Synthetic operation clocks/DTOs only. No network, production or browser.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { normalizeNng8, normalizeNng8Sync, nng8SyncPresentation, nng8PageState } from '../../src/utils/nng8Readonly.js'

const source = readFileSync(new URL('../../src/components/Nng8Readonly/index.vue', import.meta.url), 'utf8')
const observed = '2026-10-09T08:00:00+00:00'
const capture = '2026-10-09T07:50:00+00:00'
const start = '2026-10-09T07:59:50+00:00'
const finish = '2026-10-09T07:59:55+00:00'
const hash = 'a'.repeat(64)
const sync = (overrides = {}) => ({ status: 'success', reason_code: null, started_at: start,
  finished_at: finish, last_success_at: finish, consecutive_failures: 0,
  bundle_sha256: 'b'.repeat(64), writer_version: 'nng8.sync-writer.v1', ...overrides })
const report = () => ({ id: 'nng8:astock:2026-10-09:' + hash, source: 'nng8/astock',
  title: '明确标注的合成单元夹具', report_date: '2026-10-09', status: 'pass', generated_at: null, data_cutoff: null,
  source_time: capture, evidence_cutoff: '合成证据截止', publication_gate: 'passed', review_status: 'not_reviewed',
  conclusion: [], risks: [], blocks: [{ heading: '', text: '合成旧副本正文' }], body_block_count: 1,
  body_truncated: false, content_sha256: hash })
const dto = (overrides = {}) => ({ contract_version: 'workbench.nng8.v1', source: 'nng8',
  captured_at: capture, observed_at: observed, snapshot_stale: false, status: 'ok', sync: sync(),
  reports: [report()], data_quality: { status: 'pass', eligible: 1, valid: 1, invalid: 0, counts_scope: 'csi_members' },
  delivery: [{ id: 'seal', status: 'pass', source_time: capture }],
  services: [{ id: 'synthetic.service', status: 'pass' }],
  calibration: [{ id: 'ledger', status: 'pass', runtime_mode: 'FROZEN', nominal: 1, observed: 1, pending_review: 0, formal: 1 }],
  issues: [], limitations: ['合成测试，不代表任何真实业务成功'], ...overrides })
const response = overrides => ({ code: 1, msg: 'success', data: dto(overrides) })
const running = () => sync({ status: 'running', finished_at: null, last_success_at: capture })
const failed = () => sync({ status: 'failed', reason_code: 'transport_failed', consecutive_failures: 2, last_success_at: capture })
const unknown = reason_code => sync({ status: 'unknown', reason_code, started_at: null, finished_at: null,
  last_success_at: null, consecutive_failures: null, bundle_sha256: null, writer_version: null })
function deferred () {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const httpError = status => ({ response: { status, data: { msg: '/PRIVATE/PATH?token=NOT-FOR-DISPLAY' } } })
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
let moduleId = 0

async function instance (api, { enabled = true, id = 1, theme = 'realdark' } = {}) {
  const registry = '__nng8_sync_test_' + (++moduleId)
  globalThis[registry] = { getNng8Readonly: api }
  const utility = new URL('../../src/utils/nng8Readonly.js', import.meta.url).href
  const code = source.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace("import Nng8ResearchLink from '@/components/Nng8ResearchLink'", 'const Nng8ResearchLink = {}')
    .replace("import { getNng8Readonly } from '@/api/workbench'", 'const { getNng8Readonly } = globalThis[' + JSON.stringify(registry) + ']')
    .replace("from '@/utils/nng8Readonly'", 'from ' + JSON.stringify(utility))
  let options
  try { options = (await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(code))).default } finally { delete globalThis[registry] }
  const subscriptions = []
  const store = { state: { app: { theme } }, getters: { userInfo: { id } },
    subscribe (listener) { subscriptions.push(listener); return () => {} } }
  Object.defineProperty(store.getters, 'token', { get () { throw new Error('token access forbidden') } })
  const vm = { enabled, $store: store, $route: { query: {} }, $refs: {}, $nextTick: fn => Promise.resolve().then(fn) }
  Object.assign(vm, options.data.call(vm))
  for (const [key, method] of Object.entries(options.methods)) vm[key] = method.bind(vm)
  for (const [key, getter] of Object.entries(options.computed)) Object.defineProperty(vm, key, { get: getter.bind(vm) })
  for (const hook of ['created', 'activated', 'deactivated', 'beforeDestroy']) vm[hook] = options[hook].bind(vm)
  return { vm, options, store, subscriptions }
}

test('four sync states normalize independently and only success can accompany fully readable evidence', () => {
  for (const row of [sync(), running(), failed(), unknown('missing')]) {
    const view = normalizeNng8(dto({ sync: row }))
    assert.equal(view.sync.status, row.status)
    assert.equal(view.status, 'ok', 'sync does not rewrite business status')
    assert.equal(view.reports[0].blocks[0].text, '合成旧副本正文')
    assert.equal(nng8PageState({ enabled: true, snapshot: view }), row.status === 'success' ? 'available' : 'partial')
  }
})
test('sync success is neutral and explicitly not business success/freshness', () => {
  const display = nng8SyncPresentation(normalizeNng8Sync(sync(), observed))
  assert.equal(display.message, '同步完成（不代表业务成功）')
  assert.equal(display.tone, 'neutral')
  for (const overrides of [{ snapshot_stale: true }, { status: 'partial' },
    { reports: [{ ...report(), status: 'unknown' }] }, { calibration: [{ id: 'phase2', status: 'insufficient' }] }]) {
    assert.equal(nng8PageState({ enabled: true, snapshot: normalizeNng8(dto(overrides)) }), 'partial')
  }
})
test('sync operation timestamps and bundle hash never fill business/capture/report fields', () => {
  const view = normalizeNng8(dto())
  assert.equal(view.captured_at, capture); assert.equal(view.observed_at, observed)
  assert.equal(view.sync.started_at, start); assert.equal(view.sync.finished_at, finish); assert.equal(view.sync.last_success_at, finish)
  assert.equal(view.reports[0].generated_at, null); assert.equal(view.reports[0].data_cutoff, null)
  assert.equal(view.reports[0].source_time, capture)
  assert.notEqual(view.sync.bundle_sha256, view.reports[0].content_sha256)
})
test('legacy backend missing sync stays readable with unknown/not_configured, never all-green', () => {
  const old = dto(); delete old.sync
  const view = normalizeNng8(old)
  assert.equal(view.sync.status, 'unknown'); assert.equal(view.sync.reason_code, 'not_configured')
  assert.equal(view.reports.length, 1); assert.equal(nng8PageState({ enabled: true, snapshot: view }), 'partial')
})
test('missing sync fields, bad shapes, unknown status/version/fields downgrade without body loss or error leakage', () => {
  const partial = sync(); delete partial.finished_at
  for (const value of [null, [], true, 'success', {}, partial, sync({ status: 'passed' }),
    sync({ writer_version: 'unexpected-writer' }), sync({ raw_error: '/PRIVATE/PATH?token=SECRET' }),
    sync({ reason_code: '<script>SECRET</script>' })]) {
    const view = normalizeNng8(dto({ sync: value }))
    assert.equal(view.sync.status, 'unknown'); assert.equal(view.sync.reason_code, 'invalid')
    assert.equal(view.reports[0].blocks.length, 1)
    assert.equal(nng8PageState({ enabled: true, snapshot: view }), 'partial')
    assert.doesNotMatch(JSON.stringify(view.sync), /PRIVATE|SECRET|script|raw_error/)
  }
})
test('all fixed failure reasons remain plain bounded Chinese, arbitrary upstream errors never display', () => {
  const codes = ['transport_failed', 'timeout', 'size_limit', 'invalid_bundle', 'replay', 'capture_conflict',
    'source_time_regressed', 'publication_failed', 'receipt_failed', 'interrupted', 'unknown']
  for (const reason_code of codes) {
    const display = nng8SyncPresentation(normalizeNng8Sync({ ...failed(), reason_code }, observed))
    assert.equal(display.message, '本次同步失败，显示上次副本'); assert.ok(display.reason)
    assert.doesNotMatch(display.reason, /PRIVATE|token=|https?:/)
  }
  assert.match(nng8SyncPresentation({ status: 'unknown', reason_code: 'UNSAFE' }).reason, /尚未取得/)
})
test('unknown reasons retain explicit inability to confirm and no zero-filled metrics', () => {
  for (const reason of ['missing', 'invalid', 'generation_mismatch', 'health_stale', 'attempt_overdue', 'not_configured', 'unknown']) {
    const view = normalizeNng8Sync(unknown(reason), observed)
    assert.equal(view.status, 'unknown'); assert.equal(view.reason_code, reason)
    assert.equal(view.consecutive_failures, null); assert.equal(view.bundle_sha256, null)
    assert.equal(nng8SyncPresentation(view).message, '同步状态未知，不能确认本次同步成功')
  }
})
test('invalid counters/hash/time ordering cannot produce sync success', () => {
  for (const overrides of [{ consecutive_failures: true }, { consecutive_failures: -1 }, { consecutive_failures: 1000000001 },
    { consecutive_failures: null }, { consecutive_failures: 1 }, { bundle_sha256: null }, { bundle_sha256: 'B'.repeat(64) },
    { started_at: null }, { finished_at: null }, { last_success_at: null },
    { started_at: '2026-10-09T08:00:00' }, { started_at: 'not-time' },
    { started_at: observed }, { last_success_at: observed }, { last_success_at: capture }]) {
    const view = normalizeNng8Sync(sync(overrides), observed)
    assert.equal(view.status, 'unknown'); assert.equal(view.reason_code, 'invalid')
  }
})
test('future >5s, running >60s and old >10min use server observation, not current browser time', () => {
  const future = '2026-10-09T08:00:05.001+00:00'
  assert.equal(normalizeNng8Sync(sync({ finished_at: future, last_success_at: future }), observed).status, 'unknown')
  const microFuture = '2026-10-09T08:00:05.000001+00:00'
  assert.equal(normalizeNng8Sync(sync({ finished_at: microFuture, last_success_at: microFuture }), observed).status, 'unknown')
  assert.equal(normalizeNng8Sync(sync({ finished_at: '2026-10-09T07:59:55.123456Z', last_success_at: '2026-10-09T07:59:55.123455Z' }), observed).reason_code, 'invalid')
  const atBoundary = running(); atBoundary.started_at = '2026-10-09T07:59:00+00:00'
  assert.equal(normalizeNng8Sync(atBoundary, observed).status, 'running')
  const overdue = normalizeNng8Sync({ ...atBoundary, started_at: '2026-10-09T07:58:59.999+00:00' }, observed)
  assert.equal(overdue.status, 'unknown'); assert.equal(overdue.reason_code, 'attempt_overdue')
  const expired = '2026-10-09T07:49:59.999+00:00'
  const stale = normalizeNng8Sync(sync({ started_at: '2026-10-09T07:49:50+00:00', finished_at: expired, last_success_at: expired }), observed)
  assert.equal(stale.status, 'unknown'); assert.equal(stale.reason_code, 'health_stale')
  assert.equal(normalizeNng8Sync(sync(), null).status, 'unknown')
  assert.equal(normalizeNng8Sync(sync(), observed).status, 'success', 'valid synthetic historical clock is not refreshed with browser now')
})
test('running/failed state consistency is verified without assuming latest sync completed', () => {
  for (const row of [running(), failed()]) assert.equal(normalizeNng8Sync(row, observed).status, row.status)
  for (const row of [{ ...running(), finished_at: finish }, { ...running(), last_success_at: observed },
    { ...failed(), finished_at: null }, { ...failed(), reason_code: null }, { ...failed(), consecutive_failures: 0 },
    { ...failed(), last_success_at: finish }]) {
    assert.equal(normalizeNng8Sync(row, observed).status, 'unknown')
  }
})
test('unknown health accepts complete valid or fully empty metadata, never partially supplied metadata', () => {
  for (const reason_code of ['receipt_failed', 'transport_failed', 'publication_failed']) {
    const row = { ...failed(), status: 'unknown', reason_code }
    const accepted = normalizeNng8Sync(row, observed)
    assert.equal(accepted.status, 'unknown'); assert.equal(accepted.reason_code, reason_code)
    assert.equal(accepted.bundle_sha256, row.bundle_sha256)
    assert.ok(nng8SyncPresentation(accepted).reason)
    assert.equal(normalizeNng8Sync(unknown(reason_code), observed).reason_code, reason_code)
    for (const change of [{ bundle_sha256: null }, { writer_version: null }, { consecutive_failures: null }, { started_at: null }]) {
      assert.equal(normalizeNng8Sync({ ...row, ...change }, observed).reason_code, 'invalid')
    }
  }
})
test('health age uses finished-or-started; a valid recent finish does not inherit old start staleness', () => {
  const longFinished = sync({ started_at: '2026-10-09T07:40:00+00:00' })
  assert.equal(normalizeNng8Sync(longFinished, observed).status, 'success')
  const oldRunning = { ...running(), started_at: '2026-10-09T07:49:59+00:00', last_success_at: '2026-10-09T07:40:00+00:00' }
  assert.equal(normalizeNng8Sync(oldRunning, observed).reason_code, 'attempt_overdue')
  for (const stamp of ['2026-02-30T07:59:50Z', '2026-10-09T24:00:00Z', '2026-10-09T07:59:50+24:00', '2026-10-09T07:59:50.1234567Z']) {
    assert.equal(normalizeNng8Sync(sync({ started_at: stamp }), observed).reason_code, 'invalid')
  }
})
test('actual disk writer to integration to backend projector to frontend preserves success/running/failed/receipt-failed semantics', () => {
  const root = fileURLToPath(new URL('../../../../', import.meta.url))
  const program = [
    'import sys,json,tempfile,os,hashlib',
    'from pathlib import Path',
    'from unittest.mock import patch',
    'root=Path(sys.argv[1])',
    'sys.path.insert(0,str(root/"tests"))',
    'from test_sync_chain import project,run_once,load_configured_bundle,SyncError,atomic_write',
    'raw=(root/"fixtures/strict-source-bundle.json").read_bytes()',
    'rows={}',
    'with tempfile.TemporaryDirectory(prefix="frontend-sync-chain-") as tmp:',
    '    staging,control=Path(tmp).resolve()/"staging",Path(tmp).resolve()/"control"',
    '    staging.mkdir(mode=0o700); control.mkdir(mode=0o700)',
    '    env={"NNG8_BUNDLE_FILE":str(staging/"current.json"),"NNG8_SYNC_HEALTH_FILE":str(staging/"sync_health.json")}',
    '    with patch.dict(os.environ,env):',
    '        run_once(staging,control,lambda deadline:raw)',
    '        rows["success"]=project(load_configured_bundle()[0])',
    '        def during_running(deadline):',
    '            rows["running"]=project(load_configured_bundle()[0])',
    '            return raw',
    '        run_once(staging,control,during_running)',
    '        def fail_transport(deadline):',
    '            raise SyncError("transport_failed")',
    '        run_once(staging,control,fail_transport)',
    '        rows["failed"]=project(load_configured_bundle()[0])',
    '        def fail_receipt(directory,name,content,deadline,**kwargs):',
    '            if name=="receipt.json": raise OSError("synthetic receipt interruption")',
    '            return atomic_write(directory,name,content,deadline,**kwargs)',
    '        with patch("sync.writer.atomic_write",side_effect=fail_receipt):',
    '            run_once(staging,control,lambda deadline:raw)',
    '        rows["receipt_failed"]=project(load_configured_bundle()[0])',
    '        assert (staging/"current.json").read_bytes()==raw',
    'print(json.dumps({"rows":rows,"source_sha256":hashlib.sha256(raw).hexdigest()},ensure_ascii=False,allow_nan=False))'
  ].join('\n')
  // Actual local writer owns only TemporaryDirectory files. Payload stdout is
  // consumed internally; no source body/path/credential is emitted in test logs.
  const actual = JSON.parse(execFileSync('python3', ['-B', '-c', program, root], { encoding: 'utf8' }))
  assert.equal(actual.source_sha256, '64a1a9cdf781bc57e66906d38547bdf886270573199f26d3ccb93aefb25f13dd')
  for (const [mode, expectedStatus, expectedReason] of [['success', 'success', null], ['running', 'running', null],
    ['failed', 'failed', 'transport_failed'], ['receipt_failed', 'unknown', 'receipt_failed']]) {
    const actualDto = actual.rows[mode]
    assert.equal(actualDto.sync.status, expectedStatus); assert.equal(actualDto.sync.reason_code, expectedReason)
    const view = normalizeNng8(actualDto)
    assert.equal(view.sync.status, expectedStatus); assert.equal(view.sync.reason_code, expectedReason)
    assert.equal(view.reports[0].blocks.length, 107); assert.equal(view.reports[0].generated_at, null)
    assert.equal(view.reports[0].data_cutoff, null)
    assert.equal(view.sync.bundle_sha256, actual.source_sha256)
    assert.equal(nng8PageState({ enabled: true, snapshot: view }), 'partial', 'sync never repairs stale/insufficient business evidence')
  }
  assert.match(nng8SyncPresentation(normalizeNng8(actual.rows.receipt_failed).sync).reason, /回执未完成/)
})
test('actual component HTTP200 failed sync clearly shows server last copy, not browser network cache', async () => {
  const { vm } = await instance(async () => response({ sync: failed() }))
  await vm.load()
  assert.equal(vm.syncView.message, '本次同步失败，显示上次副本')
  assert.equal(vm.cached, false); assert.equal(vm.error, ''); assert.equal(vm.lastSuccessAt, observed)
  assert.equal(vm.snapshot.captured_at, capture); assert.equal(vm.pageState, 'partial')
})
test('actual component running and unknown remain readable, not a completed synchronization', async () => {
  for (const value of [running(), unknown('generation_mismatch'), undefined]) {
    const { vm } = await instance(async () => response({ sync: value }))
    await vm.load()
    assert.ok(vm.snapshot); assert.equal(vm.pageState, 'partial')
    assert.match(vm.syncView.message, /正在同步|同步状态未知/)
  }
})
test('actual component network refresh failure keeps old body but cannot reuse its sync-success assertion', async () => {
  let fail = false
  const { vm } = await instance(async () => { if (fail) throw httpError(503); return response() })
  await vm.load(); assert.equal(vm.syncView.message, '同步完成（不代表业务成功）')
  fail = true; await vm.load()
  assert.ok(vm.snapshot); assert.equal(vm.snapshot.sync.status, 'success', 'old evidence remains byte/field faithful')
  assert.equal(vm.cached, true); assert.match(vm.syncView.message, /不能确认本次同步成功/)
  assert.match(vm.error, /本次读取失败/); assert.match(vm.error, /同步状态未再次确认/)
  assert.equal(vm.lastSuccessAt, observed); assert.equal(vm.snapshot.sync.last_success_at, finish)
  assert.doesNotMatch(vm.error, /PRIVATE|token=/)
})
test('actual component in-progress browser refresh labels old health unknown until new response', async () => {
  const later = deferred(); let calls = 0
  const { vm } = await instance(() => ++calls === 1 ? Promise.resolve(response()) : later.promise)
  await vm.load(); const refresh = vm.load()
  assert.equal(vm.loading, true); assert.ok(vm.snapshot); assert.match(vm.syncView.message, /不能确认/)
  later.resolve(response({ sync: failed() })); await refresh
  assert.equal(vm.loading, false); assert.equal(vm.syncView.message, '本次同步失败，显示上次副本')
})
test('initial missing bundle uses generic503 copy and invents neither body nor sync DTO', async () => {
  const { vm } = await instance(async () => { throw httpError(503) })
  await vm.load()
  assert.equal(vm.snapshot, null); assert.equal(vm.payload, null); assert.equal(vm.cached, false)
  assert.equal(vm.pageState, 'unavailable'); assert.match(vm.stateText, /同步\/读取失败，暂无本次可用副本/)
  assert.doesNotMatch(vm.stateText, /PRIVATE|token=/)
})
test('401/403/404 clear both body and sync health on actual component refresh', async () => {
  for (const status of [401, 403, 404]) {
    let fail = false
    const { vm } = await instance(async () => { if (fail) throw httpError(status); return response() })
    await vm.load(); fail = true; await vm.load()
    assert.equal(vm.snapshot, null); assert.equal(vm.payload, null); assert.equal(vm.lastSuccessAt, null)
    assert.equal(vm.pageState, 'denied'); assert.match(vm.syncView.message, /不能确认/)
  }
})
test('account change/session logout/feature-off invalidate old sync and body together', async () => {
  const request = deferred()
  const { vm, options, store } = await instance(() => request.promise)
  const old = vm.load(); store.getters.userInfo = { id: 2 }; options.watch.accountId.call(vm)
  request.resolve(response({ sync: failed() })); await old; await tick()
  assert.equal(vm.snapshotAccount, '2'); assert.equal(vm.snapshot.sync.status, 'failed')
  vm.enabled = false; options.watch.enabled.call(vm, false)
  assert.equal(vm.snapshot, null); assert.equal(vm.payload, null); assert.equal(vm.pageState, 'disabled')
  const second = await instance(async () => response())
  second.vm.created(); await tick(); second.store.getters.userInfo = {}; second.subscriptions[0]({ type: 'SET_TOKEN' })
  assert.equal(second.vm.payload, null); assert.equal(second.vm.snapshot, null)
})
test('request race and keep-alive lifecycle cannot restore an old account/sync generation', async () => {
  const first = deferred(), second = deferred(); let calls = 0
  const { vm } = await instance(() => (++calls === 1 ? first : second).promise)
  const a = vm.load(), b = vm.load(); second.resolve(response({ sync: failed() })); await b
  first.resolve(response()); await a
  assert.equal(vm.snapshot.sync.status, 'failed')
  const late = deferred(); const inactive = await instance(() => late.promise)
  const old = inactive.vm.load(); inactive.vm.deactivated(); late.resolve(response()); await old
  assert.equal(inactive.vm.payload, null); assert.equal(inactive.vm.loading, false)
})
test('disabled hooks generate zero requests and sync strip shares dark/realdark desktop-safe tokens', async () => {
  let calls = 0
  const { vm } = await instance(() => { calls++ }, { enabled: false })
  vm.created(); vm.activated(); await vm.load(); assert.equal(calls, 0)
  for (const theme of ['light', 'dark', 'realdark']) {
    const item = await instance(async () => response(), { theme })
    assert.equal(item.vm.dark, theme !== 'light')
  }
  assert.match(source, /nng8-sync-times[^}]*repeat\(4, minmax\(0, 1fr\)\)/)
  assert.match(source, /同步操作时间，不是报告生成时间或行情截止/)
  assert.match(source, /本次页面读取成功不等于同步成功/)
  assert.match(source, /下一次页面读取或刷新时可见/)
  assert.doesNotMatch(source, /v-html|setInterval|setTimeout|Date\.now\(|getters\.token/)
})
