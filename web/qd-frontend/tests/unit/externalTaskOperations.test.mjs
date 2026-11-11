import test from 'node:test'
import assert from 'node:assert/strict'
import { createExternalOperationController, newExternalOperationState, pendingRequestStore, externalCatalog } from '../../src/utils/externalTaskOperations.js'
import { isExternalOperationWrite, isUnifiedResearchRead } from '../../src/utils/unifiedResearchRead.js'

const ok = data => ({ code: 1, data })
const task = (revision = 'rev-a') => ({ taskId: 'astock-cninfo', label: '公告证据采集', project: 'AStock', revision, state: 'available', units: [{ unit: 'astock-cninfo.timer', activeState: 'active' }], actions: [{ id: 'pause', label: '暂停调度', enabled: true, requiresConfirmation: true, startsExecution: false, effect: '只暂停后续计划，不中止当前服务。' }, { id: 'run', label: '采集公告', enabled: false, requiresConfirmation: true, startsExecution: true, reason: '现有任务尚未完成。' }] })
const catalog = (tasks = [task()]) => ok({ contractVersion: 'research.external-operations.v1', source: 'nng8', enabled: true, readOnly: false, tasks, operations: [], transport: { reachable: true, network: 'private' } })
const receipt = (request, status = 'succeeded') => ok({ operation: { operationId: 'op-1', clientRequestId: request.clientRequestId, taskId: request.taskId, action: request.action, status, detail: '来自执行器的独立操作回执' } })
const error = status => ({ response: { status } })
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function storage () { const values = new Map(); return { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), values } }
function setup (overrides = {}, shared = storage()) {
  let account = '101'; let number = 0
  const state = newExternalOperationState(); const calls = []; const pending = pendingRequestStore(shared)
  const api = {
    getExternalOperations: async () => { calls.push(['read']); return overrides.read ? overrides.read() : catalog() },
    operateExternalTask: async (taskId, action, body) => { calls.push(['dispatch', taskId, action, { ...body }]); return overrides.write ? overrides.write(taskId, action, body) : receipt({ taskId, action, ...body }) },
    getExternalOperationRequest: async id => { calls.push(['query', id]); return overrides.query ? overrides.query(id) : receipt(pending.read(account).find(row => row.clientRequestId === id)) }
  }
  const controller = createExternalOperationController({ state, api, account: () => account, key: () => 'request-' + (++number), pending })
  return { state, calls, controller, pending, storage: shared, setAccount: value => { account = value } }
}

