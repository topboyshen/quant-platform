import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { normalizeDecisionMarket } from '../../src/utils/decisionContext.js'
import { ETF_STATES, filterEtfPool, singleFlight } from '../../src/utils/etfPool.js'
import { NNG8_READONLY_ENABLED, NNG8_SECTION_ROUTE } from '../../src/utils/nng8Readonly.js'

const source = readFileSync(new URL('../../src/views/personal-home/index.vue', import.meta.url), 'utf8')
const script = source.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)[1]
const marketTemplate = source.slice(source.indexOf('<template #market-pools>'), source.indexOf('<template #audit>'))
const watchlistTemplate = marketTemplate.match(/<aside\b[\s\S]*?<\/aside>/)[0]
const style = source.match(/<style\b[^>]*>([\s\S]*?)<\/style>/)[1]

// Execute the component's real options/methods without Vue, storage or network.
// Structural assertions only cover template wiring and responsive safeguards;
// actual geometry remains the responsibility of the isolated browser check.
function home (bindings = {}, state = {}) {
  const dependencies = {
    mapState: () => ({}),
    WorkbenchOverview: {},
    NNG8_READONLY_ENABLED,
    NNG8_SECTION_ROUTE,
    ETF_STATES,
    filterEtfPool,
    normalizeDecisionMarket,
    singleFlight,
    writeDecisionContext: value => value,
    ...bindings
  }
  const body = script
    .replace(/^import\s[\s\S]*?\sfrom\s+['"][^'"]+['"]\s*$/gm, '')
    .replace(/export\s+default\s+/, 'return ')
  const options = new Function(...Object.keys(dependencies), body)(...Object.values(dependencies))
  const messages = []
  const instance = {
    ...options.data(),
    $message: {
      success: message => messages.push(['success', message]),
      error: message => messages.push(['error', message]),
      warning: message => messages.push(['warning', message])
    },
    ...state
  }
  for (const [name, method] of Object.entries(options.methods)) instance[name] = method.bind(instance)
  for (const [name, getter] of Object.entries(options.computed)) {
    if (!Object.hasOwn(instance, name)) Object.defineProperty(instance, name, { get: () => getter.call(instance) })
  }
  return { instance, options, messages }
}

test('ETF expansion is controlled, duplicate-free, and independently collapsible', () => {
  const previous = Object.freeze({ origin: 'watchlist', market: 'CNStock', symbol: '600519' })
  const forbidden = () => assert.fail('expanding evidence must not read APIs or save a selection')
  const { instance } = home({ getWatchlist: forbidden, getRadarMarketContext: forbidden, writeDecisionContext: forbidden }, { selectedContext: previous })
  const first = Object.freeze({ code: '510300', name: '沪深300ETF' })
  const second = Object.freeze({ code: '159915', name: '创业板ETF' })
  const initial = instance.expandedPoolRows
  instance.onPoolExpand(true, first)
  instance.onPoolExpand(true, { ...first })
  instance.onPoolExpand(true, second)
  assert.deepEqual(instance.expandedPoolRows, ['510300', '159915'])
  assert.deepEqual(initial, [], 'updates must replace the reactive array')
  instance.onPoolExpand(false, { code: first.code })
  assert.deepEqual(instance.expandedPoolRows, ['159915'])
  instance.onPoolExpand(false, first)
  instance.onPoolExpand(false, { code: 'not-expanded' })
  assert.deepEqual(instance.expandedPoolRows, ['159915'])
  instance.onPoolExpand(false, second)
  assert.deepEqual(instance.expandedPoolRows, [])
  instance.onPoolExpand(true, first)
  assert.deepEqual(instance.expandedPoolRows, ['510300'])
  assert.strictEqual(instance.selectedContext, previous)
  assert.deepEqual(home().instance.expandedPoolRows, [], 'instances must not share expanded rows')
})

