import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const builderPath = fileURLToPath(
  new URL('../../src/views/executor-strategies/index.vue', import.meta.url)
)
const strategyIdePath = fileURLToPath(
  new URL('../../src/views/strategy-ide/index.vue', import.meta.url)
)
const builderSource = fs.readFileSync(builderPath, 'utf8')
const strategyIdeSource = fs.readFileSync(strategyIdePath, 'utf8')

test('embedded robot builder keeps the workspace focused on template inputs and preview', () => {
  assert.doesNotMatch(builderSource, /data-testid="robot-builder-progress"/)
  assert.doesNotMatch(builderSource, /data-testid="robot-engine-compatibility"/)
  assert.doesNotMatch(builderSource, /class="executor-notes"/)
  assert.match(builderSource, /v-model="form\.dynamic_anchor"/)
  assert.match(builderSource, /executorStrategies\.dynamicAnchorHint/)
})

test('robot generation remains visible and requires a valid engine payload', () => {
  assert.match(builderSource, /class="config-actions__status"/)
  assert.match(builderSource, /:disabled="!canCreate"/)
  assert.match(builderSource, /validationIssues \(\) \{/)
  assert.match(builderSource, /value="neutral"[\s\S]*?disabled/)
  assert.match(builderSource, /\.is-embedded \.executor-workbench \{[\s\S]*?height: calc\(100% - 64px\)/)
})

test('robot configuration keeps high-frequency choices in compact horizontal controls', () => {
  assert.match(builderSource, /class="market-compact-grid"/)
  assert.match(builderSource, /v-model="form\.side" class="compact-segmented"/)
  assert.match(builderSource, /v-model="form\.market_type"[\s\S]*?class="compact-segmented"/)
  assert.match(builderSource, /v-model="form\.grid_mode" class="compact-segmented"/)
  assert.match(builderSource, /\.compact-segmented[\s\S]*?display: flex[\s\S]*?flex-wrap: nowrap/)
  assert.match(builderSource, /grid-template-columns: minmax\(430px, 480px\) minmax\(0, 1fr\)/)
})

test('strategy research preserves template metadata without requiring the retired robot route', () => {
  assert.match(strategyIdeSource, /handleTemplateChange \(payload\)[\s\S]*?this\.scriptTemplateKey = \(payload && payload\.key\) \|\| ''/)
  assert.match(strategyIdeSource, /this\.scriptParamSchema = payload && payload\.param_schema/)
  assert.match(strategyIdeSource, /buildPayload \(\)[\s\S]*?template_key: this\.scriptTemplateKey/)
  assert.match(strategyIdeSource, /param_schema: this\.currentSourceParamSchema/)
  assert.match(strategyIdeSource, /script_template_params: \{ \.\.\.this\.scriptTemplateParams \}/)
})

test('template configuration excludes run capital and leverage', () => {
  assert.doesNotMatch(builderSource, /v-model="form\.initial_capital"/)
  assert.doesNotMatch(builderSource, /v-model="form\.leverage"/)
  assert.match(builderSource, /delete templateConfig\.initial_capital/)
  assert.match(builderSource, /delete templateConfig\.leverage/)
  assert.match(builderSource, /fmtWeight \(value\)/)
})
