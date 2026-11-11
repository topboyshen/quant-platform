// Display persisted, run-bound evidence only. Never calculate trading indicators here.
const record = value => value && typeof value === 'object' && !Array.isArray(value)
const finite = value => typeof value === 'number' && Number.isFinite(value)
const day = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value
const number = (value, digits = 2) => finite(value) ? value.toFixed(digits) : '未记录'
const signalNames = { volume_breakout_ma60: '日线放量突破', weekly_macd_golden_cross: '周线金叉' }
const absent = (status, label) => ({ status, label, daily: label, weekly: '', details: [] })

function frozenDiagnosticRule (value, binding) {
  const rule = value.rule_attribution
  const declared = value.rule_attribution_status === 'run_bound' || value.validation_basis === 'frozen_run_rule' || rule != null
  if (!declared) {
    if ((value.rule_attribution_status != null && value.rule_attribution_status !== 'unavailable') ||
        (value.validation_basis != null && value.validation_basis !== 'legacy_strict_unattributed') || value.daily.enabled === false) return false
    return null // Older evidence keeps both branches; today's config is not consulted.
  }
  if (value.rule_attribution_status !== 'run_bound' || value.validation_basis !== 'frozen_run_rule' || !record(rule) ||
      rule.schema_version !== 'quant.etf-run-rule.v1' || rule.scope !== 'diagnostic_switch_and_pool_not_full_strategy' ||
      rule.run_id !== binding.runId || rule.decision_date !== binding.decisionDate ||
      typeof rule.daily_volume_breakout !== 'boolean' || value.daily.enabled !== rule.daily_volume_breakout ||
      rule.breakout_ma !== value.daily.ma_period || !Number.isInteger(rule.breakout_ma) || rule.breakout_ma < 1 || rule.breakout_ma > 1000 ||
      !Number.isInteger(rule.volume_window) || rule.volume_window < 1 || rule.volume_window > 1000 || value.daily.volume_window !== rule.volume_window ||
      !/^[0-9a-f]{64}$/.test(rule.rule_sha256) || !/^[0-9a-f]{64}$/.test(rule.decision_source_sha256)) return false
  const targets = rule.target_codes
  if (!Array.isArray(targets) || targets.length < 1 || targets.length > 200 || rule.target_count !== targets.length ||
      targets.some(code => typeof code !== 'string' || !/^(?:15|16|18|50|51|52|56|58)\d{4}$/.test(code)) ||
      new Set(targets).size !== targets.length || [...targets].sort().some((code, index) => code !== targets[index]) ||
      !targets.includes(binding.code) || !Array.isArray(binding.targetCodes)) return false
  const boundTargets = [...binding.targetCodes].sort()
  if (boundTargets.length !== targets.length || boundTargets.some((code, index) => code !== targets[index])) return false
  return rule
}

