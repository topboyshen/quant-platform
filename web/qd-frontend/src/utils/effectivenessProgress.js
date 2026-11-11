const SCHEMA = 'quant.etf-effectiveness-progress.v1'
const HORIZONS = [1, 3, 5, 10, 20]
const COUNTS = ['candidate_records', 'excluded_records', 'eligible_records', 'mature_records', 'awaiting_outcome_records', 'unfilled_records', 'invalid_outcome_records']
const EXCLUSIONS = {
  run_not_success: '所属批次未完整成功',
  outside_decision_window: '不在规定收盘窗口',
  non_oos: '不是样本外验证记录',
  invalid_record: '记录证据无效'
}
const PENDING = {
  not_due: '尚待预计收盘时点',
  due_uncollected: '已到预计日期，待结果',
  date_unknown: '缺少有效日历，日期未知'
}
const STATES = {
  sample_insufficient: { label: '样本不足', color: 'orange', insufficient: true },
  insufficient_samples: { label: '样本不足', color: 'orange', insufficient: true },
  insufficient: { label: '样本不足', color: 'orange', insufficient: true },
  uncertain: { label: '有效性不确定', color: 'gold' },
  preliminary_effective: { label: '初步有效（研究）', color: 'blue' },
  preliminary: { label: '初步有效（研究）', color: 'blue' },
  effective: { label: '有效性研究结论', color: 'blue' },
  not_effective: { label: '未见有效性', color: 'orange' },
  ineffective: { label: '未见有效性', color: 'orange' }
}

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const count = (value, max = 2000) => Number.isSafeInteger(value) && value >= 0 && value <= max
const sum = value => Object.values(value).reduce((total, n) => total + n, 0)
const countGroup = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => count(value[key]))
const rows = (counts, labels) => Object.entries(labels).map(([key, label]) => ({ key, label, count: counts[key] }))

function validDate (value) {
  if (value === null) return true
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const stamp = Date.parse(`${value}T00:00:00Z`)
  return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value
}

function validCore (core) {
  if (!object(core) || core.invalid === true || !Object.prototype.hasOwnProperty.call(STATES, core.state)) return false
  if (!count(core.sample_size, 1000000) || !count(core.minimum_samples, 10000) || core.minimum_samples === 0 || !HORIZONS.includes(core.primary_horizon_sessions)) return false
  if (Object.prototype.hasOwnProperty.call(core, 'method') && core.method !== 'live_oos_or_walk_forward_oos') return false
  if (Object.prototype.hasOwnProperty.call(core, 'metrics_available') && (typeof core.metrics_available !== 'boolean' || core.metrics_available !== (core.sample_size >= core.minimum_samples))) return false
  // A label may not turn an insufficient sample into positive evidence.
  return Boolean(STATES[core.state].insufficient) === (core.sample_size < core.minimum_samples)
}

function validProgress (progress, core) {
  if (!object(progress) || progress.schema_version !== SCHEMA || progress.unit !== 'decision_records' || progress.status !== 'ok') return false
  if (!COUNTS.every(key => count(progress[key]))) return false
  if (!countGroup(progress.excluded_reasons, Object.keys(EXCLUSIONS)) || !countGroup(progress.awaiting_breakdown, Object.keys(PENDING)) || !countGroup(progress.horizon_samples, HORIZONS.map(String))) return false
  if (progress.candidate_records !== progress.excluded_records + progress.eligible_records || progress.excluded_records !== sum(progress.excluded_reasons)) return false
  if (progress.eligible_records !== progress.mature_records + progress.awaiting_outcome_records + progress.unfilled_records + progress.invalid_outcome_records || progress.awaiting_outcome_records !== sum(progress.awaiting_breakdown)) return false
  if (progress.mature_records !== core.sample_size || progress.horizon_samples[String(core.primary_horizon_sessions)] !== core.sample_size) return false
  if (Object.values(progress.horizon_samples).some(n => n > progress.eligible_records)) return false
  if (progress.expected_maturity_is_estimate !== true || !validDate(progress.latest_evaluation_as_of) || !validDate(progress.next_expected_maturity_date)) return false
  return progress.next_expected_maturity_date === null || (progress.awaiting_outcome_records > 0 && progress.awaiting_breakdown.not_due > 0)
}

