import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import * as researchLinkAI from '../../src/utils/researchLinkAI.js'
import { instantiateBacktestCenter } from './backtestCenterHarness.mjs'

const indicatorPath = fileURLToPath(new URL('../../src/views/indicator-ide/index.vue', import.meta.url))
const source = fs.readFileSync(indicatorPath, 'utf8')
// The shared component harness injects JavaScript imports. CSS and CodeMirror
// mode side-effect imports have no bearing on these component method tests.
const harnessSource = source.replace(/^import ['"][^'"\n]+['"]\s*$/gm, '')
const harnessDir = fs.mkdtempSync(path.join(os.tmpdir(), 'qd-indicator-chain-'))
const harnessSfc = path.join(harnessDir, 'indicator.vue')
fs.writeFileSync(harnessSfc, harnessSource)
process.on('exit', () => { try { fs.rmSync(harnessDir, { recursive: true, force: true }) } catch (_) {} })

function storage () {
  const values = new Map()
  return {
    setItem (key, value) { values.set(key, String(value)) },
    getItem (key) { return values.get(key) ?? null },
    removeItem (key) { values.delete(key) }
  }
}

const first = { id: 1, name: 'Old saved indicator', code: 'old_saved_code' }
const second = { id: 2, name: 'Other saved indicator', code: 'second_saved_code' }

async function build (query = {}, overrides = {}) {
  const pushes = []
  const vm = await instantiateBacktestCenter({
    sfcPath: harnessSfc,
    real: { researchLinkAI },
    overrides: {
      indicators: [first, second],
      selectedIndicatorId: 1,
      market: 'CNStock',
      symbol: '300476',
      currentCode: 'old_saved_code',
      userId: 1,
      $route: { query: { link: 'ai', handoff: 'H1', market: 'CNStock', symbol: '300476', ...query }, fullPath: '/indicator-ide?link=ai&handoff=H1' },
      $router: { push (route) { pushes.push(route); return Promise.resolve() } },
      ...overrides
    }
  })
  vm.__pushes = pushes
  return vm
}

test('AI URL indicator_id cannot auto-select during list load or authorize strategy handoff', async () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storage()
  try {
    const vm = await build({ indicator_id: '2' }, { currentCode: 'unsaved old draft', codeDirty: true })
    assert.equal(vm.applyIndicatorRouteSelection(), false)
    await vm.loadIndicators() // exercises the unconditional finally path
    assert.equal(vm.selectedIndicatorId, 1)
    assert.equal(vm.currentCode, 'unsaved old draft')
    assert.equal(vm.codeDirty, true)
    assert.equal(vm.hasConfirmedAIIndicatorSelection, false)
    vm.handleCreateStrategyFromIndicator()
    assert.equal(vm.__pushes.length, 0)
    assert.equal(globalThis.sessionStorage.getItem('qd_indicator_to_strategy_'), null)
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('old keep-alive selection is hidden until an explicit click for this AI handoff', async () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storage()
  try {
    const vm = await build()
    vm.resolveIncomingIndicatorDecisionContext = () => ({ artifact: false, context: { market: 'CNStock', symbol: '300476' } })
    vm.getIncomingResearchEntryKey = () => 'object-only'
    vm.applyDecisionContext = () => true
    vm.applyCopilotDraft = () => false
    vm.syncIncomingResearchEntry({ force: true })
    assert.equal(vm.selectedIndicatorId, 1, 'old selection remains in the editor')
    assert.equal(vm.indicatorResearchArtifactLabel, '本次尚未选择指标')
    assert.equal(vm.hasConfirmedAIIndicatorSelection, false)
    vm.selectEditorIndicator(1) // actual user click, even on the same row
    assert.equal(vm.hasConfirmedAIIndicatorSelection, true)
    assert.equal(vm.indicatorResearchArtifactLabel, 'Old saved indicator')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('confirmation is bound to handoff, indicator ID and object, and invalidates across navigation', async () => {
  const vm = await build()
  vm.confirmCurrentIndicatorForAI()
  assert.equal(vm.hasConfirmedAIIndicatorSelection, true)
  vm.selectedIndicatorId = 2 // save-as/delete/ordinary selection must not inherit #1 approval
  assert.equal(vm.hasConfirmedAIIndicatorSelection, false)
  vm.selectedIndicatorId = 1
  vm.$route = { query: { link: 'ai', handoff: 'H1', market: 'CNStock', symbol: '600519' }, fullPath: '/indicator-ide?symbol=600519' }
  assert.equal(vm.hasConfirmedAIIndicatorSelection, false, 'same handoff with a different object is not approved')
  vm.$route = { query: { market: 'CNStock', symbol: '300476' }, fullPath: '/indicator-ide' }
  vm.resolveIncomingIndicatorDecisionContext = () => ({ artifact: false, context: { market: 'CNStock', symbol: '300476' } })
  vm.getIncomingResearchEntryKey = () => 'ordinary-navigation'
  vm.applyDecisionContext = () => true
  vm.applyCopilotDraft = () => false
  vm.syncIncomingResearchEntry()
  vm.selectedIndicatorId = 2
  vm.$route = { query: { link: 'ai', handoff: 'H1', market: 'CNStock', symbol: '300476' }, fullPath: '/indicator-ide?link=ai&handoff=H1' }
  assert.equal(vm.hasConfirmedAIIndicatorSelection, false, 'browser Back cannot reuse a prior approval')
})

test('AI draft cannot overwrite a retained old indicator or dirty code without an explicit new-draft action', async () => {
  const previous = globalThis.sessionStorage
  const browser = storage()
  browser.setItem('qd_copilot_indicator_code', 'new_ai_code')
  globalThis.sessionStorage = browser
  try {
    const vm = await build({ aiDraft: '1' }, { currentCode: 'unsaved old draft', codeDirty: true })
    vm.resolveIncomingIndicatorDecisionContext = () => ({ artifact: true, context: { market: 'CNStock', symbol: '300476' } })
    vm.getIncomingResearchEntryKey = () => 'ai-draft'
    assert.equal(vm.syncIncomingResearchEntry({ force: true }), false)
    assert.equal(vm.selectedIndicatorId, 1)
    assert.equal(vm.currentCode, 'unsaved old draft')
    assert.equal(browser.getItem('qd_copilot_indicator_code'), 'new_ai_code')
    assert.equal(vm.acceptIncomingAIDraftAsNewIndicator(), false)
    assert.equal(vm.selectedIndicatorId, 1)
    assert.equal(vm.currentCode, 'unsaved old draft')
    vm.confirmCurrentIndicatorForAI()
    assert.equal(vm.hasConfirmedAIIndicatorSelection, false, 'pending AI code cannot be attributed to the old saved ID')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('AI object-only arrival never consumes a stale code or prompt key', async () => {
  const previous = globalThis.sessionStorage
  const browser = storage()
  browser.setItem('qd_copilot_indicator_code', 'stale_code_from_other_report')
  browser.setItem('qd_copilot_indicator_prompt', 'stale_prompt_from_other_report')
  globalThis.sessionStorage = browser
  try {
    const vm = await build()
    assert.equal(vm.hasIncomingIndicatorArtifact(), false)
    assert.equal(vm.applyCopilotDraft(), false)
    assert.equal(vm.currentCode, 'old_saved_code')
    assert.equal(vm.aiPrompt, '')
    assert.equal(browser.getItem('qd_copilot_indicator_code'), 'stale_code_from_other_report')
    assert.equal(browser.getItem('qd_copilot_indicator_prompt'), 'stale_prompt_from_other_report')
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})
