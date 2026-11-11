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

// Exact synthetic output from candidate backend _clean_rule_attribution +
// _clean_diagnostic (2026-10-16, daily disabled, counterfactual daily true).
function backendDisabledFixture () {
  return {
    status: 'recorded', run_id: 'synthetic-run', decision_date: '2026-10-16',
    rule_attribution_status: 'run_bound', validation_basis: 'frozen_run_rule',
    data: { ready: true, rows: 250, minimum_rows: 65, data_date: '2026-10-16', required_date: '2026-10-16', reason_codes: [] },
    daily: { average_volume20: 100, close: 1.1, close_above_ma: true, enabled: false, evaluated: true,
      ma: 1, ma_period: 60, previous_at_or_below_ma: true, previous_close: .9, previous_ma: 1,
      price_cross: true, reason_codes: [], triggered: true, volume: 200, volume_ok: true, volume_ratio: 2,
      volume_ratio_raw: 2, volume_threshold: 1.5, volume_window: 20 },
    weekly: { completed_bars: 40, dea: .2, dif: .1, evaluated: true, golden_cross: false,
      minimum_bars: 35, now_above: false, previous_at_or_below: true, previous_dea: .2, previous_dif: .1,
      previous_week_date: '2026-10-09', reason_codes: ['no_weekly_golden_cross'], triggered: false, week_date: '2026-10-16' },
    rule_attribution: { breakout_ma: 60, daily_volume_breakout: false, decision_date: '2026-10-16',
      decision_source_sha256: 'a'.repeat(64), rule_sha256: '49114a4b8a79d23201a0d25ebbc26215fa97f6b9f77bc0ff7d16f7424e06a1ec',
      run_id: 'synthetic-run', schema_version: 'quant.etf-run-rule.v1', scope: 'diagnostic_switch_and_pool_not_full_strategy',
      target_codes: ['510300'], target_count: 1, volume_window: 20 }
  }
}
const backendBinding = { runId: 'synthetic-run', decisionDate: '2026-10-16', code: '510300', targetCodes: ['510300'] }
function poolWithBackendDiagnostic (diagnostic) {
  return { schema_version: 'quant.etf-pool.v1', configured_count: 1, run_id: backendBinding.runId,
    calendar_available: true, expected_date: backendBinding.decisionDate, decision_date: backendBinding.decisionDate, run_status: 'success',
    items: [{ code: '510300', state: 'no_trigger', is_current: true, data_status: 'ok',
      decision_date: backendBinding.decisionDate, data_date: backendBinding.decisionDate, reference_close: 1.1,
      trigger_diagnostics: diagnostic }] }
}

test('actual backend disabled-rule contract never renders a counterfactual as a trigger', () => {
  const input = backendDisabledFixture()
  const before = JSON.stringify(input)
  const view = triggerDiagnosticView(input, backendBinding)
  assert.equal(view.status, 'recorded')
  assert.equal(view.dailyEnabled, false)
  assert.equal(view.daily, '本次规则未启用日线条件（仅反事实诊断，不算触发）')
  assert.doesNotMatch(view.daily, /新上穿且量能达标|风控另判/)
  assert.equal(JSON.stringify(input), before)
})

test('disabled incomplete daily branch does not make a completed enabled weekly branch uncertain', () => {
  const diagnostic = backendDisabledFixture()
  diagnostic.daily.evaluated = false
  diagnostic.daily.reason_codes = ['volume_missing']
  diagnostic.daily.volume_ok = null
  diagnostic.daily.triggered = null
  const pool = poolWithBackendDiagnostic(diagnostic)
  const before = JSON.stringify(pool)
  const view = etfPoolView(pool)
  assert.equal(view.items[0].conditionUncertain, false)
  assert.equal(view.items[0].current, true)
  assert.equal(view.counts.no_trigger, 1)
  assert.equal(view.pendingCount, 0)
  assert.match(view.items[0].diagnostics.daily, /未启用日线条件/)
  assert.equal(JSON.stringify(pool), before)
})

test('enabled weekly branch still requires completion even when daily is disabled', () => {
  const diagnostic = backendDisabledFixture()
  diagnostic.weekly.evaluated = false
  const view = etfPoolView(poolWithBackendDiagnostic(diagnostic))
  assert.equal(view.items[0].conditionUncertain, true)
  assert.equal(view.counts.no_trigger, 0)
  assert.equal(view.counts.unknown, 1)
})