test('both expansion controls share row keys and retain all diagnostic evidence', () => {
  assert.match(marketTemplate, /:expanded-row-keys="expandedPoolRows"/)
  assert.match(marketTemplate, /@expand="onPoolExpand"/)
  assert.match(marketTemplate, /row-key="code"/)
  assert.match(marketTemplate, /@click="onPoolExpand\(!expandedPoolRows\.includes\(row\.code\), row\)"/)
  assert.match(marketTemplate, /:aria-expanded="expandedPoolRows\.includes\(row\.code\)"/)
  const detail = marketTemplate.match(/<template slot="expandedRowRender"[\s\S]*?<\/template>/)[0]
  for (const field of ['row.decision_date', 'row.diagnostics.label', 'row.diagnostics.daily', 'row.diagnostics.weekly', 'row.diagnostics.details']) {
    assert.ok(detail.includes(field), `${field} remains visible in expanded evidence`)
  }
  assert.match(detail, /历史数值缺失时不事后重算/)
  assert.match(detail, /技术触发与风控放行分开判断/)
  assert.match(marketTemplate, /row\.conditionUncertain \? '分项条件未完成，不能据此判定无信号'/)
})

test('personal list retains A-share-only filtering and original item identities', () => {
  const rows = [
    { symbol: '600519', name: '贵州茅台', market: 'CNStock' },
    { symbol: '000001.SZ', name: '平安银行' },
    { symbol: '430047.BJ', market_category: 'a-share' },
    { symbol: 'AAPL', market: 'USStock' },
    { symbol: '00700', market: 'HKStock' },
    { symbol: '600000', market: 'USStock' },
    { symbol: 'BTCUSDT', market: 'Crypto' }
  ]
  const before = structuredClone(rows)
  const { instance } = home({}, { watchlist: rows })
  assert.deepEqual(instance.aShareWatchlist, rows.slice(0, 3))
  assert.strictEqual(instance.aShareWatchlist[0], rows[0])
  assert.deepEqual(rows, before)
  assert.match(watchlistTemplate, /v-for="row in aShareWatchlist"/)
  assert.match(watchlistTemplate, /watchlist\.length !== aShareWatchlist\.length/)
  assert.match(watchlistTemplate, /仅当前账户的 A 股自选/)
})

test('a successful non-A-share-only watchlist remains a valid empty A-share list', async () => {
  const rows = [{ symbol: 'AAPL', market: 'USStock' }, { symbol: '00700', market: 'HKStock' }]
  const { instance } = home({ getWatchlist: async () => ({ code: 1, data: rows }) })
  await instance.loadWatchlist()
  assert.equal(instance.watchlist.length, 2)
  assert.deepEqual(instance.aShareWatchlist, [])
  assert.equal(instance.watchlistError, '')
  assert.notEqual(instance.watchlist.length, instance.aShareWatchlist.length, 'the cross-market footnote remains applicable')
})

test('ETF query and state filters retain untriggered research candidates', () => {
  const items = [
    { code: '510300', name: '沪深300ETF', sector: '宽基', state: 'no_trigger' },
    { code: '159915', name: '创业板ETF', sector: '成长', state: 'allowed' },
    { code: '510500', name: '中证500ETF', sector: '宽基', state: 'blocked' }
  ]
  const { instance } = home({}, { pool: { items } })
  assert.deepEqual(instance.filteredPool, items)
  instance.poolQuery = '宽基'
  assert.deepEqual(instance.filteredPool, [items[0], items[2]])
  instance.poolStateFilter = 'no_trigger'
  assert.deepEqual(instance.filteredPool, [items[0]])
  instance.poolQuery = '159915'
  assert.deepEqual(instance.filteredPool, [])
  instance.poolStateFilter = 'all'
  assert.deepEqual(instance.filteredPool, [items[1]])
})

test('personal selection keeps market, source, cutoff and evidence time', () => {
  const writes = []
  const context = { generated_at: '2026-09-21T10:00:00+08:00', market: { as_of: '2026-09-18' }, risk: { level: 'normal' } }
  const { instance, messages } = home({ writeDecisionContext: value => { writes.push(value); return value } }, { context })
  const row = { symbol: '600519', name: '合成超长个人自选股票名称', market: 'CNStock' }
  instance.selectWatchlist(row)
  assert.deepEqual(writes, [{
    origin: 'watchlist', market: 'CNStock', symbol: row.symbol, name: row.name,
    risk: context.risk, marketContext: context.market, asOf: '2026-09-18',
    source: 'QuantDinger 个人观察名单', evidenceUpdatedAt: context.generated_at
  }])
  assert.equal(instance.isSelected('watchlist', row), true)
  assert.equal(instance.isSelected('radar', row), false)
  assert.equal(instance.isSelected('watchlist', { ...row, symbol: '000001' }), false)
  assert.equal(instance.isSelected('watchlist', { ...row, market: 'USStock' }), false)
  assert.deepEqual(messages, [['success', '已选中 CNStock:600519']])
  assert.match(watchlistTemplate, /@click="selectWatchlist\(row\)"/)
  assert.match(watchlistTemplate, /:aria-pressed="isSelected\('watchlist', row\)"/)
  assert.match(watchlistTemplate, /'已选中' : '选中研究'/)
  assert.match(watchlistTemplate, /row\.name \|\| row\.symbol/)
})

