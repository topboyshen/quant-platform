import test from 'node:test'
import assert from 'node:assert/strict'
import { effectivenessDisplayView, effectivenessProgressView } from '../../src/utils/effectivenessProgress.js'

const core = { state: 'sample_insufficient', sample_size: 1, minimum_samples: 30, primary_horizon_sessions: 20, method: 'live_oos_or_walk_forward_oos', metrics_available: false }
const limitations = ['failed_or_missing_dates_not_included', 'run_rule_and_pool_attribution_unverified', 'multiple_attempts_per_date_combined']
const recent = {
  status: 'observed_records', observed_zero_trigger_dates: 29, latest_observed_date: '2026-10-16', latest_observed_trigger_count: 0,
  last_observed_trigger_date: '2026-08-28', scanned_dates: 30, truncated: false, includes_partial_runs: true,
  continuity_verified: false, rule_scope: 'mixed_or_unknown', pool_scope: 'observed_records_only', limitations
}
const replay = {
  schema_version: 'quant.etf-replay-material-assessment.v1', label: '回放·不计入实盘样本', evidence_status: 'unverified',
  included_in_live_samples: false, included_in_formal_samples: false, source_identity_verified: false, metrics_available: false,
  limitations: ['source_identity_not_fully_bound', 'self_attested_summary_only', 'risk_gate_not_replayed', 'not_natural_observations', 'not_formal_samples']
}
const display = { schema_version: 'quant.etf-effectiveness-display.v2', strict: { sample_size: 1, minimum_samples: 30, rule: 'success_runs_only' }, recent, replay }
const withRecent = changes => ({ ...display, recent: { ...recent, ...changes } })
const emptyRecent = {
  ...recent, status: 'empty', observed_zero_trigger_dates: 0, latest_observed_date: null, latest_observed_trigger_count: null,
  last_observed_trigger_date: null, scanned_dates: 0, truncated: false, includes_partial_runs: false
}

test('observed dates are not described as continuous days or a current-rule streak', () => {
  const view = effectivenessDisplayView(display, core)
  assert.match(view.recentText, /已有 1\/30，最近 29 个有数据记录日期未触发/)
  assert.match(view.recentText, /非连续交易日；含部分批次；规则与标的池归属未核齐/)
  assert.doesNotMatch(view.recentText, /近 29 日 0 触发|连续 29|当前规则/)
})

test('no stored rows explicitly means unavailable trigger knowledge, not a trigger', () => {
  const view = effectivenessDisplayView({ ...display, recent: emptyRecent }, core)
  assert.match(view.recentText, /已有 1\/30，暂无可用触发记录/)
  assert.match(view.recentText, /不能据此判断最近交易日有无触发/)
  assert.doesNotMatch(view.recentText, /最近交易日有触发|近 0 日 0 触发|最近 0 个/)
})

test('failed-only or missing-table evidence is not shown as no signal or success', () => {
  const view = effectivenessDisplayView({ ...display, recent: { ...emptyRecent, status: 'unavailable' } }, core)
  assert.equal(view.recentText, '已有 1/30，近期触发证据不可用')
  assert.doesNotMatch(view.recentText, /有触发|未触发|无信号|成功/)
})

test('zero length alone is insufficient to claim latest observed trigger', () => {
  const view = effectivenessDisplayView(withRecent({ observed_zero_trigger_dates: 0 }), core)
  assert.match(view.recentText, /证据不完整或不一致/)
  assert.doesNotMatch(view.recentText, /最近有数据记录（.*）有触发/)
})

test('a positive saved count may describe latest observed date, not latest market session', () => {
  const view = effectivenessDisplayView(withRecent({ observed_zero_trigger_dates: 0, latest_observed_trigger_count: 2, last_observed_trigger_date: '2026-10-16' }), core)
  assert.match(view.recentText, /最近有数据记录（2026-10-16）有触发/)
  assert.match(view.recentText, /非连续交易日/)
  assert.doesNotMatch(view.recentText, /最近交易日有触发/)
})

test('full batches remove partial qualifier but never infer complete calendar or rule binding', () => {
  const view = effectivenessDisplayView(withRecent({ includes_partial_runs: false }), core)
  assert.doesNotMatch(view.recentText, /含部分批次/)
  assert.match(view.recentText, /非连续交易日；规则与标的池归属未核齐/)
})

