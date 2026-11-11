import test from 'node:test'
import assert from 'node:assert/strict'
import { effectivenessProgressView } from '../../src/utils/effectivenessProgress.js'

const fixture = () => ({
  state: 'sample_insufficient', sample_size: 0, minimum_samples: 30,
  primary_horizon_sessions: 20,
  progress: {
    schema_version: 'quant.etf-effectiveness-progress.v1', status: 'ok', unit: 'decision_records',
    candidate_records: 5, excluded_records: 4,
    excluded_reasons: { run_not_success: 4, outside_decision_window: 0, non_oos: 0, invalid_record: 0 },
    eligible_records: 1, mature_records: 0, awaiting_outcome_records: 1,
    unfilled_records: 0, invalid_outcome_records: 0,
    awaiting_breakdown: { not_due: 1, due_uncollected: 0, date_unknown: 0 },
    horizon_samples: { 1: 1, 3: 1, 5: 1, 10: 1, 20: 0 },
    latest_evaluation_as_of: '2026-09-15', next_expected_maturity_date: '2026-09-18',
    expected_maturity_is_estimate: true
  }
})

test('true zero is distinct from pending and excluded decision records', () => {
  const input = fixture()
  const before = JSON.stringify(input)
  const result = effectivenessProgressView(input)
  assert.equal(result.available, true)
  assert.equal(result.label, '样本不足')
  assert.equal(result.primaryHorizon, 20)
  assert.equal(result.sampleSize, 0)
  assert.equal(result.minimumSamples, 30)
  assert.equal(result.detailsAvailable, true)
  assert.equal(result.progress.eligible, 1)
  assert.equal(result.progress.excluded, 4)
  assert.equal(result.progress.awaiting, 1)
  assert.equal(result.progress.shortHorizons.length, 4)
  assert.equal(result.progress.nextMaturity, '2026-09-18')
  assert.equal(JSON.stringify(input), before)
})

test('only a saved mature outcome changes maturity; dates never do', () => {
  const input = fixture()
  input.sample_size = 1
  Object.assign(input.progress, {
    mature_records: 1, awaiting_outcome_records: 0,
    awaiting_breakdown: { not_due: 0, due_uncollected: 0, date_unknown: 0 },
    next_expected_maturity_date: null
  })
  input.progress.horizon_samples[20] = 1
  assert.equal(effectivenessProgressView(input).sampleSize, 1)
  assert.equal(effectivenessProgressView(input).progress.awaiting, 0)
  assert.equal(effectivenessProgressView(fixture()).sampleSize, 0)
})

const brokenCore = [
  ['absent', undefined], ['null', null], ['empty', {}], ['array', []],
  ['missing sample', { sample_size: undefined }], ['null sample', { sample_size: null }],
  ['numeric string', { sample_size: '0' }], ['boolean', { sample_size: false }],
  ['negative', { sample_size: -1 }], ['fraction', { sample_size: 0.5 }],
  ['nan', { sample_size: NaN }], ['infinity', { sample_size: Infinity }],
  ['unsafe integer', { sample_size: Number.MAX_SAFE_INTEGER + 1 }],
  ['missing threshold', { minimum_samples: undefined }], ['zero threshold', { minimum_samples: 0 }],
  ['invalid flag', { invalid: true }], ['unknown state', { state: 'surprise' }],
  ['invalid state', { state: 'invalid' }], ['missing state', { state: undefined }],
  ['bad horizon', { primary_horizon_sessions: 7 }], ['missing horizon', { primary_horizon_sessions: undefined }],
  ['positive label with insufficient evidence', { state: 'preliminary_effective' }],
  ['insufficient label contradicts sample', { sample_size: 30 }],
  ['wrong method', { method: 'in_sample' }], ['invalid metrics type', { metrics_available: 'false' }],
  ['metrics contradict insufficient sample', { metrics_available: true }]
]
for (const [name, patch] of brokenCore) {
  test(`unavailable core clears every value: ${name}`, () => {
    const input = ['absent', 'null', 'empty', 'array'].includes(name) ? patch : { ...fixture(), ...patch }
    const result = effectivenessProgressView(input)
    assert.equal(result.available, false)
    assert.equal(result.label, '证据不可用')
    assert.equal(result.sampleSize, '--')
    assert.equal(result.minimumSamples, '--')
    assert.equal(result.primaryHorizon, null)
    assert.equal(result.progress, null)
    assert.equal(result.detailsAvailable, false)
  })
}

