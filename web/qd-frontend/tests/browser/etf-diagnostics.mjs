// Synthetic data, fresh isolated profile, no real login or external requests.
import assert from 'node:assert/strict'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const day = '2026-09-15'
const binding = { run_id: 'diagnostic-fixture', decision_date: day }
const codes = ['510300', '510500', '159915', '588000', '512480', '512890', '513180', '512000', '512170', '518880']
const names = ['沪深300ETF', '中证500ETF', '创业板ETF', '科创50ETF', '半导体ETF', '红利低波ETF', '恒生科技ETF', '券商ETF', '医疗ETF', '黄金ETF']
const diag = () => ({ status: 'recorded', ...binding,
  data: { ready: true, rows: 280, minimum_rows: 65, data_date: day, required_date: day },
  daily: { evaluated: true, ma_period: 60, previous_close: .55, close: .547, previous_ma: .54, ma: .54,
    previous_at_or_below_ma: false, close_above_ma: true, price_cross: false, volume: 80,
    average_volume20: 100, volume_ratio: .8, volume_threshold: 1.5, volume_ok: false, triggered: false },
  weekly: { evaluated: true, completed_bars: 56, minimum_bars: 35, previous_week_date: '2026-09-04',
    week_date: '2026-09-11', previous_dif: .2, previous_dea: .1, dif: .3, dea: .2,
    previous_at_or_below: false, now_above: true, golden_cross: false, triggered: false } })
const pool = { schema_version: 'quant.etf-pool.v1', configured_count: 10, status: 'ok', run_status: 'success',
  calendar_available: true, expected_date: day, ...binding,
  items: codes.map((code, i) => ({ code, name: names[i], sector: '合成测试', state: 'no_trigger', is_current: true,
    data_status: 'ok', decision_date: day, data_date: day, reference_close: .547, reason_code: 'signal_not_triggered',
    trigger_diagnostics: diag(), history: { status: 'none_in_window', scanned_runs: 23, excluded_stale_count: 0,
      latest_raw_trigger: null } })) }
pool.items[1].trigger_diagnostics = { status: 'not_recorded', ...binding, data: null, daily: null, weekly: null }
pool.items[2].trigger_diagnostics.weekly = { evaluated: false, completed_bars: 34, minimum_bars: 35 }
pool.items[7].history.excluded_stale_count = 1
pool.items[9].history = { status: 'available', scanned_runs: 23, excluded_stale_count: 0,
  latest_raw_trigger: { signal_type: 'weekly_macd_golden_cross', period: 'W', signal_data_date: '2026-08-21',
    first_observed_date: '2026-08-21', target_risk_action: 'allowed', risk_scope: 'target' } }

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const context = await browser.newContext({ viewport: { width: 1540, height: 1150 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let fail = false
await page.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.includes('radar/market-context')) {
      if (fail) return route.fulfill({ status: 503, json: { code: 0, error: { type: 'radar_unavailable' } } })
      return route.fulfill({ json: { code: 1, data: { generated_at: new Date().toISOString(), status: 'ok',
        research_pool: pool, market: { available: true, as_of: day, expected_as_of: day, factors: [] },
        risk: { available: true, level: 'critical', missing_fields: [] }, signals: { current: [], recent: [] } } } })
    }
    return route.fulfill({ json: { code: 1, data: [] } })
  }
  if (url.hostname !== '127.0.0.1') return route.abort()
  return route.continue()
})
try {
  await page.goto('http://127.0.0.1:5181/tests/browser/etf-pool.html')
  const table = page.locator('[data-testid="etf-universe"]')
  await page.waitForSelector('[data-testid="etf-universe"] .ant-table-row')
  assert.equal(await table.locator('.ant-table-row').count(), 10)
  const content = await table.innerText()
  assert.match(content, /昨日已在 MA60 上方/)
  assert.match(content, /量比 0.80×，未达 1.50×/)
  assert.match(content, /本批次未记录逐项原因/)
  assert.match(content, /34\/35 根，尚不能判定/)
  assert.match(content, /2026-08-21 · 周线金叉/)
  assert.match(content, /排除旧行情 1 条/)
  assert.match(content, /分项条件待核实/)
  assert.match(await page.locator('[data-testid="etf-pool-summary"]').innerText(), /待核实 1/)
  await table.locator('.ant-table-row-expand-icon').first().click()
  assert.match(await table.innerText(), /本批次判定依据/)
  assert.match(await table.innerText(), /前20日均量 100/)
  await page.screenshot({ path: process.env.QA_SCREENSHOT, fullPage: true })
  await page.evaluate(() => { window.__etfQA.store.state.app.theme = 'realdark' })
  await page.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-dark.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2))
  await page.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-mobile.png'), fullPage: true })
  fail = true
  await page.evaluate(() => window.__etfQA.root.$children[0].loadAll())
  assert.equal(await table.locator('.ant-table-row').count(), 0)
  assert.doesNotMatch(await table.innerText(), /量比 0.80|2026-08-21/)
  fail = false
  await page.evaluate(() => window.__etfQA.root.$children[0].loadAll())
  assert.equal(await table.locator('.ant-table-row').count(), 10)
  assert.deepEqual(errors, [])
  console.log('PASS: recorded / unrecorded / insufficient weeks / historical / expanded evidence / mobile / failure clearing')
} finally {
  await browser.close()
}