test('scan truncation is labelled as a lower bound of observed dates', () => {
  const view = effectivenessDisplayView(withRecent({ observed_zero_trigger_dates: 400, scanned_dates: 400, last_observed_trigger_date: null, truncated: true }), core)
  assert.match(view.recentText, /最近 400\+ 个有数据记录日期未触发/)
})

test('truncation and last-trigger date must agree with the observed record counts', () => {
  for (const changes of [
    { truncated: true }, { last_observed_trigger_date: null }, { observed_zero_trigger_dates: 30 },
    { last_observed_trigger_date: '2026-10-16' },
    { observed_zero_trigger_dates: 0, latest_observed_trigger_count: 1, last_observed_trigger_date: '2026-10-15' }
  ]) assert.match(effectivenessDisplayView(withRecent(changes), core).recentText, /证据不完整或不一致/)
})

test('mixed or unknown historical rules stay qualified despite current daily branch off', () => {
  const mixed = { ...display, daily_breakout: false, current_pool_size: 40, current_rule_version: 'approved-20261017' }
  const view = effectivenessDisplayView(mixed, core)
  assert.match(view.recentText, /规则与标的池归属未核齐/)
  assert.doesNotMatch(view.recentText, /40只|新版规则已验证|周线规则连续/)
})

test('a self-asserted current rule scope is not accepted as version binding', () => {
  assert.match(effectivenessDisplayView(withRecent({ rule_scope: 'current_rule_verified' }), core).recentText, /证据不完整或不一致/)
})

test('missing scope, status and completeness fields cannot silently use v1 semantics', () => {
  for (const key of ['status', 'continuity_verified', 'rule_scope', 'pool_scope', 'includes_partial_runs', 'truncated', 'limitations', 'latest_observed_trigger_count', 'scanned_dates']) {
    const value = { ...recent }
    delete value[key]
    assert.match(effectivenessDisplayView({ ...display, recent: value }, core).recentText, /证据不完整或不一致/, key)
  }
})

test('incomplete limitation list is not represented as verified complete coverage', () => {
  for (const missing of limitations) {
    const value = { limitations: limitations.filter(item => item !== missing) }
    assert.match(effectivenessDisplayView(withRecent(value), core).recentText, /证据不完整或不一致/)
  }
})

test('invalid numeric counts, impossible dates and inconsistent flags are rejected', () => {
  for (const changes of [
    { observed_zero_trigger_dates: -1 }, { observed_zero_trigger_dates: 1.5 }, { observed_zero_trigger_dates: '29' },
    { scanned_dates: 2 }, { scanned_dates: Number.POSITIVE_INFINITY }, { latest_observed_trigger_count: -1 },
    { latest_observed_trigger_count: null }, { latest_observed_date: null }, { latest_observed_date: '2026-02-30' },
    { latest_observed_date: '2026-10-16T15:30:00+08:00' }, { last_observed_trigger_date: '2026-10-17' },
    { includes_partial_runs: 'false' }, { continuity_verified: true }, { truncated: 'false' }, { status: 'success' }
  ]) assert.match(effectivenessDisplayView(withRecent(changes), core).recentText, /证据不完整或不一致/, JSON.stringify(changes))
})

test('an empty status with contradictory rows or a trigger cannot become empty success', () => {
  for (const changes of [
    { scanned_dates: 1 }, { observed_zero_trigger_dates: 1 }, { latest_observed_date: '2026-10-16' },
    { latest_observed_trigger_count: 0 }, { last_observed_trigger_date: '2026-10-16' }, { truncated: true }, { includes_partial_runs: true }
  ]) assert.match(effectivenessDisplayView({ ...display, recent: { ...emptyRecent, ...changes } }, core).recentText, /证据不完整或不一致/)
})

test('v1, future, missing or malformed display versions never render old trigger claims', () => {
  for (const version of ['quant.etf-effectiveness-display.v1', 'quant.etf-effectiveness-display.v3', '', null, undefined]) {
    assert.deepEqual(effectivenessDisplayView({ ...display, schema_version: version }, core), { recentText: '', replayText: '' })
  }
  for (const value of [null, undefined, [], 'display']) assert.deepEqual(effectivenessDisplayView(value, core), { recentText: '', replayText: '' })
})

