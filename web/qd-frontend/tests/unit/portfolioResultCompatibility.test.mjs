import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import {
  normalizeStrategyBacktestResult,
  strategyResultHasLedgerAudit
} from '../../src/utils/strategyResearchContracts.mjs'

const componentPath = fileURLToPath(
  new URL('../../src/views/backtest-center/PortfolioResult.vue', import.meta.url)
)
const source = fs.readFileSync(componentPath, 'utf8')

test('legacy history uses the equity curve when holding snapshots are empty', () => {
  assert.match(source, /snapshots\.length \? snapshots : \(this\.result\.equityCurve \|\| \[\]\)/)
})

test('missing legacy attribution is recovered from saved executions', () => {
  assert.match(source, /executionCommissions\.reduce\(\(sum, value\) => sum \+ Number\(value\), 0\)/)
  assert.match(source, /this\.initialCapital > 0 && totalCommission !== null/)
  assert.match(source, /counts\[status\] \+= 1/)
})

test('restored legacy details are disclosed in the result trust bar', () => {
  assert.match(source, /v-if="legacyBackfilled"/)
  assert.match(source, /strategyV2\.backtest\.legacyBackfillHint/)
})

test('standard script summaries are not promoted to passed or failed ledger audits', () => {
  const result = normalizeStrategyBacktestResult({
    resultStatus: 'completed_trades',
    totalExecutions: 2,
    trades: [
      { time: '2026-01-02', type: 'buy', amount: 100, price: 10 },
      { time: '2026-01-03', type: 'sell', amount: 100, price: 11 }
    ]
  })

  assert.equal(strategyResultHasLedgerAudit(result), false)
  assert.equal(result.audit, undefined)
  assert.equal(result.totalExecutions, 2)
  assert.equal(result.compatibility.summaryOnly, true)
  assert.equal(result.compatibility.legacyExecutions, true)
  assert.deepEqual(result.executions.map(item => [item.side, item.quantity, item.status]), [
    ['buy', 100, 'filled'],
    ['sell', 100, 'filled']
  ])
})

test('legacy short closes are adapted as buy executions', () => {
  const result = normalizeStrategyBacktestResult({
    totalExecutions: 2,
    trades: [
      { type: 'open_short', amount: 10 },
      { type: 'close_short', amount: 10 }
    ]
  })

  assert.deepEqual(result.executions.map(item => item.side), ['sell', 'buy'])
})

test('explicit ledger audit and execution details remain authoritative', () => {
  const result = {
    audit: { passed: false },
    executions: [],
    trades: [{ type: 'buy', amount: 5 }]
  }

  assert.equal(strategyResultHasLedgerAudit(result), true)
  assert.equal(normalizeStrategyBacktestResult(result), result)
})

test('ambiguous closed-trade summaries are not repurposed as execution rows', () => {
  const result = normalizeStrategyBacktestResult({
    resultStatus: 'completed_trades',
    totalExecutions: 4,
    trades: [{ entry_time: '2026-01-02', exit_time: '2026-01-03', quantity: 100 }]
  })

  assert.equal(result.executions, undefined)
  assert.equal(result.compatibility.legacyExecutions, undefined)
})

test('summary-only UI uses unavailable state and does not format absent cash as zero', () => {
  assert.match(source, /if \(!this\.hasLedgerAudit\) return this\.\$t\('strategyV2\.backtest\.auditUnavailable'\)/)
  assert.match(source, /if \(this\.hasLedgerAudit && !this\.auditPassed\) return 'is-error'/)
  assert.match(source, /formatOptionalNumber\(latestSnapshot\.cash\)/)
  assert.match(source, /value !== undefined && value !== null && value !== ''/)
  assert.match(source, /return this\.summaryOnly \? \[\] : \(this\.result\.trades \|\| \[\]\)/)
  assert.match(source, /closedTradeDetailsUnavailable/)
  assert.match(source, /formatOptionalRate\(attribution\.feeDrag\)/)
  assert.match(source, /dataIndex: 'commission', customRender: value => this\.formatOptionalNumber\(value, 4\)/)
})
