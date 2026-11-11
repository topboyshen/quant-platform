import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const componentPath = fileURLToPath(
  new URL('../../src/views/backtest-center/index.vue', import.meta.url)
)
const source = fs.readFileSync(componentPath, 'utf8')
const portfolioResultPath = fileURLToPath(
  new URL('../../src/views/backtest-center/PortfolioResult.vue', import.meta.url)
)
const portfolioResultSource = fs.readFileSync(portfolioResultPath, 'utf8')

test('backtest center compiles a source manifest before accepting runtime controls', () => {
  assert.match(source, /compileScriptSource\(\{ sourceId: requestedSourceId \}\)/)
  assert.match(source, /const nextManifest = compiledData\.manifest \|\| null/)
  assert.match(source, /const nextCapability = compiledData\.capability \|\| null/)
  assert.match(source, /const nextContractHash = typeof compiledData\.contractHash === 'string'/)
  assert.match(source, /const rawSourceRevision = compiledData\.sourceRevision \|\| null/)
  assert.match(source, /this\.sourceRevision = nextSourceRevision/)
  assert.match(source, /this\.sourceContractHash = nextContractHash/)
  assert.match(source, /!this\.isCtaBacktestSource\(nextSource\) \|\| !this\.isCtaBacktestManifest\(nextManifest\)/)
  assert.match(source, /this\.manifest = nextManifest/)
  assert.match(source, /return Boolean\(this\.manifest && this\.manifest\.leverageAllowed\)/)
})

test('backtest center submits only the strict order-intent v2 request contract', () => {
  assert.match(source, /runStrategyBacktest\(\{[\s\S]*?sourceId: this\.form\.sourceId[\s\S]*?engine: CTA_ORDER_V2_ENGINE[\s\S]*?expectedContractHash: this\.sourceContractHash[\s\S]*?startDate:[\s\S]*?endDate:[\s\S]*?params: diffParamOverrides\(this\.params, this\.paramBaseline \|\| \{\}\)/)  // gap B (§6.1 B2/B3): only changed values are overrides
  assert.doesNotMatch(source, /expectedCodeHash: this\.sourceCodeHash/)
  assert.doesNotMatch(source, /strategy_config|script_params|strict_mode|strategy_code/)
})

test('backtest source revision is cleared, validated, and required before run', () => {
  assert.match(source, /sourceRevision:\s*null/)
  assert.match(source, /clearSourceContract \(\) \{[\s\S]*?this\.sourceRevision = null/)
  assert.match(source, /const nextCodeHash = rawSourceRevision && typeof rawSourceRevision\.codeHash === 'string'/)
  assert.match(source, /if \(!nextSourceRevision\) \{\s*throw new Error\(this\.\$t\('strategyV2\.backtest\.sourceRevisionRequired'\)\)/)
  assert.match(source, /runDisabled \(\) \{\s*return[\s\S]*?!this\.sourceCodeHash/)
  assert.match(source, /if \(!this\.sourceCodeHash\) \{[\s\S]*?sourceRevisionRequired/)
})

test('phase 1 keeps non-CTA sources visible but explicitly disabled', () => {
  assert.match(source, /:disabled="!isCtaBacktestSource\(item\)"/)
  assert.match(source, /strategyV2\.backtest\.ctaOnlyOption/)
  assert.match(source, /return String\(source\.asset_type \|\| ''\)\.trim\(\)\.toLowerCase\(\) === 'script'/)
  assert.match(source, /return String\(manifest\.strategyType \|\| ''\)\.trim\(\)\.toLowerCase\(\) === 'cta'/)
  assert.match(source, /sourceSummary && !this\.isCtaBacktestSource\(sourceSummary\)/)
})

test('new runs require the exact order-intent v2 capability while disclosing execution semantics', () => {
  assert.match(source, /strategyV2\.backtest\.executionBoundaryTitle/)
  assert.match(source, /strategyV2\.backtest\.executionBoundaryDesc/)
  assert.match(source, /strategyV2\.backtest\.marketOrderRule/)
  assert.match(source, /strategyV2\.backtest\.limitOrderRule/)
  assert.match(source, /strategyV2\.backtest\.authoritativeLedgerRule/)
  assert.match(source, /strategyV2\.backtest\.legacyHistoryRule/)
  assert.match(source, /sourceCapability:\s*null/)
  assert.match(source, /sourceContractHash:\s*''/)
  assert.match(source, /clearSourceContract \(\) \{[\s\S]*?this\.sourceContractHash = ''[\s\S]*?this\.sourceCapability = null/)
  assert.match(source, /return isCtaOrderV2Capability\(capability, contractHash\)/)
  assert.match(source, /!this\.isSupportedCtaCapability\(nextCapability, nextContractHash\)/)
  assert.match(source, /!this\.isSupportedCtaCapability\(nextManifest\.capability, nextContractHash\)/)
  assert.match(source, /this\.manifest\.runnable === false \|\| !this\.isSupportedCtaCapability\(this\.sourceCapability, this\.sourceContractHash\)/)
})

test('closed trades expose lifecycle, prices, wallet equity, and profit tone', () => {
  for (const field of ['entry_time', 'exit_time', 'entry_price', 'exit_price', 'balance']) {
    assert.match(source, new RegExp(`dataIndex: '${field}'`))
  }
  assert.match(source, /dataIndex: 'profit'[\s\S]*?profitTone\(value\)/)
  assert.match(source, /number > 0\) return 'positive'/)
  assert.match(source, /number < 0\) return 'negative'/)
})

test('backtest workbench keeps execution controls visible and moves history into a drawer', () => {
  assert.match(source, /class="run-action-bar"/)
  assert.match(source, /class="config-scroll"/)
  assert.match(source, /<a-drawer[\s\S]*?backtest-history-drawer/)
  assert.match(source, /v-for="item in history"/)
  assert.doesNotMatch(source, /class="panel history-panel"/)
})

test('CTA source summary exposes the concrete instrument instead of only its count', () => {
  assert.match(source, /this\.manifest\.strategyType === 'cta' && instruments\.length/)
  assert.match(source, /instruments\.map\(this\.formatInstrument\)\.join\(', '\)/)
  assert.match(source, /marketContext\.\$\{marketType\}/)
})

test('portfolio drawdown chart uses initial capital and backend drawdown points', () => {
  assert.match(portfolioResultSource, /const base = this\.initialCapital > 0/)
  assert.match(portfolioResultSource, /const savedDrawdown = Number\(item\.drawdown\)/)
  assert.match(portfolioResultSource, /item\.drawdown !== undefined/)
  assert.match(portfolioResultSource, /strategyV2\.backtest\.maxDrawdownHint/)
})
