export const CTA_ORDER_V2_ENGINE = 'cta_order_v2'

export const CTA_ORDER_V2_FIXED_CAPABILITY = Object.freeze({
  capabilityVersion: 'cta-research-capability/2.0',
  engine: CTA_ORDER_V2_ENGINE,
  engineVersion: '2.0.0',
  runnable: true,
  mode: 'order_intent',
  researchOnly: true,
  liveExecution: false,
  markets: Object.freeze(['CNStock']),
  frequencies: Object.freeze(['1D']),
  universeMode: 'single_symbol',
  accountModel: 'single_asset_cash_long_v1',
  positionMode: 'long_only',
  maxLeverage: 1,
  decisionTiming: 'bar_close',
  activationTiming: 'next_bar',
  marketFill: 'next_bar_open',
  noLookahead: true,
  orderTypes: Object.freeze(['market', 'limit']),
  timeInForce: Object.freeze(['NEXT_BAR']),
  maxIntentsPerBar: 1,
  sizingUnit: 'base_qty',
  amountAlias: 'base_qty',
  sizingModes: Object.freeze(['base_qty', 'allocation_pct']),
  allocationPctAtFill: true,
  lotSizeRounding: 'floor',
  dynamicSizing: true,
  supportsAdd: true,
  supportsPartialClose: true,
  supportsFullClose: true,
  supportsCancel: false,
  supportsPartialFill: false,
  oversizeClosePolicy: 'reject',
  insufficientCashPolicy: 'reject',
  scriptPriceRole: 'limit_constraint',
  arbitraryFillPrice: false,
  limitFillModel: 'next_bar_ohlc_touch_price_improvement_v1',
  marketSlippageModel: 'adverse_ratio_v1',
  limitPriceImprovement: true,
  limitSlippage: false,
  ledgerVersion: 'research_order_fill_v1',
  authoritativeLedger: Object.freeze(['orders', 'fills', 'account_points']),
  legacyTradesProjection: true,
  sandboxMode: 'in_process_legacy',
  isolated: false,
  reason: ''
})

const CAPABILITY_KEYS = Object.freeze([
  ...Object.keys(CTA_ORDER_V2_FIXED_CAPABILITY),
  'contractHash'
].sort())

function isPlainObject (value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function sameValue (actual, expected) {
  if (Array.isArray(expected)) {
    return Array.isArray(actual) &&
      actual.length === expected.length &&
      actual.every((value, index) => value === expected[index])
  }
  return actual === expected
}

export function isCtaOrderV2Capability (capability, compileContractHash) {
  if (!isPlainObject(capability)) return false

  const keys = Object.keys(capability).sort()
  if (keys.length !== CAPABILITY_KEYS.length || keys.some((key, index) => key !== CAPABILITY_KEYS[index])) {
    return false
  }

  const capabilityHash = typeof capability.contractHash === 'string'
    ? capability.contractHash.trim()
    : ''
  const compiledHash = typeof compileContractHash === 'string'
    ? compileContractHash.trim()
    : ''
  if (!/^[a-f0-9]{64}$/.test(capabilityHash) || compiledHash !== capabilityHash) {
    return false
  }

  return Object.entries(CTA_ORDER_V2_FIXED_CAPABILITY)
    .every(([key, expected]) => sameValue(capability[key], expected))
}