test('strict count mismatch and invalid core suppress the companion count', () => {
  for (const strict of [null, { sample_size: 5, minimum_samples: 30 }, { sample_size: 1, minimum_samples: 31 }]) {
    assert.equal(effectivenessDisplayView({ ...display, strict }, core).recentText, '')
  }
  for (const value of [null, { ...core, invalid: true }, { ...core, sample_size: 40 }, { ...core, minimum_samples: 0 }]) {
    assert.equal(effectivenessDisplayView(display, value).recentText, '')
  }
})

test('replay material gets a verification warning, not quantitative profit claims', () => {
  const view = effectivenessDisplayView(display, core)
  assert.equal(view.replayText, '回放材料未核验：来源与输入身份未确认，不计入正式或实盘样本，不构成盈利证明。')
  assert.doesNotMatch(view.replayText, /N=|命中|净收益|34|35%|初步有效/)
})

test('even supplied plausible self-attested return metrics remain undisplayed', () => {
  const material = { ...replay, sample_size: 34, hit_rate_wilson95: { rate: 0.99, low: 0.9, high: 1 }, net_return_mean_ci95: { mean: 5, low: 4, high: 6 }, source_sha256_8: '39132667' }
  const view = effectivenessDisplayView({ ...display, replay: material }, core)
  assert.match(view.replayText, /未核验/)
  assert.doesNotMatch(view.replayText, /99%|500|命中|净收益/)
})

test('original self-asserted replay artifact is not an assessment contract', () => {
  const raw = { ...replay, schema_version: 'quant.etf-walk-forward-replay.v1', split: 'walk_forward_oos', sample_size: 34 }
  assert.equal(effectivenessDisplayView({ ...display, replay: raw }, core).replayText, '')
})

test('replay labelled verified or admitted is not a supported source of profit evidence', () => {
  for (const changes of [
    { evidence_status: 'verified' }, { source_identity_verified: true }, { metrics_available: true },
    { included_in_live_samples: true }, { included_in_formal_samples: true }, { label: '盈利证明' },
    { schema_version: 'quant.etf-replay-material-assessment.v2' }
  ]) assert.equal(effectivenessDisplayView({ ...display, replay: { ...replay, ...changes } }, core).replayText, '')
})

test('missing replay safety flags do not use permissive defaults', () => {
  for (const key of ['schema_version', 'evidence_status', 'source_identity_verified', 'metrics_available', 'included_in_live_samples', 'included_in_formal_samples']) {
    const value = { ...replay }
    delete value[key]
    assert.equal(effectivenessDisplayView({ ...display, replay: value }, core).replayText, '', key)
  }
  assert.equal(effectivenessDisplayView({ ...display, replay: null }, core).replayText, '')
})

test('companion display does not modify strict core, progress, input rows or replay', () => {
  const saved = structuredClone({ display, core })
  const before = JSON.stringify(saved)
  const strictBefore = effectivenessProgressView(saved.core)
  effectivenessDisplayView(saved.display, saved.core)
  assert.equal(JSON.stringify(saved), before)
  assert.deepEqual(effectivenessProgressView(saved.core), strictBefore)
  assert.equal(strictBefore.sampleSize, 1)
  assert.equal(strictBefore.minimumSamples, 30)
  assert.equal(strictBefore.label, '样本不足')
})

test('genuine saved mature/progress counts remain unchanged and replay adds no samples', () => {
  const strict = { ...core, progress: {
    schema_version: 'quant.etf-effectiveness-progress.v1', status: 'ok', unit: 'decision_records',
    candidate_records: 9, excluded_records: 4, eligible_records: 5, mature_records: 1, awaiting_outcome_records: 4,
    unfilled_records: 0, invalid_outcome_records: 0,
    excluded_reasons: { run_not_success: 4, outside_decision_window: 0, non_oos: 0, invalid_record: 0 },
    awaiting_breakdown: { not_due: 4, due_uncollected: 0, date_unknown: 0 },
    horizon_samples: { 1: 5, 3: 3, 5: 1, 10: 1, 20: 1 }, expected_maturity_is_estimate: true,
    latest_evaluation_as_of: '2026-10-16', next_expected_maturity_date: '2026-10-19'
  } }
  const before = effectivenessProgressView(strict)
  assert.equal(before.detailsAvailable, true)
  assert.equal(before.sampleSize, 1)
  assert.equal(before.progress.eligible, 5)
  assert.equal(before.progress.excluded, 4)
  assert.equal(before.progress.awaiting, 4)
  effectivenessDisplayView(display, strict)
  assert.deepEqual(effectivenessProgressView(strict), before)
})

