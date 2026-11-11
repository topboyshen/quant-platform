import assert from 'node:assert/strict'
import test from 'node:test'
import {
  CTA_ORDER_V2_ENGINE,
  CTA_ORDER_V2_FIXED_CAPABILITY,
  isCtaOrderV2Capability
} from '../../src/utils/strategyOrderV2Capability.mjs'

const contractHash = 'a'.repeat(64)

function exactCapability () {
  return {
    ...CTA_ORDER_V2_FIXED_CAPABILITY,
    markets: [...CTA_ORDER_V2_FIXED_CAPABILITY.markets],
    frequencies: [...CTA_ORDER_V2_FIXED_CAPABILITY.frequencies],
    orderTypes: [...CTA_ORDER_V2_FIXED_CAPABILITY.orderTypes],
    timeInForce: [...CTA_ORDER_V2_FIXED_CAPABILITY.timeInForce],
    sizingModes: [...CTA_ORDER_V2_FIXED_CAPABILITY.sizingModes],
    authoritativeLedger: [...CTA_ORDER_V2_FIXED_CAPABILITY.authoritativeLedger],
    contractHash
  }
}

test('order-intent v2 accepts only the frozen capability and matching compile hash', () => {
  const capability = exactCapability()

  assert.equal(CTA_ORDER_V2_ENGINE, 'cta_order_v2')
  assert.equal(isCtaOrderV2Capability(capability, contractHash), true)
  assert.equal(isCtaOrderV2Capability(capability, `b${contractHash.slice(1)}`), false)
  assert.equal(isCtaOrderV2Capability({ ...capability, contractHash: 'not-a-sha256' }, 'not-a-sha256'), false)
})

test('order-intent v2 fails closed on every missing or unknown capability field', () => {
  const capability = exactCapability()

  for (const key of Object.keys(capability)) {
    const missing = { ...capability }
    delete missing[key]
    assert.equal(isCtaOrderV2Capability(missing, contractHash), false, `missing ${key}`)
  }

  assert.equal(isCtaOrderV2Capability({ ...capability, futureFlag: true }, contractHash), false)
})

test('order-intent v2 rejects changes to safety, execution, sizing, ledger, and sandbox semantics', () => {
  const mutations = {
    engine: 'cta_signal_v1',
    researchOnly: false,
    liveExecution: true,
    markets: ['Crypto'],
    frequencies: ['1h'],
    positionMode: 'long_short',
    maxLeverage: 2,
    marketFill: 'same_bar_close',
    noLookahead: false,
    orderTypes: ['market'],
    timeInForce: ['GTC'],
    maxIntentsPerBar: 2,
    allocationPctAtFill: false,
    supportsAdd: false,
    supportsPartialClose: false,
    supportsFullClose: false,
    supportsCancel: true,
    supportsPartialFill: true,
    arbitraryFillPrice: true,
    limitFillModel: 'close_only',
    limitSlippage: true,
    authoritativeLedger: ['trades'],
    sandboxMode: 'isolated',
    isolated: true
  }

  for (const [key, value] of Object.entries(mutations)) {
    assert.equal(
      isCtaOrderV2Capability({ ...exactCapability(), [key]: value }, contractHash),
      false,
      `mutated ${key}`
    )
  }
})
