import assert from 'node:assert/strict'
import test from 'node:test'
import {
  actualApiClient, changeAccount, clone, deferred, editBacktest, harness, httpError, recordedRun
} from './researchWorkflowBatch1Harness.mjs'

const A = { startDate: '2025-01-06', endDate: '2025-12-30', initialCapital: 190000, commissionPct: 0.09, slippagePct: 0.19, override: { ma_period: 90 } }
const B = { startDate: '2022-02-07', endDate: '2022-11-29', initialCapital: 440000, commissionPct: 0.04, slippagePct: 0.14, override: { window: 40 } }
const form = h => clone(h.page.bt)
const posts = h => h.calls.filter(call => call.method === 'startRun')
const identity = h => ({ branch: h.page.branch && h.page.branch.id, run: h.page.currentRun && h.page.currentRun.id, context: clone(h.page.reviewCtxData), chart: clone(h.page.chart) })
async function unknownSubmission (h, error = new Error('synthetic offline disconnect')) {
  h.api.startRun = async (bid, body, key) => { h.record('startRun', { bid, body, key }); throw error }
  await h.page.startBacktest(false)
  assert.ok(h.page.pendingRunKey, 'Unknown result must retain its request key')
  assert.ok(h.page.pendingRunBody, 'Unknown result must retain its exact request body')
  return { key: h.page.pendingRunKey, body: clone(h.page.pendingRunBody) }
}

test('control: confirmed indicator restores applied40 instead of declared20', async () => {
  const h = await harness()
  assert.deepEqual(clone(h.page.ind.applied), { period: 40 })
  assert.deepEqual(clone(h.page.ind.draft), { period: 40 })
  assert.equal(h.page.ind.specs[0].default, 20)
})
test('control: normal new run uses confirmed strategy and empty override', async () => {
  const h = await harness()
  await h.page.startBacktest(false)
  assert.equal(posts(h).length, 1)
  assert.equal(posts(h)[0].body.strategyArtifactId, 'S_A')
  assert.deepEqual(posts(h)[0].body.config.paramsOverride, {})
  assert.equal(h.page.currentRun.branchId, 'A')
  assert.equal(h.page.currentRun.id, 'NEW_A')
})
test('control: current branch review binds matching run and goes to AI', async () => {
  const h = await harness()
  await h.page.toReview(recordedRun('R_A', 'A'))
  assert.equal(h.page.reviewCtx.A, 'R_A')
  assert.equal(h.page.reviewCtxData.runId, 'R_A')
  assert.equal(h.page.reviewCtxData.branchId, 'A')
  assert.equal(h.page.module, 'ai')
})

test('A override90 and all cost/date/capital fields cannot carry into first B load', async () => {
  const h = await harness()
  editBacktest(h, A)
  await h.page.loadBranch('B')
  const body = h.page.runBody()
  assert.equal(body.strategyArtifactId, 'S_B')
  assert.deepEqual(clone(body.config.paramsOverride), {})
  for (const key of ['startDate', 'endDate', 'initialCapital', 'commissionPct', 'slippagePct']) assert.notEqual(h.page.bt[key], A[key], key)
})
test('A90 to B40 to A restores each branch complete form and parameter names', async () => {
  const h = await harness()
  editBacktest(h, A)
  const a = form(h)
  await h.page.loadBranch('B')
  editBacktest(h, B)
  const b = form(h)
  await h.page.loadBranch('A')
  assert.deepEqual(form(h), a)
  assert.deepEqual(clone(h.page.runBody().config.paramsOverride), { ma_period: 90 })
  await h.page.loadBranch('B')
  assert.deepEqual(form(h), b)
  assert.deepEqual(clone(h.page.runBody().config.paramsOverride), { window: 40 })
})
test('same-branch refresh and full branch reload preserve complete local backtest form', async () => {
  const h = await harness()
  editBacktest(h, A)
  const before = form(h)
  await h.page.refreshBranch()
  assert.deepEqual(form(h), before)
  await h.page.loadBranch('A')
  assert.deepEqual(form(h), before)
})
test('component recreation restores distinct A and B backtest drafts', async () => {
  const h = await harness()
  editBacktest(h, A)
  const a = form(h)
  await h.page.loadBranch('B')
  editBacktest(h, B)
  const b = form(h)
  const second = await harness({ entries: h.entries })
  assert.deepEqual(form(second), a)
  await second.page.loadBranch('B')
  assert.deepEqual(form(second), b)
})
test('creating a new branch starts an independent backtest form even with inherited strategy', async () => {
  const h = await harness()
  editBacktest(h, A)
  h.page.openBranchDialog({ name: 'new hypothesis', carry: ['indicator', 'strategy'] })
  await h.page.doCreateBranch()
  assert.equal(h.page.branch.id, 'N')
  assert.equal(h.page.runBody().strategyArtifactId, 'S_N')
  assert.deepEqual(clone(h.page.runBody().config.paramsOverride), {})
  assert.notEqual(h.page.bt.initialCapital, A.initialCapital)
  await h.page.loadBranch('A')
  assert.equal(h.page.bt.override.ma_period, 90)
})

