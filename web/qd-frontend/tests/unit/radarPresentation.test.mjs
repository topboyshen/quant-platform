import test from 'node:test'
import assert from 'node:assert/strict'

import {
  formatRadarNumber,
  formatRadarTime,
  getRadarFreshness,
  getRadarRiskPresentation,
  localizeAuditStatus,
  localizeAvailability,
  localizeEffectivenessState,
  localizeOutcome,
  localizeRadarStatus,
  localizeReason,
  localizeRiskLevel,
  localizeSignalType,
  parseRadarTime
} from '../../src/utils/radarPresentation.js'

const NOW = Date.parse('2026-07-31T08:00:00Z')

test('localizes every Radar presentation category without leaking unknown raw values', () => {
  assert.equal(localizeRadarStatus(' DEGRADED '), '降级')
  assert.equal(localizeAvailability(false), '不可用')
  assert.equal(localizeRiskLevel('elevated'), '偏高')
  assert.equal(localizeRiskLevel('growth_blocked'), '增长受阻')
  assert.equal(localizeRiskLevel('warning'), '预警')
  assert.equal(localizeSignalType('strong-buy'), '强力买入')
  assert.equal(localizeSignalType('weekly_macd_golden_cross'), '周线 MACD 金叉')
  assert.equal(localizeSignalType('volume_breakout_ma60'), '放量突破 60 日均线')
  assert.equal(localizeAuditStatus('partial'), '部分完成')
  assert.equal(localizeEffectivenessState('insufficient_samples'), '样本不足')
  assert.equal(localizeEffectivenessState('sample_insufficient'), '样本不足')
  assert.equal(localizeOutcome('no_signal'), '无信号')
  assert.equal(localizeOutcome('emitted'), '已生成信号')

  const unknown = 'brand_new_backend_state'
  const labels = [
    localizeRadarStatus(unknown),
    localizeRiskLevel(unknown),
    localizeSignalType(unknown),
    localizeAuditStatus(unknown),
    localizeEffectivenessState(unknown),
    localizeOutcome(unknown)
  ]
  labels.forEach(label => assert.equal(label.includes(unknown), false))
})

test('formats absent Radar metrics as unavailable instead of fabricated zeroes', () => {
  for (const value of [null, undefined, '', '   ']) {
    assert.equal(formatRadarNumber(value), '--')
    assert.notEqual(formatRadarNumber(value), '0.00')
  }

  assert.equal(formatRadarNumber(0), '0.00')
  assert.equal(formatRadarNumber('13.82'), '13.82')
})

test('marks macro risk incomplete when completeness or missing fields say so', () => {
  const metadataIncomplete = getRadarRiskPresentation({
    available: true,
    data_complete: false,
    level: 'warning',
    vix: 13.82,
    us_10y: 4.09,
    missing_fields: []
  })

  assert.equal(metadataIncomplete.state, 'incomplete')
  assert.equal(metadataIncomplete.label, '数据不完整')

  const liveIncompleteSnapshot = getRadarRiskPresentation({
    available: true,
    data_complete: false,
    level: 'warning',
    vix: null,
    us_10y: null,
    missing_fields: ['crude_oil', 'us_10y', 'vix']
  })

  assert.equal(liveIncompleteSnapshot.state, 'incomplete')
  assert.equal(liveIncompleteSnapshot.label, '数据不完整')
  assert.deepEqual(liveIncompleteSnapshot.missingFields, ['crude_oil', 'us_10y', 'vix'])

  const explicitlyMissingMetric = getRadarRiskPresentation({
    available: true,
    data_complete: true,
    level: 'warning',
    vix: 13.82,
    us_10y: 4.09,
    missing_fields: ['vix']
  })

  assert.equal(explicitlyMissingMetric.state, 'incomplete')
  assert.equal(explicitlyMissingMetric.label, '数据不完整')
  assert.deepEqual(explicitlyMissingMetric.missingFields, ['vix'])
})

test('localizes known and patterned reason codes to bounded Chinese descriptions', () => {
  assert.equal(localizeReason('factor_history_insufficient'), '可信因子历史不足')
  assert.equal(localizeReason('risk_snapshot_incomplete'), '宏观风险快照不完整')
  assert.equal(localizeReason('radar_schema_mismatch'), 'Radar 数据格式不兼容')
  assert.equal(localizeReason('vendor_quote_stale'), '数据已陈旧')
  assert.equal(localizeReason('unexpected_vendor_payload'), '其他降级原因')
  assert.equal(localizeReason('unexpected_vendor_payload').includes('unexpected'), false)
})

test('detects fresh, old, lagging, future-dated and missing Radar contexts', () => {
  const fresh = getRadarFreshness({
    generated_at: '2026-07-31T07:30:00Z',
    as_of: '2026-07-31',
    expected_as_of: '2026-07-31'
  }, { now: NOW })
  assert.deepEqual(fresh, { stale: false, state: 'fresh', label: '数据新鲜', reason: '' })

  assert.equal(getRadarFreshness({ generatedAt: '2026-07-30T07:59:59Z' }, { now: NOW }).state, 'stale')
  assert.equal(getRadarFreshness({
    generatedAt: '2026-07-31T07:30:00Z',
    asOf: '2026-07-30',
    expectedAsOf: '2026-07-31'
  }, { now: NOW }).reason, '数据日早于应有数据日')
  assert.equal(getRadarFreshness({ generatedAt: '2026-07-31T08:05:01Z' }, { now: NOW }).state, 'invalid')
  assert.equal(getRadarFreshness({}, { now: NOW }).state, 'unknown')
})

test('parses and formats timestamps while treating invalid input as unavailable', () => {
  assert.equal(parseRadarTime('not-a-time'), null)
  assert.equal(formatRadarTime('not-a-time'), '--')
  assert.equal(formatRadarTime('2026-07-31'), '2026-07-31')
  const formatted = formatRadarTime('2026-07-31T06:30:00Z', { timeZone: 'Asia/Shanghai' })
  assert.match(formatted, /2026/)
  assert.match(formatted, /14[:：]30/)
})
