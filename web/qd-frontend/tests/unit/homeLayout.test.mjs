import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { homeAttention, homeMarketCutoff, homeProfitReports, homeTime } from '../../src/utils/homePresentation.js'
import { expandWorkbenchRows, summarizeWorkbench, workbenchItems } from '../../src/utils/workbench.js'
import { effectivenessProgressView } from '../../src/utils/effectivenessProgress.js'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const homeSource = read('../../src/views/personal-home/index.vue')
const workbenchSource = read('../../src/components/WorkbenchOverview/index.vue')

// Exercise component methods without mounting Vue or making network requests.
// Template checks below cover only public slots, controls and stable QA hooks.
function componentOptions (source, bindings) {
  const script = source.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)?.[1]
  assert.ok(script, 'component must expose a script block')
  const body = script
    .replace(/^import\s[\s\S]*?\sfrom\s+['"][^'"]+['"]\s*$/gm, '')
    .replace(/export\s+default\s+/, 'return ')
  return new Function(...Object.keys(bindings), body)(...Object.values(bindings))
}

const home = componentOptions(homeSource, {
  mapState: () => ({}),
  WorkbenchOverview: {},
  homeTime,
  effectivenessProgressView
})

function workbench (getWorkbenchOverview) {
  const options = componentOptions(workbenchSource, {
    getWorkbenchOverview,
    expandWorkbenchRows,
    summarizeWorkbench,
    workbenchItems,
    homeAttention,
    homeMarketCutoff,
    homeProfitReports,
    homeTime
  })
  const events = []
  const instance = { ...options.data(), $emit: (...args) => events.push(args) }
  for (const [name, method] of Object.entries(options.methods)) instance[name] = method.bind(instance)
  return { options, instance, events }
}

const healthyOverview = () => ({
  partial: false,
  all_unavailable: false,
  items: [{ id: 'digest_inputs', label: '研究摘要行情截止', status: 'ok', data_cutoff: '2026-09-18' }]
})

