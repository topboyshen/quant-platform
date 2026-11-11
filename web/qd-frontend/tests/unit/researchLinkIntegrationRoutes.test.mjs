import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = relative => fs.readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const ai = read('../../src/views/ai-analysis/components/CopilotWorkbench.vue')
const indicator = read('../../src/views/indicator-ide/index.vue')
const strategy = read('../../src/views/strategy-ide/index.vue')

function between (source, start, end) {
  const first = source.indexOf(start)
  assert.ok(first >= 0, `missing source anchor: ${start}`)
  const last = source.indexOf(end, first + start.length)
  assert.ok(last > first, `missing ending anchor: ${end}`)
  return source.slice(first, last)
}

test('indicator draft navigates only after verified browser-session storage', () => {
  const method = between(indicator, '    async handleCreateStrategyFromIndicator () {', '    // ===== Equity chart =====')
  const gate = method.indexOf('if (!writeTemporaryIndicatorHandoff(')
  const navigate = method.indexOf("this.$router.push({\n        path: '/strategy-ide'")
  assert.ok(gate >= 0 && gate < navigate, 'target navigation must occur only after readback-verified payload write')
  assert.match(method.slice(gate, navigate), /this\.\$message\.error\([\s\S]*?return/)
  assert.match(method, /convert_required:\s*'1'/)
  assert.match(method, /link:\s*'indicator'/)
  const routeQuery = between(method, '        query: {', '        }\n      })')
  for (const privateField of ['code', 'prompt', 'body', 'token', 'Authorization']) {
    assert.doesNotMatch(routeQuery, new RegExp(`\\b${privateField}\\s*:`), `${privateField} must not be a route query key`)
  }
  assert.match(routeQuery, /convert_key:\s*storageKey/)
})

test('AI to backtest requires explicit saved strategy selection, not symbol-first auto-pick', () => {
  const method = between(ai, '    continueDecision (path) {', '    applyIncomingCopilotPrompt () {')
  assert.match(method, /if \(path === '\/backtest-center'\) \{[\s\S]*?buildAIBacktestQuery\(/)
  assert.doesNotMatch(method, /sourceId\s*:/)
  assert.doesNotMatch(method, /strategy_id\s*:/)
  assert.match(ai, /if \(normalizedAction\.path === '\/backtest-center'\)[\s\S]*?buildAIBacktestQuery\(/)
})

test('strict indicator route rejects lost or changed payload without selecting current saved indicator', () => {
  const method = between(strategy, '    async applyIndicatorConvertRouteOnce (options = {}) {', '    clearIndicatorConvertRoute () {')
  assert.match(method, /isStrictIndicatorConvertRoute|convert_required|handoff_contract/)
  assert.match(method, /context\.code_digest !== temporaryCodeDigest\(context\.code\)/)
  assert.match(method, /saved\.savedCodeDigest !== context\.saved_code_digest/)
  assert.match(method, /未使用当前已保存指标替代/)
  assert.match(method, /未自动转换、未改写策略草稿/)
  assert.match(method, /当前无权读取/)
  assert.doesNotMatch(method, /context\s*=\s*this\.selectedIndicator/)
})
