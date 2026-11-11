// Presentation of the external read-only DTO; never native research evidence.
export function nng8Enabled (value) {
  return value === true || value === 'true' || value === '1'
}

export const NNG8_READONLY_ENABLED = nng8Enabled((import.meta.env || {}).VITE_NNG8_READONLY_ENABLED)
export const NNG8_SECTION_ROUTE = Object.freeze({ path: '/personal-reports', query: { section: 'nng8' } })

const SOURCE_NAMES = ['today', 'progress', 'delivery', 'projects', 'ledger', 'phase2']
const QUALITY_COUNT_SCOPES = ['csi_members', 'full_market', 'sealed_core_qlib_summary']
const SYNC_FIELDS = ['status', 'reason_code', 'started_at', 'finished_at', 'last_success_at',
  'consecutive_failures', 'bundle_sha256', 'writer_version']
const SYNC_ERRORS = ['transport_failed', 'timeout', 'size_limit', 'invalid_bundle', 'replay',
  'capture_conflict', 'source_time_regressed', 'publication_failed', 'receipt_failed', 'interrupted', 'unknown']
const SYNC_UNKNOWN_REASONS = [...SYNC_ERRORS, 'missing', 'invalid', 'generation_mismatch', 'health_stale',
  'attempt_overdue', 'not_configured']
const SYNC_REASONS = {
  transport_failed: '来源传输失败。',
  timeout: '同步传输超时。',
  size_limit: '来源超过同步大小限制。',
  invalid_bundle: '来源副本未通过完整性检查。',
  replay: '来源捕获时间早于已接收副本。',
  capture_conflict: '相同捕获时间出现不同内容。',
  source_time_regressed: '业务来源时间发生回退。',
  publication_failed: '副本发布未完成。',
  receipt_failed: '同步回执未完成，不能确认此次发布成功。',
  interrupted: '同步操作被中断。',
  missing: '同步健康记录缺失。',
  invalid: '同步健康记录不符合契约。',
  generation_mismatch: '同步健康与当前副本的代次不一致。',
  health_stale: '同步健康记录已陈旧。',
  attempt_overdue: '同步操作超过确认时限。',
  not_configured: '当前来源未提供同步健康记录。',
  unknown: '尚未取得可确认的同步健康证据。'
}
const STATES = {
  available: ['来源可读取', 'neutral'],
  ok: ['可读取', 'good'],
passed: ['通过', 'good'],
pass: ['通过', 'good'],
  active: ['进程运行', 'neutral'],
partial: ['部分可读', 'warn'],
stale: ['来源陈旧', 'warn'],
  failed: ['失败', 'bad'],
fail: ['未通过', 'bad'],
unavailable: ['不可用', 'bad'],
  insufficient: ['样本不足', 'warn'],
not_reviewed: ['未复核', 'neutral'],
  unknown: ['未知', 'neutral'],
inactive: ['未运行', 'neutral'],
blocked: ['闭锁', 'warn']
}

export function nng8Status (value) {
  const [label, tone] = STATES[value] || STATES.unknown
  return { label, tone }
}

function object (value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {} }
function text (value, limit = 4000) { return typeof value === 'string' ? value.slice(0, limit) : null }
function list (value, limit = 40) { return Array.isArray(value) ? value.slice(0, limit) : [] }
function texts (value) { return list(value).map(item => text(item)).filter(item => item !== null) }
function count (value) { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null }
function day (value) { return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null }

export function nng8Time (value) {
  return typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
    ? value
    : null
}

export function nng8Value (value) {
  return value === null || value === undefined || value === '' ? '未知' : value
}

export function nng8AccountId (info) {
  const id = info && info.id
  if (typeof id === 'number' && Number.isSafeInteger(id) && id > 0) return String(id)
  return typeof id === 'string' && /^[1-9]\d*$/.test(id) ? id : ''
}

function unknownSync (reason) {
  return {
 status: 'unknown',
reason_code: reason,
started_at: null,
finished_at: null,
    last_success_at: null,
consecutive_failures: null,
bundle_sha256: null,
writer_version: null
}
}