// --- named latest observed triggers (display-only, 2026-11-06 fixture) ---
const todayTriggers = [
  { code: '159869', name: '游戏ETF', decision_state: 'blocked', reason_code: 'macro_risk_blocked' },
  { code: '513050', name: '中概互联网ETF', decision_state: 'blocked', reason_code: 'macro_risk_blocked' }
]
const triggerDay = { observed_zero_trigger_dates: 0, latest_observed_date: '2026-11-06', latest_observed_trigger_count: 2, last_observed_trigger_date: '2026-11-06', scanned_dates: 51 }

test('names blocked triggers with Chinese macro reason and keeps 1/30 and limitations', () => {
  const view = effectivenessDisplayView(withRecent({ ...triggerDay, latest_observed_triggers: todayTriggers }), core)
  assert.equal(view.recentText, '已有 1/30，最近有数据记录（2026-11-06）有触发：159869 游戏ETF、513050 中概互联网ETF，均因宏观风险拦截，不计正式样本（非连续交易日；含部分批次；规则与标的池归属未核齐）')
  assert.doesNotMatch(view.recentText, /blocked|macro_risk/)
})

test('a later no-trigger date still names the last trigger date list', () => {
  const view = effectivenessDisplayView(withRecent({ observed_zero_trigger_dates: 1, latest_observed_date: '2026-11-09', latest_observed_trigger_count: 0, last_observed_trigger_date: '2026-11-06', scanned_dates: 52, latest_observed_triggers: todayTriggers }), core)
  assert.match(view.recentText, /^已有 1\/30，最近 1 个有数据记录日期未触发；上次有触发（2026-11-06）：159869 游戏ETF、513050 中概互联网ETF，均因宏观风险拦截，不计正式样本（非连续交易日；含部分批次；规则与标的池归属未核齐）$/)
})

test('unknown reason uses safe generic wording and still says not counted', () => {
  const view = effectivenessDisplayView(withRecent({ ...triggerDay, latest_observed_triggers: [{ code: '510300', name: null, decision_state: 'weird', reason_code: 'xyz' }] }), core)
  assert.match(view.recentText, /有触发：510300，放行状态未核实，不计正式样本（/)
  assert.doesNotMatch(view.recentText, /xyz|weird/)
  const mixed = effectivenessDisplayView(withRecent({ ...triggerDay, latest_observed_triggers: [todayTriggers[0], { code: '510300', name: '沪深300ETF', decision_state: 'blocked', reason_code: 'other' }] }), core)
  assert.match(mixed.recentText, /159869 游戏ETF（宏观风险拦截）、510300 沪深300ETF（状态未核实），不计正式样本/)
})

test('missing, empty, malformed list or invalid recent falls back without naming', () => {
  for (const list of [undefined, [], 'x', [{ code: '<b>' }], [null]]) {
    const view = effectivenessDisplayView(withRecent({ ...triggerDay, latest_observed_triggers: list }), core)
    assert.equal(view.recentText, '已有 1/30，最近有数据记录（2026-11-06）有触发（非连续交易日；含部分批次；规则与标的池归属未核齐）')
  }
  const all = effectivenessDisplayView(withRecent({ last_observed_trigger_date: null, observed_zero_trigger_dates: 30, latest_observed_triggers: todayTriggers }), core)
  assert.doesNotMatch(all.recentText, /513050|159869/)
  const bad = effectivenessDisplayView(withRecent({ observed_zero_trigger_dates: 0, latest_observed_triggers: todayTriggers }), core)
  assert.match(bad.recentText, /证据不完整或不一致/)
  assert.doesNotMatch(bad.recentText, /513050|159869/)
})
