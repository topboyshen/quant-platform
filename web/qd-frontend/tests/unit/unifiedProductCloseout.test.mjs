// Product closeout regression: synthetic immutable sources and actual Vue methods.
// No network, model call, production storage or research execution is used.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { compileTemplate } from 'vue/compiler-sfc'
import * as catalog from '../../src/utils/reportCatalog.js'
import * as unified from '../../src/utils/unifiedReportCatalog.js'
import * as nng8 from '../../src/utils/nng8Readonly.js'
import { reportIdentity } from '../../src/utils/nng8ResearchLink.js'
import { researchSection } from '../../src/utils/unifiedResearch.js'
import { harness, clone, deferred, httpError } from './researchWorkflowBatch1Harness.mjs'

const hash = 'a'.repeat(64)
const rawHash = 'b'.repeat(64)
const stamp = '2026-10-14T15:10:00+08:00'
const profit = (extra = {}) => ({ id: 'quant:profit:ec6c86fd856c:' + hash, source: 'quant/profit', title: 'Profit 盈利研究', content_sha256: hash, captured_at: stamp, generated_at: stamp, data_cutoff: '2026-10-14', body_truncated: false, body_block_count: 1, status: 'ok', blocks: [{ heading: '原文', text: '唯一正文词 <script>不执行</script>' }], ...extra })
const local = (reports = [], evidence = []) => ({ contract_version: 'workbench.reports.v1', observed_at: stamp, reports, research_evidence: evidence })
const legacy = (sha = hash) => ({ id: 'profit_research', label: '盈利研究（ec6c86fd856c/latest.md）', status: 'ok', format: 'markdown', summary: { content_sha256: sha, evidence_report_id: profit().id } })
const astock = (extra = {}) => ({ id: 'nng8:astock:2026-10-14:' + rawHash, source: 'nng8/astock', status: 'available', title: 'AStock 晨报', source_report_sha256: rawHash, content_sha256: hash, content_contract: 'nng8.report-text.v2', body_complete: true, body_truncated: false, body_block_count: 1, blocks: [{ heading: '原文', text: '完整来源' }], ...extra })

