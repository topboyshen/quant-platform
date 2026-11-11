import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const modulesPath = fileURLToPath(new URL('../../src/utils/marketModules.js', import.meta.url))
const analysisPath = fileURLToPath(new URL('../../src/views/ai-analysis/index.vue', import.meta.url))
const copilotPath = fileURLToPath(new URL('../../src/views/ai-analysis/components/CopilotWorkbench.vue', import.meta.url))
const modulesSource = fs.readFileSync(modulesPath, 'utf8')
const analysisSource = fs.readFileSync(analysisPath, 'utf8')
const copilotSource = fs.readFileSync(copilotPath, 'utf8')

test('market modules fall back to the server-controlled market catalog', () => {
  assert.match(modulesSource, /import \{ getMarketTypes \} from '@\/api\/market'/)
  assert.match(modulesSource, /const res = await getMarketTypes\(\)/)
  assert.match(modulesSource, /marketTypesAsResearchModules\(res\.data\)/)
  assert.match(modulesSource, /features: \['research'\]/)
})

test('A-share symbols remain explicitly scoped to CNStock', () => {
  assert.match(analysisSource, /\/\^\\d\{6\}\$\/\.test\(raw\)/)
  assert.match(analysisSource, /suggestedMarket: hasCN \? 'CNStock' : ''/)
  assert.match(analysisSource, /if \(market === 'CNStock'\) return \/\^\\d\{6\}\$\/\.test\(symbol\)/)
})

test('A-share watch cards expose Tencent valuation context without changing other markets', () => {
  assert.match(copilotSource, /v-if="watchValuationText\(item\)"/)
  assert.match(copilotSource, /if \(!item \|\| item\.market !== 'CNStock'\) return ''/)
  assert.match(copilotSource, /\['peTtm', 'pe_ttm'\]/)
  assert.match(copilotSource, /\['totalMarketCapYi', 'market_cap_yi'\]/)
  assert.match(copilotSource, /\['turnoverRate', 'turnover_rate'\]/)
})

test('A-share watchlist search and rendering do not silently truncate valid equities', () => {
  assert.match(copilotSource, /v-for="item in watchlist"/)
  assert.doesNotMatch(copilotSource, /watchlist\.slice\(0, 12\)/)
  assert.match(copilotSource, /searchSymbols\(\{ market, keyword: kw, limit: 50 \}\)/)
  assert.ok(copilotSource.includes("if (market === 'CNStock' && /^\\d{6}$/.test(symbol))"))
  assert.ok(copilotSource.includes("name: item.name || ''"))
})