test('registered action confirmation fixes task identity, state revision and request id before dispatch', async () => {
  const h = setup(); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); await h.controller.submit()
  assert.equal(h.calls.length, 1)
  assert.match(h.state.selected.risk, /不中止当前服务/)
  h.state.confirmed = true; await h.controller.submit()
  assert.deepEqual(h.calls[1], ['dispatch', 'astock-cninfo', 'pause', { confirmed: true, clientRequestId: 'request-1', observedRevision: 'rev-a' }])
  assert.equal(h.state.receipt.status, 'succeeded'); assert.equal(h.state.pending.length, 0)
  await h.controller.submit(); assert.equal(h.calls.length, 2)
})
test('disabled, frozen, unknown and unregistered actions never dispatch', async () => {
  const frozen = { ...task(), taskId: 'ledger', state: 'frozen', actions: [] }
  const h = setup({ read: async () => catalog([task(), frozen]) }); await h.controller.load()
  for (const [id, action] of [['astock-cninfo', 'run'], ['ledger', 'run'], ['astock-cninfo', 'arbitrary-command'], ['foreign', 'pause']]) { h.controller.choose(id, action); h.state.confirmed = true; await h.controller.submit(); assert.equal(h.state.selected, null) }
  assert.equal(h.calls.length, 1)
})
test('bad contracts, duplicate tasks and unexpected actions fail closed', () => {
  for (const response of [ok({}), catalog([task(), task()]), catalog([{ ...task(), revision: '' }]), catalog([{ ...task(), actions: [{ ...task().actions[0], id: 'unfreeze' }] }]), catalog([{ ...task(), actions: [{ ...task().actions[0], requiresConfirmation: false }] }])]) assert.throws(() => externalCatalog(response))
})
test('unavailable task with no revision remains visible without allowing any action', async () => {
  const h = setup({ read: async () => catalog([task(), { ...task(), taskId: 'offline', state: 'unavailable', revision: null, actions: [{ ...task().actions[0], enabled: false, reason: '无法核实实时状态' }] }]) }); await h.controller.load()
  assert.equal(h.state.catalog.tasks.length, 2); h.controller.choose('offline', 'pause'); assert.equal(h.state.selected, null)
})
test('double click while dispatch is pending creates one submission and blocks refresh', async () => {
  const p = deferred(); const h = setup({ write: () => p.promise }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true
  const first = h.controller.submit(); await h.controller.submit(); await h.controller.load()
  assert.equal(h.calls.filter(row => row[0] === 'dispatch').length, 1); assert.equal(h.calls.filter(row => row[0] === 'read').length, 1)
  p.resolve(receipt(h.state.selected, 'accepted')); await first; assert.equal(h.state.saving, false); assert.equal(h.state.outcomeUnknown, true)
})
test('lost reply uses GET original request only and preserves identity across reload', async () => {
  const h = setup({ write: async () => { throw Error('network lost') } }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.state.outcomeUnknown, true); assert.equal(h.state.pending.length, 1)
  await h.controller.submit(); assert.equal(h.calls.filter(row => row[0] === 'dispatch').length, 1)
  const restored = setup({}, h.storage); await restored.controller.load(); restored.controller.choose('astock-cninfo', 'pause'); assert.equal(restored.state.selected, null)
  restored.controller.resume('request-1'); await restored.controller.query()
  assert.equal(restored.state.receipt.status, 'succeeded'); assert.equal(restored.state.pending.length, 0); assert.deepEqual(restored.calls.map(row => row[0]), ['read', 'query'])
})
test('404 request lookup remains unknown and never allows a new dispatch', async () => {
  const h = setup({ write: async () => { throw Error('lost') }, query: async () => { throw error(404) } }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit(); await h.controller.query()
  assert.equal(h.state.outcomeUnknown, true); assert.equal(h.state.pending.length, 1); assert.match(h.state.error, /尚未查到/)
  h.controller.cancel(); h.controller.choose('astock-cninfo', 'pause'); assert.equal(h.state.selected, null)
})
test('known CAS refusal does not retry and asks user to reload current task version', async () => {
  const h = setup({ write: async () => { throw error(409) } }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.state.pending.length, 0); assert.equal(h.state.outcomeUnknown, false); assert.equal(h.state.confirmed, false); assert.match(h.state.error, /版本已经变化/)
  await h.controller.submit(); assert.equal(h.calls.filter(row => row[0] === 'dispatch').length, 1)
})
test('foreign receipt identity is unknown, never accepted as success', async () => {
  const h = setup({ write: async () => receipt({ clientRequestId: 'other', taskId: 'foreign', action: 'pause' }) }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.state.receipt, null); assert.equal(h.state.outcomeUnknown, true); assert.equal(h.state.pending.length, 1)
})
test('accepted and running receipts retain request until terminal failure or completion', async () => {
  const h = setup({ write: async (taskId, action, body) => receipt({ taskId, action, ...body }, 'accepted'), query: async id => receipt({ taskId: 'astock-cninfo', action: 'pause', clientRequestId: id }, 'failed') }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.state.receipt.status, 'accepted'); assert.equal(h.state.pending.length, 1)
  await h.controller.query(); assert.equal(h.state.receipt.status, 'failed'); assert.equal(h.state.pending.length, 0)
})
test('account change, departure and late success cannot revive previous operation context', async () => {
  for (const leave of [h => { h.setAccount('202'); h.controller.invalidate() }, h => h.controller.deactivate()]) {
    const p = deferred(); const h = setup({ write: () => p.promise }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; const request = h.state.selected; const first = h.controller.submit(); leave(h); p.resolve(receipt(request)); await first
    assert.equal(h.state.catalog, null); assert.equal(h.state.selected, null); assert.equal(h.state.receipt, null); assert.equal(h.state.saving, false)
    assert.equal(h.pending.read('101').length, 1)
  }
})
test('late failure and finally from an older catalog cannot replace the newer state', async () => {
  const p = deferred(); let count = 0; const h = setup({ read: () => ++count === 1 ? p.promise : catalog([task('rev-b')]) }); const first = h.controller.load(); await h.controller.load(); p.reject(Error('old error')); await first
  assert.equal(h.state.catalog.tasks[0].revision, 'rev-b'); assert.equal(h.state.error, ''); assert.equal(h.state.loading, false)
})
test('403 clears all visible project and request data, leaving no enabled old actions', async () => {
  const h = setup({ write: async () => { throw error(403) } }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.state.permission, 'denied'); assert.equal(h.state.catalog, null); assert.equal(h.state.selected, null); assert.deepEqual(h.state.pending, []); assert.equal(h.state.receipt, null); assert.equal(h.state.saving, false)
})
test('browser storage failure prevents dispatch and cannot leave a fake unknown operation', async () => {
  const shared = { getItem: () => '[]', setItem: () => { throw Error('blocked') } }; const h = setup({}, shared); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.calls.filter(row => row[0] === 'dispatch').length, 0); assert.equal(h.state.outcomeUnknown, false); assert.match(h.state.error, /操作未提交/)
})
test('pending original request remains queryable when catalog reload fails', async () => {
  const h = setup({ write: async () => { throw Error('lost') } }); await h.controller.load(); h.controller.choose('astock-cninfo', 'pause'); h.state.confirmed = true; await h.controller.submit()
  const restored = setup({ read: async () => { throw Error('unreachable') } }, h.storage); await restored.controller.load(); assert.equal(restored.state.catalog, null); assert.equal(restored.state.pending.length, 1)
  restored.controller.resume('request-1'); await restored.controller.query(); assert.equal(restored.state.receipt.status, 'succeeded')
})
test('transport auth handling allows only exact GET reads and registered POST action paths', () => {
  const base = '/api/research-lab/v2/external-operations'
  for (const url of [base, base + '/requests/request-1']) assert.equal(isUnifiedResearchRead({ url, method: 'get', unifiedResearchCallerHandlesAuth: true }), true)
  assert.equal(isExternalOperationWrite({ url: base + '/astock-cninfo/pause', method: 'post', externalOperationsCallerHandlesAuth: true }), true)
  for (const override of [{ method: 'get' }, { externalOperationsCallerHandlesAuth: false }, { url: base + '/astock-cninfo/stop' }, { url: base + '/astock-cninfo/run?cmd=1' }, { baseURL: 'https://evil.invalid' }, { params: { command: 'run' } }]) assert.equal(isExternalOperationWrite({ url: base + '/astock-cninfo/pause', method: 'post', externalOperationsCallerHandlesAuth: true, ...override }), false)
})
