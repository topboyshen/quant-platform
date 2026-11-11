// A browser-session consistency check, not a durable indicator version or
// security signature. The target must still verify the same user's indicator.
export const INDICATOR_HANDOFF_CONTRACT = 'indicator-temp-v1'
export const AI_REFERENCE_STORAGE_KEY = 'qd_research_ai_reference_v1'
const AI_REFERENCE_MAX_AGE_MS = 30 * 60 * 1000

export function isAIResearchEntry (query = {}) {
  return String(query.link || '') === 'ai'
}

export function buildAIBacktestQuery ({ market, symbol, handoff, linked } = {}) {
  const query = { source_selection: 'required' }
  if (market) query.market = String(market)
  if (symbol) query.symbol = String(symbol)
  if (handoff) query.handoff = String(handoff)
  if (linked) query.link = 'ai'
  return query
}

// The personal AI workflow does not guarantee a persistent message ID. Store
// only a local label and provenance timestamps, never the report body or code.
export function writeTemporaryAIReference (storage, reference) {
  if (!storage || !reference || !reference.market || !reference.symbol ||
    !reference.localRef || !reference.selectedAt) return false
  try {
    const value = JSON.stringify({
      market: String(reference.market),
      symbol: String(reference.symbol),
      localRef: String(reference.localRef),
      generatedAt: String(reference.generatedAt || ''),
      selectedAt: String(reference.selectedAt)
    })
    storage.setItem(AI_REFERENCE_STORAGE_KEY, value)
    if (storage.getItem(AI_REFERENCE_STORAGE_KEY) !== value) throw new Error('ai_reference_readback_mismatch')
    return true
  } catch (_) {
    try { storage.removeItem(AI_REFERENCE_STORAGE_KEY) } catch (_) {}
    return false
  }
}

export function readTemporaryAIReference (storage, target, now = Date.now()) {
  if (!storage || !target || !target.market || !target.symbol) return null
  try {
    const parsed = JSON.parse(storage.getItem(AI_REFERENCE_STORAGE_KEY) || 'null')
    const selectedAt = parsed && Date.parse(parsed.selectedAt)
    if (!parsed || parsed.market !== String(target.market) || parsed.symbol !== String(target.symbol) ||
      !parsed.localRef || !Number.isFinite(selectedAt) || now < selectedAt ||
      now - selectedAt > AI_REFERENCE_MAX_AGE_MS) return null
    return parsed
  } catch (_) {
    return null
  }
}

export function temporaryCodeDigest (value) {
  const code = String(value == null ? '' : value)
  let hash = 2166136261
  for (let index = 0; index < code.length; index += 1) {
    hash ^= code.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `fnv1a-32:${(hash >>> 0).toString(16).padStart(8, '0')}:${code.length}`
}

export function writeTemporaryIndicatorHandoff (storage, key, payload) {
  if (!storage || !key || !/^qd_indicator_to_strategy_[a-zA-Z0-9_-]+$/.test(key)) return false
  if (!payload || payload.handoff_contract !== INDICATOR_HANDOFF_CONTRACT ||
    !String(payload.code || '').trim() || payload.code_digest !== temporaryCodeDigest(payload.code)) return false

  try {
    const serialized = JSON.stringify(payload)
    if (storage.getItem(key) != null) return false
    storage.setItem(key, serialized)
    // Some storage shims silently fail or truncate. Do not navigate until the
    // exact payload can be read back from the same browser session.
    if (storage.getItem(key) !== serialized) throw new Error('handoff_readback_mismatch')
    return true
  } catch (_) {
    try { storage.removeItem(key) } catch (_) {}
    return false
  }
}
