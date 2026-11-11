import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createTaskOperationController, newTaskOperationState, operationSnapshot, taskOperationsResponse } from '../../src/utils/researchTaskOperations.js'
import { isUnifiedResearchRead } from '../../src/utils/unifiedResearchRead.js'

const row = (status = 'interrupted') => ({ run: { id: 'rr1', branchId: 'rb1', status, execution: { id: 're1' } }, actions: [{ id: 'reconcile', enabled: true, requiresConfirmation: true, startsExecution: false, reason: null }, { id: 'retry', enabled: true, requiresConfirmation: true, startsExecution: true, reason: null }] })
const ok = data => ({ code: 1, data })
const dto = operations => ok({ contractVersion: 'research.task-operations.v1', operations, externalOperations: { enabled: false, reason: '冻结任务不可解除' } })
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function setup (overrides = {}) {
  let account = '1'; let keys = 0
  const state = newTaskOperationState(); const calls = []
  const api = {
    getTaskOperations: async (...args) => { calls.push(['read', ...args]); return overrides.read ? overrides.read(...args) : dto([row()]) },
    operateTask: async (...args) => { calls.push(['operate', ...args]); return overrides.write ? overrides.write(...args) : ok({ run: row().run }) }
  }
  const controller = createTaskOperationController({ state, api, account: () => account, key: () => 'key-' + ++keys })
  return { state, calls, controller, setAccount: id => { account = id } }
}

test('task reads and navigation never dispatch actions, and confirmation is mandatory', async () => {
  const h = setup(); await h.controller.load(); h.controller.choose('rr1', 'retry'); await h.controller.submit()
  assert.equal(h.calls.filter(c => c[0] === 'operate').length, 0)
  assert.equal(h.state.selected.startsExecution, true); h.state.confirmed = true; await h.controller.submit()
  assert.equal(h.calls.filter(c => c[0] === 'operate').length, 1)
  assert.deepEqual(h.calls[1].slice(1), ['rr1', 'retry', { confirmed: true, observedExecutionId: 're1', observedStatus: 'interrupted', clientRequestId: 'key-1' }])
})
test('disabled, unknown and unowned actions cannot produce a confirmation snapshot', () => {
  assert.equal(operationSnapshot(row(), 'stop', 'key'), null)
  assert.equal(operationSnapshot({ ...row(), actions: [{ ...row().actions[1], enabled: false }] }, 'retry', 'key'), null)
  assert.equal(operationSnapshot({ ...row(), run: { status: 'running' } }, 'retry', 'key'), null)
})
test('malformed task contract fails closed, including duplicate identities and unconfirmed action', () => {
  for (const value of [{ code: 0, data: [] }, dto([row(), row()]), dto([{ ...row(), actions: [{ ...row().actions[0], requiresConfirmation: false }] }]), dto([{ ...row(), actions: [{ ...row().actions[0], id: 'unfreeze' }] }])]) assert.throws(() => taskOperationsResponse(value))
})
test('double confirmation while dispatch pending makes one request and refuses refresh', async () => {
  const pending = deferred(); const h = setup({ write: () => pending.promise })
  await h.controller.load(); h.controller.choose('rr1', 'retry'); h.state.confirmed = true
  const first = h.controller.submit(); await h.controller.submit(); await h.controller.load()
  assert.equal(h.calls.length, 2); assert.equal(h.state.saving, true)
  pending.resolve(ok({ run: row().run })); await first
  assert.equal(h.state.saving, false); assert.ok(h.state.receipt)
})
test('unknown outcome retains exact request key but 409 never automatically repeats', async () => {
  let attempts = 0; const h = setup({ write: async () => { if (++attempts === 1) throw Error('lost reply'); throw { response: { status: 409 } } } })
  await h.controller.load(); h.controller.choose('rr1', 'retry'); h.state.confirmed = true
  await h.controller.submit(); assert.equal(h.state.outcomeUnknown, true); assert.equal(h.state.saving, false)
  await h.controller.submit(); assert.equal(h.state.outcomeUnknown, false); assert.equal(h.state.confirmed, false)
  assert.equal(h.calls[1][3].clientRequestId, h.calls[2][3].clientRequestId)
  await h.controller.submit(); assert.equal(attempts, 2); assert.match(h.state.error, /状态已变化/)
})
test('account replacement, departure and late completion cannot revive old task identity', async () => {
  for (const close of [h => { h.setAccount('2'); h.controller.invalidate() }, h => h.controller.deactivate()]) {
    const pending = deferred(); const h = setup({ write: () => pending.promise }); await h.controller.load()
    h.controller.choose('rr1', 'retry'); h.state.confirmed = true; const first = h.controller.submit(); close(h)
    pending.resolve(ok({ run: row().run })); await first
    assert.equal(h.state.receipt, null); assert.equal(h.state.selected, null); assert.equal(h.state.saving, false)
  }
})
test('old loading failure/finally cannot overwrite a new successful read', async () => {
  const pending = deferred(); let reads = 0; const h = setup({ read: () => ++reads === 1 ? pending.promise : Promise.resolve(dto([row('succeeded')])) })
  const first = h.controller.load(); await h.controller.load(); pending.reject(Error('old failure')); await first
  assert.equal(h.state.operations[0].run.status, 'succeeded'); assert.equal(h.state.error, ''); assert.equal(h.state.loading, false)
})
test('foreign receipt is never presented as success and permission refusal clears operation rows', async () => {
  const h = setup({ write: async () => ok({ run: { id: 'foreign' } }) }); await h.controller.load(); h.controller.choose('rr1', 'reconcile'); h.state.confirmed = true
  await h.controller.submit(); assert.equal(h.state.receipt, null); assert.ok(h.state.error)
  const denied = setup({ write: async () => { throw { response: { status: 403 } } } }); await denied.controller.load(); denied.controller.choose('rr1', 'retry'); denied.state.confirmed = true
  await denied.controller.submit(); assert.deepEqual(denied.state.operations, []); assert.equal(denied.state.permission, 'denied'); assert.equal(denied.state.saving, false)
})
test('task operation auth exception remains GET-only and refuses arbitrary controls', () => {
  for (const url of ['/api/research-lab/v2/task-operations', '/api/research-lab/v2/task-operations?branchId=rb1', '/api/radar/workbench/reports']) assert.equal(isUnifiedResearchRead({ url, method: 'get', unifiedResearchCallerHandlesAuth: true }), true)
  for (const url of ['/api/research-lab/v2/task-operations?stop=1', '/api/research-lab/v2/task-operations/rr1/retry', '/api/research-lab/v2/task-operations?branchId=rb1&retry=1']) assert.equal(isUnifiedResearchRead({ url, method: 'post', unifiedResearchCallerHandlesAuth: true }), false)
})
test('task component makes confirmations explicit and labels external frozen operations unavailable', () => {
  const source = fs.readFileSync(new URL('../../src/components/ResearchTaskOperations/index.vue', import.meta.url), 'utf8')
  assert.match(source, /task-operation-check/); assert.match(source, /重试会开始新的执行/)
  assert.match(source, /deactivated \(\) \{ this.controller.deactivate\(\) \}/)
  assert.match(source, /冻结任务不可解除/); assert.match(source, /controller.submit\(\)/)
})
