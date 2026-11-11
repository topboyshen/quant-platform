// Declaration-driven parameters. Never guess types from a value or parameter name.
const own = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k)
const same = (a, b) => typeof a === typeof b && a === b
export const parameterType = spec => {
  const type = String((spec && spec.type) || '').trim().toLowerCase()
  if (!type && spec) {
    // Exact legacy compiler contract: infer only from the immutable declaration
    // default, never from an edited value, a parameter name or the selected row.
    if (typeof spec.default === 'boolean') return 'boolean'
    if (typeof spec.default === 'number') return Number.isInteger(spec.default) ? 'integer' : 'number'
    return 'string'
  }
  if (['int', 'integer'].includes(type)) return 'integer'
  if (['float', 'number', 'double', 'decimal', 'percent'].includes(type)) return 'number'
  if (['bool', 'boolean'].includes(type)) return 'boolean'
  if (['str', 'string', 'text', 'enum', 'select'].includes(type)) return 'string'
  return 'unknown'
}
export const parameterChoices = spec => {
  const values = spec && (spec.choices || spec.enum || spec.values)
  return Array.isArray(values) ? values.map(v => v && typeof v === 'object' && own(v, 'value') ? v.value : v) : []
}
export const parameterUnit = spec => spec && spec.unit != null && spec.unit !== '' ? String(spec.unit) : '未知'
export const parameterValueText = value => value == null ? '未知' : (typeof value === 'string' ? JSON.stringify(value) : String(value))
export const parameterRange = spec => {
  const parts = []
  if (spec.min != null) parts.push('≥ ' + spec.min)
  if (spec.max != null) parts.push('≤ ' + spec.max)
  if (spec.step != null) parts.push('步长 ' + spec.step)
  const choices = parameterChoices(spec)
  if (choices.length) parts.push('可选 ' + choices.map(parameterValueText).join(' / '))
  return parts.join(' · ') || '未声明范围'
}

export function parseDeclaredValue (spec, raw) {
  const type = parameterType(spec)
  let value = raw
  if (type === 'unknown') return { ok: false, reason: `参数 ${spec.name} 的类型未声明或不支持` }
  if (type === 'boolean') {
    if (raw === true || raw === false) value = raw
    else if (raw === 'true' || raw === 'false') value = raw === 'true'
    else return { ok: false, reason: `参数 ${spec.name} 需要布尔值` }
  } else if (type === 'number' || type === 'integer') {
    if (typeof raw === 'boolean' || raw == null || (typeof raw !== 'number' && (typeof raw !== 'string' || raw.trim() === ''))) return { ok: false, reason: `参数 ${spec.name} 需要数字（空白不是零）` }
    value = typeof raw === 'number' ? raw : Number(raw)
    if (!Number.isFinite(value) || (type === 'integer' && !Number.isInteger(value))) return { ok: false, reason: `参数 ${spec.name} 需要${type === 'integer' ? '有限整数' : '有限数字'}` }
  } else if (type === 'string' && typeof raw !== 'string') return { ok: false, reason: `参数 ${spec.name} 需要字符串` }
  const choices = parameterChoices(spec)
  if (choices.length && !choices.some(v => same(v, value))) return { ok: false, reason: `参数 ${spec.name} 不在声明选项中` }
  if (typeof value === 'number') {
    if ((spec.min != null && value < spec.min) || (spec.max != null && value > spec.max)) return { ok: false, reason: `参数 ${spec.name} 超出声明范围` }
    if (spec.step != null) {
      const step = Number(spec.step)
      const quotient = (value - (spec.min == null ? 0 : Number(spec.min))) / step
      if (!Number.isFinite(step) || step <= 0 || Math.abs(quotient - Math.round(quotient)) > 1e-9) return { ok: false, reason: `参数 ${spec.name} 不符合声明步长` }
    }
  }
  return { ok: true, value }
}

export function validateStrategySchema (response, sourceId) {
  if (!response || String(response.sourceId) !== String(sourceId) || !response.codeSha256 || !response.contractHash ||
    !Array.isArray(response.paramSchema) || !response.params || !response.params.effective || typeof response.params.effective !== 'object' || Array.isArray(response.params.effective)) return { ok: false, reason: '策略声明响应缺失或身份不匹配' }
  const names = new Set()
  for (const spec of response.paramSchema) {
    if (!spec || !spec.name || names.has(spec.name)) return { ok: false, reason: '策略声明存在重名参数' }
    names.add(spec.name)
    if (!own(response.params.effective, spec.name)) {
      if (spec.required) return { ok: false, reason: `必填参数 ${spec.name} 缺少有效值` }
      continue
    }
    const parsed = parseDeclaredValue(spec, response.params.effective[spec.name])
    if (!parsed.ok) return parsed
  }
  if (Object.keys(response.params.effective).some(k => !names.has(k))) return { ok: false, reason: '策略有效值含未声明参数' }
  return { ok: true }
}

