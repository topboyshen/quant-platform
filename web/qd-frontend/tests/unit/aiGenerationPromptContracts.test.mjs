import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const read = relativePath => fs.readFileSync(
  fileURLToPath(new URL(relativePath, import.meta.url)),
  'utf8'
)

const strategyIde = read('../../src/views/strategy-ide/index.vue')
const indicatorIde = read('../../src/views/indicator-ide/index.vue')
const copilot = read('../../src/views/ai-analysis/components/CopilotWorkbench.vue')
const overrides = read('../../src/locales/copilot-overrides.js')

test('indicator conversion prompt protects direction, edge, execution, and risk semantics', () => {
  const start = strategyIde.indexOf('buildIndicatorConversionPrompt ()')
  const end = strategyIde.indexOf('async confirmIndicatorToStrategy', start)
  const conversionPrompt = strategyIde.slice(start, end)

  assert.match(conversionPrompt, /classify every marker as long entry, long exit, warning\/wait, or visual-only/)
  assert.match(conversionPrompt, /For edge\(A \| B\), compare the complete previous composite/)
  assert.match(conversionPrompt, /define both on_init\(ctx\) and on_bar\(ctx, bar\)/)
  assert.match(conversionPrompt, /backtest panel supplies the one market, symbol, timeframe/)
  assert.match(conversionPrompt, /Confirm all conditions on completed bars, use historical bars only/)
  assert.match(conversionPrompt, /Express stops, take-profit, trailing exits, and time exits as explicit closed-bar conditions/)
  assert.match(conversionPrompt, /Do not use initialize, handle_data, set_universe/)
  assert.match(conversionPrompt, /CNStock daily, single-symbol, cash, and long-only/)
  assert.match(conversionPrompt, /explicit order_type="market" or order_type="limit"/)
  assert.match(conversionPrompt, /exactly one of qty, amount, or allocation_pct/)
  assert.match(conversionPrompt, /ctx\.close_long with no qty or amount closes the full long position/)
  assert.match(conversionPrompt, /partial close must specify exactly one of qty or amount/)
  assert.match(conversionPrompt, /Limit intents must include limit_price, are active for NEXT_BAR only/)
  assert.match(conversionPrompt, /Never emit legacy price=, positional order arguments/)
  assert.match(conversionPrompt, /Do not implement pending-next-open flags/)
  assert.doesNotMatch(conversionPrompt, /ctx\.open_short|ctx\.add_short|ctx\.close_short/)
})

test('copilot builds workflow-specific artifact rules instead of mixing indicator and strategy rules', () => {
  assert.match(copilot, /const artifactRules = isIndicatorWorkflow/)
  assert.match(copilot, /\.\.\.artifactRules/)
  assert.match(copilot, /Timeframe remains owned by the run panel/)
  assert.match(copilot, /CNStock 1D cash long-only cta_order_v2 code/)
  assert.match(copilot, /close_long with no qty\/amount is full close and partial close uses exactly one qty or amount/)
  assert.match(copilot, /limit requires limit_price and expires after NEXT_BAR/)
  assert.match(copilot, /Evaluate ATR exits on a closed bar and submit market close for next open/)
  assert.match(copilot, /Never use legacy price=, positional orders, shorts, or script-managed next-bar pending state/)
})

test('copilot script-generation overrides use the frozen order-intent v2 API', () => {
  assert.ok((overrides.match(/cta_order_v2/g) || []).length >= 4)
  assert.ok((overrides.match(/order_type='market' or 'limit'/g) || []).length >= 2)
  assert.match(overrides, /Open\/add uses exactly one of qty, amount, or allocation_pct/)
  assert.match(overrides, /close_long without qty\/amount is a full close, while a partial close uses exactly one of qty or amount/)
  assert.match(overrides, /limit requires limit_price and has fixed NEXT_BAR time-in-force/)
  assert.match(overrides, /Evaluate ATR\/risk exits on a closed bar and submit a market close for the next open/)
  assert.match(overrides, /Never emit legacy price=, positional order arguments/)
  assert.doesNotMatch(overrides, /Declare the universe and subscriptions in initialize\(context\), use target-based order functions/)
  assert.doesNotMatch(overrides, /在 initialize\(context\) 中声明标的范围和订阅，使用目标仓位下单接口/)
})

test('indicator IDE handoff prompt is locked to the same order-intent v2 contract', () => {
  const start = indicatorIde.indexOf('buildIndicatorToStrategyPrompt ()')
  const end = indicatorIde.indexOf('buildIndicatorToStrategyContext ()', start)
  const prompt = indicatorIde.slice(start, end)

  assert.match(prompt, /cta_order_v2 Python 研究策略/)
  assert.match(prompt, /on_init\(ctx\) 和 on_bar\(ctx, bar\)/)
  assert.match(prompt, /固定为 CNStock、1D、单标的、现金账户、long_only、杠杆 1/)
  assert.match(prompt, /ctx\.open_long、ctx\.add_long、ctx\.close_long/)
  assert.match(prompt, /开仓\/加仓必须且只能在 qty、amount、allocation_pct 中选一个/)
  assert.match(prompt, /ctx\.close_long 不带 qty\/amount 表示全平；部分平仓必须且只能给 qty 或 amount/)
  assert.match(prompt, /market 禁止 limit_price[\s\S]*?limit 必须给 limit_price[\s\S]*?NEXT_BAR/)
  assert.match(prompt, /禁止旧 price=、订单位置参数[\s\S]*?脚本自管 pending-next-open/)
  assert.match(prompt, /ATR 止损\/止盈[\s\S]*?已收盘 K 线[\s\S]*?下一根开盘/)
  assert.match(prompt, /每根 K 线最多生成一个订单意图/)
  assert.doesNotMatch(prompt, /交易方向由策略页\/回测页配置/)
  assert.doesNotMatch(prompt, /明确 open_long\/open_short、add\/reduce/)
})

test('English prompt overrides identify the indicator workflow as chart-only', () => {
  assert.match(
    overrides,
    /"aiAssetAnalysis\.copilot\.nativeStrategyPrompt\.workflowIndicator": "QuantDinger Chart Indicator"/
  )
  assert.doesNotMatch(
    overrides,
    /"aiAssetAnalysis\.copilot\.nativeStrategyPrompt\.workflowIndicator": "QuantDinger Python Strategy API V2"/
  )
})