for (const operation of ['run', 'review', 'chart']) {
  for (const transition of ['switch', 'deactivate']) {
    for (const settlement of ['success', 'network failure', 'HTTP failure']) {
      test(`late ${operation} ${settlement} cannot commit or clear new flags after ${transition}`, async () => {
        const h = await harness(), d = deferred()
        let promise
        if (operation === 'run') { h.api.startRun = () => d.promise; promise = h.page.startBacktest(false) }
        if (operation === 'review') { h.api.reviewContext = () => d.promise; promise = h.page.toReview(recordedRun('R_A', 'A')) }
        if (operation === 'chart') { h.api.indicatorPreview = () => d.promise; promise = h.page.computeChart() }
        if (transition === 'switch') await h.page.loadBranch('B')
        else h.component.deactivated.call(h.page)
        // Simulates active UI work after the request's generation was invalidated.
        h.page.busy = true
        h.page.runInFlight = true
        h.page.networkIssue = 'new-context-network-state'
        h.page.lastCall = 'new-context-call'
        h.page.pendingRunKey = 'new-context-key'
        h.page.pendingRunBody = { marker: 'new-context-body' }
        const before = identity(h), storageBefore = clone([...h.entries]), messageCount = h.messages.length
        if (settlement === 'network failure') d.reject(new Error('late synthetic disconnect'))
        else if (settlement === 'HTTP failure') d.reject(httpError(409, 'REVISION_CONFLICT'))
        else if (operation === 'run') d.resolve({ data: { run: recordedRun('R_A_LATE', 'A') } })
        else if (operation === 'review') d.resolve({ data: { runId: 'R_A_LATE', branchId: 'A', contextSha256: 'ctx-A-late' } })
        else d.resolve({ data: { marker: 'chart-A-late', params: { effective: { period: 40 } }, close: [3, 4], series: {} } })
        await promise
        assert.deepEqual(identity(h), before)
        assert.equal(h.page.busy, true, 'stale finally must not clear new busy flag')
        assert.equal(h.page.runInFlight, true, 'stale finally must not clear new run flag')
        assert.equal(h.page.networkIssue, 'new-context-network-state')
        assert.equal(h.page.lastCall, 'new-context-call')
        assert.equal(h.page.pendingRunKey, 'new-context-key')
        assert.deepEqual(clone(h.page.pendingRunBody), { marker: 'new-context-body' })
        assert.deepEqual(clone([...h.entries]), storageBefore)
        assert.equal(h.messages.length, messageCount)
      })
    }
  }
}
test('A to B to A does not revive the first A run request generation', async () => {
  const h = await harness(), d = deferred()
  h.api.startRun = () => d.promise
  const pending = h.page.startBacktest(false)
  await h.page.loadBranch('B')
  await h.page.loadBranch('A')
  const before = identity(h)
  d.resolve({ data: { run: recordedRun('R_A_OLD_GENERATION', 'A') } })
  await pending
  assert.deepEqual(identity(h), before)
})
test('late afterRun list request cannot replace B after submit already succeeded', async () => {
  const h = await harness(), d = deferred()
  let aLists = 0
  h.api.listRuns = async bid => {
    if (bid === 'A') { aLists++; return d.promise }
    return { data: clone(h.runs[bid]) }
  }
  const pending = h.page.startBacktest(false)
  for (let turn = 0; turn < 10 && !aLists; turn++) await Promise.resolve()
  assert.equal(aLists, 1, 'request reaches actual afterRun list stage')
  await h.page.loadBranch('B')
  const before = identity(h)
  d.resolve({ data: [recordedRun('NEW_A', 'A')] })
  await pending
  assert.deepEqual(identity(h), before)
})
test('duplicate start click cannot invalidate the first accepted response or orphan its request slot', async () => {
  const h = await harness(), d = deferred()
  h.api.startRun = (bid, body, key) => { h.record('startRun', { bid, body, key }); return d.promise }
  const first = h.page.startBacktest(false)
  const duplicate = h.page.startBacktest(false)
  const duplicateReplay = h.page.startBacktest(true)
  const submittedCount = posts(h).length
  d.resolve({ data: { run: recordedRun('FIRST_ACCEPTED_A', 'A') } })
  await Promise.all([first, duplicate, duplicateReplay])
  assert.equal(submittedCount, 1)
  assert.equal(h.page.currentRun.id, 'FIRST_ACCEPTED_A')
  assert.equal(h.page.pendingRunKey, null)
  assert.equal(h.page.runInFlight, false)
})
for (const transition of ['switch', 'deactivate']) {
  test(`a settled old POST receipt remains queryable after returning from ${transition}`, async () => {
    const h = await harness(), d = deferred()
    h.api.startRun = (bid, body, key) => { h.record('startRun', { bid, body, key }); return d.promise }
    const first = h.page.startBacktest(false)
    const request = { key: h.page.pendingRunKey, body: clone(h.page.pendingRunBody) }
    assert.ok(request.key)
    if (transition === 'switch') await h.page.loadBranch('B')
    else h.component.deactivated.call(h.page)
    d.resolve({ data: { run: recordedRun('SETTLED_OLD_A', 'A') } })
    await first
    if (transition === 'switch') await h.page.loadBranch('A')
    else await h.component.activated.call(h.page)
    assert.equal(h.page.pendingRunKey, request.key)
    assert.deepEqual(clone(h.page.pendingRunBody), request.body)
    assert.equal(h.page.runInFlight, false, 'stale local request slot must not block recovery')
    h.api.findRunByKey = async key => { h.record('findRunByKey', { key }); return { data: { run: recordedRun('SETTLED_OLD_A', 'A') } } }
    await h.page.queryPendingKey()
    assert.equal(h.calls.filter(call => call.method === 'findRunByKey').length, 1)
    assert.equal(h.page.currentRun.id, 'SETTLED_OLD_A')
    assert.equal(h.page.pendingRunKey, null)
    assert.equal(posts(h).length, 1, 'recovery must not create a second execution request')
  })
}

