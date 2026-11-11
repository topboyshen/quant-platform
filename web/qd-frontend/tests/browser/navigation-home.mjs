// Full-app QA against a local build, with isolated synthetic sessions only.
// BASE_URL=http://127.0.0.1:18880 QA_DIR=/absolute/qa-output \
// PLAYWRIGHT_MODULE=/absolute/playwright-core/index.mjs CHROME_PATH=/absolute/Chrome \
// node tests/browser/navigation-home.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const base = new URL(process.env.BASE_URL || 'http://127.0.0.1:18880')
assert.equal(base.hostname, '127.0.0.1', 'QA only accepts an explicit loopback server')
assert.equal(base.protocol, 'http:', 'QA uses the local HTTP preview, never production')
assert.equal(base.username + base.password, '', 'Do not put credentials in BASE_URL')
const qaDir = path.resolve(process.env.QA_DIR || 'qa-navigation-home')
await fs.mkdir(qaDir, { recursive: true })

const DAY = '2026-09-18'
const DISC = '合成测试数据；模拟跟踪 ≠ 账户收益'
const binding = { run_id: 'synthetic-navigation-home', decision_date: DAY }
const etfs = [
  ['510300', '沪深300ETF'], ['510500', '中证500ETF'], ['159915', '创业板ETF'], ['588000', '科创50ETF'],
  ['512480', '半导体ETF'], ['512890', '红利低波ETF'], ['513180', '恒生科技ETF'], ['512000', '券商ETF'],
  ['512170', '医疗ETF'], ['518880', '黄金ETF']
]
function diagnostics () {
  return {
    status: 'recorded', ...binding,
    data: { ready: true, rows: 280, minimum_rows: 65, data_date: DAY, required_date: DAY },
    daily: { evaluated: true, ma_period: 60, previous_close: 0.55, close: 0.547, previous_ma: 0.54, ma: 0.54,
      previous_at_or_below_ma: false, close_above_ma: true, price_cross: false, volume: 80,
      average_volume20: 100, volume_ratio: 0.8, volume_threshold: 1.5, volume_ok: false, triggered: false },
    weekly: { evaluated: true, completed_bars: 56, minimum_bars: 35, previous_week_date: '2026-09-04',
      week_date: '2026-09-11', previous_dif: 0.2, previous_dea: 0.1, dif: 0.3, dea: 0.2,
      previous_at_or_below: false, now_above: true, golden_cross: false, triggered: false }
  }
}
function marketPayload () {
  const pool = {
    schema_version: 'quant.etf-pool.v1', configured_count: 10, status: 'ok', run_status: 'success',
    calendar_available: true, expected_date: DAY, ...binding,
    items: etfs.map(([code, name]) => ({
      code, name, sector: '合成验收', state: 'no_trigger', is_current: true, data_status: 'ok',
      decision_date: DAY, data_date: DAY, reference_close: 0.547, reason_code: 'signal_not_triggered',
      trigger_diagnostics: diagnostics(),
      history: { status: 'none_in_window', scanned_runs: 23, excluded_stale_count: 0, latest_raw_trigger: null }
    }))
  }
  pool.items[1].trigger_diagnostics = { status: 'not_recorded', ...binding, data: null, daily: null, weekly: null }
  pool.items[2].trigger_diagnostics.weekly = { evaluated: false, completed_bars: 34, minimum_bars: 35 }
  pool.items[7].history.excluded_stale_count = 1
  pool.items[9].history = { status: 'available', scanned_runs: 23, excluded_stale_count: 0,
    latest_raw_trigger: { signal_type: 'weekly_macd_golden_cross', period: 'W', signal_data_date: '2026-08-21',
      first_observed_date: '2026-08-21', target_risk_action: 'allowed', risk_scope: 'target' } }
  return {
    generated_at: new Date().toISOString(), status: 'ok', source: '合成验收，不是生产行情', research_pool: pool,
    market: { available: true, as_of: DAY, expected_as_of: DAY, factors: [] },
    risk: { available: true, level: 'critical', missing_fields: [], metrics: { vix: 14.81, us_10y: 5 } },
    audit: { available: true, status: 'success', decision_date: DAY, decision_outcome: 'no_signal' },
    signals: {
      current: [], recent: [], effectiveness: {
        state: 'sample_insufficient', sample_size: 1, minimum_samples: 30, primary_horizon_sessions: 20,
        method: 'live_oos_or_walk_forward_oos', metrics_available: false,
        horizon_samples: { 1: 1, 3: 1, 5: 1, 10: 1, 20: 1 }, blocked_counterfactual_sample_size: 0,
        progress: {
          schema_version: 'quant.etf-effectiveness-progress.v1', status: 'ok', unit: 'decision_records',
          candidate_records: 5, excluded_records: 4,
          excluded_reasons: { run_not_success: 4, outside_decision_window: 0, non_oos: 0, invalid_record: 0 },
          eligible_records: 1, mature_records: 1, awaiting_outcome_records: 0, unfilled_records: 0, invalid_outcome_records: 0,
          awaiting_breakdown: { not_due: 0, due_uncollected: 0, date_unknown: 0 },
          horizon_samples: { 1: 1, 3: 1, 5: 1, 10: 1, 20: 1 },
          latest_evaluation_as_of: DAY, next_expected_maturity_date: null, expected_maturity_is_estimate: true
        }
      }
    }
  }
}
function profitFiles () {
  return [
    { file: '026a1083d9e0/latest.md', status: 'ok', conclusion: '合成结论：表现承压，盈利优势未建立',
      generated_at: '2026-09-18T07:35:36Z', market_cutoff_date: null },
    { file: 'ec6c86fd856c/latest.md', status: 'ok', conclusion: null,
      generated_at: '2026-09-18T07:10:37Z', market_cutoff_date: DAY }
  ]
}
function overviewPayload (allUnavailable = false) {
  const items = [
    { id: 'calendar_freshness', label: '交易日历更新', status: 'ok', data_cutoff: '2026-09-15T06:20:00+08:00', source: 'synthetic/calendar.json' },
    { id: 'digest_inputs', label: '研究摘要行情截止', status: 'ok', data_cutoff: DAY, source: 'synthetic/research_digest.json' },
    { id: 'eod_status', label: '收盘任务', status: 'recovered', data_cutoff: DAY, source: 'synthetic/eod.json', detail: '首次失败后重试成功，保留首次失败审计' },
    { id: 'hermes', label: 'Hermes 状态', status: 'ok', data_cutoff: new Date().toISOString(), source: 'synthetic/heartbeat', detail: '进程/心跳已读；Telegram/模型未验证' },
    { id: 'profit_radar', label: 'Profit Radar 报告', status: 'ok', data_cutoff: '2026-09-21T10:00:00+08:00', source: 'synthetic/reports', detail: DISC, value: { files: profitFiles() } },
    { id: 'execution_digest', label: '执行摘要', status: 'unavailable', data_cutoff: null, source: 'synthetic/execution.json', detail: '合成测试：目录权限不足' },
    { id: 'release_version', label: '发布版本', status: 'not_integrated', data_cutoff: null, source: 'synthetic/pin', detail: '合成测试：版本待接入' }
  ]
  return {
    schema_version: 'workbench.overview.v1', generated_at: new Date().toISOString(), partial: true, all_unavailable: allUnavailable,
    items: allUnavailable ? items.map(item => ({ ...item, status: 'unavailable', data_cutoff: null, value: null, detail: '合成测试：全部来源不可用' })) : items
  }
}
function reportsPayload () {
  return {
    reports: [
      { id: 'eod', label: '收盘日报（合成）', format: 'json', status: 'ok', generated_at: '2026-09-18T15:50:02+08:00',
        file_mtime: '2026-09-21T10:00:00+08:00', schema_version: 1, summary: { conclusion: null, market_cutoff_date: null } },
      { id: 'digest', label: '研究摘要（JSON，合成）', format: 'json', status: 'ok', generated_at: '2026-09-21T10:00:01+08:00',
        summary: { conclusion: null, market_cutoff_date: null } },
      { id: 'digest-md', label: '研究摘要（Markdown，合成）', format: 'markdown', status: 'ok', generated_at: null,
        file_mtime: '2026-09-21T10:00:01+08:00', summary: { conclusion: null, market_cutoff_date: null, disclaimer: DISC } },
      { id: 'execution', label: '执行摘要（合成）', format: 'json', status: 'unavailable', generated_at: null, detail: '合成测试：目录权限不足' },
      ...profitFiles().map(file => ({ id: file.file, label: `Profit Radar ${file.file}（合成）`, format: 'markdown', status: file.status,
        generated_at: file.generated_at, file_mtime: '2026-09-21T10:00:00+08:00', summary: { ...file, disclaimer: DISC } }))
    ]
  }
}

