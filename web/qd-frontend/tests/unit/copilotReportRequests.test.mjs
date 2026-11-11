import assert from 'node:assert/strict'
import test from 'node:test'

import {
  addCopilotReportRequestId,
  copilotTaskOwnsReportRequest,
  createCopilotReportRequest,
  isCurrentCopilotReportRequest,
  removeCopilotReportRequestId,
  sameCopilotReportTarget,
  settleCopilotReportRequest
} from '../../src/utils/copilotReportRequests.js'

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

const bindRequest = (sequence, message, target, kind = 'analysis') => {
  const request = createCopilotReportRequest(sequence, target, message.localId, kind)
  message.reportRequestId = request.id
  return request
}

const runRequest = (state, message, request, operation) => settleCopilotReportRequest({
  request,
  execute: () => operation.promise,
  isCurrent: () => isCurrentCopilotReportRequest(message, request),
  onSuccess: result => {
    message.report = result
    message.reportError = ''
    if (
      sameCopilotReportTarget(state.selectedAsset, request.target) &&
      sameCopilotReportTarget(state.decisionContext, request.target)
    ) {
      state.selectedAsset = request.target
      state.decisionContext = request.target
    }
  },
  onFailure: error => {
    message.reportError = error.message
  },
  onSettled: () => {
    if (copilotTaskOwnsReportRequest(state.pendingTask, request)) state.pendingTask = null
    if (state.activeRequestId === request.id) {
      state.activeRequestId = 0
      state.sending = false
    }
  }
})

test('old successful analysis cannot replace or finish the newer symbol task', async () => {
  const a = { market: 'CNStock', symbol: '603099', name: '长白山' }
  const b = { market: 'CNStock', symbol: '600519', name: '贵州茅台' }
  const messageA = { localId: 'report-a' }
  const messageB = { localId: 'report-b' }
  const requestA = bindRequest(0, messageA, a)
  const operationA = deferred()
  const state = {
    activeRequestId: requestA.id,
    pendingTask: { type: 'market_diagnosis', target: a, reportRequestId: requestA.id },
    selectedAsset: a,
    decisionContext: a,
    sending: true
  }
  const runningA = runRequest(state, messageA, requestA, operationA)

  const requestB = bindRequest(requestA.id, messageB, b)
  const operationB = deferred()
  state.activeRequestId = requestB.id
  state.pendingTask = { type: 'market_diagnosis', target: b, reportRequestId: requestB.id }
  state.selectedAsset = b
  state.decisionContext = b
  const runningB = runRequest(state, messageB, requestB, operationB)

  operationA.resolve({ decision: 'BUY', symbol: a.symbol })
  assert.equal((await runningA).status, 'success')
  assert.equal(messageA.report.symbol, a.symbol)
  assert.equal(state.selectedAsset.symbol, b.symbol)
  assert.equal(state.decisionContext.symbol, b.symbol)
  assert.equal(state.pendingTask.reportRequestId, requestB.id)
  assert.equal(state.sending, true)

  operationB.resolve({ decision: 'HOLD', symbol: b.symbol })
  assert.equal((await runningB).status, 'success')
  assert.equal(state.pendingTask, null)
  assert.equal(state.sending, false)
})

test('old failed analysis cannot clear or release the newer symbol task', async () => {
  const a = { market: 'CNStock', symbol: '603099', name: '长白山' }
  const b = { market: 'CNStock', symbol: '600519', name: '贵州茅台' }
  const messageA = { localId: 'failure-a' }
  const requestA = bindRequest(0, messageA, a)
  const operationA = deferred()
  const state = {
    activeRequestId: requestA.id + 1,
    pendingTask: { type: 'market_diagnosis', target: b, reportRequestId: requestA.id + 1 },
    selectedAsset: b,
    decisionContext: b,
    sending: true
  }
  const runningA = runRequest(state, messageA, requestA, operationA)

  operationA.reject(new Error('A failed late'))
  assert.equal((await runningA).status, 'failure')
  assert.equal(messageA.reportError, 'A failed late')
  assert.equal(state.pendingTask.target.symbol, b.symbol)
  assert.equal(state.activeRequestId, requestA.id + 1)
  assert.equal(state.sending, true)
})

test('retry owns the report message and ignores the superseded response', async () => {
  const target = { market: 'CNStock', symbol: '603099', name: '长白山' }
  const message = { localId: 'retry-report' }
  const initial = bindRequest(0, message, target)
  const initialOperation = deferred()
  const state = {
    activeRequestId: initial.id,
    pendingTask: { type: 'market_diagnosis', target, reportRequestId: initial.id },
    selectedAsset: target,
    decisionContext: target,
    sending: true
  }
  const initialRun = runRequest(state, message, initial, initialOperation)

  const retry = bindRequest(initial.id, message, target, 'retry')
  const retryOperation = deferred()
  state.activeRequestId = retry.id
  state.pendingTask = { ...state.pendingTask, reportRequestId: retry.id }
  const retryRun = runRequest(state, message, retry, retryOperation)

  retryOperation.resolve({ revision: 'retry', symbol: target.symbol })
  assert.equal((await retryRun).status, 'success')
  assert.equal(message.report.revision, 'retry')

  initialOperation.reject(new Error('superseded failure'))
  assert.equal((await initialRun).status, 'stale')
  assert.equal(message.report.revision, 'retry')
  assert.equal(message.reportError, '')
})

test('finishing the newest request cannot unlock while an older independent request is running', () => {
  const b = createCopilotReportRequest(1, { market: 'CNStock', symbol: '600519' }, 'report-b')
  const retryA = createCopilotReportRequest(b.id, { market: 'CNStock', symbol: '603099' }, 'report-a', 'retry')
  let inFlight = addCopilotReportRequestId([], b)
  inFlight = addCopilotReportRequestId(inFlight, retryA)
  assert.deepEqual(inFlight, [b.id, retryA.id])

  inFlight = removeCopilotReportRequestId(inFlight, retryA)
  assert.deepEqual(inFlight, [b.id])
  assert.equal(inFlight.length > 0, true)

  inFlight = removeCopilotReportRequestId(inFlight, b)
  assert.deepEqual(inFlight, [])
})
