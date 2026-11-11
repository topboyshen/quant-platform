// No user profile/login: isolated Chrome, all API calls intercepted, external traffic blocked.
// PLAYWRIGHT_MODULE=/absolute/path/to/playwright-core/index.mjs CHROME_PATH=... node tests/browser/etf-pool.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const pool = JSON.parse(fs.readFileSync(process.env.POOL_FIXTURE, 'utf8'))
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const context = await browser.newContext({ viewport: { width: 1500, height: 1100 } })
const page = await context.newPage()
const errors = []; page.on('pageerror', error => errors.push(error.message))
let fail = false; let requests = 0
await page.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.pathname.startsWith('/api/')) {
    let data = { code: 1, data: [] }
    if (url.pathname.includes('radar/market-context')) {
      requests++
      if (fail) return route.fulfill({ status: 503, json: { code: 0, error: { type: 'radar_unavailable' } } })
      data = { code: 1, data: { generated_at: new Date().toISOString(), status: 'degraded',
        research_pool: pool, market: { available: true, as_of: pool.decision_date, expected_as_of: pool.expected_date, factors: [] },
        risk: { available: true, level: 'critical', missing_fields: [] },
        signals: { current: [], recent: [{ code: '512000', name: '券商ETF', decision_date: '2026-08-27', signal_type: 'volume_breakout_ma60' }] } } }
    }
    return route.fulfill({ json: data })
  }
  if (url.hostname !== '127.0.0.1') return route.abort()
  return route.continue()
})
try {
  await page.goto('http://127.0.0.1:5181/tests/browser/etf-pool.html')
  await page.waitForSelector('[data-testid="etf-universe"] .ant-table-row')
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-table-row').count(), 10)
  assert.match(await page.locator('[data-testid="etf-pool-summary"]').innerText(), /未触发 10/)
  assert.equal(requests, 1)
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-tag-has-color').count(), 0)
  await page.getByPlaceholder('搜索代码、名称、类别').fill('513180')
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-table-row').count(), 1)
  assert.match(await page.locator('[data-testid="etf-universe"]').innerText(), /0\.547/)
  await page.locator('[data-testid="etf-universe"] button').click()
  let selection = await page.evaluate(() => JSON.parse(sessionStorage.getItem('qd:decision-context')).context)
  assert.equal(selection.symbol, '513180'); assert.equal(selection.radar.signal.type, '')
  await page.getByPlaceholder('搜索代码、名称、类别').fill('')
  await page.getByRole('tab', { name: /历史信号/ }).click()
  await page.getByRole('button', { name: '研究历史' }).click()
  selection = await page.evaluate(() => JSON.parse(sessionStorage.getItem('qd:decision-context')).context)
  assert.equal(selection.evidenceState, 'historical'); assert.equal(selection.radar.risk.available, null)
  await page.getByRole('tab', { name: /完整研究池/ }).click()
  await page.locator('[data-testid="etf-universe"] .ant-table-row').first().locator('button').click()
  await page.getByRole('button', { name: /继续到指标研究/ }).click()
  assert.equal(await page.evaluate(() => window.__etfQA.router.currentRoute.path), '/indicator-ide')
  await page.waitForTimeout(3500) // Let selection toast messages finish before the visual evidence.
  await page.screenshot({ path: process.env.QA_SCREENSHOT, fullPage: true })
  await page.evaluate(() => { window.__etfQA.store.state.app.theme = 'realdark' })
  await page.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-dark.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-table-row').count(), 10)
  await page.evaluate(async () => { const vm = window.__etfQA.root.$children[0]; await Promise.all([vm.loadAll(), vm.loadAll()]) })
  assert.equal(requests, 2)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2))
  await page.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-mobile.png'), fullPage: true })
  fail = true
  await page.evaluate(() => window.__etfQA.root.$children[0].loadAll())
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-table-row').count(), 0)
  assert.equal(await page.evaluate(() => sessionStorage.getItem('qd:decision-context')), null)
  fail = false
  await page.evaluate(() => window.__etfQA.root.$children[0].loadAll())
  assert.equal(await page.locator('[data-testid="etf-universe"] .ant-table-row').count(), 10)
  assert.deepEqual(errors, [])
  console.log('UI PASS: 10 rows, filters, 3-decimal price, no-signal selection, historical isolation, navigation, mobile, coalescing, failure/recovery')
} catch (error) {
  console.error('page errors:', errors)
  console.error((await page.locator('body').innerText()).slice(0, 2000))
  throw error
} finally { await browser.close() }