function syncTime (value) {
  const match = typeof value === 'string' && value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/)
  if (!match) return null
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || minute > 59 || second > 59) return null
  return nng8Time(value)
}

function syncClock (value) {
  const fraction = value.match(/\.(\d{1,6})(?:Z|[+-]\d{2}:\d{2})$/)
  // Date.parse truncates microseconds; keep the server's sub-millisecond suffix.
  return Date.parse(value) + (fraction ? Number(fraction[1].padEnd(6, '0').slice(3)) / 1000 : 0)
}

export function normalizeNng8Sync (value, observedAt) {
  if (value === undefined) return unknownSync('not_configured')
  const row = object(value)
  if (Object.keys(row).length !== SYNC_FIELDS.length || SYNC_FIELDS.some(key => !Object.prototype.hasOwnProperty.call(row, key)) ||
      !['success', 'running', 'failed', 'unknown'].includes(row.status)) return unknownSync('invalid')
  const reasons = row.status === 'failed' ? SYNC_ERRORS : row.status === 'unknown' ? SYNC_UNKNOWN_REASONS : []
  if ((row.reason_code !== null && !reasons.includes(row.reason_code)) ||
      (['failed', 'unknown'].includes(row.status) && row.reason_code === null)) return unknownSync('invalid')
  if (row.status === 'unknown' && SYNC_FIELDS.filter(key => !['status', 'reason_code'].includes(key)).every(key => row[key] === null)) {
    return unknownSync(row.reason_code)
  }
  if (row.writer_version !== 'nng8.sync-writer.v1' || typeof row.bundle_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.bundle_sha256) ||
      count(row.consecutive_failures) === null || row.consecutive_failures > 1000000000) return unknownSync('invalid')
  const fields = ['started_at', 'finished_at', 'last_success_at']
  if (fields.some(key => row[key] !== null && !syncTime(row[key]))) return unknownSync('invalid')
  const observed = syncTime(observedAt)
  if (!observed || fields.some(key => row[key] !== null && syncClock(row[key]) > syncClock(observed) + 5000)) return unknownSync('invalid')
  const start = row.started_at === null ? null : syncClock(row.started_at)
  const finish = row.finished_at === null ? null : syncClock(row.finished_at)
  const lastSuccess = row.last_success_at === null ? null : syncClock(row.last_success_at)
  if (start === null || (finish !== null && finish < start) ||
      (row.status !== 'success' && lastSuccess !== null && lastSuccess > start) ||
      (row.status === 'running' && finish !== null) ||
      (row.status === 'failed' && (finish === null || row.consecutive_failures === 0)) ||
      (row.status === 'success' && (lastSuccess === null || lastSuccess !== finish || row.consecutive_failures !== 0))) return unknownSync('invalid')
  // The server observation anchors health age. Never refresh it with browser now.
  if (row.status === 'running' && syncClock(observed) - start > 60000) return unknownSync('attempt_overdue')
  if (syncClock(observed) - (finish === null ? start : finish) > 600000) return unknownSync('health_stale')
  return {
 status: row.status,
reason_code: row.reason_code,
started_at: row.started_at,
finished_at: row.finished_at,
    last_success_at: row.last_success_at,
consecutive_failures: row.consecutive_failures,
    bundle_sha256: row.bundle_sha256,
writer_version: row.writer_version
}
}

export function nng8SyncPresentation (sync) {
  const row = object(sync)
  const status = ['success', 'running', 'failed', 'unknown'].includes(row.status) ? row.status : 'unknown'
  const message = {
 success: '同步完成（不代表业务成功）',
running: '正在同步，显示上次副本',
    failed: '本次同步失败，显示上次副本',
unknown: '同步状态未知，不能确认本次同步成功'
}[status]
  return {
 message,
tone: status === 'failed' ? 'bad' : status === 'success' ? 'neutral' : 'warn',
    reason: SYNC_REASONS[row.reason_code] || (status === 'unknown' ? SYNC_REASONS.unknown : '')
}
}