export function triggerDiagnosticView (value, binding = {}) {
  if (!record(value) || value.status === 'not_recorded') return absent('not_recorded', '本批次未记录逐项原因')
  if (value.status === 'unavailable') return absent('unavailable', '暂无本批次诊断')
  if (value.status !== 'recorded' || !binding.runId || value.run_id !== binding.runId ||
      !day(value.decision_date) || value.decision_date !== binding.decisionDate ||
      !record(value.data) || !record(value.daily) || !record(value.weekly) ||
      typeof value.data.ready !== 'boolean' || typeof value.daily.evaluated !== 'boolean' ||
      typeof value.weekly.evaluated !== 'boolean') return absent('invalid', '诊断证据不一致，请核实')
  const { data, daily, weekly } = value
  const frozenRule = frozenDiagnosticRule(value, binding)
  if (frozenRule === false) return absent('invalid', '规则归属证据不一致，请核实')
  const dailyEnabled = frozenRule === null || frozenRule.daily_volume_breakout
  const details = [
    `记录批次 ${value.decision_date}；行情 ${day(data.data_date) ? data.data_date : '未取得'}`,
    `日 K ${number(data.rows, 0)} 根；最低 ${number(data.minimum_rows, 0)} 根`
  ]
  if (!data.ready) {
    const dateMismatch = day(data.data_date) && day(data.required_date) && data.data_date !== data.required_date
    return {
 status: 'recorded',
label: '行情未满足评估要求',
      dailyEnabled,
      daily: dateMismatch ? `行情仅到 ${data.data_date}，需要 ${data.required_date}` : '行情不足或不可用，未评估技术条件',
      weekly: '未评估；不能视为未满足买入条件',
details
}
  }
  if (data.data_date !== value.decision_date || data.required_date !== value.decision_date) {
    return absent('invalid', '诊断行情日期不一致，请核实')
  }
  const dailyReasons = Array.isArray(daily.reason_codes) ? daily.reason_codes : []
  let dailyText = dailyReasons.includes('volume_missing') || dailyReasons.includes('volume_unavailable')
    ? '量能数据不足，日线条件尚不能完整判定'
    : dailyReasons.includes('ma_unavailable') ? '均线数据不足，尚不能判定' : '日线未完成评估'
  if (daily.evaluated) {
    if (![daily.previous_at_or_below_ma, daily.close_above_ma, daily.price_cross, daily.volume_ok, daily.triggered]
      .every(flag => typeof flag === 'boolean') || !finite(daily.ma_period) || !finite(daily.volume_threshold)) {
      return absent('invalid', '日线诊断证据不完整')
    }
    const ma = `MA${daily.ma_period}`
    const reasons = []
    if (!daily.previous_at_or_below_ma) reasons.push(`昨日已在 ${ma} 上方，不是新上穿`)
    if (!daily.close_above_ma) reasons.push(`收盘未站上 ${ma}`)
    if (!daily.volume_ok) {
 reasons.push(finite(daily.volume_ratio)
      ? `量比 ${number(daily.volume_ratio)}×，未达 ${number(daily.volume_threshold)}×`
      : '量能数据不足')
}
    dailyText = daily.triggered ? '新上穿且量能达标（风控另判）' : (reasons.join('；') || '未满足完整日线条件')
    details.push(`日线：昨日收盘 ${number(daily.previous_close, 4)} / ${ma} ${number(daily.previous_ma, 4)}；本日收盘 ${number(daily.close, 4)} / ${ma} ${number(daily.ma, 4)}`)
    details.push(`量能：本日 ${number(daily.volume, 0)} / 前${frozenRule ? frozenRule.volume_window : 20}日均量 ${number(daily.average_volume20, 0)}；判定量比 ${number(daily.volume_ratio)}×，阈值 ${number(daily.volume_threshold)}×`)
  }
  if (!dailyEnabled) dailyText = '本次规则未启用日线条件（仅反事实诊断，不算触发）'
  let weeklyText = finite(weekly.completed_bars) && finite(weekly.minimum_bars) && weekly.completed_bars < weekly.minimum_bars
    ? `完整周 K ${weekly.completed_bars}/${weekly.minimum_bars} 根，尚不能判定`
    : '周线未完成评估'
  if (weekly.evaluated) {
    if (![weekly.previous_at_or_below, weekly.now_above, weekly.golden_cross, weekly.triggered]
      .every(flag => typeof flag === 'boolean') || !day(weekly.week_date) || weekly.week_date > value.decision_date) {
      return absent('invalid', '周线诊断证据不完整')
    }
    weeklyText = weekly.triggered
? '完整周线新金叉（风控另判）'
      : !weekly.previous_at_or_below && weekly.now_above
? '已在金叉上方，并非新金叉'
        : '最新完整周线尚未金叉'
    details.push(`完整周 K ${number(weekly.completed_bars, 0)} / 最低 ${number(weekly.minimum_bars, 0)} 根；最新周 ${weekly.week_date}`)
    details.push(`周线：上周 DIF/DEA ${number(weekly.previous_dif, 4)} / ${number(weekly.previous_dea, 4)}；本周 ${number(weekly.dif, 4)} / ${number(weekly.dea, 4)}`)
  }
  return { status: 'recorded', label: '同批次记录；非实时指标', dailyEnabled, daily: dailyText, weekly: weeklyText, details }
}

export function triggerHistoryView (value, { decisionDate } = {}) {
  const unavailable = { label: '历史证据不可用', detail: '', date: '', excluded: 0 }
  if (!record(value) || !['available', 'none_in_window', 'truncated'].includes(value.status) ||
      !Number.isInteger(value.scanned_runs) || value.scanned_runs < 0 || value.scanned_runs > 400) return unavailable
  const excluded = Number.isInteger(value.excluded_stale_count) && value.excluded_stale_count >= 0 ? value.excluded_stale_count : 0
  const event = value.latest_raw_trigger
  if (!event) {
 return {
 label: value.status === 'truncated' ? '历史检索已截断，不能判定' : '所查批次未见合格触发',
    detail: `检查最近 ${value.scanned_runs} 批${excluded ? `；排除旧行情 ${excluded} 条` : ''}`,
date: '',
excluded
}
}
  if (!record(event) || !day(event.signal_data_date) || !day(event.first_observed_date) || !day(decisionDate) ||
      event.signal_data_date > event.first_observed_date || event.first_observed_date > decisionDate ||
      !Object.prototype.hasOwnProperty.call(signalNames, event.signal_type)) return unavailable
  const risk = event.target_risk_action === 'blocked'
? '该目标被风控拦截'
    : event.target_risk_action === 'allowed' ? '该目标批次含放行信号' : '风控状态未知'
  return {
 date: event.signal_data_date,
label: `${event.signal_data_date} · ${signalNames[event.signal_type]}`,
    detail: `窗口内首次记录 ${event.first_observed_date}；${risk}；仅历史，不代表现在可买${value.status === 'truncated' ? '；历史检索已截断' : ''}${excluded ? `；排除旧行情 ${excluded} 条` : ''}`,
excluded
}
}
