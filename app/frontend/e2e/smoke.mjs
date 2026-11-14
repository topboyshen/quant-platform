#!/usr/bin/env node
/**
 * Minimal frontend smoke: login -> overview -> every top-level page.
 *
 * Asserts each page returns 200, renders at least one card/table, keeps the
 * expected nav label, shows no error state, and triggers no 4xx/5xx API call.
 *
 * Env:
 *   QP_SMOKE_BASE_URL  e.g. http://127.0.0.1:18080/next/
 *   QP_SMOKE_USER / QP_SMOKE_PASSWORD
 *   QP_SMOKE_CHANNEL   optional Playwright channel (e.g. chrome)
 */
import { chromium } from 'playwright'

const base = process.env.QP_SMOKE_BASE_URL || 'http://127.0.0.1:18080/next/'
const user = process.env.QP_SMOKE_USER || 'admin'
const password = process.env.QP_SMOKE_PASSWORD || 'correct horse battery staple'
const channel = process.env.QP_SMOKE_CHANNEL || undefined

const ROUTES = [
  ['/', '总览'],
  ['/market', '行情'],
  ['/research', '研究'],
  ['/signals', '信号'],
  ['/reports', '报告'],
  ['/ops', '运维'],
  ['/settings', '设置'],
]
const BAD_TEXT = ['加载失败', '请求失败', 'Internal Server Error', 'Failed to fetch', 'Not Found']

const failures = []
const apiFailures = []

function check(ok, message) {
  if (!ok) failures.push(message)
}

function urlFor(path) {
  return path === '/' ? base : new URL(path.replace(/^\//, ''), base).toString()
}

const browser = await chromium.launch({ channel })
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
const page = await context.newPage()
page.on('pageerror', (e) => failures.push(`pageerror: ${e.message}`))
page.on('response', (r) => {
  const url = r.url()
  if (url.includes('/api/') && r.status() >= 400) {
    apiFailures.push(`${r.status()} ${r.request().method()} ${url.replace(base, '/next/')}`)
  }
})

try {
  // --- login -------------------------------------------------------------
  const loginResp = await page.goto(urlFor('/login'), { waitUntil: 'networkidle' })
  check(loginResp?.status() === 200, `login: document ${loginResp?.status()}`)
  await page.locator('input').nth(0).fill(user)
  await page.locator('input[type="password"]').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForFunction(() => !location.pathname.endsWith('/login'), null, { timeout: 15000 })
  await page.locator('.brand').waitFor({ state: 'visible', timeout: 15000 })
  const me = (await page.locator('.user').innerText()).trim()
  check(me.length > 0, 'login: user chip is empty')
  console.log(`login ok as ${me}`)

  // --- every top-level page ---------------------------------------------
  for (const [path, label] of ROUTES) {
    const apiMark = apiFailures.length
    const resp = await page.goto(urlFor(path), { waitUntil: 'networkidle' })
    check(resp?.status() === 200, `${path}: document ${resp?.status()}`)
    try {
      // empty databases legitimately render tabs + an empty state, so accept the page shell
      await page.locator('.n-card, .n-data-table, .n-statistic, .n-tabs, .n-empty, .n-result').first()
        .waitFor({ state: 'visible', timeout: 20000 })
    } catch {
      failures.push(`${path}: page shell did not render`)
    }
    const text = await page.locator('body').innerText()
    for (const bad of BAD_TEXT) {
      check(!text.includes(bad), `${path}: shows "${bad}"`)
    }
    const nav = await page.locator('.nav').innerText().catch(() => '')
    check(nav.includes(label), `${path}: nav label "${label}" missing`)
    const failed = apiFailures.slice(apiMark)
    check(failed.length === 0, `${path}: API failures ${failed.join(', ')}`)
    console.log(`${path} ok (${label})`)
  }
} catch (e) {
  failures.push(`fatal: ${e.name}: ${String(e.message).split('\n')[0]}`)
} finally {
  await context.close()
  await browser.close()
}

if (apiFailures.length) {
  console.error('API failures:')
  for (const f of apiFailures) console.error(`  ${f}`)
}
if (failures.length) {
  console.error(`SMOKE FAIL (${failures.length})`)
  for (const f of failures) console.error(`  ${f}`)
  process.exit(1)
}
console.log('SMOKE PASS')