const result = { syntheticOnly: true, baseUrl: base.origin, scenarios: [], screenshots: [], sessions: [], status: 'running' }
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true, args: ['--disable-background-networking'] })
let activePage
async function eventually (predicate, description, timeout = 12000) {
  const deadline = Date.now() + timeout
  let lastError
  while (Date.now() < deadline) {
    try { if (await predicate()) return } catch (error) { lastError = error }
    await new Promise(resolve => setTimeout(resolve, 80))
  }
  throw new Error(`${description}${lastError ? `: ${lastError.message}` : ''}`)
}
async function scenario (name, action) {
  await action()
  result.scenarios.push({ name, status: 'passed' })
  console.log(`PASS ${name}`)
}
async function session (role, theme = 'light', timezoneId = 'Asia/Shanghai') {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId, serviceWorkers: 'block' })
  const data = { role, timezoneId, api: [], deniedMutations: [], externalBlocked: [], pageErrors: [] }
  result.sessions.push(data)
  const modes = { overview: 'normal', marketFail: false }
  const user = { id: 900001, username: `synthetic-${role || 'anonymous'}`, nickname: `合成验收 ${role || '未登录'}`, is_demo: false,
    must_change_initial_password: false, role: { id: role || 'default', permissions: ['dashboard'] }, avatar: '' }
  await context.addInitScript(({ role, user, theme }) => {
    // This is a new isolated browser context; no user browser profile is loaded.
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('nav_theme', JSON.stringify(theme))
    localStorage.setItem('app_language', JSON.stringify('zh-CN'))
    localStorage.setItem('lang', JSON.stringify('zh-CN'))
    if (role) {
      localStorage.setItem('Access-Token', JSON.stringify('synthetic-local-qa-not-a-real-token'))
      localStorage.setItem('User-Info', JSON.stringify(user))
      localStorage.setItem('User-Roles', JSON.stringify([{ id: role, permissionList: ['dashboard'] }]))
    }
  }, { role, user, theme })
  if (typeof context.routeWebSocket === 'function') {
    await context.routeWebSocket('**/*', socket => socket.close({ code: 1000, reason: 'Synthetic QA blocks sockets' }))
  } else {
    await context.addInitScript(() => { window.WebSocket = class { constructor () { throw new Error('Synthetic QA blocks sockets') } } })
  }
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.hostname !== '127.0.0.1' || url.origin !== base.origin) {
      data.externalBlocked.push({ origin: url.origin, path: url.pathname })
      return route.abort()
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    data.api.push({ method: request.method(), path: url.pathname })
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      data.deniedMutations.push({ method: request.method(), path: url.pathname })
      return route.fulfill({ status: 405, json: { code: 0, msg: 'Synthetic read-only QA denied a mutation' } })
    }
    const reply = body => route.fulfill({ json: { code: 1, data: body } })
    if (url.pathname === '/api/auth/info') return reply(user)
    if (url.pathname === '/api/radar/market-context') {
      if (modes.marketFail) return route.fulfill({ status: 503, json: { code: 0, msg: 'Synthetic market failure' } })
      return reply(marketPayload())
    }
    if (url.pathname === '/api/radar/workbench/overview') {
      if (modes.overview === 'http503') return route.fulfill({ status: 503, json: { code: 0, msg: 'Synthetic overview failure' } })
      if (modes.overview === 'http503-unavailable') return route.fulfill({ status: 503, json: { code: 1, data: overviewPayload(true) } })
      if (modes.overview === 'failed-envelope-healthy-data') return route.fulfill({ status: 503, json: { code: 0, msg: '工作台读取失败（合成）', data: overviewPayload() } })
      return reply(overviewPayload(modes.overview === 'unavailable'))
    }
    if (url.pathname === '/api/radar/workbench/reports') return reply(reportsPayload())
    if (url.pathname === '/api/settings/brand-config') return reply({
      app_name: 'QuantDinger · 合成验收', logos: {}, social_accounts: [], mobile_app: {},
      contact: { email: '', support_url: '', feature_request_url: '', live_chat_url: '' },
      legal: { user_agreement_url: '', user_agreement_text: '合成测试', privacy_policy_url: '', privacy_policy_text: '合成测试' }
    })
    if (url.pathname === '/api/market/watchlist/get') return reply([
      { symbol: '600875', name: '东方电气（合成）', market: 'CNStock' },
      { symbol: '300476', name: '胜宏科技（合成）', market: 'CNStock' }
    ])
    if (url.pathname === '/api/market-modules') return reply({ markets: [{ key: 'CNStock', label: 'A股', enabled: true, features: ['research', 'backtest'] }] })
    if (url.pathname === '/api/market/types') return reply([{ value: 'CNStock', label: 'A股' }])
    if (url.pathname === '/api/strategies/notifications/unread-count') return reply({ unread: 0 })
    if (url.pathname === '/api/strategies/notifications') return reply({ items: [], total: 0 })
    if (['/api/settings/public-config', '/api/market/config', '/api/users/notification-settings'].includes(url.pathname)) return reply({})
    // Every other API is fulfilled locally. Research source/history lists and
    // chart data are intentionally empty; navigation must not generate work.
    return reply([])
  })
  const page = await context.newPage()
  activePage = page
  page.on('pageerror', error => data.pageErrors.push(error.message))
  page.setDefaultTimeout(12000)
  return { context, page, data, modes, count: apiPath => data.api.filter(item => item.path === apiPath).length }
}
const nav = page => page.locator('nav.workspace-navigation:visible')
async function homeReady (page) {
  await page.locator('.personal-home').waitFor()
  await eventually(async () => !(await page.locator('[data-testid="home-refresh-all"]').getAttribute('class')).includes('ant-btn-loading'), 'home reads did not settle')
}
async function go (page, route) {
  await page.goto(`${base.origin}/#${route}`)
}
async function expandGroup (page, label) {
  const group = nav(page).getByRole('button', { name: label, exact: true })
  if (await group.getAttribute('aria-expanded') !== 'true') await group.click()
}
async function clickNav (page, label) {
  await nav(page).getByRole('link', { name: label, exact: true }).click()
}
async function refreshAll (s) {
  const before = s.count('/api/radar/workbench/overview')
  await s.page.locator('[data-testid="home-refresh-all"]').click()
  await eventually(() => s.count('/api/radar/workbench/overview') > before, 'refresh-all did not request overview')
  await homeReady(s.page)
}
async function noOverflow (page, label) {
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, scroll: document.documentElement.scrollWidth }))
  assert.ok(dimensions.scroll <= dimensions.viewport + 2, `${label}: page overflow ${JSON.stringify(dimensions)}`)
}
async function shot (page, name) {
  await page.screenshot({ path: path.join(qaDir, `${name}.png`), fullPage: true, animations: 'disabled' })
  result.screenshots.push(`${name}.png`)
}
async function setTheme (page, theme) {
  await page.evaluate(value => {
    const vm = document.querySelector('#app').__vue__
    if (!vm || !vm.$store) throw new Error('App store unavailable for isolated theme QA')
    vm.$store.commit('nav_theme', value)
  }, theme)
  await eventually(() => page.locator(`.workspace-layout.${theme}`).count(), 'theme change did not reach layout')
}