test('same configuration sends history60, original strategy/date/capital/costs with current revision', async () => {
  const h = await harness()
  h.page.currentRun = recordedRun('HIST_A', 'A', { strategyArtifactId: 'S_A_OLD', effectiveConfig: {
    params: { ma_period: 60, enabled: false, threshold: 0, label: '' }, startDate: '2023-01-03', endDate: '2024-08-30',
    initialCapital: 234567, commission: { rate: 0.0002, unit: 'ratio' }, slippage: { rate: 0.0007, unit: 'ratio' }, leverage: 1
  } })
  editBacktest(h, A)
  h.page.branch.revision = 9
  await h.page.startBacktest(true)
  assert.equal(posts(h).length, 1)
  assert.deepEqual(posts(h)[0].body, {
    strategyArtifactId: 'S_A_OLD', expectedBranchRevision: 9,
    config: { startDate: '2023-01-03', endDate: '2024-08-30', initialCapital: 234567,
      commissionPct: 0.0002 * 100, slippagePct: 0.0007 * 100, leverage: 1, paramsOverride: { ma_period: 60, enabled: false, threshold: 0, label: '' } }
  })
})
for (const field of ['startDate', 'endDate', 'initialCapital', 'commission', 'slippage', 'leverage', 'params']) {
  test(`same configuration refuses incomplete recorded ${field} instead of current-form fallback`, async () => {
    const h = await harness()
    const history = recordedRun('INCOMPLETE_A', 'A')
    delete history.effectiveConfig[field]
    delete history.config
    h.page.currentRun = history
    editBacktest(h, A)
    await h.page.startBacktest(true)
    assert.equal(posts(h).length, 0)
    assert.equal(h.page.pendingRunKey, null)
    assert.ok(h.messages.some(message => message.kind === 'warning' || message.kind === 'error'))
  })
}
test('same configuration rejects unsupported leverage instead of silently changing it', async () => {
  const h = await harness()
  const history = recordedRun('LEVERAGED_A', 'A')
  history.effectiveConfig.leverage = 2
  history.config.leverage = 2
  h.page.currentRun = history
  await h.page.startBacktest(true)
  assert.equal(posts(h).length, 0)
})

