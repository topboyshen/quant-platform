// Presentation-only helpers for the research workbench (no business rules here).
// Status vocabulary matches the read-only integration contract `workbench.item.v1`.

export const WORKBENCH_STATUS_META = {
  ok: { label: '正常', color: 'green' },
  stale: { label: '数据过期', color: 'orange' },
  not_due: { label: '尚未到最终验收时点', color: 'blue' },
  running: { label: '进行中', color: 'blue' },
  awaiting_retry: { label: '等待当日重试', color: 'orange' },
  recovered: { label: '已恢复（保留首次失败）', color: 'cyan' },
  failed: { label: '失败', color: 'red' },
  evidence_missing: { label: '证据缺失', color: 'red' },
  unavailable: { label: '不可用', color: 'red' },
  not_integrated: { label: '待接入', color: 'default' }
}

export function statusMeta (status) {
  return WORKBENCH_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function formatCutoff (value) {
  if (!value || typeof value !== 'string') return '—'
  return value.replace('T', ' ').replace(/\+08:00$/, '')
}

/** Missing per-file fields must show 未知 (not dash, not borrowed from siblings). */
export function unknownOr (value) {
  if (value === undefined || value === null) return '未知'
  const text = String(value).trim()
  return text || '未知'
}

export function formatUnknownCutoff (value) {
  if (!value || typeof value !== 'string' || !value.trim()) return '未知'
  return formatCutoff(value)
}

export function workbenchItems (payload) {
  return payload && Array.isArray(payload.items)
    ? payload.items.map((item, index) => item && typeof item === 'object' && !Array.isArray(item)
      ? item
      : { id: `invalid-evidence-${index}`, label: '来源证据不可读', status: 'unavailable', detail: '条目格式无效，不能判断当前状态。' })
    : []
}

const ATTENTION = ['evidence_missing', 'failed', 'unavailable', 'stale', 'awaiting_retry']

export function summarizeWorkbench (payload) {
  const items = workbenchItems(payload)
  const degraded = items.filter(item => ['unavailable', 'not_integrated'].includes(item.status))
  const attention = items.filter(item => ATTENTION.includes(item.status) || !WORKBENCH_STATUS_META[item.status])
  const allUnavailable = !payload || !Array.isArray(payload.items) || !items.length || Boolean(payload.all_unavailable)
  let overall = 'ok'
  if (allUnavailable) overall = 'unavailable'
  else if (attention.length || degraded.length || (payload && payload.partial)) overall = 'partial'
  const cutoffs = items
    .filter(item => ['calendar_freshness', 'digest_inputs', 'eod_status'].includes(item.id))
    .map(item => ({ id: item.id, label: item.label, cutoff: item.data_cutoff }))
  const openQuestions = []
  if (allUnavailable) openQuestions.push('全部业务来源不可用：当前不能下任何结论。')
  degraded.forEach(item => openQuestions.push(`${item.label}（${item.id}）：${item.detail || '待接入'}`))
  attention.forEach(item => {
    if (!degraded.includes(item)) openQuestions.push(`${item.label}（${item.id}）：${statusMeta(item.status).label}`)
  })
  return {
    overall,
    overallLabel: overall === 'ok' ? '系统正常' : (overall === 'partial' ? '部分不可用' : '全部业务来源不可用'),
    partial: overall === 'partial',
    unavailableItems: (payload && payload.unavailable_items) || [],
    notIntegratedItems: (payload && payload.not_integrated_items) || [],
    cutoffs,
    openQuestions,
    generatedAt: (payload && payload.generated_at) || null
  }
}

export function latestReport (payload) {
  const reports = payload && Array.isArray(payload.reports) ? payload.reports : []
  return reports
    .filter(row => row.status === 'ok')
    .sort((a, b) => String(b.generated_at || b.file_mtime || '').localeCompare(String(a.generated_at || a.file_mtime || '')))[0] || null
}

function summaryDisclaimer (row) {
  const summary = row && row.summary && typeof row.summary === 'object' ? row.summary : null
  const text = summary && summary.disclaimer ? String(summary.disclaimer).trim() : ''
  return text || ''
}

function profitSummaryFields (row) {
  const summary = row && row.summary && typeof row.summary === 'object' ? row.summary : null
  if (!summary) return null
  return {
    conclusion: unknownOr(summary.conclusion),
    generatedAt: formatUnknownCutoff(summary.generated_at || row.generated_at),
    marketCutoff: formatUnknownCutoff(summary.market_cutoff_date || summary.market_cutoff)
  }
}

export function reportRows (payload) {
  const reports = payload && Array.isArray(payload.reports) ? payload.reports : []
  return reports.map(row => {
    const baseDetail = row.detail || ''
    const fields = profitSummaryFields(row)
    let detail = baseDetail
    if (fields) {
      const pieces = [
        `结论：${fields.conclusion}`,
        `生成时间：${fields.generatedAt}`,
        `行情截止日：${fields.marketCutoff}`
      ]
      const disc = summaryDisclaimer(row)
      if (disc) pieces.push(disc)
      detail = pieces.join('；')
    }
    return {
      id: row.id,
      label: row.label,
      format: row.format,
      status: row.status,
      statusMeta: statusMeta(row.status),
      generatedAt: formatCutoff(row.generated_at),
      fileMtime: formatCutoff(row.file_mtime),
      schemaVersion: row.schema_version === undefined || row.schema_version === null ? '—' : String(row.schema_version),
      detail
    }
  })
}

/**
 * Expand Profit Radar value.files[] into evidence table rows.
 * Never copy conclusion across files; never use aggregate data_cutoff as market cutoff.
 */
export function expandWorkbenchRows (items) {
  const list = Array.isArray(items) ? items : []
  const rows = []
  list.forEach(item => {
    const files = item && item.value && Array.isArray(item.value.files) ? item.value.files : null
    const isProfit = item && item.id === 'profit_radar'
    if (isProfit && files && files.length) {
      const rawDisc = (item.detail || (item.value && item.value.disclaimer) || '').trim()
      const disclaimer = rawDisc || '模拟跟踪≠账户收益'
      rows.push({
        id: item.id,
        label: item.label,
        status: item.status,
        statusLabel: statusMeta(item.status).label,
        statusColor: statusMeta(item.status).color,
        // Parent row: do not present aggregate data_cutoff as per-file market cutoff.
        cutoff: '见分文件',
        source: item.source,
        rule: item.expected && item.expected.rule ? item.expected.rule : '',
        detail: disclaimer
      })
      files.forEach((entry, index) => {
        const valid = entry && typeof entry === 'object' && !Array.isArray(entry)
        const file = valid ? entry : {}
        const fileId = file.file || `file-${index}`
        const short = String(fileId).split('/')[0] || fileId
        const status = valid ? (file.status || item.status) : 'unavailable'
        rows.push({
          id: `${item.id}:${fileId}`,
          label: `Profit · ${short}`,
          status,
          statusLabel: statusMeta(status).label,
          statusColor: statusMeta(status).color,
          // Per-file market cutoff only — null → 未知 (026a case).
          cutoff: formatUnknownCutoff(file.market_cutoff_date || file.market_cutoff),
          source: fileId,
          rule: '',
          detail: `结论：${unknownOr(file.conclusion)}；生成时间：${formatUnknownCutoff(file.generated_at)}；行情截止日：${formatUnknownCutoff(file.market_cutoff_date || file.market_cutoff)}`
        })
      })
      return
    }
    rows.push({
      id: item.id,
      label: item.label,
      status: item.status,
      statusLabel: statusMeta(item.status).label,
      statusColor: statusMeta(item.status).color,
      cutoff: formatCutoff(item.data_cutoff),
      source: item.source,
      rule: item.expected && item.expected.rule ? item.expected.rule : '',
      detail: item.detail || eodNotDueCopy(item) || ''
    })
  })
  return rows
}

export function eodNotDueCopy (item) {
  // 15:30-15:50 is "not yet at the final acceptance cutoff", never "has not started".
  return item && item.status === 'not_due' ? '尚未到最终验收时点（15:50）' : ''
}