try {
  const anonymous = await session(null)
  await scenario('anonymous is redirected to login without protected API reads', async () => {
    await go(anonymous.page, '/personal-home')
    await anonymous.page.waitForURL(/#\/user\/login/)
    assert.equal(await anonymous.page.locator('nav.workspace-navigation').count(), 0)
    assert.equal(anonymous.data.api.filter(row => row.path.startsWith('/api/radar/')).length, 0)
    assert.equal(anonymous.data.deniedMutations.length, 0)
    await shot(anonymous.page, 'anonymous-login')
  })
  await anonymous.context.close()

  const admin = await session('admin')
  const page = admin.page
  await go(page, '/personal-home')
  await homeReady(page)
  await page.locator('[data-testid="home-profit-report"]').first().waitFor()
  await scenario('administrator sees five groups, original research links and gated management', async () => {
    assert.equal(await nav(page).locator(':scope > .workspace-navigation__group').count(), 5)
    for (const label of ['工作台', '市场观察', '研究实验室', '报告中心', '运行与管理']) assert.match(await nav(page).innerText(), new RegExp(label))
    await expandGroup(page, '运行与管理')
    assert.equal(await nav(page).getByRole('link', { name: '系统设置', exact: true }).getAttribute('href'), '#/settings')
    assert.equal(await nav(page).getByRole('link', { name: '研究 Agent', exact: true }).getAttribute('href'), '#/agent-tokens')
    assert.doesNotMatch(await nav(page).innerText(), /券商|资金|订阅|实盘|用户管理/)
    await noOverflow(page, '1440px home')
    await shot(page, 'home-light-1440')
  })
  await scenario('ETF 1/30 and per-file unknown Profit fields are retained', async () => {
    const card = await page.locator('[data-testid="effectiveness-card"]').innerText()
    assert.match(card, /1\s*\/\s*30/)
    assert.match(card, /20个交易日成熟样本/)
    assert.match(card, /观察记录\s*1/)
    assert.match(card, /待完成20日结果\s*0/)
    assert.match(card, /未计入\s*4/)
    const files = page.locator('[data-testid="home-profit-report"]')
    assert.equal(await files.count(), 2)
    assert.match(await files.nth(0).innerText(), /表现承压[\s\S]*2026-09-18 15:35:36[\s\S]*行情截止 未知/)
    assert.match(await files.nth(1).innerText(), /结论未知[\s\S]*2026-09-18 15:10:37[\s\S]*行情截止 2026-09-18/)
    assert.doesNotMatch(await files.nth(1).innerText(), /表现承压/)
    assert.match(await page.locator('.workbench-readline').innerText(), /研究摘要行情截止\s*2026-09-18/)
    assert.doesNotMatch(await page.locator('.workbench-readline').innerText(), /2026-09-15/)
    assert.equal(await page.locator('[data-testid="home-market-section"]').getAttribute('open'), null)
    assert.equal(await page.locator('[data-testid="workbench-evidence"]').getAttribute('open'), null)
  })
  await scenario('market section opens, repeated link reopens it and diagnostics remain inspectable', async () => {
    await clickNav(page, '市场观察')
    await page.waitForURL(/section=market/)
    const section = page.locator('[data-testid="home-market-section"]')
    await eventually(async () => await section.getAttribute('open') !== null, 'market section did not open')
    await section.locator(':scope > summary').click()
    await clickNav(page, '市场观察')
    await eventually(async () => await section.getAttribute('open') !== null, 'same-section click did not reopen market')
    assert.equal(await nav(page).getByRole('link', { name: '市场观察', exact: true }).getAttribute('aria-current'), 'page')
    const table = page.locator('[data-testid="etf-universe"]')
    assert.equal(await table.locator('.ant-table-row').count(), 10)
    assert.match(await table.innerText(), /量比 0.80×，未达 1.50×/)
    assert.match(await table.innerText(), /本批次未记录逐项原因/)
    assert.match(await table.innerText(), /34\/35 根，尚不能判定/)
    assert.match(await table.innerText(), /2026-08-21 · 周线金叉/)
    await table.locator('.ant-table-row-expand-icon').first().click()
    assert.match(await table.innerText(), /前20日均量 100/)
    await noOverflow(page, '1440px expanded market')
    await shot(page, 'market-diagnostics-1440')
  })
  await scenario('evidence link and repeated click preserve visible failures and dates', async () => {
    await expandGroup(page, '运行与管理')
    await clickNav(page, '运行证据')
    await page.waitForURL(/section=evidence/)
    const evidence = page.locator('[data-testid="workbench-evidence"]')
    await eventually(async () => await evidence.getAttribute('open') !== null, 'evidence did not open')
    await evidence.locator(':scope > summary').click()
    await clickNav(page, '运行证据')
    await eventually(async () => await evidence.getAttribute('open') !== null, 'same-section click did not reopen evidence')
    assert.match(await evidence.innerText(), /目录权限不足/)
    assert.match(await evidence.innerText(), /Telegram.*模型.*未验证/)
    assert.match(await evidence.innerText(), /已恢复（保留首次失败）/)
    assert.match(await page.locator('[data-testid="workbench-overall"]').innerText(), /部分不可用/)
    await noOverflow(page, '1440px expanded evidence')
    await shot(page, 'evidence-expanded-light-1440')
    await clickNav(page, '工作台')
    await page.waitForURL(/#\/personal-home$/)
    await eventually(async () => {
      const top = await page.locator('.personal-home > .hero').evaluate(element => element.getBoundingClientRect().top)
      return top >= -5 && top < 200
    }, 'returning to workbench did not restore the overview position')
  })
  await scenario('refresh-all and report return each request fresh overview', async () => {
    await refreshAll(admin)
    const before = admin.count('/api/radar/workbench/overview')
    await clickNav(page, '报告中心')
    await page.locator('[data-testid="report-center"]').waitFor()
    await page.locator('[data-testid="report-rows"] .ant-table-row').first().waitFor()
    assert.match(await page.locator('[data-testid="report-rows"]').innerText(), /收盘日报/)
    const report2 = page.locator('[data-testid="report-rows"] .ant-table-row').filter({ hasText: 'ec6c86fd856c' })
    assert.match(await report2.innerText(), /结论：未知/)
    assert.doesNotMatch(await report2.innerText(), /表现承压/)
    await page.getByRole('button', { name: '返回工作台', exact: true }).click()
    await eventually(() => admin.count('/api/radar/workbench/overview') > before, 'report return did not refresh overview')
    await homeReady(page)
  })
  await scenario('original AI, indicator, strategy and backtest routes remain navigable without executing work', async () => {
    await expandGroup(page, '研究实验室')
    for (const [label, route, selector] of [
      ['AI 资产分析', '/ai-asset-analysis', '.personal-ai-asset-analysis, .ai-asset-analysis, .copilot-workbench'],
      ['指标研究', '/indicator-ide', '.indicator-ide'],
      ['策略研究', '/strategy-ide', '.strategy-ide-shell'],
      ['回测验证', '/backtest-center', '[data-testid="backtest-center"]']
    ]) {
      await clickNav(page, label)
      await eventually(() => new URL(page.url()).hash.startsWith(`#${route}`), `${label} original route unavailable`)
      if (label !== 'AI 资产分析') await page.locator(selector).first().waitFor()
      else await page.getByText('个人研究模式', { exact: true }).first().waitFor()
      assert.equal(await nav(page).getByRole('link', { name: label, exact: true }).getAttribute('aria-current'), 'page')
    }
    assert.deepEqual(admin.data.deniedMutations, [])
    await clickNav(page, '工作台')
    await homeReady(page)
  })
  await scenario('HTTP503 and all-unavailable overview never retain green Profit evidence', async () => {
    admin.modes.overview = 'http503'
    await refreshAll(admin)
    assert.match(await page.locator('[data-testid="personal-home-workbench"]').innerText(), /工作台读取失败/)
    assert.equal(await page.locator('[data-testid="home-profit-report"]').count(), 0)
    assert.equal(await page.locator('[data-testid="workbench-status"].ant-tag-green').count(), 0)
    assert.doesNotMatch(await page.locator('[data-testid="workbench-overall"]').innerText(), /系统正常/)
    await shot(page, 'overview-http503')
    admin.modes.overview = 'unavailable'
    await refreshAll(admin)
    assert.match(await page.locator('[data-testid="workbench-overall"]').innerText(), /全部业务来源不可用/)
    assert.match(await page.locator('[data-testid="personal-home-workbench"]').innerText(), /全部业务来源不可用/)
    assert.equal(await page.locator('[data-testid="home-profit-report"]').count(), 0)
    assert.equal(await page.locator('[data-testid="workbench-status"].ant-tag-green').count(), 0)
    admin.modes.overview = 'http503-unavailable'
    await refreshAll(admin)
    assert.match(await page.locator('[data-testid="workbench-overall"]').innerText(), /全部业务来源不可用/)
    assert.match(await page.locator('[data-testid="workbench-evidence"]').textContent(), /全部来源不可用/)
    assert.equal(await page.locator('[data-testid="workbench-status"]').count(), 7)
    assert.equal(await page.locator('[data-testid="workbench-status"].ant-tag-green').count(), 0)
    assert.equal(await page.locator('[data-testid="home-profit-report"]').count(), 0)
    admin.modes.overview = 'failed-envelope-healthy-data'
    await refreshAll(admin)
    assert.match(await page.locator('[data-testid="personal-home-workbench"]').innerText(), /工作台读取失败/)
    assert.equal(await page.locator('[data-testid="home-profit-report"]').count(), 0)
    assert.equal(await page.locator('[data-testid="workbench-status"].ant-tag-green').count(), 0)
    admin.modes.overview = 'normal'
    await refreshAll(admin)
    assert.equal(await page.locator('[data-testid="home-profit-report"]').count(), 2)
  })
  await scenario('market failure clears ETF data and recovers without stale diagnostics', async () => {
    await clickNav(page, '市场观察')
    const table = page.locator('[data-testid="etf-universe"]')
    assert.equal(await table.locator('.ant-table-row').count(), 10)
    admin.modes.marketFail = true
    await refreshAll(admin)
    assert.equal(await table.locator('.ant-table-row').count(), 0)
    assert.doesNotMatch(await table.innerText(), /量比 0.80|2026-08-21/)
    const card = await page.locator('[data-testid="effectiveness-card"]').innerText()
    assert.match(card, /证据不可用/)
    assert.doesNotMatch(card, /1\s*\/\s*30|观察记录\s*1/)
    admin.modes.marketFail = false
    await refreshAll(admin)
    assert.equal(await table.locator('.ant-table-row').count(), 10)
    await clickNav(page, '工作台')
    await homeReady(page)
  })
  await scenario('dark and mobile navigation retain five groups without page overflow', async () => {
    await setTheme(page, 'realdark')
    await page.locator('[data-testid="home-market-section"]').evaluate(element => { element.open = false })
    await page.locator('[data-testid="workbench-evidence"]').evaluate(element => { element.open = false })
    await noOverflow(page, '1440px dark home')
    await shot(page, 'home-dark-1440')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.locator('.workspace-navigation--mobile').waitFor()
    assert.equal(await nav(page).locator(':scope > .workspace-navigation__group').count(), 5)
    assert.equal(await page.locator('.ant-drawer-open').count(), 0)
    await noOverflow(page, '390px dark home')
    await shot(page, 'home-dark-390')
    await setTheme(page, 'light')
    await shot(page, 'home-light-390')
    await clickNav(page, '市场观察')
    await eventually(async () => await page.locator('[data-testid="home-market-section"]').getAttribute('open') !== null, 'mobile market did not open')
    await noOverflow(page, '390px expanded market')
    await expandGroup(page, '运行与管理')
    await clickNav(page, '运行证据')
    await eventually(async () => await page.locator('[data-testid="workbench-evidence"]').getAttribute('open') !== null, 'mobile evidence did not open')
    await noOverflow(page, '390px expanded evidence')
    await shot(page, 'evidence-expanded-light-390')
    assert.deepEqual(admin.data.deniedMutations, [])
    assert.deepEqual(admin.data.pageErrors, [])
  })
  await admin.context.close()

  const member = await session('researcher')
  await scenario('non-admin keeps research and evidence, without management or retired routes', async () => {
    await go(member.page, '/personal-home')
    await homeReady(member.page)
    await expandGroup(member.page, '运行与管理')
    assert.equal(await nav(member.page).getByRole('link', { name: '运行证据', exact: true }).count(), 1)
    assert.equal(await nav(member.page).getByRole('link', { name: '系统设置', exact: true }).count(), 0)
    assert.equal(await nav(member.page).getByRole('link', { name: '研究 Agent', exact: true }).count(), 0)
    await go(member.page, '/settings')
    await member.page.waitForURL(/#\/404/)
    assert.equal(member.count('/api/settings/values'), 0)
    await go(member.page, '/billing')
    await member.page.waitForURL(/#\/personal-home/)
    await homeReady(member.page)
    assert.deepEqual(member.data.deniedMutations, [])
    assert.deepEqual(member.data.pageErrors, [])
    await shot(member.page, 'non-admin-home')
  })
  await member.context.close()

  const utc = await session('admin', 'light', 'UTC')
  await scenario('UTC browser still presents known homepage timestamps as CST', async () => {
    await go(utc.page, '/personal-home')
    await homeReady(utc.page)
    await utc.page.locator('[data-testid="home-profit-report"]').first().waitFor()
    assert.equal(await utc.page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone), 'UTC')
    const files = utc.page.locator('[data-testid="home-profit-report"]')
    assert.match(await files.nth(0).innerText(), /2026-09-18 15:35:36/)
    assert.match(await files.nth(1).innerText(), /2026-09-18 15:10:37/)
    assert.doesNotMatch(await files.nth(0).innerText(), /07:35:36/)
    assert.match(await utc.page.locator('.workbench-readline').innerText(), /CST/)
    assert.deepEqual(utc.data.deniedMutations, [])
    assert.deepEqual(utc.data.pageErrors, [])
    await shot(utc.page, 'home-cst-in-utc-context')
  })
  await utc.context.close()
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'
  result.error = error.stack || error.message
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({ path: path.join(qaDir, 'failure.png'), fullPage: true }).catch(() => {})
    result.failureUrl = activePage.url()
  }
  throw error
} finally {
  await fs.writeFile(path.join(qaDir, 'results.json'), JSON.stringify(result, null, 2))
  await browser.close()
}