test('pending query calls only key lookup; actual API client uses encoded read-only GET', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  h.calls.length = 0
  await h.page.queryPendingKey()
  assert.deepEqual(h.calls.filter(call => ['startRun', 'findRunByKey'].includes(call.method)), [{ method: 'findRunByKey', key: request.key }])
  const client = actualApiClient()
  await client.findRunByKey('rk-key ?/+')
  assert.equal(client.requests[0].method, 'get')
  assert.equal(client.requests[0].url, '/api/research-lab/v2/runs?idempotencyKey=rk-key%20%3F%2F%2B')
  assert.equal(client.requests[0].data, undefined)
})
test('unknown request key and exact body survive component recreation in same branch', async () => {
  const h = await harness()
  editBacktest(h, A)
  const request = await unknownSubmission(h)
  const second = await harness({ entries: h.entries })
  assert.equal(second.page.pendingRunKey, request.key)
  assert.deepEqual(clone(second.page.pendingRunBody), request.body)
  await second.page.loadBranch('B')
  assert.equal(second.page.pendingRunKey, null)
  await second.page.loadBranch('A')
  assert.equal(second.page.pendingRunKey, request.key)
  assert.deepEqual(clone(second.page.pendingRunBody), request.body)
})
test('404 lookup preserves pending request; only explicit resubmit uses identical key and body', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  h.calls.length = 0
  h.api.findRunByKey = async key => { h.record('findRunByKey', { key }); throw httpError(404, 'RUN_NOT_FOUND') }
  await h.page.queryPendingKey()
  assert.equal(posts(h).length, 0)
  assert.equal(h.page.pendingRunKey, request.key)
  assert.deepEqual(clone(h.page.pendingRunBody), request.body)
  assert.equal(typeof h.page.resubmitPendingRun, 'function')
  editBacktest(h, A)
  h.api.startRun = async (bid, body, key) => { h.record('startRun', { bid, body, key }); throw new Error('still offline') }
  await h.page.resubmitPendingRun()
  assert.equal(posts(h).length, 1)
  assert.equal(posts(h)[0].key, request.key)
  assert.deepEqual(posts(h)[0].body, request.body)
})
for (const status of [500, 503]) {
  test(`HTTP${status} initial submission keeps request key/body recoverable`, async () => {
    const h = await harness()
    const request = await unknownSubmission(h, httpError(status))
    const second = await harness({ entries: h.entries })
    assert.equal(second.page.pendingRunKey, request.key)
    assert.deepEqual(clone(second.page.pendingRunBody), request.body)
  })
}
test('HTTP500 key lookup keeps pending body and cannot initiate a run', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  h.calls.length = 0
  h.api.findRunByKey = async key => { h.record('findRunByKey', { key }); throw httpError(500) }
  await h.page.queryPendingKey()
  assert.equal(h.page.pendingRunKey, request.key)
  assert.deepEqual(clone(h.page.pendingRunBody), request.body)
  assert.equal(posts(h).length, 0)
})
test('unresolved submission blocks new keys and resubmit before an explicit missing lookup', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  h.calls.length = 0
  await h.page.startBacktest(false)
  await h.page.startBacktest(true)
  assert.equal(typeof h.page.resubmitPendingRun, 'function')
  await h.page.resubmitPendingRun()
  assert.equal(posts(h).length, 0)
  assert.equal(h.page.pendingRunKey, request.key)
  assert.deepEqual(clone(h.page.pendingRunBody), request.body)
})
test('failure to durably store pending key/body refuses POST and explains recovery limitation', async () => {
  const h = await harness()
  h.storage.setItem = () => { throw new Error('synthetic sessionStorage quota failure') }
  await h.page.startBacktest(false)
  assert.equal(posts(h).length, 0, 'never submit before the recovery receipt has been persisted')
  assert.equal(h.page.runInFlight, false)
  assert.ok(h.messages.some(message => (message.kind === 'error' || message.kind === 'warning') && message.msg))
})
for (const transition of ['switch', 'deactivate']) {
  for (const settlement of ['success', 'network failure', 'HTTP failure']) {
    test(`late pending GET ${settlement} preserves current context after ${transition}`, async () => {
      const h = await harness(), d = deferred()
      await unknownSubmission(h)
      h.api.findRunByKey = key => { h.record('findRunByKey', { key }); return d.promise }
      const pending = h.page.queryPendingKey()
      assert.equal(h.calls.filter(call => call.method === 'findRunByKey').length, 1, 'actual key lookup reaches the deferred GET')
      if (transition === 'switch') await h.page.loadBranch('B')
      else h.component.deactivated.call(h.page)
      h.page.busy = true
      h.page.networkIssue = 'new-context-network-state'
      h.page.pendingRunKey = 'new-context-key'
      h.page.pendingRunBody = { marker: 'new-context-body' }
      const before = identity(h), storageBefore = clone([...h.entries]), messageCount = h.messages.length
      if (settlement === 'network failure') d.reject(new Error('late lookup disconnect'))
      else if (settlement === 'HTTP failure') d.reject(httpError(500))
      else d.resolve({ data: { run: recordedRun('R_A_LATE_GET', 'A') } })
      await pending
      assert.deepEqual(identity(h), before)
      assert.deepEqual(clone([...h.entries]), storageBefore)
      assert.equal(h.page.pendingRunKey, 'new-context-key')
      assert.deepEqual(clone(h.page.pendingRunBody), { marker: 'new-context-body' })
      assert.equal(h.page.busy, true)
      assert.equal(h.page.networkIssue, 'new-context-network-state')
      assert.equal(h.messages.length, messageCount)
    })
  }
}
test('lookup response for another branch cannot be shown or clear this branch receipt', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  h.api.findRunByKey = async key => { h.record('findRunByKey', { key }); return { data: { run: recordedRun('FOREIGN_B', 'B') } } }
  await h.page.queryPendingKey()
  assert.equal(h.calls.filter(call => call.method === 'findRunByKey').length, 1)
  assert.equal(h.page.currentRun.branchId, 'A')
  assert.equal(h.page.pendingRunKey, request.key)
  assert.deepEqual(clone(h.page.pendingRunBody), request.body)
})
test('changed auth token invalidates a pending GET even when account and branch IDs match', async () => {
  const h = await harness(), d = deferred()
  await unknownSubmission(h)
  h.api.findRunByKey = key => { h.record('findRunByKey', { key }); return d.promise }
  const pending = h.page.queryPendingKey()
  assert.equal(h.calls.filter(call => call.method === 'findRunByKey').length, 1, 'actual key lookup is pending before auth change')
  h.page.$store.state.user.token = 'replacement-token'
  h.page.$store.getters.token = 'replacement-token'
  const before = identity(h), storageBefore = clone([...h.entries])
  d.resolve({ data: { run: recordedRun('OLD_AUTH_RUN', 'A') } })
  await pending
  assert.deepEqual(identity(h), before)
  assert.deepEqual(clone([...h.entries]), storageBefore)
})