test('request failure overrides even valid previous core and progress', () => {
  const result = effectivenessProgressView(fixture(), { unavailable: true })
  assert.equal(result.label, '证据不可用')
  assert.equal(result.sampleSize, '--')
  assert.equal(result.progress, null)
})

test('old backend core is retained without manufacturing progress', () => {
  const input = fixture()
  delete input.progress
  input.sample_size = 4
  const result = effectivenessProgressView(input)
  assert.equal(result.sampleSize, 4)
  assert.equal(result.minimumSamples, 30)
  assert.equal(result.label, '样本不足')
  assert.equal(result.progress, null)
  assert.equal(result.detailMessage, '进度明细暂不可用')
})

const brokenProgress = [
  ['unknown schema', p => { p.schema_version = 'future' }],
  ['wrong unit', p => { p.unit = 'trades' }],
  ['invalid status', p => { p.status = 'invalid' }],
  ['unavailable status', p => { p.status = 'unavailable' }],
  ['truncated status', p => { p.status = 'truncated' }],
  ['negative', p => { p.eligible_records = -1 }],
  ['fraction', p => { p.eligible_records = 0.5 }],
  ['nan', p => { p.eligible_records = NaN }],
  ['infinity', p => { p.eligible_records = Infinity }],
  ['boolean', p => { p.eligible_records = true }],
  ['numeric string', p => { p.eligible_records = '1' }],
  ['over scan cap', p => { p.candidate_records = 2001 }],
  ['candidate balance', p => { p.candidate_records = 6 }],
  ['exclusion balance', p => { p.excluded_reasons.run_not_success = 3 }],
  ['eligible balance', p => { p.eligible_records = 2; p.candidate_records = 6 }],
  ['pending balance', p => { p.awaiting_breakdown.not_due = 0 }],
  ['mature mismatch', p => { p.mature_records = 1; p.eligible_records = 2; p.candidate_records = 6 }],
  ['primary horizon mismatch', p => { p.horizon_samples[20] = 1 }],
  ['horizon exceeds eligible', p => { p.horizon_samples[3] = 2 }],
  ['missing horizon', p => { delete p.horizon_samples[3] }],
  ['horizon invalid type', p => { p.horizon_samples[3] = null }],
  ['unknown exclusion', p => { p.excluded_reasons.new_reason = 0 }],
  ['missing pending reason', p => { delete p.awaiting_breakdown.date_unknown }],
  ['non date', p => { p.latest_evaluation_as_of = 'now' }],
  ['non canonical date', p => { p.latest_evaluation_as_of = '2026-9-15' }],
  ['impossible date', p => { p.latest_evaluation_as_of = '2026-02-30' }],
  ['missing date field', p => { delete p.latest_evaluation_as_of }],
  ['bad maturity date', p => { p.next_expected_maturity_date = '2026-13-01' }],
  ['non estimate', p => { p.expected_maturity_is_estimate = false }],
  ['date despite no pending estimate', p => { p.awaiting_breakdown.not_due = 0; p.awaiting_breakdown.date_unknown = 1 }]
]
for (const [name, change] of brokenProgress) {
  test(`bad progress preserves true core but clears all explanatory values: ${name}`, () => {
    const input = fixture()
    change(input.progress)
    const result = effectivenessProgressView(input)
    assert.equal(result.available, true)
    assert.equal(result.sampleSize, 0)
    assert.equal(result.minimumSamples, 30)
    assert.equal(result.detailsAvailable, false)
    assert.equal(result.progress, null)
    assert.match(result.detailMessage, /进度明细暂不可用/)
    assert.notEqual(result.color, 'green')
  })
}