test('catalogue collapses legacy summary only with the same stable report id and content hash', () => {
  const exact = unified.unifiedReportRows(local([legacy()], [profit()]), null)
  assert.equal(exact.length, 1); assert.equal(exact[0].attachments.length, 1)
  const changed = unified.unifiedReportRows(local([legacy('c'.repeat(64))], [profit()]), null)
  assert.equal(changed.length, 2); assert.equal(changed[0].attachments.length, 0)
  const differentJob = profit({ id: 'quant:profit:026a1083d9e0:' + hash })
  assert.equal(unified.unifiedReportRows(local([legacy()], [differentJob]), null).length, 2)
  const sharedJob = unified.unifiedReportRows(local([legacy(), legacy('c'.repeat(64))], [profit()]), null)
  assert.equal(sharedJob.length, 2)
  assert.equal(sharedJob[0].attachments.length, 1)
  assert.equal(sharedJob[1].contentSha256, 'c'.repeat(64))
})
test('readable report does not turn task failure or DATA_BLOCKED into a business pass', () => {
  const failed = unified.unifiedReportRows(local([], [profit({ business: { taskStatus: 'failed', reason: '入口拒绝' } })]), null)[0]
  assert.equal(failed.readable, true); assert.equal(failed.business.label, '执行失败'); assert.equal(failed.business.tone, 'red'); assert.equal(failed.needsAttention, true)
  const blocked = unified.reportBusiness({ status: 'ok', business: { taskStatus: 'completed', dataStatus: 'blocked', researchStatus: 'DATA_BLOCKED', reason: '缺终点' } })
  assert.equal(blocked.label, '数据阻断'); assert.equal(blocked.reason, '缺终点')
  assert.equal(unified.reportBusiness({ status: 'ok' }).label, '业务结论未知')
  assert.equal(unified.reportBusiness({ status: 'partial', data_mode: 'live' }).label, '来源证据部分可用')
  assert.equal(unified.reportBusiness({ business: { taskStatus: 'completed', dataStatus: 'ready', signalStatus: 'none' } }).label, '检查完成 · 无新信号')
})
test('all-source filtering searches full text, preserves evidence identity and separates ETF snapshots', () => {
  const etf = profit({ id: 'quant:etf:effectiveness:' + hash, source: 'quant/etf', title: 'ETF 完整验证', blocks: [{ text: 'ETF 完整快照' }] })
  const rows = unified.unifiedReportRows(local([], [profit(), etf]), { reports: [astock()], captured_at: stamp, snapshot_stale: false })
  assert.equal(unified.filterUnifiedReports(rows, { query: '唯一正文词' })[0].identity.reportId, profit().id)
  assert.equal(unified.filterUnifiedReports(rows, { group: 'astock' }).length, 1)
  assert.equal(unified.filterUnifiedReports(rows, { group: 'etf' })[0].key, etf.id)
  assert.equal(catalog.groupByFamily(rows).length, 3)
})
test('v2 raw markdown and canonical body hashes are distinct but valid identities', () => {
  assert.deepEqual(reportIdentity(astock(), stamp), { reportId: astock().id, contentSha256: hash, capturedAt: stamp })
  for (const extra of [{ body_complete: false }, { body_truncated: true }, { source_report_sha256: 'c'.repeat(64) }]) assert.equal(reportIdentity(astock(extra), stamp), null)
})
test('frontend v2 projection preserves long Unicode blocks and rejects over-bound bodies without disguising them as complete', () => {
  const dto = report => ({ contract_version: 'workbench.nng8.v1', source: 'nng8', reports: [report], delivery: [], services: [], calibration: [], captured_at: stamp, observed_at: stamp })
  const body = '研'.repeat(3000)
  const complete = nng8.normalizeNng8(dto(astock({ blocks: [{ heading: '完整正文', text: body }] }))).reports[0]
  assert.equal(complete.blocks[0].text, body); assert.equal(complete.body_complete, true); assert.equal(complete.body_truncated, false)
  const oversized = nng8.normalizeNng8(dto(astock({ blocks: [{ text: '研'.repeat(4001) }] }))).reports[0]
  assert.equal(oversized.body_complete, false); assert.equal(oversized.body_truncated, true); assert.equal(reportIdentity(oversized, stamp), null)
})
test('stale synchronized copy is visible but never grants archive eligibility', () => {
  const row = unified.unifiedReportRows(local(), { reports: [astock()], captured_at: stamp, snapshot_stale: true })[0]
  assert.equal(row.identity, null); assert.equal(row.business.label, '来源副本陈旧'); assert.match(row.archiveReason, /陈旧/)
})

