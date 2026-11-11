// Pure, framework-free helpers for the V2 research workspace (unit-tested in tests/unit/researchLabV2Workspace.test.mjs).

export const MODULES = [
  { key: 'ai', label: 'AI 资产分析' },
  { key: 'indicator', label: '指标研究' },
  { key: 'strategy', label: '策略研究' },
  { key: 'backtest', label: '回测验证' }
]

export const STALE_LABELS = {
  EVIDENCE_CHANGED: '基于旧版本（外部证据已改）',
  HYPOTHESIS_CHANGED: '基于旧版本（研究假设已改）',
  INDICATOR_CHANGED: '基于旧版本（指标快照已改）',
  STRATEGY_CHANGED: '基于旧版本（策略版本已改）',
  RUN_CHANGED: '基于旧版本（回测结果已改）',
  INHERITED_REVIEW: '基于旧版本（继承自父分支）'
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

// Local forms are drafts, never inferred from a selected historical run.
export function defaultBacktestForm () {
  return { startDate: '2024-09-02', endDate: '2026-09-25', initialCapital: 100000, commissionPct: 0.03, slippagePct: 0.05, override: {} }
}

export function normalizeBacktestForm (value) {
  const form = defaultBacktestForm()
  if (!value || typeof value !== 'object' || Array.isArray(value)) return form
  Object.keys(form).filter(k => k !== 'override').forEach(k => {
    if (Object.prototype.hasOwnProperty.call(value, k)) form[k] = value[k]
  })
  if (value.override && typeof value.override === 'object' && !Array.isArray(value.override)) form.override = { ...value.override }
  return form
}

export function researchStorageKey (kind, userId, projectId, branchId) {
  if (userId === null || userId === undefined || userId === '' || !projectId || !branchId) return null
  return 'rlv2.v2.' + [kind, userId, projectId, branchId].map(v => encodeURIComponent(String(v))).join('.')
}

// This canonical body summary is for integrity/identity comparison, not a cryptographic digest.
export function pendingRunBodySummary (value) {
  const canonical = v => {
    if (Array.isArray(v)) return v.map(canonical)
    if (v && typeof v === 'object') return Object.keys(v).sort().reduce((o, k) => { o[k] = canonical(v[k]); return o }, {})
    return v
  }
  return JSON.stringify(canonical(value))
}

export function sameConfigRunBody (run, branchId, branchRevision) {
  const c = run && run.effectiveConfig
  const number = v => typeof v === 'number' && Number.isFinite(v)
  const date = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v)
  const params = c && c.params
  if (!run || run.branchId !== branchId || !run.strategyArtifactId || !c || !date(c.startDate) || !date(c.endDate) ||
    c.startDate > c.endDate || !number(c.initialCapital) || c.initialCapital <= 0 ||
    !c.commission || c.commission.unit !== 'ratio' || !number(c.commission.rate) || c.commission.rate < 0 ||
    !c.slippage || c.slippage.unit !== 'ratio' || !number(c.slippage.rate) || c.slippage.rate < 0 ||
    c.leverage !== 1 || !params || typeof params !== 'object' || Array.isArray(params) ||
    Object.values(params).some(v => v === undefined || v === null || typeof v === 'object' || (typeof v === 'number' && !Number.isFinite(v)))) {
    throw new Error('所选记录缺少当前引擎支持的完整配置，无法按相同配置创建新请求')
  }
  return {
    strategyArtifactId: run.strategyArtifactId,
    expectedBranchRevision: branchRevision,
    config: {
      startDate: c.startDate,
      endDate: c.endDate,
      initialCapital: c.initialCapital,
      commissionPct: c.commission.rate * 100,
      slippagePct: c.slippage.rate * 100,
      leverage: c.leverage,
      paramsOverride: { ...params }
    }
  }
}

// Truthful save state (contract §3.6): never "saved" while there are unsaved local edits.
export function saveStateLabel ({ dirty = false, draft = null, confirmedId = null } = {}) {
  if (dirty) return { tone: 'warn', text: '本页有未保存修改（仅本浏览器会话，退出登录即丢失）' }
  if (draft && draft.keptAfterConfirm) {
    return { tone: 'ok', text: `已确认为快照 ${draft.confirmedAsArtifactId} · 草稿 rev ${draft.draftRevision} 仍保留` }
  }
  if (draft) return { tone: 'info', text: `草稿已保存 rev ${draft.draftRevision}（未确认）` }
  if (confirmedId) return { tone: 'ok', text: '与已确认内容一致' }
  return { tone: 'muted', text: '尚无已确认内容' }
}

// Latest-request-wins guard for out-of-order responses.
export function createLatestGuard () {
  let seq = 0
  return {
    next () { seq += 1; return seq },
    isLatest (token) { return token === seq }
  }
}

export function newIdempotencyKey (rand = Math.random) {
  let out = ''
  const cryptoObj = typeof globalThis !== 'undefined' ? globalThis.crypto : null
  if (cryptoObj && cryptoObj.getRandomValues) {
    const a = new Uint8Array(16)
    cryptoObj.getRandomValues(a)
    a.forEach(b => { out += b.toString(16).padStart(2, '0') })
    return 'rk-' + out
  }
  for (let i = 0; i < 32; i++) out += Math.floor(rand() * 16).toString(16)
  return 'rk-' + out
}

