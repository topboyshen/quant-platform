// 报告中心第一批（纯前端）目录逻辑。
//
// 契约约束（重要）：
// - 只使用 /api/radar/workbench/reports (v1) 已经返回的字段，不新增接口调用。
// - 不做业务判断：不写盈利结论、不评分数、不做 LLM 加工。
// - 时间三件事分开：generated_at（报告自身）/ market_cutoff（行情截止）/ observed_at（本次读取）。
//   file_mtime 只作技术证据，绝不用于"刷鲜"或排序。
// - 报告键稳定化走前端映射（白名单 job id），不使用数组下标；无法识别时明确提示，不静默选错。

// ---------- 可读取性词表（"可读取"不等于"业务成功"） ----------
export const READABILITY = {
  ok: { label: '可读取', tone: 'green', readable: true },
  unavailable: { label: '不可读取', tone: 'red', readable: false },
  not_integrated: { label: '待接入', tone: 'grey', readable: false },
  invalid: { label: '条目异常', tone: 'red', readable: false }
}

export function readabilityOf (status) {
  return READABILITY[status] || { label: '状态未知', tone: 'grey', readable: false }
}

// ---------- 页面级状态机（空/失败/未知契约不得进入绿色分支） ----------
export const PAGE_STATE = {
  LOADING: 'loading',
  AVAILABLE: 'available',
  PARTIAL: 'partial',
  EMPTY: 'empty',
  UNAVAILABLE: 'unavailable',
  UNAUTHORIZED: 'unauthorized'
}

export const PAGE_STATE_COPY = {
  loading: { label: '读取中', tone: 'blue' },
  available: { label: '全部可读取', tone: 'green' },
  partial: { label: '部分报告不可读取', tone: 'orange' },
  empty: { label: '没有报告', tone: 'grey' },
  unavailable: { label: '读取失败', tone: 'red' },
  unauthorized: { label: '需要登录', tone: 'red' }
}

/**
 * 只有"契约有效 + 条目存在 + 全部可读"才是 available。
 * @param {{loading?:boolean, error?:string, unauthorized?:boolean, contractOk?:boolean, reports?:Array}} input
 */
export function pageState (input) {
  const { loading, error, unauthorized, contractOk, reports } = input || {}
  if (loading) return PAGE_STATE.LOADING
  if (unauthorized) return PAGE_STATE.UNAUTHORIZED
  if (error) return PAGE_STATE.UNAVAILABLE
  if (contractOk !== true) return PAGE_STATE.UNAVAILABLE
  const rows = Array.isArray(reports) ? reports : []
  if (!rows.length) return PAGE_STATE.EMPTY
  return rows.every(row => row.readable) ? PAGE_STATE.AVAILABLE : PAGE_STATE.PARTIAL
}

// ---------- 报告家族（固定映射；5 类逻辑入口） ----------
// jobId 来自白名单来源路径，是"稳定键"的首选依据；中文标题是兼容后备。
export const FAMILIES = [
  { key: 'profit_research', title: 'QuantDinger 盈利研究', group: 'research', jobIds: ['ec6c86fd856c'] },
  { key: 'profit_radar', title: 'Profit Radar 健康看板', group: 'research', jobIds: ['026a1083d9e0'] },
  { key: 'research_digest', title: '市场研究摘要', group: 'close_run', reportIds: ['research_digest_json', 'research_digest_md'] },
  { key: 'eod_daily', title: '收盘验收日报', group: 'close_run', reportIds: ['eod_daily_report'] },
  { key: 'execution_digest', title: '执行摘要', group: 'close_run', reportIds: ['execution_digest_json', 'execution_digest_md'] }
]

export const GROUP_FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'research', label: '研究报告' },
  { value: 'close_run', label: '收盘与运行' }
]

export const GROUP_LABEL = { research: '研究报告', close_run: '收盘与运行' }

// 白名单 job id 的文本形态（实测线上 label 用全角括号）：
//   "Profit Radar 报告（026a1083d9e0/latest.md）"；兼容半角括号与裸路径片段
const JOB_ID_PATTERNS = [
  /[(（]([0-9a-f]{8,32})[/)）]/, // （026a…/latest.md） | (026a…/latest.md)
  /([0-9a-f]{8,32})\/[A-Za-z0-9._-]+/ // 裸路径片段 026a…/latest.md
]
const HEX_ID = /^[0-9a-f]{8,32}$/

/** 从来源路径/标题里取白名单 job id；取不到就返回 null（不猜）。 */
export function jobIdOf (row) {
  const candidates = [row && row.path, row && row.label, row && row.summary && row.summary.file]
  for (const value of candidates) {
    if (typeof value !== 'string') continue
    for (const pattern of JOB_ID_PATTERNS) {
      const hit = pattern.exec(value)
      if (hit) return hit[1]
    }
    const bare = value.trim()
    if (HEX_ID.test(bare)) return bare
  }
  return null
}

