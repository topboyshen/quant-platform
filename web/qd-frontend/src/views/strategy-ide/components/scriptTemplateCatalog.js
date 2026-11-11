// Script-code template catalog.
//
// Template data is loaded from the backend so template content can be managed in
// the database. This module only keeps code-param parsing and template rendering
// helpers used by the editor.
export const SCRIPT_TEMPLATE_CATALOG = []

export function normalizeScriptTemplate (raw) {
  if (!raw || typeof raw !== 'object') return null
  const key = String(raw.key || raw.template_key || raw.templateKey || '').trim()
  const code = String(raw.code || raw.source || '')
  if (!key || !code.trim()) return null
  const paramSchema = raw.param_schema || raw.paramSchema || {}
  const params = Array.isArray(raw.params)
    ? raw.params
    : (Array.isArray(paramSchema.params) ? paramSchema.params : [])
  const normalizedParams = params
    .filter(item => item && item.name)
    .map(item => ({
      ...item,
      source: item.source || 'template_schema'
    }))
  return {
    key,
    assetType: raw.asset_type === 'portfolio_strategy' ? 'portfolio_strategy' : 'script',
    title: String(raw.title || raw.name || key).trim(),
    desc: String(raw.desc || raw.description || '').trim(),
    code,
    params: normalizedParams,
    icon: raw.icon || (raw.metadata && raw.metadata.icon) || 'appstore',
    accent: raw.accent || (raw.metadata && raw.metadata.accent) || 'blue',
    tags: Array.isArray(raw.tags) ? raw.tags : [],
    titleI18nKey: raw.name_i18n_key || raw.title_i18n_key || '',
    descriptionI18nKey: raw.description_i18n_key || '',
    metadata: raw.metadata || {}
  }
}

export function setScriptTemplateCatalog (items = []) {
  const next = (Array.isArray(items) ? items : [])
    .map(normalizeScriptTemplate)
    .filter(Boolean)
  SCRIPT_TEMPLATE_CATALOG.splice(0, SCRIPT_TEMPLATE_CATALOG.length, ...next)
  return SCRIPT_TEMPLATE_CATALOG
}

