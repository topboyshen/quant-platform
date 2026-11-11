import { normalizeReports, jobIdOf } from './reportCatalog.js'
import { reportIdentity } from './nng8ResearchLink.js'

const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
const text = value => typeof value === 'string' ? value : ''
export const REPORT_SOURCE_FILTERS = [
  { value: 'all', label: '全部来源' }, { value: 'profit', label: 'Profit' },
  { value: 'etf', label: 'ETF' }, { value: 'astock', label: 'AStock' },
  { value: 'close_run', label: '收盘与运行' }
]

export function reportBusiness (report) {
  const source = object(report)
  const business = object(source.business || object(source.summary).business)
  const researchStatus = business.researchStatus || source.research_status || object(source.summary).research_status
  if (business.taskStatus === 'failed') return { label: '执行失败', tone: 'red', reason: text(business.reason), attention: true }
  if (business.dataStatus === 'blocked' || researchStatus === 'DATA_BLOCKED') return { label: '数据阻断', tone: 'orange', reason: text(business.reason), attention: true }
  if (business.signalStatus === 'candidates') return { label: '已产生研究候选', tone: 'blue', reason: text(business.reason), attention: false }
  if (business.taskStatus === 'completed' && business.signalStatus === 'none') return { label: '检查完成 · 无新信号', tone: 'blue', reason: text(business.reason), attention: false }
  if (business.taskStatus === 'completed') return { label: '检查完成', tone: 'blue', reason: text(business.reason), attention: business.dataStatus !== 'ready' }
  if (['failed', 'fail'].includes(source.status)) return { label: '来源失败', tone: 'red', reason: text(source.detail), attention: true }
  if (source.status === 'stale' || source.freshness === 'stale') return { label: '来源陈旧', tone: 'orange', reason: text(source.detail), attention: true }
  if (source.publication_gate === 'blocked') return { label: '发布被阻断', tone: 'orange', reason: text(source.detail), attention: true }
  if (['partial', 'degraded'].includes(source.status)) return { label: '来源证据部分可用', tone: 'orange', reason: text(source.detail), attention: true }
  return { label: '业务结论未知', tone: 'grey', reason: text(source.detail), attention: true }
}

function fullRow (report, capturedAt, observedAt, stale) {
  const source = report.source === 'nng8/astock' ? 'astock' : report.source === 'quant/profit' ? 'profit' : 'etf'
  const identity = stale ? null : reportIdentity(report, capturedAt)
  const blocks = Array.isArray(report.blocks) ? report.blocks : []
  const business = stale ? { label: '来源副本陈旧', tone: 'orange', reason: '同步副本已过期；来源报告的业务结论仍保留在正文。', attention: true } : reportBusiness(report)
  return {
    key: report.id,
familyKey: report.id,
title: report.title || '来源标题未知',
group: source,
    groupLabel: { astock: 'AStock · nnG8', profit: 'Profit · Quant-Server', etf: 'ETF · Quant-Server' }[source],
    rawId: report.id,
rawLabel: report.title,
jobId: source === 'profit' ? report.id.split(':')[2] : null,
    status: report.status,
readable: blocks.length > 0,
    readability: { label: blocks.length ? '内容可读取' : '内容未取得', tone: blocks.length ? 'grey' : 'red' },
    business,
conclusion: { kind: 'conclusion', text: business.reason || (Array.isArray(report.conclusion) ? report.conclusion.join('；') : report.conclusion) || '请阅读来源正文和数据状态。' },
    generatedAt: report.generated_at || null,
generatedAtMissing: !report.generated_at,
    marketCutoff: report.data_cutoff || null,
observedAt,
fileMtime: null,
marketCutoffFrom: '来源 data_cutoff',
    variantLabel: source === 'etf' ? '完整验证快照' : '来源正文',
format: source === 'etf' ? 'json' : 'text',
    schemaVersion: report.content_contract || '',
sourcePath: report.source,
summaryKeys: [],
    blocks,
report,
capturedAt,
identity,
keyReason: '',
keyAmbiguous: false,
    detail: report.detail || '',
contentSha256: report.content_sha256,
    archiveReason: stale ? '来源副本陈旧，请刷新来源后重新确认。' : identity ? '' : '完整内容或稳定来源身份尚未确认，暂不能用于研究。',
    needsAttention: stale || business.attention || !identity,
    attachments: []
  }
}

// One catalogue, without equating a job name/date with a content identity.
export function unifiedReportRows (local, external) {
  const data = object(local)
  const raw = Array.isArray(data.reports) ? data.reports : []
  const legacy = normalizeReports(data).map((row, index) => {
    const original = raw[index] || {}
    const group = ['ec6c86fd856c', '026a1083d9e0'].includes(jobIdOf(original)) ? 'profit' : row.rawId === 'eod_daily_report' ? 'etf' : 'close_run'
    const business = reportBusiness(original)
    return {
 ...row,
group,
business,
blocks: [],
report: null,
identity: null,
attachments: [],
      archiveReason: '这份条目只提供摘要，尚未提供完整来源和可归档身份。',
      needsAttention: row.needsAttention || business.attention,
contentSha256: original.content_sha256 || object(original.summary).content_sha256 || null,
      evidenceReportId: original.evidence_report_id || object(original.summary).evidence_report_id || null
}
  })
  const full = [
    ...(Array.isArray(data.research_evidence) ? data.research_evidence : []).map(report => fullRow(report, report.captured_at, report.observed_at || data.observed_at, false)),
    ...((external && external.reports) || []).map(report => fullRow(report, external.captured_at, external.observed_at, external.snapshot_stale))
  ]
  const matched = new Set()
  for (const row of full) {
    row.attachments = legacy.filter(old => old.evidenceReportId === row.key && old.contentSha256 && old.contentSha256 === row.contentSha256)
    row.attachments.forEach(old => matched.add(old))
  }
  const combined = [...full, ...legacy.filter(row => !matched.has(row))]
  const counts = new Map()
  combined.forEach(row => counts.set(row.key, (counts.get(row.key) || 0) + 1))
  return combined.map(row => counts.get(row.key) > 1 ? { ...row, keyAmbiguous: true, identity: null, keyReason: '来源标识重复，不能安全选择。', needsAttention: true } : row)
}

export function filterUnifiedReports (rows, { group = 'all', query = '', attentionOnly = false } = {}) {
  const needle = String(query).trim().toLowerCase()
  return rows.filter(row => (group === 'all' || row.group === group) && (!attentionOnly || row.needsAttention) && (!needle ||
    [row.title, row.groupLabel, row.business.label, row.business.reason, row.detail, row.rawLabel, ...(row.blocks || []).map(block => block.text)].join(' ').toLowerCase().includes(needle)))
}
