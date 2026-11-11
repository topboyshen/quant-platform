import test from 'node:test'
import assert from 'node:assert/strict'
import { etfPoolView, filterEtfPool, etfResearchContext, historicalEtfContext, singleFlight } from '../../src/utils/etfPool.js'
import { normalizeDecisionContext, isDecisionContextStale, buildDecisionResearchBrief, writeDecisionContext, readDecisionContext } from '../../src/utils/decisionContext.js'

const now = Date.parse('2026-09-12T16:00:00+08:00')
const day = '2026-09-11'
function payload () {
  return { generated_at: new Date(now).toISOString(), market: { available: true }, risk: { available: true, level: 'critical' },
    signals: { current: [], recent: [] }, research_pool: {
      schema_version: 'quant.etf-pool.v1', configured_count: 10, status: 'ok', run_status: 'success',
      calendar_available: true, expected_date: day, decision_date: day,
      items: Array.from({ length: 10 }, (_, index) => ({ code: String(510300 + index), name: `ETF ${index}`, sector: '宽基',
        state: 'no_trigger', is_current: true, data_status: 'ok', decision_date: day, data_date: day,
        reference_close: .547, reason_code: 'signal_not_triggered' }))
    } }
}

test('zero recent signals does not erase ten configured and evaluated ETFs', () => {
  const p = payload(); const view = etfPoolView(p.research_pool)
  assert.equal(view.configuredCount, 10)
  assert.equal(view.items.length, 10)
  assert.equal(view.counts.no_trigger, 10)
  assert.equal(view.pendingCount, 0)
  assert.equal(view.items[0].reference_close, .547)
})

for (const value of [null, {}, { configured_count: '10' }, { schema_version: 'quant.etf-pool.v1', configured_count: 10, items: [] }]) {
  test(`missing/malformed universe is unknown, not inferred: ${JSON.stringify(value)}`, () => {
    assert.equal(etfPoolView(value).configuredCount, null)
    assert.deepEqual(etfPoolView(value).items, [])
  })
}

test('duplicate codes reject an ambiguous universe', () => {
  const p = payload(); p.research_pool.items[1].code = '510300'
  assert.equal(etfPoolView(p.research_pool).available, false)
})

for (const [field, value] of [['is_current', 'true'], ['data_date', '2026-09-10'], ['decision_date', '2026-09-14'],
  ['data_status', 'failed'], ['reference_close', null], ['reference_close', Infinity], ['reference_close', 0]]) {
  test(`invalid row cannot look successfully evaluated: ${field}`, () => {
    const p = payload(); p.research_pool.items[0][field] = value
    const view = etfPoolView(p.research_pool)
    assert.equal(view.items[0].state, 'unknown'); assert.equal(view.counts.unknown, 1)
  })
}

test('search and status filters do not change total membership', () => {
  const p = payload(); p.research_pool.items[1].state = 'data_failed'; p.research_pool.items[1].is_current = false
  const view = etfPoolView(p.research_pool)
  assert.equal(filterEtfPool(view.items, 'ETF 1').length, 1)
  assert.equal(filterEtfPool(view.items, '510300')[0].code, '510300')
  assert.equal(filterEtfPool(view.items, '', 'data_failed').length, 1)
  assert.equal(filterEtfPool(view.items, '宽基').length, 10)
  assert.equal(view.configuredCount, 10)
})

test('untriggered ETF remains researchable without manufacturing a signal', () => {
  const p = payload()
  p.signals.recent = [{ code: '510300', decision_date: '2026-08-27', signal_type: 'buy' }]
  const context = normalizeDecisionContext(etfResearchContext({ code: '510300' }, p, { now }), { now })
  assert.equal(context.symbol, '510300')
  assert.equal(context.radar.signal.type, '')
  assert.match(context.radar.signal.summary, /未触发信号/)
  assert.equal(context.radar.risk.level, 'critical')
  assert.equal(isDecisionContextStale(context, undefined, now), false)
})