function deferred () {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('home times convert UTC and explicit offsets to CST including date rollover', () => {
  assert.equal(homeTime('2026-09-20T16:05:06Z'), '2026-09-21 00:05:06')
  assert.equal(homeTime('2026-09-21T08:05:06+08:00'), '2026-09-21 08:05:06')
  assert.equal(homeTime('2026-09-20T19:05:06-05:00'), '2026-09-21 08:05:06')
  assert.equal(home.methods.formatTime('2026-09-21T00:05:06Z'), '2026-09-21 08:05:06')
})

test('offset-free contract timestamps stay CST and date-only cutoffs do not gain time', () => {
  assert.equal(homeTime('2026-09-21T08:05:06'), '2026-09-21 08:05:06')
  assert.equal(homeTime('2026-09-21 08:05:06'), '2026-09-21 08:05:06')
  assert.equal(homeTime(' 2026-09-18 '), '2026-09-18')
})

test('missing or invalid timestamp values remain explicitly unknown', () => {
  for (const value of [undefined, null, '', '  ', 0, false, '2026-13-01T00:00:00Z']) {
    assert.equal(homeTime(value), '未知')
  }
  assert.equal(homeTime(null, '—'), '—')
  assert.equal(homeTime('未知'), '未知')
  assert.equal(homeTime('2026-02-30'), '未知')
  assert.equal(homeTime('2026-02-30T08:00:00+08:00'), '未知')
  assert.equal(homeTime('2025-02-29T08:00:00Z'), '未知')
  assert.equal(homeTime('2024-02-29T00:00:00Z'), '2024-02-29 08:00:00')
})

test('only digest input evidence supplies the market cutoff', () => {
  const payload = {
    generated_at: '2026-09-21T10:00:00+08:00',
    sync_at: '2026-09-21T11:00:00+08:00',
    items: [
      { id: 'calendar_freshness', data_cutoff: '2026-09-21' },
      { id: 'calendar_coverage', data_cutoff: '2026-12-31' },
      { id: 'eod_status', data_cutoff: '2026-09-20T15:50:00+08:00' },
      { id: 'profit_radar', data_cutoff: '2026-09-20', file_mtime: '2026-09-21' },
      { id: 'digest_inputs', data_cutoff: '2026-09-18' }
    ]
  }
  assert.equal(homeMarketCutoff(payload), '2026-09-18')
  payload.items.pop()
  assert.equal(homeMarketCutoff(payload), '未知')
  payload.items.push({ id: 'digest_inputs', data_cutoff: null, generated_at: '2026-09-21' })
  assert.equal(homeMarketCutoff(payload), '未知')
  assert.equal(homeMarketCutoff(null), '未知')
})

test('profit summaries keep each file conclusion and timestamps without borrowing evidence', () => {
  const payload = {
    generated_at: '2026-09-21T09:00:00+08:00',
    items: [{
      id: 'profit_radar', status: 'ok', data_cutoff: '2026-09-21', conclusion: '聚合结论不可借用',
      value: { files: [
        { file: '026a1083d9e0/latest.md', status: 'ok', conclusion: '盈利优势未建立', generated_at: '2026-09-18T07:35:36Z', market_cutoff_date: null, market_cutoff: null },
        { file: 'ec6c86fd856c/latest.md', status: 'unavailable', conclusion: null, generated_at: null, market_cutoff_date: '2026-09-18', file_mtime: '2026-09-21T09:00:00+08:00' }
      ] }
    }]
  }
  const original = structuredClone(payload)
  const reports = homeProfitReports(payload)
  assert.equal(reports.length, 2)
  assert.deepEqual(reports.map(report => [report.job, report.conclusion, report.generatedAt, report.marketCutoff]), [
    ['026a1083d9e0', '盈利优势未建立', '2026-09-18 15:35:36', '未知'],
    ['ec6c86fd856c', '未知', '未知', '2026-09-18']
  ])
  assert.equal(reports[1].status, 'unavailable')
  assert.notEqual(reports[1].statusMeta.color, 'green')
  assert.deepEqual(payload, original)
  assert.deepEqual(homeProfitReports(null), [])
  assert.deepEqual(homeProfitReports({ items: [{ id: 'profit_radar', status: 'ok' }] }), [])
})

test('absent or empty workbench evidence never reports a healthy system', () => {
  for (const payload of [undefined, null, {}, { items: null }, { items: [] }]) {
    const summary = summarizeWorkbench(payload)
    assert.equal(summary.overall, 'unavailable')
    assert.notEqual(summary.overallLabel, '系统正常')
    assert.match(summary.openQuestions.join(' '), /不能下任何结论/)
  }
  assert.equal(summarizeWorkbench(healthyOverview()).overall, 'ok')
})

test('not-integrated sources explicitly produce partial status without a backend partial flag', () => {
  const payload = healthyOverview()
  payload.items.push({ id: 'release_version', label: '发布版本', status: 'not_integrated' })
  const summary = summarizeWorkbench(payload)
  assert.equal(summary.overall, 'partial')
  assert.equal(summary.partial, true)
  assert.match(summary.openQuestions.join(' '), /发布版本.*待接入/)
  assert.deepEqual(homeAttention(payload).map(item => [item.id, item.status]), [['release_version', '待接入']])
})

test('unknown evidence status cannot silently produce a healthy workbench', () => {
  const payload = healthyOverview()
  payload.items.push({ id: 'future_source', label: '新增来源', status: 'unrecognized' })
  assert.equal(summarizeWorkbench(payload).overall, 'partial')
  assert.match(summarizeWorkbench(payload).openQuestions.join(' '), /新增来源.*未知状态/)
  assert.deepEqual(homeAttention(payload).map(item => item.status), ['未知状态'])
})

test('malformed individual workbench or Profit entries degrade without taking down other evidence', () => {
  const payload = healthyOverview()
  payload.items.push(null, [], { id: 'profit_radar', status: 'ok', value: { files: [null, 'bad', [], { file: 'ec6c86fd856c/latest.md', conclusion: '保持原样', generated_at: '2026-09-18' }] } })
  assert.equal(summarizeWorkbench(payload).overall, 'partial')
  assert.equal(homeMarketCutoff(payload), '2026-09-18')
  assert.equal(homeAttention(payload).length, 2)
  const reports = homeProfitReports(payload)
  assert.deepEqual(reports.slice(0, 3).map(row => [row.status, row.conclusion, row.generatedAt]), Array(3).fill(['unavailable', '未知', '未知']))
  assert.equal(reports[3].conclusion, '保持原样')
  const evidence = expandWorkbenchRows(workbenchItems(payload))
  assert.equal(evidence.filter(row => row.id.startsWith('profit_radar:file-')).length, 3)
  assert.ok(evidence.filter(row => row.id.startsWith('profit_radar:file-')).every(row => row.status === 'unavailable'))
  assert.doesNotThrow(() => expandWorkbenchRows(workbenchItems({ items: [null, []] })))
})

test('home effectiveness binding never turns missing samples or a failed read into green evidence', () => {
  const complete = { state: 'preliminary_effective', sample_size: 30, minimum_samples: 30, primary_horizon_sessions: 20 }
  for (const state of [
    { signals: {}, error: '' },
    { signals: { effectiveness: { ...complete, sample_size: null } }, error: '' },
    { signals: { effectiveness: complete }, error: 'Radar 读取失败' }
  ]) {
    const view = home.computed.effectivenessView.call(state)
    assert.equal(view.available, false)
    assert.equal(view.sampleSize, '--')
    assert.equal(view.minimumSamples, '--')
    assert.notEqual(view.color, 'green')
  }
})

test('failed workbench refresh clears the prior evidence and records only the failed attempt', async () => {
  let fail = false
  const { instance, events } = workbench(async () => {
    if (fail) throw new Error('synthetic offline')
    return { code: 1, data: healthyOverview() }
  })
  await instance.load()
  assert.equal(summarizeWorkbench(instance.overview).overall, 'ok')
  fail = true
  await instance.load()
  assert.equal(instance.overview, null)
  assert.equal(instance.observedAt, '—')
  assert.match(instance.lastAttemptAt, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  assert.ok(instance.error)
  assert.equal(summarizeWorkbench(instance.overview).overall, 'unavailable')
  assert.equal(instance.loading, false)
  assert.deepEqual(events, [['loading-change', true], ['loading-change', false], ['loading-change', true], ['loading-change', false]])
})

test('an unsuccessful workbench envelope without evidence cannot retain a prior healthy result', async () => {
  const { instance } = workbench(async () => ({ code: 0, msg: '合成读取失败' }))
  instance.overview = healthyOverview()
  await instance.load()
  assert.equal(instance.overview, null)
  assert.equal(instance.error, '合成读取失败')
  assert.equal(instance.observedAt, '—')
  assert.equal(summarizeWorkbench(instance.overview).overall, 'unavailable')
})

test('failure envelopes reject attached apparently healthy data while explicit 503 evidence stays visible', async () => {
  const attached = workbench(async () => ({ code: 0, msg: '读取失败', data: healthyOverview() }))
  await attached.instance.load()
  assert.equal(attached.instance.overview, null)
  assert.equal(attached.instance.observedAt, '—')
  const data = { all_unavailable: true, items: [{ id: 'digest_inputs', label: '行情输入', status: 'unavailable', detail: '合成证据缺失' }] }
  const transport = workbench(async () => { throw { response: { status: 503, data: { code: 1, data } } } })
  await transport.instance.load()
  assert.equal(transport.instance.error, '')
  assert.equal(transport.instance.overview.items[0].detail, '合成证据缺失')
  assert.equal(summarizeWorkbench(transport.instance.overview).overall, 'unavailable')
})

test('overlapping workbench loads ignore an older success after the latest request fails', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const { instance } = workbench(() => (++calls === 1 ? first.promise : second.promise))
  const older = instance.load()
  const newer = instance.load()
  second.reject(new Error('latest request failed'))
  await newer
  first.resolve({ code: 1, data: healthyOverview() })
  await older
  assert.equal(instance.overview, null)
  assert.ok(instance.error)
  assert.equal(instance.loading, false)
  assert.equal(instance.loadSeq, 2)
})

test('destroying the workbench invalidates an outstanding load', async () => {
  const request = deferred()
  const { options, instance } = workbench(() => request.promise)
  const loading = instance.load()
  options.beforeDestroy.call(instance)
  request.resolve({ code: 1, data: healthyOverview() })
  await loading
  assert.equal(instance.overview, null)
  assert.equal(instance.observedAt, '—')
})

test('home refresh-all and activation still refresh the mounted workbench', async () => {
  const calls = []
  const instance = {
    $refs: { workbenchOverview: { load: async () => { calls.push('workbench') } } },
    loadContext: async () => { calls.push('radar') },
    loadWatchlist: async () => { calls.push('watchlist') },
    refreshSelectedContext: () => { calls.push('selection') },
    focusRouteSection: () => { calls.push('section') },
    refreshWorkbench: home.methods.refreshWorkbench
  }
  await home.methods.loadAll.call(instance)
  assert.deepEqual(calls.sort(), ['radar', 'watchlist', 'workbench'])
  calls.length = 0
  home.activated.call(instance)
  assert.deepEqual(calls.sort(), ['radar', 'section', 'selection', 'watchlist', 'workbench'])
  await home.methods.refreshWorkbench.call({ $refs: {} })
})

test('home sections keep their named slots and workbench instance independently of collapse state', () => {
  const opening = homeSource.match(/<workbench-overview\b[^>]*>/)?.[0]
  assert.ok(opening)
  assert.match(opening, /\bref=["']workbenchOverview["']/)
  assert.doesNotMatch(opening, /\bv-if\s*=/)
  assert.match(opening, /@loading-change=/)
  for (const slot of ['research', 'market-summary', 'research-entry', 'market-pools', 'audit']) {
    assert.match(homeSource, new RegExp(`<template\\b[^>]*(?:#${slot}|v-slot:${slot}|slot=["']${slot}["'])`))
    assert.match(workbenchSource, new RegExp(`<slot\\b[^>]*name=["']${slot}["']`))
  }
})

test('evidence and market sections remain native details that can be opened through navigation', () => {
  for (const [source, marker] of [[homeSource, 'home-market-section'], [workbenchSource, 'workbench-evidence']]) {
    const opening = source.match(new RegExp(`<details\\b[^>]*data-testid=["']${marker}["'][^>]*>`))?.[0]
    assert.ok(opening, `${marker} must remain a native details disclosure`)
    assert.doesNotMatch(opening, /\sopen(?:\s|=|>)/)
  }
  const calls = []
  const evidence = { open: false, scrollIntoView: () => calls.push('evidence-scroll') }
  const { instance } = workbench(async () => ({ code: 1, data: healthyOverview() }))
  instance.$refs = { evidenceSection: evidence }
  instance.$nextTick = callback => callback()
  const market = { open: false, scrollIntoView: () => calls.push('market-scroll') }
  const owner = { $refs: { workbenchOverview: instance, marketSection: market }, $nextTick: callback => callback() }
  home.methods.focusHomeSection.call(owner, 'evidence')
  home.methods.focusHomeSection.call(owner, 'market')
  assert.equal(evidence.open, true)
  assert.equal(market.open, true)
  assert.deepEqual(calls, ['evidence-scroll', 'market-scroll'])
})

test('home preserves stable ETF and workbench QA hooks without binding them to grid layout', () => {
  for (const marker of ['home-refresh-all', 'personal-home-workbench', 'effectiveness-card', 'effectiveness-maturity', 'effectiveness-detail-unavailable', 'effectiveness-detail', 'etf-universe', 'etf-pool-summary']) {
    assert.match(homeSource, new RegExp(`data-testid=["']${marker}["']`))
  }
  for (const marker of ['workbench-overview', 'workbench-overall', 'workbench-refresh', 'workbench-items', 'workbench-status']) {
    assert.match(workbenchSource, new RegExp(`data-testid=["']${marker}["']`))
  }
  assert.match(homeSource, /<details\b[^>]*data-testid=["']effectiveness-detail["']/)
})