export function classifyError (err) {
  const res = err && err.response
  if (!res) return { kind: 'network', msg: '网络中断：请求结果未知（服务端可能已处理）。', type: 'NETWORK', status: 0, data: null }
  const body = res.data || {}
  const type = (body.error && body.error.type) || ''
  const status = res.status
  let kind = 'server'
  if (status === 409) kind = 'conflict'
  else if (status === 403 && type === 'FEATURE_DISABLED') kind = 'disabled'
  else if (status === 403) kind = 'forbidden'
  else if (status === 404) kind = 'not_found'
  else if (status === 400 || status === 422) kind = 'validation'
  else if (status === 503) kind = 'unavailable'
  return { kind, type, status, msg: body.msg || `请求失败（HTTP ${status}）`, data: body.data || null }
}

export function runDisplay (run) {
  if (!run) return null
  const labels = []
  if (run.displayState === 'pending_reconcile') labels.push({ tone: 'warn', text: '待对账：尚未证明已停止，不允许重跑' })
  if (run.status === 'running') labels.push({ tone: 'info', text: run.reconcileSuggested ? '执行中（租约已过期 ≠ 已停止，请先对账）' : '执行中' })
  if (run.status === 'interrupted') labels.push({ tone: 'warn', text: '已对账确认中断，可在同一请求下重试' })
  if (run.status === 'failed') labels.push({ tone: 'error', text: `失败：${run.errorCode || ''}` })
  if (run.status === 'succeeded') {
    labels.push(run.adoption === 'adopted' ? { tone: 'ok', text: '当前结果' } : { tone: 'warn', text: '未设为当前结果' })
  }
  ;(run.lateResults || []).forEach(l => labels.push({ tone: 'warn', text: l.notice }))
  return labels
}

// "# @param name type default ..." (same comment syntax the backend parser reads); preview only,
// the server re-parses on capture.
export function parseParamComments (code) {
  const out = []
  String(code || '').split('\n').forEach(line => {
    const m = line.match(/^\s*#\s*@param\s+(\w+)\s+(\w+)\s+(\S+)(.*)$/)
    if (!m) return
    const [, name, type, raw, rest] = m
    let def = raw
    if (/^(int|integer)$/i.test(type)) def = parseInt(raw, 10)
    else if (/^(float|number)$/i.test(type)) def = parseFloat(raw)
    else if (/^(bool|boolean)$/i.test(type)) def = /^true$/i.test(raw)
    const range = (rest || '').match(/range=([-\d.]+):([-\d.]+)/)
    out.push({ name, type, default: def, min: range ? Number(range[1]) : null, max: range ? Number(range[2]) : null })
  })
  return out
}

export function paramRows (specs = [], confirmed = {}, applied = {}, draft = {}) {
  return specs.map(s => ({
    name: s.name,
    declared: s.default,
    confirmed: confirmed && Object.prototype.hasOwnProperty.call(confirmed, s.name) ? confirmed[s.name] : null,
    applied: applied && Object.prototype.hasOwnProperty.call(applied, s.name) ? applied[s.name] : s.default,
    draft: draft && Object.prototype.hasOwnProperty.call(draft, s.name) ? draft[s.name] : (applied && Object.prototype.hasOwnProperty.call(applied, s.name) ? applied[s.name] : s.default),
    range: s.min != null ? `${s.min}–${s.max}` : ''
  }))
}

export function isStrategyWorkEmpty (w) {
  return !w || (!w.handoffArtifactId && (w.mapped === null || w.mapped === undefined) && !w.mappingConfirmed && !w.code && !w.sourceId && !Object.keys(w.mappings || {}).length)
}

export function workEquals (a, b) {
  const keys = ['handoffArtifactId', 'mapped', 'mappingConfirmed', 'code', 'sourceId', 'mappingVersion', 'indicatorPayloadSha256', 'mappings', 'confirmedMappingKey']
  const norm = (w, k) => (k === 'mappingConfirmed' ? !!(w || {})[k] : ((w || {})[k] ?? null))
  return keys.every(k => same(norm(a, k), norm(b, k)))
}

export function describeEffective (params) {
  return Object.keys(params || {}).map(k => `${k}=${params[k]}`).join(', ')
}

// Minimal line-chart geometry for the indicator preview (server-computed series; nothing is recomputed here).
const CHART_COLORS = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd', '#ff7f0e']
export function chartPaths (preview, width = 600, height = 200, pad = 6) {
  if (!preview || !Array.isArray(preview.close) || !preview.close.length) return { lines: [], min: null, max: null }
  const named = [['close', preview.close], ...Object.entries(preview.series || {})]
  const vals = named.flatMap(([, arr]) => arr).filter(v => typeof v === 'number' && isFinite(v))
  if (!vals.length) return { lines: [], min: null, max: null }
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const n = preview.close.length
  const x = i => pad + (n === 1 ? 0 : (i * (width - 2 * pad)) / (n - 1))
  const y = v => (max === min ? height / 2 : pad + ((max - v) * (height - 2 * pad)) / (max - min))
  const lines = named.map(([name, arr], k) => {
    let d = ''
    let pen = false
    arr.forEach((v, i) => {
      if (typeof v !== 'number' || !isFinite(v)) { pen = false; return }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
      pen = true
    })
    const last = [...arr].reverse().find(v => typeof v === 'number' && isFinite(v))
    return { name, d, color: CHART_COLORS[k % CHART_COLORS.length], last: last === undefined ? null : last }
  })
  return { lines, min, max }
}

// AI review result is only archivable against the run it was produced for (same run id + same context sha).
export function reviewArchivable (result, ctx) {
  return !!(result && ctx && result.runId === ctx.runId && result.contextSha256 === ctx.contextSha256 && result.reply)
}