function reportPage () {
  const source = readFileSync(new URL('../../src/views/personal-reports/index.vue', import.meta.url), 'utf8')
  const script = source.split('<script>')[1].split('</script>')[0].replace(/import[\s\S]*?from\s+['"][^'"]+['"]\s*/g, '').replace('export default {', 'module.exports = {')
  const api = { getWorkbenchReports: async () => ({ code: 1, data: local([], [profit()]) }), getNng8Readonly: async () => ({ code: 0 }) }
  const module = { exports: {} }
  vm.runInNewContext(script, { module, ...catalog, ...unified, ...nng8, NNG8_READONLY_ENABLED: false, researchSection, Nng8ResearchLink: {}, UnifiedResearchEvidence: {}, getWorkbenchReports: () => api.getWorkbenchReports(), getNng8Readonly: () => api.getNng8Readonly() })
  const component = module.exports
  const page = { $route: { query: {} }, $store: { getters: { userInfo: { id: 1 } }, state: { app: { theme: 'dark' } } } }
  Object.assign(page, component.data.call(page), component.methods)
  for (const [name, getter] of Object.entries(component.computed)) Object.defineProperty(page, name, { get: () => getter.call(page) })
  return { page, api, component, source }
}
test('real catalogue component discards late list responses and makes failure/401 explicit', async () => {
  const h = reportPage(); const old = deferred(); h.api.getWorkbenchReports = () => old.promise
  const first = h.page.fetchReports()
  h.api.getWorkbenchReports = async () => ({ code: 1, data: local([], [profit({ title: '新报告' })]) })
  await h.page.fetchReports(); old.resolve({ code: 1, data: local([], [profit({ title: '旧报告' })]) }); await first
  assert.equal(h.page.rows[0].title, '新报告'); assert.equal(h.page.stateCopy.tone, 'blue')
  h.api.getWorkbenchReports = async () => { throw httpError(503) }; await h.page.fetchReports()
  assert.equal(h.page.state, 'unavailable'); assert.equal(h.page.rows.length, 0); assert.ok(h.page.error)
  h.api.getWorkbenchReports = async () => { throw httpError(401) }; await h.page.fetchReports()
  assert.equal(h.page.state, 'unauthorized'); assert.equal(h.page.rows.length, 0)
})
test('independent source failure preserves the other successful catalogue without declaring all healthy', async () => {
  const h = reportPage(); h.page.nng8Enabled = true
  h.api.getWorkbenchReports = async () => { throw httpError(503) }
  h.api.getNng8Readonly = async () => ({ code: 1, data: { contract_version: 'workbench.nng8.v1', source: 'nng8', reports: [astock()], delivery: [], services: [], calibration: [], captured_at: stamp, observed_at: stamp } })
  await h.page.fetchReports()
  assert.equal(h.page.rows.length, 1); assert.equal(h.page.rows[0].group, 'astock'); assert.equal(h.page.state, 'partial'); assert.ok(h.page.localError)
  h.api.getWorkbenchReports = async () => ({ code: 1, data: local([], [profit()]) }); h.api.getNng8Readonly = async () => { throw httpError(503) }
  await h.page.fetchReports()
  assert.equal(h.page.rows.length, 1); assert.equal(h.page.rows[0].group, 'profit'); assert.equal(h.page.state, 'partial'); assert.ok(h.page.externalError)
})
test('real report template compiles and renders plain source text without duplicate old source panels', () => {
  const h = reportPage(); const template = h.source.split('<template>')[1].split('</template>')[0]
  const result = compileTemplate({ source: h.source.slice(h.source.indexOf('<template>') + 10, h.source.indexOf('<script>')).replace(/<\/template>\s*$/, ''), filename: 'personal-reports.vue', id: 'offline-closeout' })
  assert.deepEqual(result.errors, [])
  assert.match(h.source, /\{\{ block\.text \}\}/); assert.doesNotMatch(h.source, /v-html|<controlled-research-evidence|<nng8-readonly/)
  assert.ok(template.includes('report-source-status') || h.source.includes('selected.business.label'))
})

