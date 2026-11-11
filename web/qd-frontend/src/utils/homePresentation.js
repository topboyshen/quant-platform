// Presentation only: never recompute freshness, sample eligibility or outcomes.
import { statusMeta, unknownOr, workbenchItems } from './workbench.js'
import { reportBusiness } from './unifiedReportCatalog.js'

export function homeTime (value, missing = '未知') {
  if (typeof value !== 'string' || !value.trim()) return missing
  const text = value.trim()
  const day = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])/)
  if (day) {
    const [, year, month, date] = day.map(Number)
    const maxDay = new Date(Date.UTC(year, month, 0)).getUTCDate()
    if (month < 1 || month > 12 || date < 1 || date > maxDay) return missing
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)) return text
  // Contract timestamps without an offset are already Asia/Shanghai wall time.
  const timestamp = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text.replace(' ', 'T')}+08:00`
  const date = new Date(timestamp)
  if (!Number.isFinite(date.getTime())) return missing
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23'
  }).formatToParts(date)
  const get = name => parts.find(part => part.type === name).value
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`
}

export function homeAttention (payload) {
  return workbenchItems(payload).filter(item => !['ok', 'not_due', 'running', 'recovered'].includes(item.status))
    .map(item => ({
      id: item.id,
      label: item.label || item.id,
      status: statusMeta(item.status).label,
      detail: item.detail || statusMeta(item.status).label
    }))
}

const PROFIT_NAMES = {
  '026a1083d9e0': 'Profit Radar 健康看板',
  ec6c86fd856c: 'QuantDinger 盈利研究'
}

export function homeProfitReports (payload) {
  const item = workbenchItems(payload).find(row => row.id === 'profit_radar')
  const files = item && item.value && item.value.files
  if (!Array.isArray(files)) return []
  return files.map((entry, index) => {
    const valid = entry && typeof entry === 'object' && !Array.isArray(entry)
    const file = valid ? entry : {}
    const id = String(file.file || `file-${index}`)
    const job = id.split('/')[0]
    const status = valid ? (file.status || item.status || 'unavailable') : 'unavailable'
    const business = reportBusiness(file)
    return {
      id,
      title: PROFIT_NAMES[job] || `Profit · ${job}`,
      job,
      status,
      statusMeta: file.business ? { label: business.label, tone: business.tone === 'red' ? 'bad' : business.tone === 'orange' ? 'warn' : 'neutral' } : statusMeta(status),
      conclusion: business.reason || unknownOr(file.conclusion),
      generatedAt: homeTime(file.generated_at),
      marketCutoff: homeTime(file.market_cutoff_date || file.market_cutoff)
    }
  })
}

export function homeMarketCutoff (payload) {
  // Calendar update / EOD runtime / file mtime / sync_at are NOT market dates.
  const item = workbenchItems(payload).find(row => row.id === 'digest_inputs')
  return item ? homeTime(item.data_cutoff) : '未知'
}