export function familyOf (row) {
  const jobId = jobIdOf(row)
  if (jobId) {
    const byJob = FAMILIES.find(family => (family.jobIds || []).includes(jobId))
    if (byJob) return { family: byJob, via: 'job_id', jobId }
    return { family: null, via: 'job_id', jobId }
  }
  const byId = FAMILIES.find(family => (family.reportIds || []).includes(row && row.id))
  if (byId) return { family: byId, via: 'report_id', jobId: null }
  return { family: null, via: 'none', jobId: null }
}

/**
 * 稳定键：优先 job id，其次家族+格式（标题仅作后备）。
 * 返回 reason 时调用方必须提示"请重新选择"，不得静默选中。
 */
export function stableKeyOf (row) {
  const found = familyOf(row)
  if (found.jobId) return { key: `job:${found.jobId}`, via: 'job_id', family: found.family, reason: '' }
  if (found.via === 'report_id' && found.family) {
    const format = row && row.format ? String(row.format) : 'unknown'
    return { key: `family:${found.family.key}:${format}`, via: 'report_id', family: found.family, reason: '' }
  }
  const title = row && row.label ? String(row.label).trim() : ''
  if (title) return { key: `title:${title}`, via: 'title', family: null, reason: '该报告缺少稳定标识，已按标题临时识别' }
  return { key: null, via: 'none', family: null, reason: '该报告缺少稳定标识，无法识别，请重新选择' }
}

// ---------- 时间三件事 ----------
export function cstText (value) {
  if (value === null || value === undefined) return null
  const text = String(value).trim()
  if (!text) return null
  return text.replace('T', ' ').replace(/\.\d+/, '').replace(/\+08:00$/, '')
}

export function isDateOnly (value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value.trim())
}

/**
 * 时间字段分离。缺失一律为 null（由展示层写"未知"），
 * 绝不使用 file_mtime 或 payload 的 generated_at 顶替报告自身的生成时间。
 */
export function timeFieldsOf (row) {
  const summary = row && row.summary && typeof row.summary === 'object' ? row.summary : {}
  const family = familyOf(row).family
  // 行情/业务截止的取值来源（家族内显式字段，不跨报告借用、不用 mtime）：
  // - 研究 / Profit 家族：summary.market_cutoff_date → summary.market_cutoff
  // - 收盘验收日报：summary.trade_date（该报告自身覆盖的交易日）
  const explicitField = summary.market_cutoff_date
    ? 'market_cutoff_date'
    : (summary.market_cutoff ? 'market_cutoff' : null)
  const explicit = explicitField ? summary[explicitField] : null
  const byFamily = family && family.key === 'eod_daily' ? (summary.trade_date || null) : null
  const rawCutoff = explicit || byFamily
  return {
    generatedAt: cstText(row && row.generated_at),
    generatedAtMissing: !cstText(row && row.generated_at),
    marketCutoff: rawCutoff ? (isDateOnly(rawCutoff) ? String(rawCutoff).trim() : cstText(rawCutoff)) : null,
    marketCutoffRaw: rawCutoff,
    marketCutoffFrom: explicitField ? `summary.${explicitField}` : (byFamily ? 'summary.trade_date' : null),
    fileMtime: cstText(row && row.file_mtime),
    observedAt: cstText(row && row.observed_at)
  }
}

// ---------- 一句话结论（不做加工） ----------
export function conclusionOf (row) {
  const summary = row && row.summary && typeof row.summary === 'object' ? row.summary : {}
  const explicit = summary.conclusion === null || summary.conclusion === undefined ? '' : String(summary.conclusion).trim()
  if (explicit) return { kind: 'conclusion', label: '结论', text: explicit }
  const excerpt = summary.message || summary.note
  const excerptText = excerpt === null || excerpt === undefined ? '' : String(excerpt).trim()
  if (excerptText) return { kind: 'excerpt', label: '原文摘要', text: excerptText }
  return { kind: 'missing', label: '', text: '报告未提供明确结论' }
}

// ---------- 行规范化 ----------
export function normalizeReport (row) {
  const source = row && typeof row === 'object' ? row : {}
  const key = stableKeyOf(source)
  const readability = readabilityOf(source.status)
  const time = timeFieldsOf(source)
  const conclusion = conclusionOf(source)
  const family = key.family
  return {
    key: key.key,
    keyVia: key.via,
    keyReason: key.reason,
    keyAmbiguous: false,
    jobId: key.via === 'job_id' ? key.key.replace(/^job:/, '') : null,
    rawId: source.id || '',
    // 主标题只用中文家族名；技术来源下沉到证据区
    title: family ? family.title : (source.label || '未识别报告'),
    variantLabel: source.format ? String(source.format).toUpperCase() : '未知格式',
    group: family ? family.group : 'close_run',
    groupLabel: family ? GROUP_LABEL[family.group] : '未归类',
    status: source.status || 'unavailable',
    readable: readability.readable,
    readability,
    conclusion,
    generatedAt: time.generatedAt,
    generatedAtMissing: time.generatedAtMissing,
    marketCutoff: time.marketCutoff,
    marketCutoffFrom: time.marketCutoffFrom,
    fileMtime: time.fileMtime,
    observedAt: time.observedAt,
    detail: source.detail || '',
    format: source.format || '',
    // 原始 label 仅用于检索（不显示），使 “profit radar” 能同时找到两个家族
    rawLabel: source.label || '',
    schemaVersion: source.schema_version === undefined || source.schema_version === null ? '' : String(source.schema_version),
    sourcePath: source.path || '',
    summaryKeys: source.summary && typeof source.summary === 'object' ? Object.keys(source.summary).sort() : [],
    needsAttention: !readability.readable || time.generatedAtMissing || Boolean(key.reason)
  }
}

