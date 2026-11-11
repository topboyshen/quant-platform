// Isolated synthetic UI QA: no account, real data, external requests or production writes.
import assert from 'node:assert/strict'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const fresh = () => ({
  state: 'sample_insufficient', sample_size: 0, minimum_samples: 30,
  primary_horizon_sessions: 20, method: 'live_oos_or_walk_forward_oos',
  metrics_available: false, horizon_samples: { 1: 1, 3: 1, 5: 1, 10: 1, 20: 0 },
  blocked_counterfactual_sample_size: 0,
  progress: {
    schema_version: 'quant.etf-effectiveness-progress.v1', status: 'ok', unit: 'decision_records',
    candidate_records: 5, excluded_records: 4,
    excluded_reasons: { run_not_success: 4, outside_decision_window: 0, non_oos: 0, invalid_record: 0 },
    eligible_records: 1, mature_records: 0, awaiting_outcome_records: 1,
    unfilled_records: 0, invalid_outcome_records: 0,
    awaiting_breakdown: { not_due: 1, due_uncollected: 0, date_unknown: 0 },
    horizon_samples: { 1: 1, 3: 1, 5: 1, 10: 1, 20: 0 },
    latest_evaluation_as_of: '2026-09-15', next_expected_maturity_date: '2026-09-18',
    expected_maturity_is_estimate: true
  }
})
let evidence = fresh()
let failure = false
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const context = await browser.newContext({ viewport: { width: 1540, height: 1150 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
await page.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.includes('radar/market-context')) {
      if (failure) return route.fulfill({ status: 503, json: { code: 0, error: { type: 'radar_unavailable' } } })
      return route.fulfill({ json: { code: 1, data: {
        generated_at: new Date().toISOString(), status: 'ok',
        market: { available: true, as_of: '2026-09-16', factors: [] },
        risk: { available: true, level: 'critical', missing_fields: [] },
        signals: { current: [], recent: [], effectiveness: evidence }
      } } })
    }
    return route.fulfill({ json: { code: 1, data: [] } })
  }
  if (url.hostname !== '127.0.0.1') return route.abort()
  return route.continue()
})
const refresh = async () => {
  await page.evaluate(() => window.__etfQA.root.$children[0].loadAll())
  await page.waitForFunction(() => !window.__etfQA.root.$children[0].loading)
}
const card = page.locator('[data-testid="effectiveness-card"]')
const text = async () => (await card.innerText()).replace(/\s+/g, ' ')
try {
  await page.goto('http://127.0.0.1:5181/tests/browser/etf-pool.html')
  await page.waitForSelector('[data-testid="effectiveness-card"]')
  await page.waitForFunction(() => !window.__etfQA.root.$children[0].loading)
  assert.match(await text(), /ETF信号验证/)
  assert.match(await text(), /0\s*\/\s*30/)
  assert.match(await text(), /观察记录\s*1/)
  assert.match(await text(), /未计入\s*4/)
  assert.equal(await card.locator('details').getAttribute('open'), null)
  await card.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-summary.png'), animations: 'disabled' })
  await card.locator('summary').click()
  assert.match(await text(), /2026-09-18/)
  assert.match(await text(), /2026-09-15/)
  assert.match(await text(), /Profit Radar/)
  assert.match(await text(), /独立/)
  await card.screenshot({ path: process.env.QA_SCREENSHOT, animations: 'disabled' })
  await page.evaluate(() => { window.__etfQA.store.state.app.theme = 'realdark' })
  await card.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-dark.png'), animations: 'disabled' })
  await page.setViewportSize({ width: 390, height: 844 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2))
  await card.screenshot({ path: process.env.QA_SCREENSHOT.replace('.png', '-mobile.png'), animations: 'disabled' })

  // A real maturity increment is shown without changing the declared threshold.
  evidence = fresh()
  evidence.sample_size = 1
  Object.assign(evidence.progress, { mature_records: 1, awaiting_outcome_records: 0,
    awaiting_breakdown: { not_due: 0, due_uncollected: 0, date_unknown: 0 }, next_expected_maturity_date: null })
  evidence.progress.horizon_samples[20] = 1
  evidence.horizon_samples[20] = 1
  await refresh()
  assert.match(await text(), /1\s*\/\s*30/)

  // Mixed-version rollout: valid old core is visible, never invent progress.
  delete evidence.progress
  await refresh()
  assert.match(await text(), /1\s*\/\s*30/)
  assert.match(await text(), /进度明细暂不可用/)
  assert.doesNotMatch(await text(), /2026-09-18|未计入\s*4/)

  // Contradiction invalidates only the new explanation, not the real legacy core.
  evidence = fresh()
  evidence.progress.eligible_records = 99
  await refresh()
  assert.match(await text(), /0\s*\/\s*30/)
  assert.match(await text(), /进度明细/)
  assert.doesNotMatch(await text(), /观察记录\s*99|2026-09-18/)

  // Missing core and request error must not display sample_insufficient/0/30.
  for (const value of [null, {}, { state: 'sample_insufficient', sample_size: null, minimum_samples: 30 }]) {
    evidence = value
    await refresh()
    assert.match(await text(), /证据不可用/)
    assert.doesNotMatch(await text(), /样本不足|0\s*\/\s*30|2026-09-18/)
  }
  evidence = fresh()
  await refresh()
  assert.match(await text(), /观察记录\s*1/)
  failure = true
  await refresh()
  assert.match(await text(), /证据不可用/)
  assert.doesNotMatch(await text(), /样本不足|0\s*\/\s*30|2026-09-18|观察记录\s*1/)
  failure = false
  await refresh()
  assert.match(await text(), /观察记录\s*1/)
  assert.deepEqual(errors, [])
  console.log('PASS: true zero / observation + exclusions / maturity / old API / contradictions / missing core / HTTP failure clearing / recovery / desktop + dark + mobile')
} finally {
  await browser.close()
}
