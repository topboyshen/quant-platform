import { reportIdentity } from './nng8ResearchLink.js'

export function evidenceSourcesResponse (response) {
  const data = response && response.code === 1 && response.data
  if (!data || data.contract_version !== 'workbench.reports.v1' || !Array.isArray(data.research_evidence)) throw new Error('Profit / ETF 归档来源接口尚未就绪；不把摘要替代为完整证据。')
  const occurrences = data.research_evidence.reduce((counts, report) => {
    const id = report && report.id
    counts.set(id, (counts.get(id) || 0) + 1)
    return counts
  }, new Map())
  return data.research_evidence.map((report, index) => {
    const row = report && typeof report === 'object' ? report : {}
    const capturedAt = Object.prototype.hasOwnProperty.call(row, 'captured_at') ? row.captured_at : undefined
    const identity = reportIdentity(row, capturedAt)
    const duplicated = occurrences.get(row.id) > 1
    return {
      report: row,
capturedAt,
identity: duplicated ? null : identity,
      key: duplicated || !row.id ? 'invalid-source-' + index : row.id,
      reason: duplicated ? '来源标识重复，不能确认归档对象。' : identity ? '' : row.detail || '完整内容、来源身份或时间语义未确认，不能归档。'
    }
  })
}

export function filterEvidenceSources (rows, query) {
  const needle = String(query || '').trim().toLowerCase()
  return !needle ? rows : rows.filter(row => [row.report.title, row.report.source, ...(row.report.blocks || []).map(block => block.text)].join(' ').toLowerCase().includes(needle))
}
