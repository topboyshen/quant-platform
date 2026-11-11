import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import * as researchLinkAI from '../../src/utils/researchLinkAI.js'
import { deferred, instantiateBacktestCenter } from './backtestCenterHarness.mjs'

const originalSfcPath = fileURLToPath(new URL('../../src/views/strategy-ide/index.vue', import.meta.url))
const source = fs.readFileSync(originalSfcPath, 'utf8')
assert.match(source, /v-if="supportsScriptSources && showIndicatorConvertModal && isActiveStrategyRoute"/)
const modalCalls = []
globalThis.__researchStrategyModal = { confirm (options) { modalCalls.push(options) } }
const harnessDir = fs.mkdtempSync(path.join(os.tmpdir(), 'qd-strategy-modal-'))
const sfcPath = path.join(harnessDir, 'strategy.vue')
fs.writeFileSync(sfcPath, source.replace(
  /^import \{ Modal \} from 'ant-design-vue'$/m,
  'const Modal = globalThis.__researchStrategyModal'
))
process.on('exit', () => {
  try { fs.rmSync(harnessDir, { recursive: true, force: true }) } catch (_) {}
  delete globalThis.__researchStrategyModal
})
const key = 'qd_indicator_to_strategy_modal_guard_1'
const savedCode = 'def saved_indicator(ctx): return ctx.close'
const draftCode = 'def draft_indicator(ctx): return ctx.close * 2'

function browserStorage () {
  const values = new Map()
  return {
    setItem (name, value) { values.set(name, String(value)) },
    getItem (name) { return values.get(name) ?? null },
    removeItem (name) { values.delete(name) }
  }
}

function validPayload () {
  return {
    handoff_contract: researchLinkAI.INDICATOR_HANDOFF_CONTRACT,
    indicatorId: 12,
    name: 'QA Indicator',
    code: draftCode,
    code_digest: researchLinkAI.temporaryCodeDigest(draftCode),
    saved_code_digest: researchLinkAI.temporaryCodeDigest(savedCode),
    is_draft: true,
    market: 'CNStock',
    symbol: '300476'
  }
}

async function build (api = {}, overrides = {}) {
  return instantiateBacktestCenter({
    sfcPath,
    real: { researchLinkAI },
    api,
    overrides: {
      supportsScriptSources: true,
      scriptCode: 'KEEP CURRENT STRATEGY DRAFT',
      $route: {
        path: '/strategy-ide',
        query: { convert: 'indicator', convert_required: '1', link: 'indicator', convert_key: key, source_indicator_id: '12' },
        params: {}
      },
      ...overrides
    }
  })
}

test('leaving a cached strategy page closes its portal modal and invalidates pending handoff identity', async () => {
  const vm = await build({}, {
    showIndicatorConvertModal: true,
    incomingResearchEntrySeq: 7,
    incomingResearchEntryKey: 'old-handoff'
  })
  vm.deactivated()
  assert.equal(vm.showIndicatorConvertModal, false)
  assert.equal(vm.incomingResearchEntrySeq, 8)
  assert.equal(vm.incomingResearchEntryKey, '')
  assert.equal(vm.scriptCode, 'KEEP CURRENT STRATEGY DRAFT')
  vm.showIndicatorConvertModal = true
  vm.beforeDestroy()
  assert.equal(vm.showIndicatorConvertModal, false)
  assert.equal(vm.incomingResearchEntrySeq, 9)
})

test('beforeRouteLeave unmounts the portal and flushes Vue before next navigation', async () => {
  const vm = await build({}, {
    showIndicatorConvertModal: true,
    incomingResearchEntryKey: 'open-handoff'
  })
  const flush = deferred()
  const events = []
  vm.hasPendingStrategyWork = () => false
  vm.$nextTick = () => flush.promise
  const leaving = vm.beforeRouteLeave({ path: '/indicator-ide' }, vm.$route, () => events.push('next'))
  assert.equal(vm.showIndicatorConvertModal, false)
  assert.equal(vm.incomingResearchEntryKey, '')
  assert.deepEqual(events, [], 'router next waits for the portal-unmount flush')
  flush.resolve()
  await leaving
  assert.deepEqual(events, ['next'])
})

test('canceling dirty route leave keeps both the draft and conversion modal intact', async () => {
  modalCalls.length = 0
  const vm = await build({}, {
    showIndicatorConvertModal: true,
    incomingResearchEntryKey: 'open-handoff',
    scriptCode: 'UNSAVED STRATEGY DRAFT'
  })
  const events = []
  vm.hasPendingStrategyWork = () => true
  await vm.beforeRouteLeave({ path: '/indicator-ide' }, vm.$route, value => events.push(value))
  assert.equal(modalCalls.length, 1)
  assert.equal(vm.showIndicatorConvertModal, true, 'do not close before the user decides')
  assert.equal(vm.incomingResearchEntryKey, 'open-handoff')
  modalCalls[0].onCancel()
  assert.deepEqual(events, [false])
  assert.equal(vm.showIndicatorConvertModal, true)
  assert.equal(vm.scriptCode, 'UNSAVED STRATEGY DRAFT')
  assert.equal(vm.incomingResearchEntryKey, 'open-handoff')

  // The affirmative branch must use the same portal flush as clean leave.
  const flush = deferred()
  vm.$nextTick = () => flush.promise
  const leaving = modalCalls[0].onOk()
  assert.equal(vm.showIndicatorConvertModal, false)
  assert.deepEqual(events, [false])
  flush.resolve()
  await leaving
  assert.deepEqual(events, [false, undefined])
})