function escapeForRegExp (value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function normalizePercentParamValue (raw) {
  const n = Number(raw)
  if (!Number.isFinite(n)) return null
  return n
}

export function percentParamToRatio (value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  return n
}

function parsePythonLiteral (raw) {
  const text = String(raw == null ? '' : raw).trim()
  if (!text) return ''
  if (text === 'True') return true
  if (text === 'False') return false
  if (text === 'None') return null
  const quote = text[0]
  if ((quote === '"' || quote === "'") && text[text.length - 1] === quote) {
    return text.slice(1, -1)
  }
  const n = Number(text)
  return Number.isFinite(n) ? n : text
}

function paramNameParts (name) {
  return String(name || '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)
}

export function isPeriodLikeParamName (name) {
  const key = String(name || '').toLowerCase()
  const parts = paramNameParts(key)
  if (!parts.length) return false
  const periodTokens = ['period', 'lookback', 'window', 'length', 'len', 'bars', 'ema', 'sma', 'rsi', 'adx', 'ma']
  // 以**末位 token** 判定周期：vol_ratio_period 末位是 period → 是周期。
  // 中间出现的 ratio/rate 只描述量比口径，不应把周期参数排除。
  const last = parts[parts.length - 1]
  if (periodTokens.includes(last)) return true
  if (key.endsWith('_atr_period')) return true
  return periodTokens.some(token => key.endsWith(`_${token}`))
}

export function isMultiplierLikeParamName (name) {
  // 倍数：ratio/rate 在本策略族里表示倍数（如量比阈值 1.1、量比周期 20），
  // 而不是 0～1 的比例。周期参数更不是。
  const parts = paramNameParts(name)
  return parts.some(token => ['ratio', 'rate', 'mult', 'multiplier'].includes(token))
}

export function isExplicitPercentName (name) {
  // 显式百分比：pct/percent/allocation/weight/position/take_profit/stop/arm
  const parts = paramNameParts(name)
  return parts.some(token => ['pct', 'percent', 'allocation', 'weight', 'position', 'take_profit', 'stop', 'arm'].includes(token))
}

export function isPercentParamName (name, value) {
  const key = String(name || '').toLowerCase()
  if (isPeriodLikeParamName(key)) return false
  if (isMultiplierLikeParamName(key) && !isExplicitPercentName(key)) return false
  if (/(pct|percent|allocation|weight|position|take_profit|stop|arm)/.test(key)) {
    return typeof value === 'number'
  }
  return false
}

export function inferParamType (name, value) {
  const key = String(name || '').toLowerCase()
  if (typeof value === 'boolean') return 'boolean'
  if (isPercentParamName(name, value)) return 'percent'
  if (Number.isInteger(value)) return 'integer'
  if (typeof value === 'number') return 'number'
  if (key.endsWith('_mode') || key.endsWith('_type')) return 'text'
  return 'text'
}

export function inferParamDefaults (name, value, type) {
  if (type === 'percent') {
    return {
      default: normalizePercentParamValue(value) ?? 0,
      min: 0,
      max: 1,
      step: 0.001
    }
  }
  if (type === 'integer') {
    const lowerName = String(name || '').toLowerCase()
    const positiveWindow = isPeriodLikeParamName(lowerName)
    const zeroAllowed = /(cooldown|delay|wait|pause)/.test(lowerName)
    return {
      default: Number.isFinite(value) ? value : 1,
      min: positiveWindow && !zeroAllowed ? 1 : (zeroAllowed ? 0 : undefined),
      max: positiveWindow && !zeroAllowed ? 500 : undefined,
      step: 1
    }
  }
  if (type === 'number') {
    return {
      default: Number.isFinite(value) ? value : 0,
      step: 0.1
    }
  }
  return { default: value == null ? '' : value }
}

const STRATEGY_ANNOTATION_SCHEMA = {
  entryPct: {
    type: 'percent',
    label: 'Entry size',
    description: 'Fraction of the run-panel investment amount used per entry.',
    min: 0.01,
    max: 1,
    step: 0.01
  },
  stopLossPct: {
    type: 'percent',
    label: 'Stop loss',
    description: 'Engine-managed stop-loss ratio.',
    min: 0,
    max: 1,
    step: 0.001
  },
  takeProfitPct: {
    type: 'percent',
    label: 'Take profit',
    description: 'Engine-managed take-profit ratio.',
    min: 0,
    max: 5,
    step: 0.001
  },
  trailingEnabled: {
    type: 'boolean',
    label: 'Trailing stop enabled',
    description: 'Enables engine-managed trailing stop logic.'
  },
  trailingStopPct: {
    type: 'percent',
    label: 'Trailing distance',
    description: 'Trailing stop distance ratio.',
    min: 0,
    max: 1,
    step: 0.001
  },
  trailingActivationPct: {
    type: 'percent',
    label: 'Trailing activation',
    description: 'Profit ratio required before trailing stop activates.',
    min: 0,
    max: 1,
    step: 0.001
  },
  maxHoldingBars: {
    type: 'integer',
    label: 'Max holding bars',
    description: 'Maximum holding bars before engine-managed exit. 0 disables it.',
    min: 0,
    max: 100000,
    step: 1
  }
}

function parseStrategyAnnotationValue (name, raw) {
  const schema = STRATEGY_ANNOTATION_SCHEMA[name]
  const text = String(raw == null ? '' : raw).trim()
  if (!schema) return text
  if (schema.type === 'boolean') return /^(true|1|yes|on)$/i.test(text)
  const n = Number(text)
  return Number.isFinite(n) ? n : 0
}

export function extractScriptParamsFromCode (code) {
  const source = String(code || '')
  const seen = new Set()
  const params = []
  let match
  const paramPattern = /ctx\.param\(\s*['"]([^'"]+)['"]\s*,\s*((?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|True|False|None|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))(?:\s*,\s*([^)\n]*))?\)/g
  source.split(/\r?\n/).forEach((line) => {
    paramPattern.lastIndex = 0
    while ((match = paramPattern.exec(line)) !== null) {
      const prefix = line.slice(0, match.index).trim()
      if (prefix.startsWith('#')) continue
      const name = String(match[1] || '').trim()
      if (!name || seen.has(name)) continue
      if (isRuntimeReservedParam(name)) continue
      seen.add(name)
      const parsed = parsePythonLiteral(match[2])
      const type = inferParamType(name, parsed)
      params.push({
        name,
        source: 'code_param',
        type,
        ...inferParamDefaults(name, parsed, type),
        ...parseParamKeywordOptions(match[3], type)
      })
    }
  })
  const strategyPattern = /^\s*#\s*@strategy\s+([A-Za-z_][\w-]*)\s*:?\s+([^\s#]+).*$/gim
  while ((match = strategyPattern.exec(source)) !== null) {
    const name = String(match[1] || '').trim()
    const schema = STRATEGY_ANNOTATION_SCHEMA[name]
    if (!name || !schema || seen.has(name)) continue
    seen.add(name)
    const parsed = parseStrategyAnnotationValue(name, match[2])
    const defaults = schema.type === 'percent'
      ? { default: normalizePercentParamValue(parsed) ?? 0 }
      : inferParamDefaults(name, parsed, schema.type)
    params.push({
      name,
      source: 'strategy_annotation',
      type: schema.type,
      label: schema.label,
      description: schema.description,
      min: schema.min,
      max: schema.max,
      step: schema.step,
      ...defaults
    })
  }
  if (!params.length) {
    const indicatorParamPattern = /^\s*#\s*@param\s+(\w+)\s+(int|float|bool|str|string)\s+(\S+)\s*(.*)$/gim
    while ((match = indicatorParamPattern.exec(source)) !== null) {
      const name = String(match[1] || '').trim()
      if (!name || seen.has(name)) continue
      seen.add(name)
      const declaredType = String(match[2] || '').toLowerCase()
      const parsed = parsePythonLiteral(match[3])
      const desc = String(match[4] || '')
      const type = declaredType === 'bool'
        ? 'boolean'
        : declaredType === 'int'
          ? 'integer'
          : declaredType === 'float'
            ? (isPercentParamName(name, parsed) ? 'percent' : 'number')
            : 'text'
      const range = parseDeclaredRange(desc, type)
      params.push({
        name,
        type,
        source: 'indicator_param',
        ...inferParamDefaults(name, parsed, type),
        ...range
      })
    }
  }
  if (!params.length) return null
  return {
    key: '__code_params__',
    inferred: true,
    params
  }
}

function parseParamKeywordOptions (raw, type) {
  if (!['integer', 'number', 'percent'].includes(type)) return {}
  const text = String(raw || '')
  const out = {}
  ;['min', 'max', 'step'].forEach(key => {
    const match = text.match(new RegExp(`(?:^|,)\\s*${key}\\s*=\\s*(-?\\d+(?:\\.\\d+)?(?:[eE][-+]?\\d+)?)`, 'i'))
    if (!match) return
    const value = Number(match[1])
    if (Number.isFinite(value)) out[key] = value
  })
  return out
}

function parseDeclaredRange (desc, type) {
  const text = String(desc || '')
  const rangeMatch = text.match(/range\s*=\s*(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)/i)
  if (rangeMatch && ['integer', 'number', 'percent'].includes(type)) {
    const min = Number(rangeMatch[1])
    const max = Number(rangeMatch[2])
    const step = Number(rangeMatch[3])
    if (Number.isFinite(min) && Number.isFinite(max) && Number.isFinite(step) && step > 0) {
      return { min, max, step }
    }
  }
  const valuesMatch = text.match(/values\s*=\s*([^\s]+)/i)
  if (valuesMatch) {
    const values = valuesMatch[1].split(',').map(item => item.trim()).filter(Boolean).map(item => {
      const n = Number(item)
      return Number.isFinite(n) ? n : item
    })
    if (values.length) return { values }
  }
  return {}
}

function isRuntimeReservedParam (name) {
  const key = String(name || '').trim().toLowerCase()
  return key === 'direction' ||
    key === 'trade_direction' ||
    key === 'market_type' ||
    key === 'markettype' ||
    key === 'symbol' ||
    key === 'timeframe' ||
    key === 'tick_interval_sec' ||
    key === 'leverage' ||
    key === 'investment_amount' ||
    key === 'initial_capital' ||
    key === 'base_notional'
}

/**
 * 安全写回 ctx.param(<name>, <default>, …) 的**默认值**（第二个位置参数）。
 *
 * 解析规则：
 * - 从调用点开始逐字符扫描，正确处理单/双引号字符串、转义符、# 注释与括号深度；
 * - 括号深度回到 0 之前遇到的**第一个顶层逗号**即默认值边界；
 * - 默认值必须是**简单字面量**（数字 / 带引号字符串 / True/False/None）；
 *   嵌套括号、复合表达式、跨行 → 拒绝（不猜测、不截断）；
 * - 目标出现多次（重复声明）→ 拒绝。
 *
 * 成功：{ ok:true, source }（仅默认值被替换，其余逐字不变）
 * 拒绝：{ ok:false, reason:'<原因>', source:原文 }
 */
/**
 * 安全写回 ctx.param(<name>, <default>, …) 的默认值（第二个位置参数）。
 *
 * 纯逐字符扫描（不用正则），显式处理：
 * - 单/双引号字符串与转义；
 * - # 注释；
 * - 括号深度（嵌套调用不算本调用结束）。
 *
 * 规则：
 * - 目标调用点必须唯一（出现多次＝重复声明 → 拒绝，避免只改第一处）；
 * - 括号深度回到 0 之前遇到的第一个顶层逗号即默认值边界；
 * - 默认值必须是简单字面量（数字 / 带引号字符串 / True/False/None）；
 *   嵌套括号、复合表达式、跨行 → 拒绝（不猜测、不截断）。
 *
 * 成功：{ ok:true, source }（仅默认值被替换，其余逐字不变）
 * 拒绝：{ ok:false, reason:'<原因>', source:原文 }
 */
export function rewriteCtxParamDefault (source, escapedName, literal) {
  return rewriteCallDefault(source, 'ctx.param(', escapedName, literal)
}

/**
 * 安全写回**函数调用**形式的默认值（第二个位置参数）：ctx.param / params.get / context.params.get。
 *
 * 纯逐字符扫描（不用正则），显式处理引号/转义/注释/括号深度；
 * 目标调用点必须唯一；默认值必须是简单字面量，否则整次拒绝（不猜测、不截断）。
 * 注意：context.params.get 内含 params.get 子串，故按 marker 扫描时需排除已被更具体 marker 覆盖的匹配。
 */
export function rewriteCallDefault (source, marker, escapedName, literal) {
  const src = String(source || '')
  const found = findCallDefault(src, marker, escapedName)
  if (!found.ok) return { ok: false, reason: found.reason, source: src }
  if (found.broken.length) {
    // “找到了但边界不可靠”≠“没找到”：必须整次拒绝，不得跳过后去改别的同名调用
    return { ok: false, reason: found.broken[0].reason, source: src }
  }
  const { matches } = found
  if (!matches.length) return { ok: false, reason: `未找到该参数的 ${marker.slice(0, -1)} 调用`, source: src }
  if (matches.length > 1) return { ok: false, reason: '同一参数多次声明，拒绝自动写回', source: src }
  const { span } = matches[0]
  const defaultStart = span[0]
  const defaultEnd = span[1]
  if (defaultEnd <= defaultStart) return { ok: false, reason: '默认值为空', source: src }
  // 只替换 token 本身，保留其周围空白（跨行调用在 token 明确时也可安全写回）
  let tokenStart = defaultStart
  let tokenEnd = defaultEnd
  while (tokenStart < tokenEnd && /\s/.test(src[tokenStart])) tokenStart += 1
  while (tokenEnd > tokenStart && /\s/.test(src[tokenEnd - 1])) tokenEnd -= 1
  const defaultText = src.slice(tokenStart, tokenEnd)
  if (!defaultText) return { ok: false, reason: '默认值为空', source: src }
  if (!isSimpleLiteral(defaultText)) {
    return { ok: false, reason: '默认值不是简单字面量（嵌套/复合），拒绝自动写回', source: src }
  }
  return { ok: true, source: src.slice(0, tokenStart) + literal + src.slice(tokenEnd) }
}

/** 读取函数调用形式当前声明的默认值文本（供“真实变化”判定）；找不到返回 ok:false。 */
export function readCallDefault (source, marker, escapedName) {
  const found = findCallDefault(String(source || ''), marker, escapedName)
  if (!found.ok || found.broken.length || found.matches.length !== 1) {
    return { ok: false, text: null, reason: found.broken.length ? found.broken[0].reason : 'not_found' }
  }
  const { span } = found.matches[0]
  return { ok: true, text: found.src.slice(span[0], span[1]).trim(), reason: '' }
}

/** 构建词法掩码：mask[i] 为 true 表示该字符位于字符串字面量或注释内（不得作为代码定位）。 */
export function buildLexicalMask (src) {
  const text = String(src || '')
  const mask = new Array(text.length).fill(false)
  let i = 0
  while (i < text.length) {
    const ch = text[i]
    if (ch === '#') {
      while (i < text.length && text[i] !== '\n') { mask[i] = true; i += 1 }
      continue
    }
    if (ch === '"' || ch === "'") {
      const triple = text.slice(i, i + 3) === ch + ch + ch
      const width = triple ? 3 : 1
      const closer = triple ? ch + ch + ch : ch
      for (let k = 0; k < width && i + k < text.length; k += 1) mask[i + k] = true
      i += width
      while (i < text.length) {
        if (text[i] === '\\') {
          mask[i] = true
          if (i + 1 < text.length) mask[i + 1] = true
          i += 2
          continue
        }
        if (text.slice(i, i + closer.length) === closer) {
          for (let k = 0; k < closer.length && i + k < text.length; k += 1) mask[i + k] = true
          i += closer.length
          break
        }
        mask[i] = true
        i += 1
      }
      continue
    }
    i += 1
  }
  return mask
}

function findCallDefault (src, marker, escapedName) {
  // 定位阶段三件事：① 排除字符串/注释内的文本；② 要求完整对象名（otherctx.param / xxx.params.get 不算）；
  // ③ 区分“没找到”与“找到了但边界不可靠”（后者必须整次拒绝，不得跳过或改写别处）。
  const mask = buildLexicalMask(src)
  const starts = []
  let cursor = 0
  while (true) {
    const at = src.indexOf(marker, cursor)
    if (at < 0) break
    cursor = at + marker.length
    if (mask[at]) continue
    const prev = at > 0 ? src[at - 1] : ''
    if (/[A-Za-z0-9_.]/.test(prev)) continue
    starts.push(at)
  }

  const matches = []
  const broken = []
  const label = marker.slice(0, -1)
  for (const at of starts) {
    let i = at + marker.length
    while (i < src.length && /\s/.test(src[i])) i += 1
    if (src[i] !== "'" && src[i] !== '"') {
      broken.push({ at, reason: '参数名不是字符串字面量，无法确认该调用是否属于本参数' })
      continue
    }
    const quote = src[i]
    i += 1
    let name = ''
    while (i < src.length && src[i] !== quote) {
      if (src[i] === '\\') { name += src[i] + (src[i + 1] || ''); i += 2; continue }
      name += src[i]; i += 1
    }
    if (i >= src.length) {
      broken.push({ at, reason: '参数名字符串未闭合' })
      continue
    }
    i += 1
    while (i < src.length && /\s/.test(src[i])) i += 1
    if (src[i] !== ',') {
      if (name === escapedName) broken.push({ at, reason: `${label}('${name}') 没有默认值参数，无法写回` })
      continue
    }
    if (name !== escapedName) continue
    i += 1
    while (i < src.length && /\s/.test(src[i])) i += 1
    const call = scanCall(src, i)
    if (!call) {
      broken.push({ at, reason: `${label}('${name}') 调用未闭合，边界不可靠` })
      continue
    }
    if (!call.args.length) {
      broken.push({ at, reason: `${label}('${name}') 没有默认值参数，无法写回` })
      continue
    }
    matches.push({ at, span: call.args[0] })
  }
  return { ok: true, src, matches, broken }
}

function scanCall (src, start) {
  let depth = 0
  let i = start
  let quote = null
  let escaped = false
  const args = []
  let partStart = i
  let partOpen = false
  while (i < src.length) {
    const ch = src[i]
    if (quote) {
      if (escaped) { escaped = false } else if (ch === '\\') { escaped = true } else if (ch === quote) { quote = null }
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === '#') {
      while (i < src.length && src[i] !== '\n') i += 1
    } else if (ch === '(') {
      depth += 1
    } else if (ch === ')') {
      if (depth === 0) {
        if (partOpen) args.push([partStart, i])
        return { end: i, args }
      }
      depth -= 1
    } else if (ch === ',' && depth === 0) {
      if (partOpen) args.push([partStart, i])
      partOpen = false
      partStart = i + 1
    }
    if (!/\s/.test(ch) && !partOpen) { partStart = i; partOpen = true }
    i += 1
  }
  return null
}

function isSimpleLiteral (text) {
  return /^(\d+(?:\.\d+)?|-\d+(?:\.\d+)?|'[^'\\]*'|"[^"\\]*"|True|False|None)$/.test(text)
}

/**
 * 判断该参数在源码中**是否真的变化**（用于“只改已编辑参数”）。
 * 0 / false / 空字符串都是真实值，不得当成缺失。
 */
export function paramValueChanged (existingText, newValue) {
  if (newValue === undefined || newValue === null) return false
  if (existingText === undefined || existingText === null) return true
  const text = String(existingText).trim()
  if (typeof newValue === 'boolean') {
    return !(/^(true|false)$/i.test(text) && (text.toLowerCase() === String(newValue)))
  }
  if (typeof newValue === 'number') {
    const n = Number(text)
    return !(Number.isFinite(n) && n === newValue)
  }
  const unquoted = (text[0] === "'" || text[0] === '"') && text[text.length - 1] === text[0]
    ? text.slice(1, -1)
    : text
  return unquoted !== String(newValue)
}

function toPythonLiteral (value) {
  if (typeof value === 'boolean') {
    return value ? 'True' : 'False'
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : '0'
  }
  if (value === null || value === undefined) {
    return 'None'
  }
  return `'${String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
}

export function getScriptTemplateByKey (key) {
  return SCRIPT_TEMPLATE_CATALOG.find(item => item.key === key) || null
}

export function buildTemplateParamValues (templateOrKey, overrides = {}) {
  const template = typeof templateOrKey === 'string' ? getScriptTemplateByKey(templateOrKey) : templateOrKey
  if (!template) return {}
  return template.params.filter(param => !isRuntimeReservedParam(param.name)).reduce((acc, param) => {
    const raw = Object.prototype.hasOwnProperty.call(overrides, param.name)
      ? overrides[param.name]
      : param.default
    if (param.type === 'percent') {
      acc[param.name] = Number.isFinite(Number(raw)) ? Number(raw) : Number(param.default || 0)
    } else {
      acc[param.name] = raw
    }
    return acc
  }, {})
}

export function buildRuntimeParamValues (params = [], overrides = {}) {
  return (params || []).filter(param => param && param.name && !isRuntimeReservedParam(param.name)).reduce((acc, param) => {
    const raw = Object.prototype.hasOwnProperty.call(overrides, param.name)
      ? overrides[param.name]
      : param.default
    acc[param.name] = param.type === 'percent' ? percentParamToRatio(raw) : raw
    return acc
  }, {})
}

export function buildTemplateCode (templateOrKey, overrides = {}) {
  const template = typeof templateOrKey === 'string' ? getScriptTemplateByKey(templateOrKey) : templateOrKey
  // 失败不返回空串冒充成功：统一返回 { ok, source, reason }，由调用方决定是否提示
  if (!template) return { ok: false, reason: '未找到模板', source: '' }
  const values = buildTemplateParamValues(template, overrides)
  return buildScriptCodeWithParamValues(template.code, template.params, values)
}

/** 读取源码中该参数当前声明的值（用于真实变化判定）：函数调用优先，其次注释声明。 */
export function readParamValueFromSource (source, name) {
  const escaped = escapeForRegExp(name)
  for (const marker of ['context.params.get(', 'ctx.param(', 'params.get(']) {
    const r = readCallDefault(source, marker, escaped)
    if (r.ok) return { ok: true, form: marker.slice(0, -1), text: r.text }
  }
  const src = String(source || '')
  const paramRe = new RegExp(`(^\\s*#\\s*@param\\s+${escaped}\\s+(?:int|float|bool|str|string)\\s+)(\\S+)(.*$)`, 'im')
  const m1 = paramRe.exec(src)
  if (m1) return { ok: true, form: '# @param', text: m1[2] }
  const strategyRe = new RegExp(`(^\\s*#\\s*@strategy\\s+${escaped}\\s*:?\\s+)(\\S+)(.*$)`, 'im')
  const m2 = strategyRe.exec(src)
  if (m2) return { ok: true, form: '# @strategy', text: m2[2] }
  return { ok: false, form: '', text: null }
}

export function buildScriptCodeWithParamValues (code, params = [], overrides = {}) {
  // 整次应用必须“全部成功或完全不变”：
  // original 始终保持原文；所有修改只作用于工作副本，且仅在全部成功后才返回。
  const original = String(code || '')
  let source = original
  for (const param of params || []) {
    if (!param || !param.name) continue
    // 未提供覆盖值 → 视为未编辑：逐字保留（0 / false / '' 都是真实值，不得当成缺失）
    if (!Object.prototype.hasOwnProperty.call(overrides, param.name)) continue
    const stored = overrides[param.name]
    if (stored === undefined || stored === null) continue
    const codeValue = stored
    const literal = toPythonLiteral(codeValue)
    const escaped = escapeForRegExp(param.name)

    // “真实变化”判定：与源码中编辑前的值比较；未变化 → 逐字保留原文（不改数字格式/空白）
    const current = readParamValueFromSource(source, param.name)
    if (current.ok && !paramValueChanged(current.text, codeValue)) continue

    let handled = false
    // 函数调用形式：ctx.param / params.get / context.params.get 各自扫描，只替换第二个位置参数
    for (const marker of ['context.params.get(', 'ctx.param(', 'params.get(']) {
      const result = rewriteCallDefault(source, marker, escaped, literal)
      if (result.ok) {
        source = result.source
        handled = true
      } else if (!/未找到该参数的/.test(result.reason)) {
        // 全或无：任一形式边界不明（多次声明/非简单字面量/跨行）→ 整次拒绝，返回原文
        return { ok: false, reason: `${marker.slice(0, -1)}(${param.name}) ${result.reason}`, source: original }
      }
    }
    // 注释声明形式：维持既有实现（仅替换值 token，值后的内容在 $3 中逐字保留）
    // 注释形式只能表达“单个非空白 token”；空字符串或含空白/# 的文本无法表达 → 整次拒绝，不删 token、不扩展语法
    const commentToken = param.type === 'boolean' ? (codeValue ? 'true' : 'false') : String(codeValue)
    const commentExpressible = /^[^\s#]+$/.test(commentToken)
    const indicatorLiteral = param.type === 'boolean' ? (codeValue ? 'true' : 'false') : String(codeValue)
    const indicatorPattern = new RegExp(`(^\\s*#\\s*@param\\s+${escaped}\\s+(?:int|float|bool|str|string)\\s+)(\\S+)(.*$)`, 'im')
    if (indicatorPattern.test(source)) {
      if (!commentExpressible) {
        return { ok: false, reason: `# @param ${param.name} 无法表达该值（空字符串或含空白/# 的文本），拒绝自动写回`, source: original }
      }
      source = source.replace(indicatorPattern, `$1${indicatorLiteral}$3`)
      handled = true
    }
    const strategyLiteral = param.type === 'boolean' ? (codeValue ? 'true' : 'false') : String(codeValue)
    const strategyPattern = new RegExp(`(^\\s*#\\s*@strategy\\s+${escaped}\\s*:?\\s+)(\\S+)(.*$)`, 'im')
    if (strategyPattern.test(source)) {
      if (!commentExpressible) {
        return { ok: false, reason: `# @strategy ${param.name} 无法表达该值（空字符串或含空白/# 的文本），拒绝自动写回`, source: original }
      }
      source = source.replace(strategyPattern, `$1${strategyLiteral}$3`)
      handled = true
    }
    // 参数在源码中没有任何受支持的声明形式 → 边界不明，整次拒绝
    if (!handled && !current.ok) {
      return { ok: false, reason: `参数 ${param.name} 在源码中没有可识别的声明（ctx.param / params.get / # @param / # @strategy）`, source: original }
    }
  }
  return { ok: true, source }
}
