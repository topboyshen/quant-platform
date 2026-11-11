export const DEFAULT_RADAR_MAX_AGE_MS = 24 * 60 * 60 * 1000

const FUTURE_CLOCK_SKEW_MS = 5 * 60 * 1000

const normalizeKey = value => String(value == null ? '' : value)
  .trim()
  .toLowerCase()
  .replace(/[\s-]+/g, '_')

const localize = (value, labels, fallback) => labels[normalizeKey(value)] || fallback

const STATUS_LABELS = {
  ok: '正常',
  healthy: '正常',
  ready: '已就绪',
  degraded: '降级',
  warning: '需注意',
  error: '异常',
  failed: '失败',
  unavailable: '不可用',
  unknown: '未知'
}

const RISK_LABELS = {
  low: '低',
  normal: '正常',
  stable: '稳定',
  warning: '预警',
  moderate: '中等',
  medium: '中等',
  elevated: '偏高',
  high: '高',
  critical: '严重',
  extreme: '极高',
  risk_on: '风险偏好',
  risk_off: '风险规避',
  growth_blocked: '增长受阻',
  recession_risk: '衰退风险',
  inflation_pressure: '通胀压力',
  liquidity_tightening: '流动性收紧',
  liquidity_supportive: '流动性支撑',
  unavailable: '不可用',
  unknown: '未知'
}

const SIGNAL_LABELS = {
  strong_buy: '强力买入',
  buy: '买入',
  long: '做多',
  bullish: '看多',
  accumulate: '逐步建仓',
  strong_sell: '强力卖出',
  sell: '卖出',
  short: '做空',
  bearish: '看空',
  reduce: '减仓',
  hold: '持有',
  wait: '观望',
  watch: '观望',
  neutral: '中性',
  no_signal: '无信号',
  no_trigger: '未触发',
  weekly_macd_golden_cross: '周线 MACD 金叉',
  volume_breakout_ma60: '放量突破 60 日均线',
  blocked: '已拦截',
  unavailable: '不可用'
}

const AUDIT_LABELS = {
  completed: '已完成',
  complete: '已完成',
  success: '成功',
  passed: '已通过',
  running: '运行中',
  pending: '等待中',
  partial: '部分完成',
  failed: '失败',
  error: '异常',
  missing: '缺失',
  skipped: '已跳过',
  unavailable: '不可用'
}

const EFFECTIVENESS_LABELS = {
  insufficient: '样本不足',
  insufficient_samples: '样本不足',
  sample_insufficient: '样本不足',
  uncertain: '有效性不确定',
  preliminary: '初步有效',
  effective: '有效',
  ineffective: '未见有效性',
  invalid: '无效',
  unavailable: '不可用',
  unknown: '未知'
}

const OUTCOME_LABELS = {
  allowed: '已放行',
  approved: '已批准',
  pass: '已通过',
  passed: '已通过',
  eligible: '具备决策资格',
  blocked: '已拦截',
  rejected: '已拒绝',
  no_signal: '无信号',
  no_trigger: '未触发',
  data_failed: '数据失败',
  failed: '失败',
  pending: '待定',
  emitted: '已生成信号',
  completed: '已完成',
  unavailable: '不可用'
}

const REASON_LABELS = {
  signal_not_triggered: '已评估，技术条件未触发',
  signal_allowed: '已触发并通过风控',
  signal_blocked: '已触发但未获风控放行',
  macro_risk_blocked: '已触发，被宏观风控拦截',
  trend_data_unavailable: '所需行情尚不可用',
  eod_evidence_missing: '本次运行缺少逐标的证据',
  eod_evidence_invalid: '证据不完整或相互矛盾',
  etf_pool_evidence_incomplete: 'ETF 研究池证据需核实',
  etf_universe_invalid: 'ETF 研究池配置不可判定',
  market_calendar_unavailable: '交易日历不可判定',
  eod_running: '收盘评估进行中',
  factor_history_insufficient: '可信因子历史不足',
  audit_missing: '今日审计缺失',
  audit_running: '今日审计仍在运行',
  data_incomplete: '数据不完整',
  risk_snapshot_incomplete: '宏观风险快照不完整',
  macro_data_stale: '宏观数据已陈旧',
  market_data_stale: '市场数据已陈旧',
  signal_data_stale: '信号数据已陈旧',
  market_data_unavailable: '市场数据不可用',
  risk_data_unavailable: '风险数据不可用',
  no_current_signal: '当前没有有效信号',
  radar_not_configured: 'Radar 服务尚未配置',
  radar_invalid_configuration: 'Radar 服务配置无效',
  radar_credential_unavailable: 'Radar 服务凭据不可用',
  radar_credential_invalid: 'Radar 服务凭据无效',
  radar_auth_failed: 'Radar 服务认证失败',
  radar_unavailable: 'Radar 服务暂时不可用',
  radar_upstream_error: 'Radar 上游服务异常',
  radar_schema_mismatch: 'Radar 数据格式不兼容',
  radar_invalid_response: 'Radar 返回了无效数据',
  radar_response_too_large: 'Radar 返回数据超出限制',
  radar_redirect_rejected: 'Radar 返回了不允许的跳转'
}

export function localizeRadarStatus (value) {
  return localize(value, STATUS_LABELS, '未知状态')
}

export function localizeAvailability (value) {
  if (value === true) return '可用'
  if (value === false) return '不可用'
  return '未知'
}

export function localizeRiskLevel (value) {
  return localize(value, RISK_LABELS, '未知风险')
}