test('late indicator source response cannot reopen conversion modal after leaving', async () => {
  const previous = globalThis.sessionStorage
  const storage = browserStorage()
  assert.equal(researchLinkAI.writeTemporaryIndicatorHandoff(storage, key, validPayload()), true)
  globalThis.sessionStorage = storage
  try {
    const pending = deferred()
    const vm = await build()
    vm.getIncomingResearchEntryKey = () => 'pending-handoff'
    vm.loadIndicatorOptions = () => pending.promise
    const reading = vm.syncIncomingResearchEntry({ sourcesReady: true })
    await Promise.resolve()
    vm.deactivated()
    pending.resolve(true)
    assert.equal(await reading, false)
    assert.equal(vm.showIndicatorConvertModal, false)
    assert.equal(vm.indicatorConvertContext, null)
    assert.equal(vm.incomingResearchEntryKey, '')
    assert.equal(vm.scriptCode, 'KEEP CURRENT STRATEGY DRAFT')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('non-strategy route rejects source sync and a late read without reopening the portal', async () => {
  const previous = globalThis.sessionStorage
  const storage = browserStorage()
  assert.equal(researchLinkAI.writeTemporaryIndicatorHandoff(storage, key, validPayload()), true)
  globalThis.sessionStorage = storage
  try {
    const vm = await build()
    let sourcesLoaded = 0
    vm.loadSources = async () => { sourcesLoaded += 1 }
    vm.$route.path = '/indicator-ide'
    assert.equal(await vm.syncIncomingResearchEntry(), false)
    assert.equal(await vm.applyIndicatorConvertRouteOnce(), false)
    assert.equal(sourcesLoaded, 0)
    assert.equal(vm.showIndicatorConvertModal, false)

    vm.$route.path = '/strategy-ide'
    const pending = deferred()
    vm.loadIndicatorOptions = () => pending.promise
    const reading = vm.applyIndicatorConvertRouteOnce()
    await Promise.resolve()
    vm.$route.path = '/indicator-ide'
    pending.resolve(true)
    assert.equal(await reading, false)
    assert.equal(vm.showIndicatorConvertModal, false)
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('conversion confirmation cannot generate or save from another page or an invisible modal', async () => {
  let generated = 0
  let saved = 0
  const vm = await build({
    aiGenerateStrategy: async () => { generated += 1; return { data: { code: 'new code' } } },
    createScriptSource: async () => { saved += 1; return { code: 1, data: { id: 99 } } }
  }, {
    indicatorConvertContext: validPayload(),
    showIndicatorConvertModal: true,
    $route: { path: '/indicator-ide', query: {}, params: {} }
  })
  await vm.confirmIndicatorToStrategy()
  assert.equal(generated, 0)
  assert.equal(saved, 0)
  vm.$route = { path: '/strategy-ide', query: {}, params: {} }
  vm.showIndicatorConvertModal = false
  await vm.confirmIndicatorToStrategy()
  assert.equal(generated, 0)
  assert.equal(saved, 0)
  assert.equal(vm.scriptCode, 'KEEP CURRENT STRATEGY DRAFT')
})

test('visible modal on strategy page still converts only after explicit confirmation', async () => {
  let generated = 0
  let saved = 0
  const vm = await build({
    aiGenerateStrategy: async () => { generated += 1; return { data: { code: 'def on_bar(ctx): return None' } } },
    createScriptSource: async () => { saved += 1; return { code: 1, data: { id: 99 } } }
  }, {
    indicatorConvertContext: validPayload(),
    showIndicatorConvertModal: true
  })
  vm.buildIndicatorConversionPrompt = () => 'explicit conversion request'
  vm.extractScriptMetadataFromCode = () => ({ name: 'QA converted strategy', description: 'QA' })
  vm.buildGeneratedMetadata = () => ({ source_indicator_id: 12 })
  vm.loadSources = async () => true
  vm.getScriptSourceId = () => 99
  vm.openSource = async () => true
  vm.finishIndicatorConversion = () => { vm.showIndicatorConvertModal = false }
  await vm.confirmIndicatorToStrategy()
  assert.equal(generated, 1)
  assert.equal(saved, 1)
  assert.equal(vm.showIndicatorConvertModal, false)
})