export function normalizeReports (payload) {
  const rows = payload && Array.isArray(payload.reports) ? payload.reports : []
  const normalized = rows.map(normalizeReport)
  const seen = new Map()
  normalized.forEach(row => {
    if (!row.key) return
    seen.set(row.key, (seen.get(row.key) || 0) + 1)
  })
  return normalized.map(row => {
    const duplicated = row.key && seen.get(row.key) > 1
    return duplicated
      ? { ...row, keyAmbiguous: true, keyReason: '标识重复，无法安全区分，已停止选择；请查看其他报告或等待目录修复。', needsAttention: true }
      : row
  })
}

// ---------- 筛选 ----------
export function filterReports (rows, options) {
  const { group = 'all', query = '', attentionOnly = false } = options || {}
  const needle = String(query).trim().toLowerCase()
  return (Array.isArray(rows) ? rows : []).filter(row => {
    if (group !== 'all' && row.group !== group) return false
    if (attentionOnly && !row.needsAttention) return false
    if (!needle) return true
    const haystack = [row.title, row.variantLabel, row.groupLabel, row.detail, row.keyReason, row.rawLabel]
      .filter(Boolean).join(' ').toLowerCase()
    return haystack.includes(needle)
  })
}

// ---------- 选中项（稳定键；失败必须提示，不静默） ----------
// 注意：**不可读取的报告也允许被选中**（否则用户看不到走不通的原因）；
// 但“默认选中”只会在可读取项里挑。
export function resolveSelection (rows, wantedKey) {
  const list = Array.isArray(rows) ? rows : []
  if (wantedKey) {
    const matches = list.filter(row => row.key === wantedKey)
    if (matches.length > 1 || matches.some(row => row.keyAmbiguous)) {
      return { selectedKey: null, note: '标识重复，无法安全区分，已停止选择；请查看其他报告或等待目录修复。', fellBack: false }
    }
    const hit = findRow(list, wantedKey)
    if (hit) return { selectedKey: hit.key, note: hit.keyReason || '', fellBack: false }
    const fallback = list.find(row => row.readable && findRow(list, row.key)) || null
    return {
      selectedKey: fallback ? fallback.key : null,
      note: fallback
        ? `原选中项已不在当前列表中，已切换到其他可读取报告。${fallback.keyReason || ''}`
        : '原选中项已不在当前列表中，当前没有可安全选中的报告，请重新选择。',
      fellBack: true
    }
  }
  const usable = list.filter(row => row.readable && findRow(list, row.key))
  if (!usable.length) {
    const note = list.some(row => !row.key || row.keyAmbiguous || list.filter(other => other.key === row.key).length > 1)
      ? '当前没有可安全选中的报告（标识缺失或重复），请查看其他报告或等待目录修复。'
      : '当前没有可读取的报告。'
    return { selectedKey: null, note, fellBack: false }
  }
  const preferred = usable.find(row => row.key === 'job:ec6c86fd856c') || usable[0]
  return { selectedKey: preferred.key, note: preferred.keyReason || '', fellBack: false }
}

export function findRow (rows, key) {
  if (!key) return null
  const matches = (Array.isArray(rows) ? rows : []).filter(row => row.key === key)
  // keyAmbiguous comes from the full catalogue and survives later filtering.
  return matches.length === 1 && !matches[0].keyAmbiguous ? matches[0] : null
}

// ---------- 竞态保护（旧响应不得覆盖新响应） ----------
export function createLoader () {
  let seq = 0
  return {
    next () { seq += 1; return seq },
    isCurrent (token) { return token === seq },
    get current () { return seq }
  }
}

// ---------- 摘要分组（同家族多格式 → 一个入口，但保留各自状态与时间） ----------
export function groupByFamily (rows) {
  const map = new Map()
  ;(Array.isArray(rows) ? rows : []).forEach(row => {
    const familyKey = row.familyKey || (row.key && row.key.startsWith('job:') ? row.key : `family:${(row.group || 'unknown')}:${row.title}`)
    if (!map.has(familyKey)) map.set(familyKey, { familyKey, title: row.title, groupLabel: row.groupLabel, variants: [] })
    map.get(familyKey).variants.push(row)
  })
  return Array.from(map.values())
}
