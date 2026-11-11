const clean = value => String(value || '').trim()

export function sameCopilotTarget (left, right) {
  return Boolean(
    left && right &&
    clean(left.market) === clean(right.market) &&
    clean(left.symbol).toUpperCase() === clean(right.symbol).toUpperCase()
  )
}

export function informativeCopilotTargetName (value, symbol) {
  const name = clean(value)
  return name && name.toUpperCase() !== clean(symbol).toUpperCase() ? name : ''
}

export function resolveCopilotTargetName (target, { current = null, watchlist = [], canonical = null } = {}) {
  if (!target || !clean(target.symbol)) return ''
  const incoming = informativeCopilotTargetName(target.name, target.symbol)
  if (incoming) return incoming

  const watched = (Array.isArray(watchlist) ? watchlist : [])
    .find(item => sameCopilotTarget(item, target))
  const candidates = [
    sameCopilotTarget(current, target) ? current && current.name : '',
    watched && watched.name,
    sameCopilotTarget(canonical, target) ? canonical && canonical.name : ''
  ]
  return candidates
    .map(value => informativeCopilotTargetName(value, target.symbol))
    .find(Boolean) || clean(target.symbol).toUpperCase()
}

export default {
  informativeName: informativeCopilotTargetName,
  resolveName: resolveCopilotTargetName,
  sameTarget: sameCopilotTarget
}
