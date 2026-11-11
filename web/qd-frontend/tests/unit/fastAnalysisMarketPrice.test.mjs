import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { marketPricePrefix, prefixMarketPrice } from '../../src/utils/marketPrice.js'

const report = fs.readFileSync(
  fileURLToPath(new URL('../../src/views/ai-analysis/components/FastAnalysisReport.vue', import.meta.url)),
  'utf8'
)

test('fast analysis prices use the report market instead of a universal dollar sign', () => {
  assert.equal(marketPricePrefix('CNStock'), '¥')
  assert.equal(marketPricePrefix('a-share'), '¥')
  assert.equal(marketPricePrefix('HKStock'), 'HK$')
  assert.equal(marketPricePrefix('USStock'), '$')
  assert.equal(marketPricePrefix('Crypto'), '')
  assert.equal(prefixMarketPrice('1688.00', 'CNStock'), '¥1688.00')
  assert.equal(prefixMarketPrice('1688.00', 'Crypto'), '1688.00')
  assert.equal(prefixMarketPrice('--', 'CNStock'), '--')
})

test('every report price level goes through the market-aware formatter', () => {
  assert.match(report, /prefixMarketPrice\(this\.formatPrice\(value\), this\.result && this\.result\.market\)/)
  assert.doesNotMatch(report, /\$\{\{\s*formatPrice\(/)
  assert.doesNotMatch(report, /'\$'\s*\+\s*this\.formatPrice\(/)
  for (const key of ['ma5', 'BB_upper', 'pivot', 'swing_high', 'atr', 'current_price']) {
    assert.match(report, new RegExp(`formatMarketPrice\\([^)]*${key}`))
  }
})
