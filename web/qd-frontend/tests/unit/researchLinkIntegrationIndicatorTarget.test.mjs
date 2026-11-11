import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import * as researchLinkAI from '../../src/utils/researchLinkAI.js'
import { instantiateBacktestCenter } from './backtestCenterHarness.mjs'

const SFC = fileURLToPath(new URL('../../src/views/strategy-ide/index.vue', import.meta.url))
const KEY = 'qd_indicator_to_strategy_integration_1'
const SAVED = 'def saved_indicator(ctx): return ctx.close'
const DRAFT = 'def draft_indicator(ctx): return ctx.close * 2'

function storage () {
  const values = new Map()
  return {
    setItem (key, value) { values.set(key, String(value)) },
    getItem (key) { return values.get(key) ?? null },
    removeItem (key) { values.delete(key) }
  }
}

async function build (overrides = {}) {
  return instantiateBacktestCenter({
    sfcPath: SFC,
    real: { researchLinkAI },
    overrides: {
      supportsScriptSources: true,
      scriptCode: 'EXISTING STRATEGY DRAFT',
      indicatorConvertIndicators: [{
        indicatorId: '12',
        name: 'QA Indicator',
        code: SAVED,
        savedCodeDigest: researchLinkAI.temporaryCodeDigest(SAVED),
        codeHidden: false
      }],
      $route: {
        query: { convert: 'indicator', convert_required: '1', link: 'indicator', convert_key: KEY, source_indicator_id: '12' },
        params: {}
      },
      ...overrides
    }
  })
}

function handoff (overrides = {}) {
  return {
    handoff_contract: researchLinkAI.INDICATOR_HANDOFF_CONTRACT,
    indicatorId: 12,
    name: 'QA Indicator',
    code: DRAFT,
    code_digest: researchLinkAI.temporaryCodeDigest(DRAFT),
    saved_code_digest: researchLinkAI.temporaryCodeDigest(SAVED),
    is_draft: true,
    market: 'CNStock',
    symbol: '300476',
    ...overrides
  }
}

test('lost indicator handoff fails closed without replacing current strategy draft', async () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storage()
  try {
    const vm = await build()
    const accepted = await vm.applyIndicatorConvertRouteOnce()
    assert.equal(accepted, false)
    assert.equal(vm.scriptCode, 'EXISTING STRATEGY DRAFT')
    assert.equal(vm.showIndicatorConvertModal, false)
    assert.match(vm.decisionContextNotice, /载荷缺失|载荷不可读取/)
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('strict indicator marker takes precedence over an incidental saved source ID', async () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storage()
  try {
    const vm = await build({
      $route: {
        query: { convert_required: '1', link: 'indicator', sourceId: '7', source_indicator_id: '12' },
        params: {}
      }
    })
    vm.getIncomingResearchEntryKey = () => 'strict-indicator-missing-payload'
    let opened = 0
    vm.openSource = async () => { opened += 1; return true }
    const accepted = await vm.syncIncomingResearchEntry({ initial: true, sourcesReady: true })
    assert.equal(accepted, false)
    assert.equal(opened, 0, 'a failed indicator handoff must never open sourceId=7')
    assert.equal(vm.scriptCode, 'EXISTING STRATEGY DRAFT')
    assert.match(vm.decisionContextNotice, /指标交接载荷不可读取/)
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('indicator saved source change or 403 is not silently replaced with latest code', async () => {
  const previous = globalThis.sessionStorage
  const browser = storage()
  assert.equal(researchLinkAI.writeTemporaryIndicatorHandoff(browser, KEY, handoff()), true)
  globalThis.sessionStorage = browser
  try {
    const vm = await build({
      indicatorConvertIndicators: [{
        indicatorId: '12',
        code: 'CHANGED SAVED CODE',
        savedCodeDigest: researchLinkAI.temporaryCodeDigest('CHANGED SAVED CODE'),
        codeHidden: false
      }]
    })
    // Server refetch supersedes a stale cache. Its changed code must be rejected.
    vm.indicatorConvertIndicators = [{ indicatorId: '12', code: SAVED, savedCodeDigest: researchLinkAI.temporaryCodeDigest(SAVED), codeHidden: false }]
    vm.loadIndicatorOptions = async () => {
      vm.indicatorConvertIndicators = [{
        indicatorId: '12',
        code: 'CHANGED SAVED CODE',
        savedCodeDigest: researchLinkAI.temporaryCodeDigest('CHANGED SAVED CODE'),
        codeHidden: false
      }]
      return true
    }
    assert.equal(await vm.applyIndicatorConvertRouteOnce(), false)
    assert.match(vm.decisionContextNotice, /原始源码已变化/)
    assert.equal(vm.scriptCode, 'EXISTING STRATEGY DRAFT')
    assert.equal(vm.showIndicatorConvertModal, false)

    vm.indicatorConvertIndicators = []
    vm.loadIndicatorOptions = async () => {
      vm.indicatorConvertIndicators = []
      vm.indicatorConvertError = '403 Forbidden'
      return false
    }
    assert.equal(await vm.applyIndicatorConvertRouteOnce(), false)
    assert.match(vm.decisionContextNotice, /无法读取|无权读取/)
    assert.equal(vm.scriptCode, 'EXISTING STRATEGY DRAFT')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('valid unsaved indicator handoff is labelled temporary and awaits explicit conversion', async () => {
  const previous = globalThis.sessionStorage
  const browser = storage()
  assert.equal(researchLinkAI.writeTemporaryIndicatorHandoff(browser, KEY, handoff()), true)
  globalThis.sessionStorage = browser
  try {
    const vm = await build()
    vm.loadIndicatorOptions = async () => {
      vm.indicatorConvertIndicators = [{
        indicatorId: '12',
        name: 'QA Indicator',
        code: SAVED,
        savedCodeDigest: researchLinkAI.temporaryCodeDigest(SAVED),
        codeHidden: false
      }]
      return true
    }
    assert.equal(await vm.applyIndicatorConvertRouteOnce(), true)
    assert.equal(vm.indicatorConvertContext.code, DRAFT)
    assert.equal(vm.indicatorConvertContext.isDraft, true)
    assert.match(vm.decisionContextNotice, /未保存草稿/)
    assert.equal(vm.showIndicatorConvertModal, true)
    assert.equal(vm.scriptCode, 'EXISTING STRATEGY DRAFT', 'viewing handoff alone does not edit strategy source')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})
