const normalizedMarket = value => String(value || '')
  .trim()
  .toLowerCase()
  .replace(/[\s_-]+/g, '')

export function marketPricePrefix (market) {
  const key = normalizedMarket(market)
  if (['cnstock', 'astock', 'ashare', 'a股'].includes(key)) return '¥'
  if (['hkstock', 'hkstocks'].includes(key)) return 'HK$'
  if (['usstock', 'usstocks', 'usequity'].includes(key)) return '$'
  return ''
}

export function prefixMarketPrice (formattedValue, market) {
  const value = String(formattedValue == null ? '' : formattedValue).trim()
  if (!value || value === '--') return '--'
  return `${marketPricePrefix(market)}${value}`
}

export default { marketPricePrefix, prefixMarketPrice }
