// V1 gap A / gap B regression list (contract §6.1). Offline; node --test.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  diffParamOverrides,
  mergeDeclaredWithSaved,
  resolveIndicatorHandoffParams
} from '../../src/utils/researchParamHandoff.js'

const SPECS = [{ name: 'period', defaultValue: 20 }, { name: 'show_band', defaultValue: true }, { name: 'label', defaultValue: 'x' }]
const IND = readFileSync(new URL('../../src/views/indicator-ide/index.vue', import.meta.url), 'utf8')
const STR = readFileSync(new URL('../../src/views/strategy-ide/index.vue', import.meta.url), 'utf8')
const BT = readFileSync(new URL('../../src/views/backtest-center/index.vue', import.meta.url), 'utf8')

test('A1 declared 20, nothing applied -> hands off 20 labelled declared', () => {
  const r = resolveIndicatorHandoffParams(SPECS, {})
  assert.equal(r.effective.period, 20)
  assert.equal(r.sourceOf.period, 'declared')
})

test('A2 applied 60 -> hands off 60 labelled applied; page uses it for prompt, context and metadata', () => {
  const r = resolveIndicatorHandoffParams(SPECS, { period: 60 })
  assert.equal(r.effective.period, 60)
  assert.equal(r.sourceOf.period, 'applied')
  assert.match(IND, /const params = this\.resolveIndicatorHandoff\(code \|\| ''\)\.effective/)
  assert.match(IND, /params_source_of: handoffParams\.sourceOf/)
  assert.doesNotMatch(IND.split('buildIndicatorToStrategyPrompt ()')[1].split('handleCreateStrategyFromIndicator')[0], /parseIndicatorParamRaw/)
  assert.match(STR, /source_indicator_params_effective:/)
  assert.match(STR, /source_indicator_code_digest:/)
})

test('A3 applied 60, unapplied draft 90 -> draft reported, default still 60 (confirm dialog)', () => {
  const r = resolveIndicatorHandoffParams(SPECS, { period: 60 }, { period: 90 })
  assert.equal(r.effective.period, 60)
  assert.equal(r.draftDiffers, true)
  assert.deepEqual(r.draftDiffs, [{ name: 'period', applied: 60, draft: 90 }])
  assert.match(IND, /confirmIndicatorHandoffDraft/)
  assert.match(IND, /okText: '携带已应用参数'/)
})

test('A4 0 / false / "" are kept, never defaulted', () => {
  const r = resolveIndicatorHandoffParams(SPECS, { period: 0, show_band: false, label: '' })
  assert.deepEqual(r.effective, { period: 0, show_band: false, label: '' })
  assert.deepEqual(r.sourceOf, { period: 'applied', show_band: 'applied', label: 'applied' })
})

test('A5 hidden indicator: handoff still blocked before any params are sent (unchanged guard)', () => {
  assert.match(IND, /if \(this\.selectedIndicatorCodeHidden\) \{\s*this\.\$message\.warning\(this\.\$t\('indicatorIde\.aiConvertHiddenBlocked'\)\)/)
})

test('A6 "save as default" (localStorage) path unchanged', () => {
  assert.match(IND, /saveIndicatorParamDefaults \(\) \{[\s\S]*?this\.persistIndicatorParamDefaults\(\)/)
})

test('B1 template_params 60 -> backtest form shows 60 labelled saved', () => {
  const r = mergeDeclaredWithSaved([{ name: 'ma_period', default: 20 }], { ma_period: 60 })
  assert.deepEqual(r.params, { ma_period: 60 })
  assert.equal(r.sourceOf.ma_period, 'saved')
  assert.match(BT, /mergeDeclaredWithSaved\(nextParamDefinitions, savedParams\)/)
})

test('B2 unchanged run submits an empty override object', () => {
  assert.deepEqual(diffParamOverrides({ ma_period: 60 }, { ma_period: 60 }), {})
  assert.match(BT, /params: diffParamOverrides\(this\.params, this\.paramBaseline \|\| \{\}\)/)
})

test('B3 60 -> 50 submits only {ma_period: 50}', () => {
  assert.deepEqual(diffParamOverrides({ ma_period: 50, fast: 5 }, { ma_period: 60, fast: 5 }), { ma_period: 50 })
})

test('B4 history replay still uses defaults ⊕ run params (unchanged)', () => {
  assert.match(BT, /this\.params = \{ \.\.\.defaults, \.\.\.strategyRunParams\(run\) \}/)
})

test('B5 key missing from template_params -> declared default, not submitted as override', () => {
  const r = mergeDeclaredWithSaved([{ name: 'ma_period', default: 20 }, { name: 'fast', default: 5 }], { ma_period: 60 })
  assert.deepEqual(r.params, { ma_period: 60, fast: 5 })
  assert.equal(r.sourceOf.fast, 'declared')
  assert.deepEqual(diffParamOverrides(r.params, r.params), {})
})

test('B6 history detail is read from the stored run (no recompute path added)', () => {
  assert.doesNotMatch(BT, /diffParamOverrides\([^)]*strategyRunParams/)
})