test('account202 cannot restore account101 form or pending key from same browser storage', async () => {
  const h = await harness()
  editBacktest(h, A)
  const request = await unknownSubmission(h)
  const second = await harness({ entries: h.entries, userId: 202 })
  assert.equal(second.page.pendingRunKey, null)
  assert.equal(second.page.pendingRunBody, null)
  assert.deepEqual(clone(second.page.bt.override), {})
  assert.notEqual(second.page.bt.initialCapital, A.initialCapital)
  await second.page.queryPendingKey()
  assert.equal(second.calls.some(call => call.method === 'findRunByKey' && call.key === request.key), false)
})
test('cached component account change cannot query or restore previous account request', async () => {
  const h = await harness()
  editBacktest(h, A)
  const request = await unknownSubmission(h)
  h.component.deactivated.call(h.page)
  changeAccount(h, 202)
  await h.component.activated.call(h.page)
  await h.page.queryPendingKey()
  assert.equal(h.calls.some(call => call.method === 'findRunByKey' && call.key === request.key), false)
  assert.equal(h.page.pendingRunKey, null)
  assert.equal(h.page.pendingRunBody, null)
  assert.notEqual(h.page.bt.override.ma_period, 90)
})
test('late result under a changed account cannot restore old pending/result state', async () => {
  const h = await harness(), d = deferred()
  h.api.startRun = () => d.promise
  const pending = h.page.startBacktest(false)
  changeAccount(h, 202)
  await h.page.loadBranch('B')
  const before = identity(h)
  d.resolve({ data: { run: recordedRun('R_ACCOUNT101_LATE', 'A') } })
  await pending
  assert.deepEqual(identity(h), before)
  assert.equal(h.page.pendingRunKey, null)
})
test('corrupt JSON local and pending caches fail safe without blocking branch load', async () => {
  const h = await harness()
  editBacktest(h, A)
  await unknownSubmission(h)
  assert.ok(h.entries.size > 0)
  const entries = new Map([...h.entries].map(([key]) => [key, '{invalid-json']))
  const second = await harness({ entries, bootstrap: false })
  await assert.doesNotReject(second.page.loadBranch('A'))
  assert.equal(second.page.branch.id, 'A')
  assert.equal(second.page.pendingRunKey, null)
  assert.equal(second.page.pendingRunBody, null)
  assert.deepEqual(clone(second.page.bt.override), {})
})
test('malformed pending body permits at most read-only key recovery and cannot resubmit', async () => {
  const h = await harness()
  const request = await unknownSubmission(h)
  const entries = new Map(h.entries)
  const pendingEntry = [...entries].find(([, value]) => value.includes(request.key))
  assert.ok(pendingEntry, 'pending request is durably recorded')
  const stored = JSON.parse(pendingEntry[1])
  // Preserve the actual envelope shape, corrupting only the recorded body.
  if (stored.body) stored.body = { strategyArtifactId: null, config: 'not-a-config' }
  else if (stored.pendingRunBody) stored.pendingRunBody = { strategyArtifactId: null, config: 'not-a-config' }
  else throw new Error('pending cache must retain a reviewable request body')
  entries.set(pendingEntry[0], JSON.stringify(stored))
  const second = await harness({ entries })
  assert.equal(second.page.pendingRunKey, request.key)
  assert.equal(second.page.pendingRunBody, null)
  second.page.pendingRunMissing = true
  if (second.page.resubmitPendingRun) await second.page.resubmitPendingRun()
  assert.equal(posts(second).length, 0)
  await second.page.queryPendingKey()
  assert.equal(posts(second).length, 0)
  for (const lookup of second.calls.filter(call => call.method === 'findRunByKey')) assert.equal(lookup.key, request.key)
})
test('legacy unscoped caches are not adopted by a logged-in account', async () => {
  const entries = new Map([
    ['rlv2.local.A.backtest', JSON.stringify(A)],
    ['rlv2.pending.A', JSON.stringify({ key: 'foreign-legacy-key', body: { strategyArtifactId: 'S_A', config: A } })],
    ['rlv2.reviewCtx', JSON.stringify({ A: 'FOREIGN_REVIEW' })]
  ])
  const h = await harness({ entries })
  assert.equal(h.page.pendingRunKey, null)
  assert.equal(h.page.bt.override.ma_period, undefined)
  assert.equal(!!(h.page.reviewCtxData && h.page.reviewCtxData.runId === 'FOREIGN_REVIEW'), false)
})
for (const failureStage of ['getBranch', 'listRuns']) {
  test(`failed B ${failureStage} cannot install B identity with A form or submit cross-branch`, async () => {
    const h = await harness()
    editBacktest(h, A)
    const original = form(h)
    const prior = h.api[failureStage]
    h.api[failureStage] = async (...args) => args[0] === 'B' ? Promise.reject(httpError(403, 'FORBIDDEN')) : prior(...args)
    await assert.doesNotReject(h.page.loadBranch('B'))
    assert.equal(h.page.branch && h.page.branch.id, 'A')
    assert.deepEqual(form(h), original)
    await h.page.startBacktest(false)
    assert.equal(posts(h).some(call => call.bid === 'B'), false)
    for (const call of posts(h)) assert.equal(call.body.strategyArtifactId, 'S_A')
  })
}