test('failed selection storage does not pretend a new asset was selected', () => {
  const previous = { origin: 'watchlist', market: 'CNStock', symbol: '600519' }
  const { instance, messages } = home({ writeDecisionContext: () => null }, { selectedContext: previous })
  instance.selectWatchlist({ symbol: '000001', name: '平安银行' })
  assert.strictEqual(instance.selectedContext, previous)
  assert.equal(messages[0][0], 'error')
})

for (const nested of [false, true]) {
  test(`watchlist read preserves the ${nested ? 'nested' : 'array'} API envelope`, async () => {
    const calls = []
    const rows = [{ symbol: '600519', market: 'CNStock' }, { symbol: 'AAPL', market: 'USStock' }, null, {}]
    const { instance } = home({ getWatchlist: async (...args) => { calls.push(args); return { code: 1, data: nested ? { watchlist: rows } : rows } } })
    await instance.loadWatchlist()
    assert.deepEqual(calls, [[]], 'read API remains parameter-free')
    assert.deepEqual(instance.watchlist, rows.slice(0, 2))
    assert.deepEqual(instance.aShareWatchlist, rows.slice(0, 1))
    assert.equal(instance.watchlistError, '')
    assert.equal(instance.watchlistLoading, false)
  })
}

for (const failure of ['transport', 'unsuccessful-envelope', 'malformed-data']) {
  test(`watchlist ${failure} clears stale rows and retains an explicit error`, async () => {
    const { instance } = home({ getWatchlist: async () => {
      if (failure === 'transport') throw new Error('synthetic offline')
      return failure === 'unsuccessful-envelope' ? { code: 0, data: [{ symbol: '600519' }] } : { code: 1, data: {} }
    } }, { watchlist: [{ symbol: '600519' }] })
    await instance.loadWatchlist()
    assert.deepEqual(instance.watchlist, [])
    assert.deepEqual(instance.aShareWatchlist, [])
    assert.match(instance.watchlistError, /加载失败/)
    assert.equal(instance.watchlistLoading, false)
  })
}

test('valid empty, loading and failed watchlists remain distinguishable', async () => {
  let finish
  const { instance } = home({ getWatchlist: () => new Promise(resolve => { finish = resolve }) })
  const pending = instance.loadWatchlist()
  await Promise.resolve()
  assert.equal(instance.watchlistLoading, true)
  finish({ code: 1, data: [] })
  await pending
  assert.deepEqual(instance.aShareWatchlist, [])
  assert.equal(instance.watchlistError, '')
  assert.equal(instance.watchlistLoading, false)
  assert.match(watchlistTemplate, /<a-alert v-if="watchlistError"[^>]*:message="watchlistError"/)
  assert.match(watchlistTemplate, /<a-spin :spinning="watchlistLoading"/)
  assert.match(watchlistTemplate, /v-else-if="!watchlistLoading"/)
  assert.match(watchlistTemplate, /watchlistError \? '观察名单加载失败，请重试' : '个人 A 股观察名单为空'/)
  assert.match(watchlistTemplate, /watchlistError \? '未知' : aShareWatchlist\.length/)
  assert.match(watchlistTemplate, /正在读取个人观察名单/)
})