export function localizeSignalType (value) {
  return localize(value, SIGNAL_LABELS, '其他信号')
}

export function localizeAuditStatus (value) {
  return localize(value, AUDIT_LABELS, '未知审计状态')
}

export function localizeEffectivenessState (value) {
  return localize(value, EFFECTIVENESS_LABELS, '有效性未知')
}

export function localizeOutcome (value) {
  return localize(value, OUTCOME_LABELS, '无终态')
}

export function localizeReason (value) {
  const key = normalizeKey(value)
  if (REASON_LABELS[key]) return REASON_LABELS[key]
  if (key.includes('stale')) return '数据已陈旧'
  if (key.includes('missing')) return '必需数据缺失'
  if (key.includes('incomplete')) return '数据不完整'
  if (key.includes('insufficient')) return '数据或样本不足'
  if (key.includes('unavailable')) return '数据暂时不可用'
  if (key.includes('running') || key.includes('pending')) return '数据处理中'
  if (key.includes('timeout')) return '数据请求超时'
  if (key.includes('failed') || key.includes('error')) return '数据处理失败'
  return '其他降级原因'
}

export function parseRadarTime (value) {
  if (value === null || value === undefined || value === '') return null
  const timestamp = typeof value === 'number' ? value : Date.parse(String(value))
  return Number.isFinite(timestamp) ? timestamp : null
}

export function formatRadarTime (value, options = {}) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))) return String(value)
  const timestamp = parseRadarTime(value)
  if (timestamp === null) return '--'
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      ...(options.timeZone ? { timeZone: options.timeZone } : {})
    }).format(new Date(timestamp))
  } catch (_) {
    return new Date(timestamp).toISOString().replace('T', ' ').slice(0, 16)
  }
}

export function formatRadarNumber (value) {
  if (value === null || value === undefined || typeof value === 'boolean') return '--'
  if (typeof value === 'string' && value.trim() === '') return '--'
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return Math.abs(number) >= 100 ? number.toFixed(0) : number.toFixed(2)
}

export function getRadarRiskPresentation (input = {}, options = {}) {
  const risk = input && typeof input === 'object' && !Array.isArray(input) ? input : {}
  const requiredFields = Array.isArray(options.requiredFields) && options.requiredFields.length
    ? options.requiredFields
    : ['vix', 'us_10y']
  const explicitMissing = Array.isArray(risk.missing_fields)
    ? risk.missing_fields.map(normalizeKey).filter(Boolean)
    : []
  const inferredMissing = requiredFields.filter(field => {
    const value = risk[field]
    return value === null || value === undefined || (typeof value === 'string' && value.trim() === '')
  })
  const missingFields = [...new Set([...explicitMissing, ...inferredMissing])]
  const hasSnapshot = Object.keys(risk).length > 0
  const unavailable = options.unavailable === true || risk.available === false

  if (unavailable) {
    return {
      state: 'unavailable',
      label: localizeRiskLevel('unavailable'),
      level: 'unavailable',
      available: false,
      complete: false,
      missingFields
    }
  }
  if (!hasSnapshot) {
    return {
      state: 'unknown',
      label: localizeRiskLevel('unknown'),
      level: 'unknown',
      available: false,
      complete: false,
      missingFields
    }
  }
  if (risk.data_complete === false || missingFields.length > 0) {
    return {
      state: 'incomplete',
      label: '数据不完整',
      level: normalizeKey(risk.level) || 'unknown',
      available: true,
      complete: false,
      missingFields
    }
  }
  const level = normalizeKey(risk.level) || 'unknown'
  return {
    state: 'complete',
    label: localizeRiskLevel(level),
    level,
    available: true,
    complete: true,
    missingFields
  }
}

export function getRadarFreshness (input = {}, options = {}) {
  const now = Number.isFinite(Number(options.now)) ? Number(options.now) : Date.now()
  const maxAgeMs = options.maxAgeMs === undefined ? DEFAULT_RADAR_MAX_AGE_MS : Number(options.maxAgeMs)
  const generatedAt = parseRadarTime(input.generatedAt || input.generated_at)
  const asOf = parseRadarTime(input.asOf || input.as_of)
  const expectedAsOf = parseRadarTime(input.expectedAsOf || input.expected_as_of)

  if (generatedAt === null && asOf === null) {
    return { stale: true, state: 'unknown', label: '时间未知', reason: '缺少数据时间' }
  }
  if (expectedAsOf !== null && asOf !== null && asOf < expectedAsOf) {
    return { stale: true, state: 'stale', label: '数据陈旧', reason: '数据日早于应有数据日' }
  }
  if (generatedAt !== null && generatedAt > now + FUTURE_CLOCK_SKEW_MS) {
    return { stale: true, state: 'invalid', label: '时间异常', reason: '生成时间晚于当前时间' }
  }
  if (generatedAt !== null && (!Number.isFinite(maxAgeMs) || maxAgeMs < 0 || now - generatedAt >= maxAgeMs)) {
    return { stale: true, state: 'stale', label: '数据陈旧', reason: '距离最近生成已超过时效' }
  }
  return { stale: false, state: 'fresh', label: '数据新鲜', reason: '' }
}

export default {
  localizeRadarStatus,
  localizeAvailability,
  localizeRiskLevel,
  localizeSignalType,
  localizeAuditStatus,
  localizeEffectivenessState,
  localizeOutcome,
  localizeReason,
  parseRadarTime,
  formatRadarTime,
  formatRadarNumber,
  getRadarRiskPresentation,
  getRadarFreshness
}