/** Present saved evidence only: no date-based maturation or sample reclassification. */
export function effectivenessProgressView (core, options = {}) {
  const view = {
    available: false,
    label: '证据不可用',
    color: '',
    sampleSize: '--',
    minimumSamples: '--',
    primaryHorizon: null,
    detailsAvailable: false,
    detailMessage: '缺少有效的成熟样本证据。',
    progress: null
  }
  if (options.unavailable === true || !validCore(core)) return view
  Object.assign(view, {
    available: true,
    label: STATES[core.state].label,
    color: STATES[core.state].color,
    sampleSize: core.sample_size,
    minimumSamples: core.minimum_samples,
    primaryHorizon: core.primary_horizon_sessions,
    detailMessage: '进度明细暂不可用'
  })
  if (core.progress == null) return view
  if (!validProgress(core.progress, core)) {
    const status = object(core.progress) ? core.progress.status : null
    view.detailMessage = status === 'truncated'
      ? '进度明细暂不可用：记录超过本次读取上限'
      : status === 'unavailable'
        ? '进度明细暂不可用：未取得完整证据'
        : '进度明细暂不可用：证据不完整或不一致'
    return view
  }
  const progress = core.progress
  view.detailsAvailable = true
  view.detailMessage = ''
  view.progress = {
    candidate: progress.candidate_records,
    eligible: progress.eligible_records,
    awaiting: progress.awaiting_outcome_records,
    excluded: progress.excluded_records,
    unfilled: progress.unfilled_records,
    invalidOutcome: progress.invalid_outcome_records,
    excludedReasons: rows(progress.excluded_reasons, EXCLUSIONS),
    awaitingReasons: rows(progress.awaiting_breakdown, PENDING),
    shortHorizons: HORIZONS.filter(h => h < core.primary_horizon_sessions).map(h => ({ days: h, count: progress.horizon_samples[String(h)] })),
    latestEvaluation: progress.latest_evaluation_as_of,
    nextMaturity: progress.next_expected_maturity_date
  }
  return view
}

const REPLAY_LABEL = '回放·不计入实盘样本'
const DISPLAY_SCHEMA = 'quant.etf-effectiveness-display.v2'
const RECENT_LIMITATIONS = ['failed_or_missing_dates_not_included', 'run_rule_and_pool_attribution_unverified', 'multiple_attempts_per_date_combined']

function validRecent (recent) {
  if (!object(recent) || !['empty', 'observed_records', 'unavailable'].includes(recent.status)) return false
  if (recent.status === 'unavailable') return true
  if (!count(recent.observed_zero_trigger_dates, 100000) || !count(recent.scanned_dates, 100000) || recent.observed_zero_trigger_dates > recent.scanned_dates) return false
  if (typeof recent.truncated !== 'boolean' || typeof recent.includes_partial_runs !== 'boolean' || recent.continuity_verified !== false) return false
  if (recent.rule_scope !== 'mixed_or_unknown' || recent.pool_scope !== 'observed_records_only') return false
  if (!Array.isArray(recent.limitations) || !RECENT_LIMITATIONS.every(value => recent.limitations.includes(value))) return false
  if (!validDate(recent.latest_observed_date) || !validDate(recent.last_observed_trigger_date)) return false
  if (recent.status === 'empty') {
    return recent.scanned_dates === 0 && recent.observed_zero_trigger_dates === 0 && recent.latest_observed_date === null &&
      recent.latest_observed_trigger_count === null && recent.last_observed_trigger_date === null && !recent.truncated && !recent.includes_partial_runs
  }
  if (recent.scanned_dates === 0 || recent.latest_observed_date === null || !count(recent.latest_observed_trigger_count, 1000000)) return false
  if (recent.last_observed_trigger_date !== null && recent.last_observed_trigger_date > recent.latest_observed_date) return false
  if ((recent.last_observed_trigger_date === null) !== (recent.observed_zero_trigger_dates === recent.scanned_dates)) return false
  if (recent.truncated && (recent.last_observed_trigger_date !== null || recent.observed_zero_trigger_dates !== recent.scanned_dates)) return false
  if (recent.observed_zero_trigger_dates === 0 && recent.last_observed_trigger_date !== recent.latest_observed_date) return false
  if (recent.observed_zero_trigger_dates > 0 && recent.last_observed_trigger_date === recent.latest_observed_date) return false
  return (recent.observed_zero_trigger_dates > 0) === (recent.latest_observed_trigger_count === 0)
}