function editIndicator (h, value) { h.page.ind.draft = { period: value }; h.page.persistLocal('indicator') }
test('late restore for A never overwrites B or removes B local draft', async () => {
  const h = await harness(); const pending = deferred(); h.api.getDraft = () => pending.promise
  const read = h.page.restoreDraft('indicator')
  h.page.invalidateHandoff(); h.page.branch = clone(h.branches.B); h.page.handoffTargetBranchId = 'B'; h.page.handoffBranchReady = true; h.page.handoffInactive = false
  editIndicator(h, 80); const key = h.page.localKey('B', 'indicator'); const before = h.entries.get(key)
  pending.resolve({ data: { draftRevision: 2, payload: { draft: { period: 30 } } } }); await read
  assert.equal(h.page.ind.draft.period, 80); assert.equal(h.entries.get(key), before)
  assert.ok(!h.storageWrites.some(row => row.op === 'remove' && row.key === key))
})
test('restore failure or empty response preserves edits; newer same-branch editing cannot be overwritten', async () => {
  const h = await harness(); editIndicator(h, 80); const key = h.page.localKey('A', 'indicator'); const before = h.entries.get(key)
  h.api.getDraft = async () => { throw httpError(503) }; await h.page.restoreDraft('indicator')
  assert.equal(h.page.ind.draft.period, 80); assert.equal(h.entries.get(key), before)
  h.api.getDraft = async () => ({ data: null }); await h.page.restoreDraft('indicator'); assert.equal(h.entries.get(key), before)
  const pending = deferred(); h.api.getDraft = () => pending.promise; const request = h.page.restoreDraft('indicator'); editIndicator(h, 90)
  pending.resolve({ data: { payload: { draft: { period: 30 } }, draftRevision: 1 } }); await request
  assert.equal(h.page.ind.draft.period, 90); assert.equal(h.page.indDirty, true)
})
test('save receipt confirms the submitted 50 only; edits to 80 remain dirty and local', async () => {
  const h = await harness(); editIndicator(h, 50); const pending = deferred(); let sent
  h.api.putDraft = (bid, module, body) => { sent = clone(body); return pending.promise }
  const save = h.page.saveDraft('indicator'); editIndicator(h, 80)
  pending.resolve({ data: { draftRevision: 1, payload: clone(sent.payload) } }); await save
  assert.equal(sent.payload.draft.period, 50); assert.equal(h.page.drafts.indicator.payload.draft.period, 50)
  assert.equal(h.page.ind.draft.period, 80); assert.equal(h.page.indDirty, true)
  assert.equal(JSON.parse(h.entries.get(h.page.localKey('A', 'indicator'))).draft.period, 80)
  assert.ok(h.messages.some(row => row.msg.includes('之后的新编辑仍未保存')))
})
test('discarding an inapplicable result still releases busy state for the same request', async () => {
  const h = await harness(); const pending = deferred(); let current = true
  const call = h.page.guarded(() => pending.promise, { isCurrent: () => current })
  assert.equal(h.page.busy, true); current = false; pending.resolve({ data: {} })
  assert.equal(await call, null); assert.equal(h.page.busy, false)
})
function allowEvidenceAI (h) {
  h.page.branch.hasDownstream = false; h.page.branch.heads.evidence = 'E_A'
  h.page.artifactCache.E_A = { id: 'E_A', branchId: 'A', kind: 'external_evidence', payload: { source: 'quant/profit', reportId: profit().id, blocks: profit().blocks } }
  h.page.caps.ai = { configured: true, evidenceHypotheses: true }
}
const aiResult = () => ({ data: { branch: { id: 'A', projectId: 'P' }, artifact: { id: 'AI_A', branchId: 'A', kind: 'ai_report', payload: { content: { conclusion: '合成草稿', hypotheses: [{ title: '合成假设', evidence: [{ blockIndex: 0, quote: '唯一正文词' }] }] } } } } })
test('evidence AI is an explicit click with immutable branch/evidence/revision and never auto-adopts', async () => {
  const h = await harness(); allowEvidenceAI(h); let body; let count = 0
  h.api.evidenceHypotheses = async (bid, value) => { count++; assert.equal(bid, 'A'); body = clone(value); return aiResult() }
  assert.equal(count, 0); const heads = clone(h.page.branch.heads)
  await h.page.generateEvidenceHypotheses()
  assert.equal(count, 1); assert.equal(body.evidenceArtifactId, 'E_A'); assert.equal(body.expectedRevision, 7); assert.ok(body.clientRequestId)
  assert.equal(h.page.reports.length, 1); assert.deepEqual(clone(h.page.branch.heads), heads); assert.equal(h.page.busy, false)
  assert.equal(h.page.evidenceCitationText(h.page.reports[0].payload.content.hypotheses[0].evidence), '段落 1：唯一正文词')
})
test('evidence AI configuration denial and late different-branch response cannot create a visible draft', async () => {
  const h = await harness(); allowEvidenceAI(h); let count = 0
  h.api.evidenceHypotheses = async () => { count++; return aiResult() }; h.page.caps.ai.configured = false
  await h.page.generateEvidenceHypotheses(); assert.equal(count, 0)
  h.page.caps.ai.configured = true; const pending = deferred(); h.api.evidenceHypotheses = () => pending.promise
  const request = h.page.generateEvidenceHypotheses(); h.page.invalidateHandoff(); h.page.branch = clone(h.branches.B); h.page.handoffTargetBranchId = 'B'; h.page.handoffBranchReady = true
  pending.resolve(aiResult()); await request
  assert.equal(h.page.reports.length, 0); assert.equal(h.page.artifactCache.AI_A, undefined); assert.equal(h.page.hypothesisCalling, false)
})
test('workspace context links carry project branch and selected run; action buttons never concatenate parameter values', async () => {
  const h = await harness(); h.page.currentRun = { id: 'R_A' }
  assert.deepEqual(clone(h.page.researchEvidenceRoute('validation')), { path: '/personal-reports', query: { section: 'validation', projectId: 'P', branchId: 'A', runId: 'R_A' } })
  const source = readFileSync(new URL('../../src/views/research-lab-v2/index.vue', import.meta.url), 'utf8')
  for (const button of source.matchAll(/<a-button\b[\s\S]*?<\/a-button>/g)) assert.doesNotMatch(button[0], /\{\{\s*describe\(ind\.applied\)/)
  assert.match(source, /rl2-parameter-summary/); assert.match(source, /rl2-evidence-ai-generate/)
})

function externalResultRoute () {
  const source = readFileSync(new URL('../../src/components/ExternalTaskOperations/index.vue', import.meta.url), 'utf8')
  const script = source.split('<script>')[1].split('</script>')[0].replace(/import[\s\S]*?from\s+['"][^'"]+['"]\s*/g, '').replace('export default {', 'module.exports = {')
  const module = { exports: {} }
  vm.runInNewContext(script, { module, qualityValue: value => value, EXTERNAL_STATUS: {} })
  return task => clone(module.exports.methods.resultRoute.call({}, task))
}
test('real external result navigation accepts backend query DTO and labels only the related source', () => {
  const route = externalResultRoute()
  for (const source of ['astock', 'ledger', 'phase2', 'data']) {
    for (const section of ['reports', 'quality', 'validation']) {
      const task = { resultLink: { path: '/personal-reports', query: { section, source } }, resultCorrelation: 'source_only' }
      assert.deepEqual(route(task), { path: '/personal-reports', query: { section, source, ...(source === 'astock' ? { group: 'astock' } : {}) } })
    }
  }
})
test('external result navigation refuses unknown destination, context injection and unverified correlation', () => {
  const route = externalResultRoute()
  const task = { resultLink: { path: '/personal-reports', query: { section: 'reports', source: 'astock' } }, resultCorrelation: 'source_only' }
  for (const value of [
    { ...task, resultCorrelation: 'execution' }, { ...task, resultCorrelation: undefined },
    { ...task, resultLink: { ...task.resultLink, path: 'https://untrusted.invalid/' } },
    { ...task, resultLink: { ...task.resultLink, params: { runId: 'unverified' } } },
    { ...task, resultLink: { path: '/personal-reports', query: { ...task.resultLink.query, projectId: 'unverified' } } },
    { ...task, resultLink: { path: '/personal-reports', query: { section: 'reports', source: 'nng8/astock' } } },
    { ...task, resultLink: { path: '/personal-reports', query: { section: 'untrusted', source: 'astock' } } },
    { ...task, resultLink: { path: '/personal-reports', section: 'reports', source: 'nng8/astock' } }
  ]) assert.equal(route(value), null)
})