export const emptyStrategyDraft = () => ({ mappingVersion: 1, handoffArtifactId: null, indicatorPayloadSha256: null, mappings: {}, mappingConfirmed: false, confirmedMappingKey: null, code: null, sourceId: undefined })
export function normalizeStrategyDraft (draft) {
  if (!draft || typeof draft !== 'object') return emptyStrategyDraft()
  if (draft.mappingVersion !== 1 || !draft.mappings || typeof draft.mappings !== 'object' || Array.isArray(draft.mappings)) {
    // Preserve old fields for loss detection and explicit legacy display, never
    // promote their scalar to a verified per-parameter mapping.
    return { ...emptyStrategyDraft(), ...draft, mappings: {}, legacyMapping: true, mappingConfirmed: false, confirmedMappingKey: null }
  }
  return { ...emptyStrategyDraft(), ...draft, mappings: Object.fromEntries(Object.entries(draft.mappings).map(([k, v]) => [k, { ...v }])) }
}
export function indicatorParameterInfo (artifact) {
  const payload = artifact && artifact.payload
  const declared = payload && payload.params && payload.params.specs
  const effective = artifact && artifact.params && artifact.params.effective
  return { artifactId: artifact && artifact.id, payloadSha256: artifact && artifact.payloadSha256, specs: Array.isArray(declared) ? declared : [], effective: effective && typeof effective === 'object' && !Array.isArray(effective) ? effective : {} }
}
export function identityCompatible (target, source) {
  const a = parameterType(target); const b = parameterType(source)
  if (a === 'unknown' || b === 'unknown' || (a !== b && !(['number', 'integer'].includes(a) && ['number', 'integer'].includes(b)))) return false
  return !(target.unit && source.unit && String(target.unit) !== String(source.unit))
}
export function defaultMappings (schema, previous = {}) {
  return Object.fromEntries(schema.paramSchema.map(spec => [spec.name, previous[spec.name] ? { ...previous[spec.name] } : { mode: 'default', sourceParam: null, value: schema.params.effective[spec.name], reason: '' }]))
}
export function mappingIdentity (draft, schema) {
  if (!schema) return null
  return JSON.stringify([draft.sourceId, schema.codeSha256, schema.contractHash, draft.handoffArtifactId, draft.indicatorPayloadSha256, draft.mappings])
}
export function buildParameterMapping (draft, schema, indicator) {
  if (!schema) return { ok: false, reason: '尚未获得策略声明；不能猜测参数' }
  const validity = validateStrategySchema(schema, draft.sourceId)
  if (!validity.ok) return validity
  const source = indicatorParameterInfo(indicator)
  const targets = new Set(schema.paramSchema.map(s => s.name))
  if (Object.keys(draft.mappings || {}).some(k => !targets.has(k))) return { ok: false, reason: '映射包含当前策略未声明的目标，请重新选择' }
  const paramMapping = []; const applied = {}; const effective = { ...schema.params.effective }
  for (const target of schema.paramSchema) {
    const row = (draft.mappings || {})[target.name]
    if (!row || row.mode === 'default') continue
    if (!['identity', 'manual_override'].includes(row.mode)) return { ok: false, reason: `参数 ${target.name} 的转换未登记` }
    if (!source.artifactId || source.artifactId !== draft.handoffArtifactId || !source.payloadSha256 || source.payloadSha256 !== draft.indicatorPayloadSha256) return { ok: false, reason: '来源指标快照已变化或缺少可核验指纹，请重新交接' }
    let value = row.value
    let from = null
    if (row.sourceParam) {
      const spec = source.specs.find(s => s.name === row.sourceParam)
      if (!spec || !own(source.effective, row.sourceParam)) return { ok: false, reason: `来源参数 ${row.sourceParam} 未声明或缺失` }
      from = 'indicator.' + row.sourceParam
      if (row.mode === 'identity') {
        if (!identityCompatible(target, spec)) return { ok: false, reason: `参数 ${target.name} 的类型或单位不兼容，不能同值映射` }
        value = source.effective[row.sourceParam]
      }
    } else if (row.mode === 'identity') return { ok: false, reason: `参数 ${target.name} 尚未明确选择来源参数` }
    if (row.mode === 'manual_override' && (typeof row.reason !== 'string' || !row.reason.trim())) return { ok: false, reason: `参数 ${target.name} 的人工覆盖必须填写原因` }
    const parsed = parseDeclaredValue(target, value)
    if (!parsed.ok) return parsed
    applied[target.name] = parsed.value; effective[target.name] = parsed.value
    const mapping = { from, to: target.name, value: parsed.value, transform: row.mode }
    if (row.mode === 'manual_override') mapping.reason = row.reason.trim()
    paramMapping.push(mapping)
  }
  return { ok: true, applied, paramMapping, effective }
}
