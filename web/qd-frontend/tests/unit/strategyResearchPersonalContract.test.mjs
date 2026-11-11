import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import {
  backtestRatioToPercent,
  strategyRunFailed,
  strategyRunParams,
  strategyRunStatus
} from '../../src/utils/strategyResearchContracts.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '../..')
const api = fs.readFileSync(path.join(root, 'src/api/strategy.js'), 'utf8')
const backtest = fs.readFileSync(path.join(root, 'src/views/backtest-center/index.vue'), 'utf8')
const strategyIde = fs.readFileSync(path.join(root, 'src/views/strategy-ide/index.vue'), 'utf8')

test('personal strategy research uses implemented backend routes', () => {
  assert.match(api, /verifyCode:\s*'\/api\/strategies\/verify-code'/)
  assert.match(api, /aiGenerate:\s*'\/api\/strategies\/ai-generate'/)
  assert.match(api, /strategyBacktestRun:\s*'\/api\/strategies\/backtest'/)
  assert.match(api, /strategyBacktestHistory:\s*'\/api\/strategies\/backtest\/history'/)
  assert.match(api, /strategyBacktestGet:\s*'\/api\/strategies\/backtest\/get'/)
  const runStart = api.indexOf('export function runStrategyBacktest (data)')
  const runEnd = api.indexOf('export function runStrategyFactorResearch', runStart)
  const runAdapter = api.slice(runStart, runEnd)

  assert.ok(runStart >= 0 && runEnd > runStart)
  assert.match(runAdapter, /const sourceId = requirePositiveSourceId\(payload\.sourceId\)/)
  assert.match(runAdapter, /const engine = requireCtaOrderV2Engine\(payload\.engine\)/)
  assert.match(runAdapter, /const expectedContractHash = requireExpectedContractHash\(payload\.expectedContractHash\)/)
  assert.match(runAdapter, /const requestPayload = \{\s*sourceId,\s*engine,\s*expectedContractHash,[\s\S]*?overrideConfig:/)
  assert.doesNotMatch(runAdapter, /strategyId/)
  assert.doesNotMatch(runAdapter, /expectedCodeHash/)
  assert.match(api, /commission:\s*backtestRatioToPercent\(payload\.commission\)/)
  assert.match(api, /slippage:\s*backtestRatioToPercent\(payload\.slippage\)/)
  assert.match(api, /data:\s*\{[\s\S]*?\.\.\.data\.result,[\s\S]*?runId:\s*data\.runId/)
})

test('compile and order-intent run adapters whitelist sourceId without legacy fallback', () => {
  const compileStart = api.indexOf('export function compileScriptSource (data)')
  const compileAdapter = api.slice(compileStart)

  assert.ok(compileStart >= 0)
  assert.match(api, /function requirePositiveSourceId \(value\) \{[\s\S]*?Number\.isInteger\(sourceId\)[\s\S]*?sourceId <= 0/)
  assert.match(api, /function requireExpectedContractHash \(value\) \{[\s\S]*?expectedContractHash must be a non-empty string/)
  assert.match(api, /function requireCtaOrderV2Engine \(value\) \{[\s\S]*?value !== 'cta_order_v2'/)
  assert.match(compileAdapter, /const sourceId = requirePositiveSourceId\(data && data\.sourceId\)/)
  assert.match(compileAdapter, /data: \{ sourceId \}/)
  assert.doesNotMatch(compileAdapter, /strategyId/)
})

test('unimplemented factor research is hidden and never requested on startup', () => {
  assert.match(backtest, /supportsFactorResearch:\s*false/)
  assert.match(backtest, /v-if="supportsFactorResearch"\s+value="factor"/)
  assert.match(backtest, /if \(this\.supportsFactorResearch\) \{[\s\S]*?getStrategyFactorResearchHistory/)
  assert.match(backtest, /const portfolioResponse = await getStrategyBacktestHistory/)
  assert.doesNotMatch(backtest, /const \[portfolioResponse, factorResponse\] = await Promise\.all/)
})

test('backtest source compilation can supply inferred parameter schema', () => {
  assert.match(backtest, /const compiledData = \(compiled && compiled\.data\) \|\| \{\}/)
  assert.match(backtest, /nextSource = \{ \.\.\.nextSource, param_schema: compiledData\.param_schema \}/)
})

test('backtest source switching is latest-request-wins and commits atomically', () => {
  const start = backtest.indexOf('async selectSource (sourceId, options = {})')
  const end = backtest.indexOf('sourceTypeLabel (item)', start)
  const selectSource = backtest.slice(start, end)

  assert.ok(start >= 0 && end > start)
  assert.match(backtest, /selectingSource:\s*false/)
  assert.match(backtest, /sourceSelectionSeq:\s*0/)
  assert.match(backtest, /runDisabled \(\) \{\s*return this\.selectingSource \|\| this\.sourceSelectionRequired \|\| !!this\.sourceLoadError \|\| !this\.manifest/)
  assert.match(selectSource, /const selectionSeq = \+\+this\.sourceSelectionSeq/)
  assert.match(selectSource, /this\.selectingSource = true[\s\S]*?this\.clearSourceContract\(\)[\s\S]*?await Promise\.all/)
  assert.match(selectSource, /getScriptSourceDetail\(requestedSourceId\)[\s\S]*?compileScriptSource\(\{ sourceId: requestedSourceId \}\)/)
  assert.match(selectSource, /if \(selectionSeq !== this\.sourceSelectionSeq\) return null/)
  assert.ok(selectSource.indexOf('this.source = nextSource') > selectSource.indexOf('await Promise.all'))
  assert.match(selectSource, /this\.source = nextSource\s*this\.manifest = nextManifest\s*this\.sourceRevision = nextSourceRevision\s*this\.sourceContractHash = nextContractHash\s*this\.sourceCapability = nextCapability\s*this\.backtestRangePolicy = nextBacktestRangePolicy\s*this\.params = nextParams\s*this\.form\.sourceId = resolvedSourceId/)
  assert.match(selectSource, /catch \(error\) \{[\s\S]*?this\.clearSourceContract\(\)[\s\S]*?this\.sourceLoadError = this\.sourceLoadFailureMessage\(error\)[\s\S]*?return false/)
  assert.match(selectSource, /finally \{\s*if \(selectionSeq === this\.sourceSelectionSeq\) \{\s*this\.selectingSource = false/)
})

test('failed backtests refresh persisted history immediately', () => {
  const start = backtest.indexOf('async run ()')
  const end = backtest.indexOf('async runFactorResearch ()', start)
  const run = backtest.slice(start, end)
  const catchStart = run.indexOf('} catch (error) {')

  assert.ok(start >= 0 && end > start && catchStart >= 0)
  assert.ok(run.indexOf('await this.loadHistory()', catchStart) > catchStart)
})

test('viewing a saved run shows its own snapshot without touching the current draft', () => {
  const openStart = backtest.indexOf('async openRun (item)')
  const openEnd = backtest.indexOf('useRunAsNewBacktest (run)', openStart)
  assert.ok(openStart >= 0 && openEnd > openStart, 'openRun 与 useRunAsNewBacktest 都必须存在')
  const openRun = backtest.slice(openStart, openEnd)

  // 查看历史：只展示该次运行的快照（结果与身份）
  assert.match(openRun, /this\.selectedRun = run/)
  assert.match(openRun, /this\.result = normalizeStrategyBacktestResult\(result\)/)
  assert.match(openRun, /this\.historyViewing = true/)
  assert.doesNotMatch(openRun, /isSupportedCtaCapability|isCtaOrderV2Capability/)
  // 不得覆盖当前草稿、不得切换/校验当前源码、不得清空源码契约
  assert.doesNotMatch(openRun, /applyRunConfiguration\(/, '查看历史不得改写当前参数草稿')
  assert.doesNotMatch(openRun, /await this\.selectSource\(/, '查看历史不得切换并校验当前源码')
  assert.doesNotMatch(openRun, /clearSourceContract\(/, '查看历史不得清空当前源码契约')
  // 乱序保护：迟到响应不得覆盖最后一次选择
  assert.match(openRun, /shouldAcceptResponse\(seq, this\.runOpenSeq\)/)

  // 显式动作「以此配置新回测」：此时才校验当前源码，真实失败仍会提示
  const actionStart = backtest.indexOf('async useRunAsNewBacktest (run)')
  const actionEnd = backtest.indexOf('applyRunConfiguration (run, options = {})', actionStart)
  assert.ok(actionStart >= 0 && actionEnd > actionStart)
  const action = backtest.slice(actionStart, actionEnd)
  assert.match(action, /applyRunConfiguration\(target, \{ historyView: false \}\)/)
  assert.match(action, /preserveResult:\s*true[\s\S]*?runConfiguration:\s*target[\s\S]*?silentError:\s*true/)
  assert.match(action, /if \(!Number\.isFinite\(sourceId\) \|\| sourceId <= 0\) \{[\s\S]*?this\.sourceLoadError = this\.\$t\('strategyV2\.sourceContractRequired'\)/)

  const selectStart = backtest.indexOf('async selectSource (sourceId, options = {})')
  const selectEnd = backtest.indexOf('sourceTypeLabel (item)', selectStart)
  const selectSource = backtest.slice(selectStart, selectEnd)
  // 行为断言：不要求代码文本紧邻——该分支必须先让在途历史详情失效，再清理结果与所选记录
  assert.match(selectSource, /if \(!preserveResult\) \{[\s\S]*?this\.invalidateHistoryDetail\(\)[\s\S]*?this\.result = null[\s\S]*?this\.factorResult = null[\s\S]*?this\.selectedRun = null[\s\S]*?this\.runError = ''/)
  assert.match(backtest, /v-if="sourceLoadError"[\s\S]*?:description="sourceLoadError"/)
})

test('history list distinguishes loading / failed / empty', () => {
  // 失败优先于空态：不得用 !history.length 单独判定空态，也不得把失败渲染成“没有历史”
  assert.match(backtest, /historyState === 'empty'/)
  assert.match(backtest, /historyViewState\(\{/)
  assert.match(backtest, /v-if="historyError"[\s\S]*?:description="historyError"/)
  assert.doesNotMatch(backtest, /v-if="!historyLoading && !history\.length"/)
})

test('strategy IDE verifies current editor contents before opening backtest', () => {
  assert.match(strategyIde, /this\.scriptCode = this\.getCurrentScriptCode\(\)[\s\S]*?verifyStrategyCode\(verificationPayload\)/)
  assert.match(strategyIde, /openBacktestCenter \(\) \{[\s\S]*?await this\.verifyScriptCode\(\{ silentSuccess: true \}\)[\s\S]*?if \(!verified\) return/)
})

test('strategy backtest adapter converts ratio rates into API percentage points', () => {
  assert.equal(backtestRatioToPercent(0.0005), 0.05)
  assert.equal(backtestRatioToPercent(0), 0)
  assert.equal(backtestRatioToPercent(undefined), undefined)
})

test('saved run adapters restore params and preserve semantic result status', () => {
  const run = {
    status: 'success',
    result_status: 'open_position_only',
    config_snapshot: {
      signalConfig: { indicatorParams: { period: 7, mode: 'fast' } }
    }
  }
  assert.deepEqual(strategyRunParams(run), { period: 7, mode: 'fast' })
  assert.equal(strategyRunStatus(run), 'open_position_only')
  assert.equal(strategyRunFailed(run), false)
  assert.equal(strategyRunStatus({ status: 'failed', result_status: 'complete' }), 'failed')
})

test('backtest workbench supports text params, failed runs, and resilient startup', () => {
  assert.match(backtest, /item\.type !== 'text' && item\.type !== 'string'/)
  assert.match(backtest, /v-else-if="runError \|\| historyDetailError"[\s\S]*?data-testid="backtest-error"/)
  assert.match(backtest, /applyRunConfiguration \(run, options = \{\}\)[\s\S]*?strategyRunParams\(run\)/)
  assert.match(backtest, /this\.loadSources\(\)\.catch\(\(\) => false\)/)
  assert.match(backtest, /const routeSource = this\.sources\.find/)
})
