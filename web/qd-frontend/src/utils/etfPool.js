import { triggerDiagnosticView, triggerHistoryView } from './etfDiagnostics.js'

// Membership is a configuration contract, not a count of historical signals.
export const ETF_STATES = {
  no_trigger: { label: '未触发信号', color: '' },
  allowed: { label: '含放行信号', color: 'blue' },
  blocked: { label: '风控拦截', color: 'orange' },
  data_failed: { label: '行情不足', color: 'red' },
  missing: { label: '缺少评估证据', color: 'orange' },
  unknown: { label: '证据待核实', color: 'orange' },
  stale: { label: '历史批次', color: 'orange' },
  running: { label: '评估进行中', color: 'blue' }
}

const record = value => value && typeof value === 'object' && !Array.isArray(value)
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value
const code = value => typeof value === 'string' && /^(?:15|16|18|50|51|52|56|58)\d{4}$/.test(value)
const text = value => typeof value === 'string' ? value.slice(0, 120) : ''
const empty = () => ({
  available: false,
  configuredCount: null,
  expectedDate: '',
  decisionDate: '',
  observedAt: '',
  items: [],
  counts: {},
  pendingCount: 0
})

export function etfPoolView (value, options = {}) {
  const now = options.now === undefined ? Date.now() : options.now
  const fetched = Date.parse(options.generatedAt)
  const snapshotCurrent = !Object.prototype.hasOwnProperty.call(options, 'generatedAt') ||
    (Number.isFinite(fetched) && fetched <= now + 300000 && now - fetched < 300000)
  if (!record(value) || value.schema_version !== 'quant.etf-pool.v1' ||
      !Number.isInteger(value.configured_count) || value.configured_count < 1 || value.configured_count > 200 ||
      !Array.isArray(value.items) || value.items.length !== value.configured_count) return empty()
  const codes = new Set()
  const items = []
  for (const raw of value.items) {
    if (!record(raw) || !code(raw.code) || codes.has(raw.code) || !Object.prototype.hasOwnProperty.call(ETF_STATES, raw.state)) return empty()
    codes.add(raw.code)
    const validPrice = typeof raw.reference_close === 'number' && Number.isFinite(raw.reference_close) && raw.reference_close > 0
    const diagnostics = triggerDiagnosticView(raw.trigger_diagnostics, { runId: value.run_id, decisionDate: raw.decision_date, code: raw.code, targetCodes: value.items.map(item => record(item) ? item.code : null) })
    // Keep the immutable business decision untouched, but do not present a
    // partially evaluated OR-rule as a fully verified absence of triggers.
    const conditionUncertain = raw.state === 'no_trigger' && (diagnostics.status === 'invalid' ||
      (diagnostics.status === 'recorded' && ((diagnostics.dailyEnabled !== false && raw.trigger_diagnostics.daily.evaluated !== true) || raw.trigger_diagnostics.weekly.evaluated !== true)))
    const current = !conditionUncertain && snapshotCurrent && raw.is_current === true && value.calendar_available === true &&
      ['success', 'partial'].includes(value.run_status) && date(value.expected_date) &&
      raw.decision_date === value.expected_date && raw.data_date === value.expected_date &&
      raw.data_status === 'ok' && validPrice && ['no_trigger', 'allowed', 'blocked'].includes(raw.state)
    const state = ['no_trigger', 'allowed', 'blocked'].includes(raw.state) && !current ? 'unknown' : raw.state
    items.push({
      code: raw.code,
      name: text(raw.name) || raw.code,
      sector: text(raw.sector),
      market: 'CNStock',
      state,
      current,
      decision_date: date(raw.decision_date) ? raw.decision_date : '',
      data_date: date(raw.data_date) ? raw.data_date : '',
      reference_close: validPrice ? raw.reference_close : null,
      reason_code: text(raw.reason_code),
      risk_level: text(raw.risk_level),
      diagnostics,
      conditionUncertain,
      triggerHistory: triggerHistoryView(raw.history, { decisionDate: value.decision_date }),
      ...ETF_STATES[state],
      ...(conditionUncertain ? { label: '分项条件待核实' } : {})
    })
  }
  const counts = Object.fromEntries(Object.keys(ETF_STATES).map(state => [state, items.filter(item => item.state === state).length]))
  return {
    available: true,
    configuredCount: value.configured_count,
    expectedDate: date(value.expected_date) ? value.expected_date : '',
    decisionDate: date(value.decision_date) ? value.decision_date : '',
    observedAt: text(value.observed_at),
    items,
    counts,
    pendingCount: items.filter(item => !item.current).length
  }
}

export function filterEtfPool (items, query = '', state = 'all') {
  const needle = String(query).trim().toLowerCase()
  return items.filter(item => (state === 'all' || item.state === state) &&
    [item.code, item.name, item.sector].some(value => value.toLowerCase().includes(needle)))
}

export function etfResearchContext (item, payload = {}, options = {}) {
  const pool = etfPoolView(payload.research_pool, { ...options, generatedAt: payload.generated_at })
  const row = pool.items.find(candidate => candidate.code === item.code)
  if (!row) return null
  // The legacy signal list has no run_id/per-signal gating provenance. Never
  // promote a date+code match to proof that THIS attempt allowed that signal.
  return {
    origin: 'radar',
    market: 'CNStock',
    symbol: row.code,
    name: row.name,
    signal: { summary: `研究池评估：${row.label}；具体信号需核对本次审计，不代表买卖建议。`, decision_date: row.decision_date },
    risk: row.current ? payload.risk : {},
    marketContext: row.current ? payload.market : {},
    asOf: row.data_date || row.decision_date,
    source: 'Trading Radar ETF 研究池（只读审计）',
    evidenceState: row.current ? 'current' : 'unavailable',
    evidenceUpdatedAt: payload.generated_at
  }
}

export function historicalEtfContext (row, payload = {}) {
  if (!record(row) || !code(row.code) || !date(row.decision_date)) return null
  return {
    origin: 'radar',
    market: 'CNStock',
    symbol: row.code,
    name: text(row.name),
    signal: row,
    // Current global risk must not masquerade as risk at the historical signal date.
    risk: {},
    marketContext: {},
    asOf: row.decision_date,
    source: 'Trading Radar 历史信号（仅回看）',
    evidenceState: 'historical',
    evidenceUpdatedAt: payload.generated_at
  }
}

// Coalesce overlapping refreshes; responses cannot arrive out of order and revive stale data.
export function singleFlight (owner, key, task) {
  if (owner[key]) return owner[key]
  const pending = Promise.resolve().then(task).finally(() => {
    if (owner[key] === pending) owner[key] = null
  })
  owner[key] = pending
  return pending
}