test('a self-asserted disabled flag without frozen attribution cannot relax legacy validation', () => {
  for (const changes of [
    { rule_attribution: null, rule_attribution_status: 'unavailable', validation_basis: 'legacy_strict_unattributed' },
    { rule_attribution: undefined, rule_attribution_status: undefined, validation_basis: undefined },
    { rule_attribution_status: 'unavailable' }, { validation_basis: 'legacy_strict_unattributed' }
  ]) {
    const diagnostic = { ...backendDisabledFixture(), ...changes }
    assert.equal(triggerDiagnosticView(diagnostic, backendBinding).status, 'invalid')
    const view = etfPoolView(poolWithBackendDiagnostic(diagnostic))
    assert.equal(view.items[0].conditionUncertain, true)
    assert.equal(view.counts.no_trigger, 0)
  }
})

test('malformed run, date, pool, switch or source binding cannot disable the daily branch', () => {
  for (const changes of [
    { run_id: 'other' }, { decision_date: '2026-10-15' }, { schema_version: 'unknown' },
    { scope: 'full_strategy_verified' }, { daily_volume_breakout: true }, { breakout_ma: 61 },
    { volume_window: 21 }, { decision_source_sha256: 'a'.repeat(8) }, { rule_sha256: 'bad' },
    { target_codes: ['510301'] }, { target_codes: ['510300', '510300'], target_count: 2 }, { target_count: true }
  ]) {
    const diagnostic = backendDisabledFixture()
    Object.assign(diagnostic.rule_attribution, changes)
    assert.equal(triggerDiagnosticView(diagnostic, backendBinding).status, 'invalid', JSON.stringify(changes))
  }
})

test('all declared attribution fields and the complete pool binding are required', () => {
  for (const key of ['rule_attribution_status', 'validation_basis', 'rule_attribution']) {
    const diagnostic = backendDisabledFixture()
    delete diagnostic[key]
    assert.equal(triggerDiagnosticView(diagnostic, backendBinding).status, 'invalid', key)
  }
  for (const bound of [
    { ...backendBinding, code: '510301' }, { ...backendBinding, targetCodes: ['510300', '510301'] },
    { ...backendBinding, targetCodes: [] }, { ...backendBinding, targetCodes: undefined }
  ]) assert.equal(triggerDiagnosticView(backendDisabledFixture(), bound).status, 'invalid')
})

test('frozen enabled branch keeps its evaluation requirement and trigger wording', () => {
  const diagnostic = backendDisabledFixture()
  diagnostic.rule_attribution.daily_volume_breakout = true
  diagnostic.daily.enabled = true
  const view = triggerDiagnosticView(diagnostic, backendBinding)
  assert.equal(view.dailyEnabled, true)
  assert.match(view.daily, /新上穿且量能达标（风控另判）/)
  diagnostic.daily.evaluated = false
  assert.equal(etfPoolView(poolWithBackendDiagnostic(diagnostic)).items[0].conditionUncertain, true)
})

test('unattributed legacy incomplete daily branch stays uncertain without consulting current settings', () => {
  const diagnostic = fixture()
  diagnostic.rule_attribution_status = 'unavailable'
  diagnostic.validation_basis = 'legacy_strict_unattributed'
  diagnostic.rule_attribution = null
  diagnostic.daily.evaluated = false
  const p = poolWithBackendDiagnostic(diagnostic)
  p.run_id = binding.runId
  p.expected_date = binding.decisionDate
  p.decision_date = binding.decisionDate
  Object.assign(p.items[0], { decision_date: binding.decisionDate, data_date: binding.decisionDate })
  p.current_config = { daily_volume_breakout: false }
  assert.equal(etfPoolView(p).items[0].conditionUncertain, true)
})

test('bound volume-window label follows recorded window, not an unconditional twenty days', () => {
  const diagnostic = backendDisabledFixture()
  diagnostic.rule_attribution.volume_window = 30
  diagnostic.daily.volume_window = 30
  const view = triggerDiagnosticView(diagnostic, backendBinding)
  assert.match(view.details.join('\n'), /前30日均量/)
  assert.doesNotMatch(view.details.join('\n'), /前20日均量/)
})

