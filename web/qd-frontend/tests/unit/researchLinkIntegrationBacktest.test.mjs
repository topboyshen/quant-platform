import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import * as historyState from '../../src/views/backtest-center/historyState.js'
import * as contracts from '../../src/utils/strategyResearchContracts.mjs'
import * as researchLinkRun from '../../src/utils/researchLinkRun.js'
import { compiled as syntheticCompiled, sources as syntheticSources } from '../../qa/research-link/fixtures.mjs'
import { instantiateBacktestCenter, deferred } from './backtestCenterHarness.mjs'

const SFC = fileURLToPath(new URL('../../src/views/backtest-center/index.vue', import.meta.url))

async function build (api, overrides = {}) {
  return instantiateBacktestCenter({
    sfcPath: SFC,
    api,
    real: { historyState, contracts, researchLinkRun },
    overrides: {
      decisionContextReady: true,
      sourceListLoadError: '',
      sources: [{ id: 7, symbol: 'A' }, { id: 8, symbol: 'A' }],
      runnableSources: [{ id: 7, symbol: 'A' }, { id: 8, symbol: 'A' }],
      form: { sourceId: 7, symbol: 'A', initialCapital: 10000 },
      params: {},
      $route: { query: {}, params: {} },
      ...overrides
    }
  })
}

test('same-symbol AI handoff does not silently select the first strategy or reuse old source', async () => {
  let compiled = 0
  const vm = await build({ compileScriptSource: async () => { compiled += 1; throw new Error('must not compile') } }, {
    $route: { query: { market: 'CNStock', symbol: 'A', source_selection: 'required', handoff: 'ai-1' }, params: {} },
    source: { id: 7, symbol: 'A' },
    manifest: { strategyType: 'cta' }
  })
  const accepted = await vm.syncIncomingResearchEntry({ initial: true, sourcesReady: true })
  assert.equal(accepted, false)
  assert.equal(vm.sourceSelectionRequired, true)
  assert.match(vm.sourceLoadError, /手动选择已保存策略/)
  assert.equal(vm.form.sourceId, 7, 'existing draft remains untouched, but cannot be treated as newly selected')
  assert.equal(compiled, 0)
})

