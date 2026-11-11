import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { reportIdentity, captureAvailable } from '../../src/utils/nng8ResearchLink.js'
import { evidenceSourcesResponse, filterEvidenceSources } from '../../src/utils/controlledEvidence.js'
import { recordedDataQuality, qualityRunProjection } from '../../src/utils/researchRunQuality.js'

const hash = 'a'.repeat(64)
const stamp = '2026-10-09T15:10:00+08:00'
const report = (type = 'profit') => ({ id: type === 'profit' ? 'quant:profit:ec6c86fd856c:' + hash : 'quant:etf:effectiveness:' + hash, source: 'quant/' + type, content_sha256: hash, captured_at: type === 'profit' ? stamp : null, generated_at: type === 'profit' ? stamp : null, data_cutoff: null, status: 'ok', title: type === 'profit' ? 'Profit 盈利研究' : 'ETF 验证快照', body_truncated: false, body_block_count: 1, blocks: [{ heading: '完整来源', text: type === 'profit' ? '完整 Profit 报告' : '{"sample_size":1,"minimum_samples":30}' }] })
const response = rows => ({ code: 1, data: { contract_version: 'workbench.reports.v1', research_evidence: rows } })
const p = { id: 'p', subject: { symbol: '600875' } }; const b = { id: 'b', projectId: 'p' }
const r = { id: 'r', branchId: 'b', resultArtifactId: 'a', status: 'succeeded', execution: { id: 'e' } }
const a = { id: 'a', kind: 'run_result', branchId: 'b', payload: { requestId: 'r', executionId: 'e', dataFingerprint: hash } }
const quality = () => ({ contractVersion: 'research.run-data-quality.v1', evidenceScope: 'selected_run', runId: 'r', executionId: 'e', status: 'partial', observedBars: 10, expectedBars: null, missingBars: null, missingDates: null, invalidBars: 0, duplicateBars: 0, provider: 'recorded-provider', actualRange: ['2026-01-05', '2026-01-16'], dataFingerprint: hash, dataVersion: null, metricsVersion: 'metrics.v2', adjustmentMode: 'qfq', calendarSource: null, unknownReasons: { expectedBars: '这次运行未保存权威交易日历' } })