test('all known zero counts remain real zero with unavailable dates', () => {
  const input = fixture()
  const p = input.progress
  for (const key of ['candidate_records', 'excluded_records', 'eligible_records', 'awaiting_outcome_records']) p[key] = 0
  p.excluded_reasons.run_not_success = 0
  p.awaiting_breakdown.not_due = 0
  for (const key of Object.keys(p.horizon_samples)) p.horizon_samples[key] = 0
  p.next_expected_maturity_date = null
  p.latest_evaluation_as_of = null
  const result = effectivenessProgressView(input)
  assert.equal(result.detailsAvailable, true)
  assert.equal(result.progress.eligible, 0)
  assert.equal(result.progress.awaiting, 0)
  assert.equal(result.progress.excluded, 0)
})

test('due pending is neutral, calendar-unknown is not fabricated as zero', () => {
  const input = fixture()
  input.progress.awaiting_breakdown = { not_due: 0, due_uncollected: 1, date_unknown: 0 }
  input.progress.next_expected_maturity_date = null
  const due = effectivenessProgressView(input)
  assert.equal(due.detailsAvailable, true)
  assert.deepEqual(due.progress.awaitingReasons[1], { key: 'due_uncollected', label: '已到预计日期，待结果', count: 1 })
  assert.notEqual(due.color, 'red')
  input.progress.awaiting_breakdown = { not_due: 0, due_uncollected: 0, date_unknown: 1 }
  const unknown = effectivenessProgressView(input)
  assert.equal(unknown.progress.awaitingReasons[2].count, 1)
  assert.equal(unknown.progress.awaiting, 1)
})

test('unfilled and invalid outcomes are separate from awaiting and mature records', () => {
  const input = fixture()
  Object.assign(input.progress, { candidate_records: 7, eligible_records: 3, unfilled_records: 1, invalid_outcome_records: 1 })
  const result = effectivenessProgressView(input)
  assert.equal(result.detailsAvailable, true)
  assert.equal(result.progress.unfilled, 1)
  assert.equal(result.progress.invalidOutcome, 1)
  assert.equal(result.progress.awaiting, 1)
  assert.equal(result.sampleSize, 0)
})

test('uses actual primary horizon, never sums overlapping short-horizon observations', () => {
  const input = fixture()
  input.primary_horizon_sessions = 5
  input.progress.horizon_samples[5] = 0
  const result = effectivenessProgressView(input)
  assert.equal(result.primaryHorizon, 5)
  assert.deepEqual(result.progress.shortHorizons, [{ days: 1, count: 1 }, { days: 3, count: 1 }])
  assert.equal(result.progress.eligible, 1)
  assert.equal(result.sampleSize, 0)
})

test('current backend states and legacy aliases never claim profitability', () => {
  for (const state of ['uncertain', 'preliminary_effective', 'not_effective', 'preliminary', 'effective', 'ineffective']) {
    const result = effectivenessProgressView({ state, sample_size: 30, minimum_samples: 30, primary_horizon_sessions: 20 })
    assert.equal(result.available, true)
    assert.equal(result.sampleSize, 30)
    assert.notEqual(result.color, 'green')
    assert.doesNotMatch(result.label, /盈利|赚钱/)
  }
})

test('optional core metadata must match the saved policy result when present', () => {
  const input = { ...fixture(), method: 'live_oos_or_walk_forward_oos', metrics_available: false }
  assert.equal(effectivenessProgressView(input).available, true)
  input.state = 'uncertain'
  input.sample_size = 30
  assert.equal(effectivenessProgressView(input).available, false)
  input.metrics_available = true
  assert.equal(effectivenessProgressView(input).available, true)
})
