import { getMarketModules } from '@/api/marketModules'
import { getMarketTypes } from '@/api/market'

export const FALLBACK_MARKET_MODULES = [
  { key: 'Crypto', label: 'Crypto', enabled: true, features: ['research', 'backtest', 'paper', 'live'] },
  { key: 'USStock', label: 'US Stocks', enabled: true, features: ['research', 'backtest', 'paper', 'live'] },
  { key: 'Forex', label: 'Forex', enabled: true, features: ['research', 'backtest', 'paper', 'live'] }
]

export function toMarketOption (module) {
  const key = module && (module.key || module.value)
  return {
    value: key,
    label: module.label || key,
    i18nKey: `dashboard.analysis.market.${key}`,
    module
  }
}

function filterMarketModules (markets, include) {
  return markets
    .filter(market => market && market.enabled !== false)
    .filter(market => {
      if (include.length === 0) return true
      const features = market.features || []
      return include.some(feature => features.includes(feature))
    })
    .map(toMarketOption)
}

function marketTypesAsResearchModules (items) {
  return (items || []).map(item => ({
    ...(typeof item === 'object' ? item : {}),
    key: typeof item === 'object' ? item.value : item,
    label: typeof item === 'object' ? (item.label || item.value) : item,
    enabled: true,
    features: ['research']
  }))
}

export async function loadEnabledMarketOptions (opts = {}) {
  const include = Array.isArray(opts.includeFeatures) ? opts.includeFeatures : []
  const fallback = Array.isArray(opts.fallback) ? opts.fallback : FALLBACK_MARKET_MODULES
  try {
    const res = await getMarketModules()
    if (res && res.code === 1 && res.data && Array.isArray(res.data.markets)) {
      return filterMarketModules(res.data.markets, include)
    }
  } catch (e) {}

  // Older personal backends expose the canonical market list but not the
  // richer module contract.  Use that server-controlled list before static
  // defaults so CNStock remains available without bypassing visibility flags.
  try {
    const res = await getMarketTypes()
    if (res && res.code === 1 && Array.isArray(res.data)) {
      return filterMarketModules(marketTypesAsResearchModules(res.data), include)
    }
  } catch (e) {}

  return filterMarketModules(fallback, include)
}

export function firstMarketValue (options, fallback = 'Crypto') {
  return options && options.length > 0 ? options[0].value : fallback
}