const TRIGGER_REASONS = { macro_risk_blocked: '宏观风险拦截' }
const TRIGGER_LIMIT = 10
const hasUnsafeLabelChar = (value) => {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i)
    if (code < 0x20 || code === 0x3c || code === 0x3e) return true
  }
  return false
}
const safeLabel = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max && !hasUnsafeLabelChar(value)

// Optional display-only list; any malformed entry drops the whole list (fall back to unnamed text).
function namedTriggersText (recent) {
  const list = recent.latest_observed_triggers
  if (recent.last_observed_trigger_date === null || !Array.isArray(list) || list.length === 0 || list.length > TRIGGER_LIMIT) return ''
  if (!list.every(item => object(item) && safeLabel(item.code, 16) && /^[A-Za-z0-9.]+$/.test(item.code) &&
    (item.name == null || safeLabel(item.name, 64)))) return ''
  const reasons = list.map(item => (item.decision_state === 'blocked' && TRIGGER_REASONS[item.reason_code]) || '状态未核实')
  const names = list.map(item => item.name ? `${item.code} ${item.name}` : item.code)
  const more = recent.latest_observed_triggers_truncated === true ? '等' : ''
  const tail = '，不计正式样本'
  if (reasons.every(reason => reason === reasons[0])) {
    const why = reasons[0] === '状态未核实' ? '放行状态未核实' : `因${reasons[0]}`
    return `${names.join('、')}${more}，${list.length > 1 ? '均' : ''}${why}${tail}`
  }
  return `${names.map((label, i) => `${label}（${reasons[i]}）`).join('、')}${more}${tail}`
}

/**
 * Display-only companion of the strict effectiveness core.
 * Observed record dates are not a verified complete trading calendar or current-rule
 * streak. Static replay assertions are not verified source/input or profit evidence.
 * This view never changes the strict sample count or promotes replay into samples.
 */
export function effectivenessDisplayView (display, core) {
  const view = { recentText: '', replayText: '' }
  if (!object(display) || display.schema_version !== DISPLAY_SCHEMA) return view
  const strict = display.strict
  const recent = display.recent
  if (validCore(core) && object(strict) && strict.sample_size === core.sample_size && strict.minimum_samples === core.minimum_samples) {
    const base = `已有 ${strict.sample_size}/${strict.minimum_samples}`
    if (!validRecent(recent)) {
      view.recentText = `${base}，近期触发证据不完整或不一致`
    } else if (recent.status === 'unavailable') {
      view.recentText = `${base}，近期触发证据不可用`
    } else if (recent.status === 'empty') {
      view.recentText = `${base}，暂无可用触发记录（不能据此判断最近交易日有无触发）`
    } else {
      const scope = `非连续交易日${recent.includes_partial_runs ? '；含部分批次' : ''}；规则与标的池归属未核齐`
      const named = namedTriggersText(recent)
      if (recent.observed_zero_trigger_dates > 0) {
        const head = `${base}，最近 ${recent.observed_zero_trigger_dates}${recent.truncated ? '+' : ''} 个有数据记录日期未触发`
        view.recentText = named
          ? `${head}；上次有触发（${recent.last_observed_trigger_date}）：${named}（${scope}）`
          : `${head}（${scope}）`
      } else {
        view.recentText = named
          ? `${base}，最近有数据记录（${recent.latest_observed_date}）有触发：${named}（${scope}）`
          : `${base}，最近有数据记录（${recent.latest_observed_date}）有触发（${scope}）`
      }
    }
  }
  const replay = display.replay
  if (object(replay) && replay.schema_version === 'quant.etf-replay-material-assessment.v1' && replay.label === REPLAY_LABEL &&
      replay.included_in_live_samples === false && replay.included_in_formal_samples === false && replay.metrics_available === false &&
      replay.evidence_status === 'unverified' && replay.source_identity_verified === false) {
    view.replayText = '回放材料未核验：来源与输入身份未确认，不计入正式或实盘样本，不构成盈利证明。'
  }
  return view
}