test('Profit and ETF capture use exact server-defined identity and preserve unknown generation time', () => {
  for (const type of ['profit', 'etf']) {
    const value = report(type); const identity = reportIdentity(value, value.captured_at)
    assert.equal(identity.reportId, value.id); assert.equal(identity.contentSha256, hash)
    assert.equal(identity.capturedAt, value.captured_at)
  }
  assert.equal(report('etf').generated_at, null)
})
test('arbitrary Quant sources, nonwhitelist jobs and incomplete content cannot be archived', () => {
  for (const edit of [{ source: 'quant/intake' }, { id: 'quant:profit:other:' + hash }, { body_truncated: true }, { body_block_count: 2 }, { status: 'stale' }, { content_sha256: 'b'.repeat(64) }]) assert.equal(reportIdentity({ ...report(), ...edit }, stamp), null)
  assert.equal(reportIdentity(report('etf'), undefined), null)
})
test('Profit / ETF archive admission is separate from disabled nnG8 admission and respects per-source list', () => {
  const permission = { enabled: true, write: true, externalEvidence: { enabled: false }, reportEvidence: { enabled: true, schemaReady: true, capture: true, contractVersion: 'research.external-evidence.v1', sources: ['profit', 'etf'] } }
  assert.equal(captureAvailable(permission, 'quant/profit'), true); assert.equal(captureAvailable(permission, 'quant/etf'), true)
  assert.equal(captureAvailable(permission, 'nng8/astock'), false)
  for (const changed of [{ schemaReady: false }, { capture: false }, { sources: ['profit'] }, { contractVersion: 'unreviewed' }]) assert.equal(captureAvailable({ ...permission, reportEvidence: { ...permission.reportEvidence, ...changed } }, 'quant/etf'), false)
})
test('evidence sources require reviewed contract; duplicate identities disable every copy', () => {
  assert.throws(() => evidenceSourcesResponse({ code: 1, data: { research_evidence: [report()] } }))
  const rows = evidenceSourcesResponse(response([report(), report(), report('etf')]))
  assert.equal(rows[0].identity, null); assert.equal(rows[1].identity, null); assert.ok(rows[2].identity)
  assert.notEqual(rows[0].key, rows[1].key)
})
test('Profit full text and ETF JSON search remain source-backed and read-only', () => {
  const rows = evidenceSourcesResponse(response([report(), report('etf')]))
  assert.equal(filterEvidenceSources(rows, 'sample_size')[0].report.source, 'quant/etf')
  assert.equal(filterEvidenceSources(rows, '完整 Profit')[0].report.source, 'quant/profit')
  assert.deepEqual(rows[1].report.blocks, report('etf').blocks)
})
test('saved-run quality shows actual counts and provider, without manufacturing expected calendar', () => {
  const view = qualityRunProjection(p, b, { ...r, dataQuality: quality() }, a)
  assert.deepEqual(view.actual, ['2026-01-05', '2026-01-16']); assert.equal(view.quality.observedBars, 10)
  assert.equal(view.quality.invalidBars, 0); assert.equal(view.quality.duplicateBars, 0)
  assert.equal(view.quality.provider, 'recorded-provider'); assert.equal(view.adjustment, 'qfq'); assert.equal(view.metricsVersion, 'metrics.v2')
  assert.equal(view.quality.expectedBars, null); assert.equal(view.coverage, null); assert.equal(view.missingBars, null)
  assert.match(view.quality.unknownReasons.expectedBars, /未保存权威交易日历/)
})
test('quality never adopts another run, execution, global scope or content fingerprint', () => {
  for (const edit of [{ runId: 'foreign' }, { runId: null }, { executionId: undefined }, { executionId: 'foreign' }, { evidenceScope: 'global_summary' }, { dataFingerprint: 'foreign' }]) assert.throws(() => recordedDataQuality({ ...r, dataQuality: { ...quality(), ...edit } }, a), /不一致/)
})
test('invalid counts, malformed dates and absent calendar do not assert zero gaps or complete coverage', () => {
  const value = recordedDataQuality({ ...r, dataQuality: { ...quality(), observedBars: -1, expectedBars: 10, missingBars: 0, missingDates: [], coverageRatio: 1 } }, a)
  assert.equal(value.observedBars, null); assert.equal(value.coverage, null); assert.equal(value.missingDates, null); assert.equal(value.expectedBars, null); assert.equal(value.missingBars, null)
  const bad = recordedDataQuality({ ...r, dataQuality: { ...quality(), expectedBars: 10, missingBars: 1, calendarSource: 'saved-exchange-calendar', missingDates: ['2026-02-30'] } }, a)
  assert.equal(bad.missingDates, null)
})
test('authoritative saved calendar can show ratio and real gap dates, not current global coverage', () => {
  const value = recordedDataQuality({ ...r, dataQuality: { ...quality(), expectedBars: 11, missingBars: 1, missingDates: ['2026-01-07'], coverageRatio: 10 / 11, calendarSource: 'saved-exchange-calendar' } }, a)
  assert.equal(value.coverage, 10 / 11); assert.deepEqual(value.missingDates, ['2026-01-07'])
  assert.equal(recordedDataQuality(r, a).available, false)
})
test('archive view declares full JSON scope and keeps actual source text unparsed', () => {
  const source = fs.readFileSync(new URL('../../src/components/ControlledResearchEvidence/index.vue', import.meta.url), 'utf8')
  assert.match(source, /ETF 系统有效性 · 完整 JSON 快照/); assert.match(source, /block\.text/)
  assert.doesNotMatch(source, /v-html|innerHTML|startRun|aiChat|putDraft/)
  assert.match(source, /deactivated \(\) \{ this.active = false; this.invalidate\(\) \}/)
})