test('legacy date+code signal match lacks run provenance and cannot become a current allowed signal', () => {
  const p = payload(); p.research_pool.items[0].state = 'allowed'
  p.signals.current = [{ code: '510301', decision_date: day, signal_type: 'buy' },
    { code: '510300', decision_date: '2026-08-27', signal_type: 'buy' }]
  assert.equal(normalizeDecisionContext(etfResearchContext({ code: '510300' }, p, { now }), { now }).radar.signal.type, '')
  p.signals.current.push({ code: '510300', decision_date: day, signal_type: 'volume_breakout_ma60' })
  assert.equal(normalizeDecisionContext(etfResearchContext({ code: '510300' }, p, { now }), { now }).radar.signal.type, '')
})

test('historical signal cannot be refreshed into live evidence or mixed with current risk', () => {
  const p = payload()
  const historical = historicalEtfContext({ code: '510300', name: 'ETF', decision_date: '2026-08-27', signal_type: 'buy' }, p)
  const context = normalizeDecisionContext(historical, { now })
  assert.equal(isDecisionContextStale(context, undefined, now), true)
  assert.equal(context.radar.risk.available, null)
  assert.equal(context.asOf, '2026-08-27')
  assert.match(buildDecisionResearchBrief(context, { now }), /context.evidenceState="historical"/)
  assert.match(buildDecisionResearchBrief(context, { now }), /context.stale=true/)
  const storage = { value: '', setItem (_key, value) { this.value = value }, getItem () { return this.value } }
  writeDecisionContext(historical, { storage, now })
  assert.equal(readDecisionContext({ storage, now, allowStale: true }).evidenceState, 'historical')
})

test('expired response cannot stay green or propagate stale global risk', () => {
  const p = payload()
  const later = now + 300001
  const view = etfPoolView(p.research_pool, { generatedAt: p.generated_at, now: later })
  assert.equal(view.counts.unknown, 10)
  const context = normalizeDecisionContext(etfResearchContext({ code: '510300' }, p, { now: later }), { now: later })
  assert.equal(context.radar.risk.available, null)
  assert.equal(isDecisionContextStale(context, undefined, later), true)
})

test('overlapping refresh requests coalesce and failure allows another attempt', async () => {
  const owner = {}; let calls = 0; let finish
  const pending = new Promise(resolve => { finish = resolve })
  const run = () => { calls++; return pending }
  const a = singleFlight(owner, 'request', run); const b = singleFlight(owner, 'request', run)
  assert.equal(a, b)
  await Promise.resolve(); assert.equal(calls, 1)
  finish('ok'); assert.equal(await a, 'ok'); assert.equal(owner.request, null)
  await assert.rejects(singleFlight(owner, 'request', () => Promise.reject(new Error('offline'))), /offline/)
  assert.equal(owner.request, null)
  assert.equal(await singleFlight(owner, 'request', () => 2), 2)
})

test('current configuration flags cannot relax malformed disabled-branch diagnostic evidence', () => {
  const p = payload()
  p.research_pool.run_id = 'synthetic-disabled'
  p.research_pool.current_config = { daily_volume_breakout: false }
  p.research_pool.items[0].trigger_diagnostics = {
    status: 'recorded', run_id: 'synthetic-disabled', decision_date: day,
    data: { ready: true, data_date: day, required_date: day },
    daily: { enabled: false, evaluated: false }, weekly: { evaluated: true }
  }
  const original = JSON.stringify(p)
  const view = etfPoolView(p.research_pool)
  assert.equal(view.items[0].conditionUncertain, true)
  assert.equal(view.items[0].state, 'unknown')
  assert.equal(view.counts.no_trigger, 9)
  assert.equal(view.counts.unknown, 1)
  assert.equal(JSON.stringify(p), original)
})

test('malformed later pool item is rejected without throwing while binding earlier diagnostics', () => {
  const p = payload()
  p.research_pool.items[1] = null
  assert.doesNotThrow(() => etfPoolView(p.research_pool))
  assert.equal(etfPoolView(p.research_pool).available, false)
})