test('linked strategy-to-backtest handoff stops when compiled code or contract changed', async () => {
  const nonce = 'linked-abcdef1234'
  const storage = {
    values: new Map(),
    setItem (key, value) { this.values.set(key, String(value)) },
    getItem (key) { return this.values.get(key) ?? null }
  }
  const pin = researchLinkRun.sourceHandoffIdentity(7, 'a'.repeat(64), 'b'.repeat(64))
  assert.equal(researchLinkRun.writeSourceHandoff(storage, nonce, pin), true)
  const prior = globalThis.sessionStorage
  globalThis.sessionStorage = storage
  try {
    const vm = await build({
      compileScriptSource: async () => ({ data: { sourceRevision: { codeHash: 'c'.repeat(64) }, contractHash: 'b'.repeat(64) } })
    }, {
      $route: { query: { sourceId: '7', handoff: nonce, link: 'strategy-source' }, params: {} },
      incomingResearchEntryKey: `source:7:${nonce}`,
      source: { id: 7, symbol: 'A' },
      manifest: { strategyType: 'cta' }
    })
    const accepted = await vm.syncIncomingResearchEntry({ initial: true, sourcesReady: true })
    assert.equal(accepted, false)
    assert.equal(vm.sourceSelectionRequired, true)
    assert.match(vm.sourceLoadError, /交接后已变化|不一致/)
    assert.equal(vm.form.sourceId, 7, 'old draft is kept, not silently refreshed to current source')
  } finally {
    if (prior === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = prior
  }
})

test('linked strategy 403 is reported as permission refusal, not a version mismatch or alternate selection', async () => {
  const nonce = 'linked-403-abcdef1234'
  const storage = {
    values: new Map(),
    setItem (key, value) { this.values.set(key, String(value)) },
    getItem (key) { return this.values.get(key) ?? null }
  }
  assert.equal(researchLinkRun.writeSourceHandoff(storage, nonce, {
    sourceId: 7, codeHash: 'a'.repeat(64), contractHash: 'b'.repeat(64)
  }), true)
  const prior = globalThis.sessionStorage
  globalThis.sessionStorage = storage
  try {
    const vm = await build({
      compileScriptSource: async () => {
        throw Object.assign(new Error('Synthetic permission denied'), { response: { status: 403 } })
      }
    }, {
      $route: { query: { sourceId: '7', handoff: nonce, link: 'strategy-source' }, params: {} },
      incomingResearchEntryKey: `source:7:${nonce}`,
      source: { id: 7, symbol: 'A' },
      manifest: { strategyType: 'cta' }
    })
    const accepted = await vm.syncIncomingResearchEntry({ initial: true, sourcesReady: true })
    assert.equal(accepted, false)
    assert.equal(vm.sourceSelectionRequired, true)
    assert.match(vm.sourceLoadError, /无权读取/)
    assert.equal(vm.form.sourceId, 7)
  } finally {
    if (prior === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = prior
  }
})

test('detail and compile timestamps straddling a source edit cannot satisfy a pinned handoff', async () => {
  const source = syntheticSources[0]
  const receipt = syntheticCompiled(source)
  const pin = researchLinkRun.sourceHandoffIdentity(source.id, receipt.sourceRevision.codeHash, receipt.contractHash)
  const vm = await build({
    getScriptSourceDetail: async () => ({ data: { ...source, updated_at: '2026-09-24T03:00:01Z' } }),
    compileScriptSource: async () => ({ data: receipt })
  }, { sources: [source] })
  const accepted = await vm.selectSource(source.id, { expectedIdentity: pin })
  assert.equal(accepted, false)
  assert.match(vm.sourceLoadError, /并非同一来源修订/)
  assert.equal(vm.manifest, null)
})

test('a failed shared-context write cannot partially commit a linked source or parameters', async () => {
  const nextSource = syntheticSources[0]
  const receipt = syntheticCompiled(nextSource)
  const pin = researchLinkRun.sourceHandoffIdentity(nextSource.id, receipt.sourceRevision.codeHash, receipt.contractHash)
  const originalSource = { id: 8, symbol: 'OLD-DRAFT' }
  const originalManifest = { strategyType: 'cta', marker: 'old-manifest' }
  const originalParams = { keepDraft: 21 }
  const vm = await build({
    getScriptSourceDetail: async () => ({ data: nextSource }),
    compileScriptSource: async () => ({ data: receipt })
  }, {
    sources: [nextSource],
    source: originalSource,
    manifest: originalManifest,
    params: originalParams,
    form: { sourceId: 8, symbol: 'OLD-DRAFT', initialCapital: 12345 }
  })
  vm.syncDecisionContextFromSource = () => null
  const accepted = await vm.selectSource(nextSource.id, { expectedIdentity: pin })
  assert.equal(accepted, false)
  assert.equal(vm.source, originalSource)
  assert.equal(vm.manifest, originalManifest)
  assert.equal(vm.params, originalParams)
  assert.equal(vm.form.sourceId, 8)
  assert.equal(vm.linkedSourcePin, null)
})

test('history detail rejects a response with another run ID instead of borrowing its source snapshot', async () => {
  const vm = await build({
    getStrategyBacktestRun: async () => ({
      data: { id: 999, symbol: 'WRONG', sourceMeta: { sourceId: 8 }, result: { total_return: 0.9 } }
    })
  })
  const accepted = await vm.openRun({ id: 201, symbol: 'REQUESTED' })
  assert.equal(accepted, false)
  assert.match(vm.historyDetailError, /运行 ID.*不一致/)
  assert.equal(vm.result, null)
  assert.notEqual(vm.historySourceIdentity.sourceId, '8')
})

test('an older run detail response cannot replace the last-selected run source identity', async () => {
  const first = deferred()
  const second = deferred()
  const pending = [first, second]
  let index = 0
  const vm = await build({ getStrategyBacktestRun: () => pending[index++].promise })
  const a = vm.openRun({ id: 201, symbol: 'A' })
  const b = vm.openRun({ id: 202, symbol: 'B' })
  second.resolve({ data: { id: 202, symbol: 'B', result: { total_return: 0.2 }, sourceMeta: { sourceId: 8, codeHash: 'b'.repeat(64), contractHash: 'c'.repeat(64) } } })
  await b
  first.resolve({ data: { id: 201, symbol: 'A', result: { total_return: 0.9 }, sourceMeta: { sourceId: 7, codeHash: 'a'.repeat(64), contractHash: 'd'.repeat(64) } } })
  await a
  assert.equal(vm.selectedRun.id, 202)
  assert.equal(vm.historySourceIdentity.sourceId, '8')
  assert.equal(vm.historySourceIdentity.codeHash, 'b'.repeat(64))
  assert.equal(vm.result.total_return, 0.2)
})
