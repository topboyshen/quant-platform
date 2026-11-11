import test from 'node:test'
import assert from 'node:assert/strict'
import { triggerDiagnosticView, triggerHistoryView } from '../../src/utils/etfDiagnostics.js'
import { etfPoolView } from '../../src/utils/etfPool.js'

const binding = { runId: 'run-one', decisionDate: '2026-09-15' }
function fixture () {
  return { status: 'recorded', run_id: 'run-one', decision_date: binding.decisionDate,
    data: { ready: true, rows: 280, minimum_rows: 65, data_date: binding.decisionDate, required_date: binding.decisionDate },
    daily: { evaluated: true, ma_period: 60, previous_close: 10, close: 11, previous_ma: 9, ma: 9.1,
      previous_at_or_below_ma: false, close_above_ma: true, price_cross: false,
      volume: 80, average_volume20: 100, volume_ratio_raw: .8, volume_ratio: .8, volume_threshold: 1.5,
      volume_ok: false, triggered: false },
    weekly: { evaluated: true, completed_bars: 56, minimum_bars: 35,
      previous_week_date: '2026-09-04', week_date: '2026-09-11', previous_dif: .2, previous_dea: .1,
      dif: .3, dea: .2, previous_at_or_below: false, now_above: true, golden_cross: false, triggered: false } }
}

test('plain-language explanation separates new-cross condition from volume and weekly state', () => {
  const view = triggerDiagnosticView(fixture(), binding)
  assert.equal(view.status, 'recorded')
  assert.match(view.daily, /昨日已在 MA60 上方/)
  assert.match(view.daily, /0.80×.*1.50×/)
  assert.match(view.weekly, /并非新金叉/)
  assert.match(view.details.join('\n'), /前20日均量/)
})

test('old runs are unrecorded, not reconstructed as failed conditions', () => {
  for (const value of [undefined, null, { status: 'not_recorded' }]) {
    const view = triggerDiagnosticView(value, binding)
    assert.equal(view.status, 'not_recorded')
    assert.match(view.daily, /未记录/)
    assert.doesNotMatch(view.daily, /量能不足|未金叉/)
  }
})

for (const [field, value] of [['run_id', 'different-run'], ['decision_date', '2026-09-14'], ['daily', null], ['status', 'invalid']]) {
  test(`mismatched or malformed diagnostics fail closed: ${field}`, () => {
    const input = fixture(); input[field] = value
    assert.equal(triggerDiagnosticView(input, binding).status, 'invalid')
  })
}

test('null booleans cannot become false conditions', () => {
  const input = fixture(); input.daily.volume_ok = null
  assert.equal(triggerDiagnosticView(input, binding).status, 'invalid')
})

test('stale input remains an unevaluated data failure', () => {
  const input = fixture(); input.data.ready = false; input.data.data_date = '2026-09-14'
  input.daily.evaluated = false; input.weekly.evaluated = false
  const view = triggerDiagnosticView(input, binding)
  assert.match(view.daily, /仅到 2026-09-14/)
  assert.match(view.weekly, /未评估/)
})

test('a completed-week shortage is not mistaken for no golden cross', () => {
  const input = fixture(); input.weekly = { evaluated: false, completed_bars: 34, minimum_bars: 35 }
  const view = triggerDiagnosticView(input, binding)
  assert.match(view.weekly, /34\/35.*尚不能判定/)
})

test('a triggered condition is not presented as risk approval or an order', () => {
  const input = fixture()
  Object.assign(input.daily, { previous_at_or_below_ma: true, price_cross: true, volume_ok: true, triggered: true })
  assert.match(triggerDiagnosticView(input, binding).daily, /风控另判/)
})

function history () {
  return { status: 'available', scanned_runs: 23, excluded_stale_count: 1,
    latest_raw_trigger: { signal_type: 'weekly_macd_golden_cross', period: 'W', signal_data_date: '2026-08-21',
      first_observed_date: '2026-08-21', target_risk_action: 'allowed', risk_scope: 'target', mixed_signal_gating_possible: true } }
}

test('history displays the immutable bar date, does not claim a per-signal risk approval', () => {
  const view = triggerHistoryView(history(), binding)
  assert.equal(view.date, '2026-08-21')
  assert.match(view.detail, /批次含放行信号/)
  assert.match(view.detail, /不代表现在可买/)
  assert.match(view.detail, /排除旧行情 1 条/)
})

test('no record within the bounded window never means never triggered', () => {
  const value = { status: 'none_in_window', scanned_runs: 400, latest_raw_trigger: null }
  const view = triggerHistoryView(value, binding)
  assert.match(view.label, /所查批次/)
  assert.doesNotMatch(view.label, /从未/)
})

test('truncated history remains explicitly incomplete', () => {
  const value = history(); value.status = 'truncated'
  assert.match(triggerHistoryView(value, binding).detail, /截断/)
  value.latest_raw_trigger = null
  assert.match(triggerHistoryView(value, binding).label, /不能判定/)
})

test('future or malformed history cannot be shown as an observed trigger', () => {
  const value = history(); value.latest_raw_trigger.first_observed_date = '2026-09-20'
  assert.equal(triggerHistoryView(value, binding).date, '')
})

test('history and diagnostics do not promote expired rows or alter current evidence counts', () => {
  const p = { schema_version: 'quant.etf-pool.v1', configured_count: 1, run_id: binding.runId,
    calendar_available: true, expected_date: binding.decisionDate, decision_date: binding.decisionDate, run_status: 'success',
    items: [{ code: '510300', state: 'no_trigger', is_current: true, data_status: 'ok',
      decision_date: binding.decisionDate, data_date: binding.decisionDate, reference_close: 11,
      trigger_diagnostics: fixture(), history: history() }] }
  const view = etfPoolView(p, { generatedAt: '2026-09-15T16:00:00+08:00', now: Date.parse('2026-09-16T14:00:00+08:00') })
  assert.equal(view.counts.unknown, 1)
  assert.equal(view.items[0].current, false)
  assert.equal(view.items[0].triggerHistory.date, '2026-08-21')
})

test('insufficient branch evidence is pending, not a fully verified no-trigger summary', () => {
  const diagnostic = fixture()
  diagnostic.daily.evaluated = false
  diagnostic.daily.reason_codes = ['volume_missing']
  diagnostic.weekly = { evaluated: false, completed_bars: 13, minimum_bars: 35 }
  const p = { schema_version: 'quant.etf-pool.v1', configured_count: 1, run_id: binding.runId,
    calendar_available: true, expected_date: binding.decisionDate, decision_date: binding.decisionDate, run_status: 'success',
    items: [{ code: '510300', state: 'no_trigger', is_current: true, data_status: 'ok',
      decision_date: binding.decisionDate, data_date: binding.decisionDate, reference_close: 11,
      trigger_diagnostics: diagnostic }] }
  const view = etfPoolView(p)
  assert.equal(view.counts.no_trigger, 0)
  assert.equal(view.pendingCount, 1)
  assert.equal(view.items[0].current, false)
  assert.match(view.items[0].label, /分项条件待核实/)
  assert.match(view.items[0].diagnostics.daily, /量能数据不足/)
  assert.equal(p.items[0].state, 'no_trigger') // original immutable audit unchanged
})