export function normalizeNng8 (payload) {
  const raw = object(payload)
  if (raw.contract_version !== 'workbench.nng8.v1' || raw.source !== 'nng8' ||
      !Array.isArray(raw.reports) || !Array.isArray(raw.delivery) || !Array.isArray(raw.services) ||
      !Array.isArray(raw.calibration)) return null
  const rejected = []
  const reports = list(raw.reports, 10).flatMap((value, index) => {
    const report = object(value)
    if (report.source !== 'nng8/astock' || typeof report.id !== 'string' ||
        !/^nng8:astock:\d{4}-\d{2}-\d{2}:[a-f0-9]{64}$/.test(report.id)) {
      rejected.push({ source: 'today', code: 'report_source_rejected', message: '报告来源或外部标识不符合只读契约，未展示正文。' })
      return []
    }
    const rawBlocks = Array.isArray(report.blocks) ? report.blocks : []
    const contentV2 = report.content_contract === 'nng8.report-text.v2'
    const maxBlocks = contentV2 ? 240 : 128
    const maxChars = contentV2 ? 4000 : 2000
    const bodyBlockCount = count(report.body_block_count)
    let locallyTruncated = !Array.isArray(report.blocks) || rawBlocks.length > maxBlocks
    let totalChars = 0
    const blocks = list(rawBlocks, maxBlocks).flatMap(value => {
      const block = object(value)
      if (typeof block.text !== 'string') { locallyTruncated = true; return [] }
      // Match the server's Unicode-character bounds, not UTF-16 code units.
      const characters = Array.from(block.text)
      const heading = typeof block.heading === 'string' ? Array.from(block.heading) : null
      totalChars += characters.length
      if (characters.length > maxChars || totalChars > 180000 || (heading && heading.length > 160)) locallyTruncated = true
      return [{ heading: heading ? heading.slice(0, 160).join('') : null, text: characters.slice(0, maxChars).join('') }]
    })
    const bodyTruncated = report.body_truncated === true || locallyTruncated ||
      (bodyBlockCount !== null && bodyBlockCount !== blocks.length)
      ? true
      : (typeof report.body_truncated === 'boolean' ? false : null)
    if (bodyTruncated && !list(raw.issues).some(issue => object(issue).source === 'today' && object(issue).code === 'content_truncated')) {
      rejected.push({ source: 'today', code: 'content_truncated', message: `报告正文未完整保留；显示受${maxBlocks}段/单段${maxChars}字等边界限制，不能当作完整正文。` })
    }
    return [{
      id: report.id,
title: text(report.title, 200) || 'nnG8 报告',
source: 'nng8/astock',
      report_date: day(report.report_date),
generated_at: nng8Time(report.generated_at),
      data_cutoff: text(report.data_cutoff, 100),
evidence_cutoff: text(report.evidence_cutoff, 100),
      source_time: nng8Time(report.source_time),
status: text(report.status, 40) || 'unknown',
      publication_gate: text(report.publication_gate, 40) || 'unknown',
      review_status: text(report.review_status, 40) || 'unknown',
      conclusion: texts(report.conclusion),
risks: texts(report.risks),
      blocks,
      body_block_count: bodyBlockCount,
      body_truncated: bodyTruncated,
      content_contract: contentV2 ? report.content_contract : null,
      body_complete: contentV2 && report.body_complete === true && !bodyTruncated,
      source_report_sha256: /^[a-f0-9]{64}$/.test(report.source_report_sha256 || '') ? report.source_report_sha256 : null,
      data_mode: text(report.data_mode, 40),
      freshness: text(report.freshness, 40),
      preview: text(report.preview, 200),
      truncation_reasons: texts(report.truncation_reasons),
      content_sha256: typeof report.content_sha256 === 'string' && /^[a-f0-9]{64}$/.test(report.content_sha256) ? report.content_sha256 : null,
      render_key: report.id + ':' + index
    }]
  })
  const quality = object(raw.data_quality)
  const countsScope = QUALITY_COUNT_SCOPES.includes(quality.counts_scope) ? quality.counts_scope : null
  const qualityCounts = { eligible: count(quality.eligible), valid: count(quality.valid), invalid: count(quality.invalid) }
  const countsConflict = (quality.counts_contract && quality.active_contract && quality.counts_contract !== quality.active_contract) ||
    (quality.counts_target_date && quality.market_date && quality.counts_target_date !== quality.market_date)
  if (!countsScope || countsConflict || Object.values(qualityCounts).some(value => value === null) || qualityCounts.valid + qualityCounts.invalid !== qualityCounts.eligible) {
    qualityCounts.eligible = qualityCounts.valid = qualityCounts.invalid = null
    rejected.push({ source: 'progress', code: 'quality_counts_unknown', message: '缺少同一口径的可核验标的数，未补零或跨范围合计。' })
  }
  return {
    contract_version: 'workbench.nng8.v1',
source: 'nng8',
captured_at: nng8Time(raw.captured_at),
    observed_at: nng8Time(raw.observed_at),
snapshot_stale: raw.snapshot_stale === true,
    status: text(raw.status, 40) || 'unknown',
    sync: normalizeNng8Sync(raw.sync, raw.observed_at),
reports,
    data_quality: {
      status: text(quality.status, 40) || 'unavailable',
source_time: nng8Time(quality.source_time),
      active_contract: text(quality.active_contract, 200),
market_date: day(quality.market_date),
      counts_scope: countsScope,
counts_contract: text(quality.counts_contract, 200),
counts_target_date: day(quality.counts_target_date),
      current_run_status: text(quality.current_run_status, 40) || 'unknown',
      full_snapshot: typeof quality.full_snapshot === 'boolean' ? quality.full_snapshot : null,
      ...qualityCounts,
      historical_issue_dates: list(quality.historical_issue_dates).map(day).filter(Boolean),
limitations: texts(quality.limitations)
    },
    delivery: list(raw.delivery, 20).map(value => {
      const row = object(value)
      return {
 id: text(row.id, 120),
label: text(row.label, 200),
status: text(row.status, 40) || 'unknown',
        source_time: nng8Time(row.source_time),
limitations: texts(row.limitations)
}
    }),
    services: list(raw.services, 30).map(value => {
      const row = object(value)
      return {
 id: text(row.id, 120),
label: text(row.label, 200),
status: text(row.status, 40) || 'unknown',
        last_run: nng8Time(row.last_run),
next_run: nng8Time(row.next_run)
}
    }),
    calibration: list(raw.calibration, 2).filter(value => ['ledger', 'phase2'].includes(object(value).id)).map(value => {
      const row = object(value)
      return {
 id: row.id,
label: text(row.label, 200),
runtime_mode: ['RUNNING', 'FROZEN', 'UNKNOWN'].includes(row.runtime_mode) ? row.runtime_mode : null,
        status: text(row.status, 40) || 'unknown',
nominal: count(row.nominal),
observed: count(row.observed),
        pending_review: count(row.pending_review),
formal: count(row.formal),
limitations: texts(row.limitations)
}
    }),
    issues: [...list(raw.issues).filter(value => SOURCE_NAMES.includes(object(value).source)).map(value => {
      const row = object(value)
      return { source: row.source, code: text(row.code, 80), message: text(row.message) }
    }), ...rejected],
limitations: texts(raw.limitations)
  }
}

export function nng8PageState ({ enabled, loading, permission, error, snapshot }) {
  if (!enabled) return 'disabled'
  if (permission) return 'denied'
  if (loading) return 'loading'
  if (error || !snapshot) return 'unavailable'
  if (snapshot.status === 'unavailable') return 'unavailable'
  if (!snapshot.reports.length && !snapshot.delivery.length && !snapshot.services.length &&
      !snapshot.calibration.length && snapshot.data_quality.status === 'unavailable') return 'empty'
  const explicitlyReadable = value => value === 'ok' || value === 'pass'
  if (!explicitlyReadable(snapshot.status) || !snapshot.sync || snapshot.sync.status !== 'success' ||
      snapshot.issues.length || snapshot.snapshot_stale || !snapshot.captured_at || !snapshot.observed_at ||
      !explicitlyReadable(snapshot.data_quality.status) || snapshot.reports.some(row => !explicitlyReadable(row.status)) ||
      snapshot.delivery.some(row => !explicitlyReadable(row.status)) || snapshot.services.some(row => !explicitlyReadable(row.status)) ||
      snapshot.calibration.some(row => !explicitlyReadable(row.status))) return 'partial'
  return 'available'
}