test('Radar still uses its existing read API and reconciles selection after success', async () => {
  const calls = []
  const evidence = { generated_at: '2026-09-21T10:00:00+08:00' }
  const { instance } = home({ getRadarMarketContext: async (...args) => { calls.push(args); return { code: 1, data: evidence } } })
  const actions = []
  instance.hydrateSelectedWatchlistRadarContext = () => actions.push('hydrate')
  instance.reconcileRadarSelection = () => actions.push('reconcile')
  await instance.loadContext()
  assert.deepEqual(calls, [[]])
  assert.strictEqual(instance.context, evidence)
  assert.deepEqual(actions, ['hydrate', 'reconcile'])
  assert.equal(instance.loading, false)
  assert.match(script, /import \{ getRadarMarketContext \} from '@\/api\/radar'/)
  assert.match(script, /import \{ getWatchlist \} from '@\/api\/market'/)
})

test('current ETF and historical signal actions keep separate context constructors', () => {
  const calls = []
  const current = { origin: 'radar', symbol: '510300', market: 'CNStock' }
  const historical = { ...current, evidenceState: 'historical' }
  const { instance } = home({
    etfResearchContext: (row, context) => { calls.push(['current', row, context]); return current },
    historicalEtfContext: (row, context) => { calls.push(['history', row, context]); return historical }
  })
  const row = { code: '510300', name: '沪深300ETF' }
  instance.selectPool(row)
  assert.strictEqual(instance.selectedContext, current)
  assert.equal(instance.isSelected('radar', row), true)
  instance.selectRadar(row)
  assert.strictEqual(instance.selectedContext, historical)
  assert.equal(instance.isSelected('radar', row), false)
  assert.deepEqual(calls, [['current', row, instance.context], ['history', row, instance.context]])
  assert.match(marketTemplate, /@click="selectPool\(row\)"/)
  assert.match(marketTemplate, /@click="selectRadar\(row\)"/)
  assert.match(marketTemplate, /v-model="poolQuery"/)
  assert.match(marketTemplate, /v-model="poolStateFilter"/)
  assert.match(marketTemplate, /<a-tab-pane key="history"/)
  assert.match(marketTemplate, /不代表当前仍有信号或仍获风控放行/)
})

test('market entry opens the still-mounted selection section before scrolling', () => {
  const calls = []
  const target = { open: false, scrollIntoView: options => { assert.equal(target.open, true); calls.push(options) } }
  const { instance } = home({}, { $refs: { marketSection: target }, $nextTick: callback => callback() })
  instance.focusHomeSection('market')
  assert.equal(target.open, true)
  assert.deepEqual(calls, [{ block: 'start' }])
  assert.match(source, /@click="focusHomeSection\('market'\)"/)
  const opening = marketTemplate.match(/<details\b[^>]*ref="marketSection"[^>]*>/)[0]
  assert.doesNotMatch(opening, /v-if=/)
  assert.match(opening, /data-testid="home-market-section"/)
})

test('responsive layout replaces the fixed 18:6 split without clipping actions', () => {
  assert.doesNotMatch(marketTemplate, /:xl="(?:18|6)"/)
  assert.match(marketTemplate, /data-testid="market-selection-grid"/)
  assert.match(style, /\.selection-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
  assert.match(style, /@container market\s*\(min-width:\s*1200px\)/)
  assert.match(style, /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+320px/)
  assert.match(style, /\.watchlist-item\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto/)
  assert.match(style, /\.watchlist-item \.ant-btn\s*\{[^}]*white-space:\s*nowrap/)
  assert.doesNotMatch(watchlistTemplate, /<a-table\b/)
  assert.match(marketTemplate, /:scroll="\{ x: \d+ \}"/)
  assert.match(style, /\.dark \.watchlist-item\.is-selected/)
})

test('mobile table minimum-width override stays scoped to the market selection grid', () => {
  assert.match(source, /<style\b[^>]*\bscoped\b[^>]*>/)
  assert.match(style, /\.selection-grid\s+::v-deep\s+\.ant-table\s*\{[^}]*min-width:\s*0\s*;/)
  const overrides = [...style.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, selector, declarations]) => /\.ant-table\b/.test(selector) && /min-width:\s*0\s*;/.test(declarations))
  assert.ok(overrides.length > 0)
  for (const [, selectors] of overrides) {
    for (const selector of selectors.split(',')) {
      assert.match(selector.trim(), /^\.selection-grid\s+/, 'do not relax table sizing outside this market section')
    }
  }
})
