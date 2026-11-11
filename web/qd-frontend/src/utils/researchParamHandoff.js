// V1 gap A / gap B fixes (contract §1.4, §6.1, decision D4). Pure helpers, no Vue dependency.
// Only affects FUTURE operations: nothing here reads or rewrites stored history.

const has = (obj, key) => !!obj && Object.prototype.hasOwnProperty.call(obj, key)

// 0 / false / '' are real values (A4): never fall back with `||`.
export function paramValuesEqual (a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Number.isNaN(a) ? Number.isNaN(b) : a === b
  return JSON.stringify(a) === JSON.stringify(b)
}

// Gap A: the indicator → strategy handoff carries declared ⊕ applied (what the chart shows),
// labelled per parameter. `draft` (unapplied drawer edits) is reported, never carried silently (A3).
export function resolveIndicatorHandoffParams (specs = [], applied = {}, draft = null) {
  const declared = {}
  const effective = {}
  const sourceOf = {}
  for (const spec of specs || []) {
    if (!spec || !spec.name) continue
    const def = has(spec, 'defaultValue') ? spec.defaultValue : spec.default
    declared[spec.name] = def
    if (has(applied, spec.name) && !paramValuesEqual(applied[spec.name], def)) {
      effective[spec.name] = applied[spec.name]
      sourceOf[spec.name] = 'applied'
    } else {
      effective[spec.name] = has(applied, spec.name) ? applied[spec.name] : def
      sourceOf[spec.name] = 'declared'
    }
  }
  const draftDiffs = []
  if (draft && typeof draft === 'object') {
    for (const name of Object.keys(effective)) {
      if (has(draft, name) && !paramValuesEqual(draft[name], effective[name])) {
        draftDiffs.push({ name, applied: effective[name], draft: draft[name] })
      }
    }
  }
  return { declared, effective, sourceOf, draftDiffs, draftDiffers: draftDiffs.length > 0 }
}

export function describeParams (params = {}) {
  return Object.keys(params || {}).map(k => `${k}=${JSON.stringify(params[k])}`).join(', ')
}

// Gap B: the backtest form starts from declared ⊕ saved template_params (B1, B5).
export function mergeDeclaredWithSaved (definitions = [], saved = {}) {
  const out = {}
  const sourceOf = {}
  for (const def of definitions || []) {
    if (!def || !def.name) continue
    if (has(saved, def.name)) {
      out[def.name] = saved[def.name]
      sourceOf[def.name] = 'saved'
    } else {
      out[def.name] = def.default
      sourceOf[def.name] = 'declared'
    }
  }
  return { params: out, sourceOf }
}

// Gap B: only values the user actually changed on the backtest page are submitted as overrides (B2, B3).
export function diffParamOverrides (current = {}, baseline = {}) {
  const out = {}
  for (const name of Object.keys(current || {})) {
    if (!has(baseline, name) || !paramValuesEqual(current[name], baseline[name])) out[name] = current[name]
  }
  return out
}
