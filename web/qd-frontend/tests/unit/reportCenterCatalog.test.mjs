// 报告中心第一批：目录逻辑回归（随项目测试套件运行；不依赖网络与生产数据）
// 运行：node --test tests/unit/*.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  PAGE_STATE, pageState, normalizeReports, stableKeyOf, jobIdOf, familyOf,
  timeFieldsOf, conclusionOf, filterReports, resolveSelection, createLoader,
  groupByFamily, readabilityOf, findRow, normalizeReport
} from '../../src/utils/reportCatalog.js'

// 内联夹具（结构按 2026-09-21 实测响应复刻；自由文本部分合成）。
// 独立于预览，随项目测试树一起运行；不读取任何生产数据。
const OBSERVED_AT = '2026-09-21T13:12:00+08:00'
const profitRow = (job, label, generatedAt, mtime, conclusion, marketCutoff) => ({
  id: `profit_radar_${job}`,
  label: `${label}（${job}/latest.md）`,
  path: `data/hermes-workbench-allowlist/hermes-reports/${job}/latest.md`,
  format: 'markdown', status: 'ok', generated_at: generatedAt, file_mtime: mtime, size: 4817,
  schema_version: null, detail: '', observed_at: OBSERVED_AT,
  summary: { conclusion, disclaimer: '模拟跟踪 ≠ 账户收益', generated_at: generatedAt, head: ['【盈利研究】'],
    market_cutoff: marketCutoff, market_cutoff_date: marketCutoff }
})
const reportsAvailable = {
  contract_version: 'workbench.reports.v1', generated_at: OBSERVED_AT,
  reports: [
    { id: 'eod_daily_report', label: '收盘日报', path: 'data/eod_daily_report.json', format: 'json', status: 'ok',
      generated_at: '2026-09-18T15:50:02+08:00', file_mtime: '2026-09-18T15:50:02.117485+08:00', schema_version: 1,
      detail: '', observed_at: OBSERVED_AT,
      summary: { message: 'EOD success，逐标的证据 10/10，outcome=no_signal', terminal_ok: true, terminal_reason: 'success', trade_date: '2026-09-18' } },
    { id: 'research_digest_json', label: '研究摘要（JSON）', format: 'json', status: 'ok', generated_at: null,
      file_mtime: '2026-09-21T13:00:01.947662+08:00', schema_version: null, detail: '', observed_at: OBSERVED_AT,
      summary: { note: '四指数收盘缓存已刷新' } },
    { id: 'research_digest_md', label: '研究摘要（Markdown）', format: 'markdown', status: 'ok', generated_at: null,
      file_mtime: '2026-09-21T13:00:01.947662+08:00', schema_version: null, detail: '', observed_at: OBSERVED_AT,
      summary: { conclusion: null, disclaimer: '', generated_at: null, head: ['【研究摘要】'],
        market_cutoff: '2026-09-18｜收盘口径，非实时', market_cutoff_date: '2026-09-18' } },
    { id: 'execution_digest_json', label: '执行摘要（JSON）', format: 'json', status: 'unavailable', generated_at: null,
      file_mtime: null, schema_version: null, observed_at: OBSERVED_AT, summary: null,
      detail: '执行摘要目录权限不足（部署清单：approved-signals 需只读遍历权限；不扩大 intake 数据库权限）' },
    { id: 'execution_digest_md', label: '执行摘要（Markdown）', format: 'markdown', status: 'unavailable', generated_at: null,
      file_mtime: null, schema_version: null, observed_at: OBSERVED_AT, summary: null,
      detail: '执行摘要目录权限不足（部署清单：approved-signals 需只读遍历权限；不扩大 intake 数据库权限）' },
    profitRow('026a1083d9e0', 'Profit Radar 报告', '2026-09-18T15:35:36+08:00', '2026-09-18T15:35:36.374014+08:00', '表现承压，盈利优势未建立', null),
    profitRow('ec6c86fd856c', 'Profit Radar 报告', '2026-09-18T15:10:37+08:00', '2026-09-18T15:10:42.729572+08:00', '样本不足，暂不下结论', '2026-09-18')
  ]
}
const reportsEmpty = { contract_version: 'workbench.reports.v1', generated_at: OBSERVED_AT, reports: [] }
const reportsUnknownContract = { contract_version: 'workbench.reports.v2', generated_at: OBSERVED_AT }
const reportsAllUnreadable = { contract_version: 'workbench.reports.v1', generated_at: OBSERVED_AT,
  reports: reportsAvailable.reports.filter(row => row.status !== 'ok') }
const reportsWithBadRow = { contract_version: 'workbench.reports.v1', generated_at: OBSERVED_AT,
  reports: [null, { id: 'ghost', label: '', format: 'json', status: 'ok' }, ...reportsAvailable.reports] }
const PAGE_STATES = {
  available: { payload: reportsAvailable, loading: false, error: '', unauthorized: false, contractOk: true },
  empty: { payload: reportsEmpty, loading: false, error: '', unauthorized: false, contractOk: true },
  unavailable: { payload: null, loading: false, error: '报告中心读取失败，请稍后重试。', unauthorized: false, contractOk: false },
  unauthorized: { payload: null, loading: false, error: '', unauthorized: true, contractOk: false },
  loading: { payload: reportsAvailable, loading: true, error: '', unauthorized: false, contractOk: true },
  contractBroken: { payload: reportsUnknownContract, loading: false, error: '', unauthorized: false, contractOk: false }
}


const rows = () => normalizeReports(reportsAvailable)

// ---------- 状态机：空/失败/未知契约不得为绿 ----------
test('状态机：只有契约有效且全部可读才是 available', () => {
  assert.equal(pageState({ loading: false, error: '', contractOk: true, reports: rows() }), PAGE_STATE.PARTIAL)
  assert.equal(pageState({ loading: false, error: '', contractOk: true, reports: [] }), PAGE_STATE.EMPTY)
  assert.equal(pageState({ loading: false, error: 'boom', contractOk: false, reports: [] }), PAGE_STATE.UNAVAILABLE)
  assert.equal(pageState({ loading: false, error: '', contractOk: false, reports: [] }), PAGE_STATE.UNAVAILABLE)
  assert.equal(pageState({ loading: true, error: '', contractOk: true, reports: rows() }), PAGE_STATE.LOADING)
})

test('状态机：空列表与失败、未知契约都不得判为全部可读', () => {
  for (const name of ['empty', 'unavailable', 'unauthorized', 'contractBroken']) {
    const state = PAGE_STATES[name]
    const value = pageState({
      loading: state.loading, error: state.error, unauthorized: state.unauthorized,
      contractOk: state.contractOk,
      reports: state.payload ? normalizeReports(state.payload) : []
    })
    assert.notEqual(value, PAGE_STATE.AVAILABLE, `${name} 不得为 available`)
  }
})

test('状态机：全部不可读时不显示"部分"以外的绿色', () => {
  const allBad = normalizeReports(reportsAllUnreadable)
  assert.equal(allBad.every(row => !row.readable), true)
  assert.equal(pageState({ contractOk: true, reports: allBad }), PAGE_STATE.PARTIAL)
})

test('unknown 契约：normalizeReports 不抛错且返回 0 行', () => {
  assert.deepEqual(normalizeReports(reportsUnknownContract), [])
})

// ---------- 稳定键：job id 优先，不用下标 ----------
test('稳定键：Profit 行取白名单 job id，不依赖数组位置', () => {
  const list = rows()
  const keys = list.filter(r => r.jobId).map(r => r.key).sort()
  assert.deepEqual(keys, ['job:026a1083d9e0', 'job:ec6c86fd856c'])
  // 打乱顺序后键不变
  const shuffled = normalizeReports({ ...reportsAvailable, reports: [...reportsAvailable.reports].reverse() })
  assert.deepEqual(shuffled.filter(r => r.jobId).map(r => r.key).sort(), keys)
  // 下标式 id 不应出现在键里
  assert.equal(list.some(r => r.key && r.key.includes('profit_radar_')), false)
})

test('稳定键：家族映射正确（ec6c=盈利研究，026a=健康看板）', () => {
  const list = rows()
  const ec6c = list.find(r => r.jobId === 'ec6c86fd856c')
  const n26a = list.find(r => r.jobId === '026a1083d9e0')
  assert.equal(ec6c.title, 'QuantDinger 盈利研究')
  assert.equal(n26a.title, 'Profit Radar 健康看板')
  assert.equal(ec6c.group, 'research')
})

test('稳定键：无 job id 时按家族+格式，而非下标', () => {
  const list = rows()
  const digestJson = list.find(r => r.rawId === 'research_digest_json')
  const digestMd = list.find(r => r.rawId === 'research_digest_md')
  assert.equal(digestJson.key, 'family:research_digest:json')
  assert.equal(digestMd.key, 'family:research_digest:markdown')
})

test('稳定键：完全无法识别时返回 null 并给出原因（不静默选错）', () => {
  const bad = stableKeyOf({ format: 'json', status: 'ok' })
  assert.equal(bad.key, null)
  assert.match(bad.reason, /无法识别/)
})

test('稳定键：标识重复会被标记为需关注', () => {
  const dup = normalizeReports({
    contract_version: 'v1', reports: [
      { id: 'a', label: 'Profit Radar 报告（ec6c86fd856c/latest.md）', format: 'markdown', status: 'ok', summary: {} },
      { id: 'b', label: 'Profit Radar 报告（ec6c86fd856c/latest.md）', format: 'markdown', status: 'ok', summary: {} }
    ]
  })
  assert.equal(dup.length, 2)
  assert.equal(dup.every(r => r.needsAttention), true)
})

// ---------- 时间：三件事分开，缺失一律未知 ----------
test('时间：generated_at 缺失写未知，绝不用 file_mtime 顶替', () => {
  const list = rows()
  const json = list.find(r => r.rawId === 'research_digest_json')
  assert.equal(json.generatedAt, null)
  assert.equal(json.generatedAtMissing, true)
  assert.equal(json.fileMtime, '2026-09-21 13:00:01', 'file_mtime 只作技术证据，单独字段')
})

test('时间：profit 两份的生成时间与文件时间分列，互不覆盖', () => {
  const list = rows()
  const ec6c = list.find(r => r.jobId === 'ec6c86fd856c')
  const n26a = list.find(r => r.jobId === '026a1083d9e0')
  assert.equal(ec6c.generatedAt, '2026-09-18 15:10:37')
  assert.equal(ec6c.fileMtime, '2026-09-18 15:10:42')
  assert.equal(n26a.generatedAt, '2026-09-18 15:35:36')
  assert.equal(n26a.marketCutoff, null, '026a 行情截止缺失 → 未知')
  assert.equal(ec6c.marketCutoff, '2026-09-18')
})

test('时间：行情截止不会借另一份报告或生成时间', () => {
  const list = rows()
  const n26a = list.find(r => r.jobId === '026a1083d9e0')
  const ec6c = list.find(r => r.jobId === 'ec6c86fd856c')
  // 缺失就是 null（展示层写“未知”），不得等于自己的生成时间
  assert.equal(n26a.marketCutoff, null)
  assert.notEqual(n26a.marketCutoff, n26a.generatedAt)
  // 不得跨报告借用截止日
  assert.equal(ec6c.marketCutoff, '2026-09-18')
  const borrowed = list.filter(r => r.marketCutoff && r.marketCutoff === n26a.generatedAt)
  assert.equal(borrowed.length, 0)
  // 两份 profit 的截止日各自独立：只有 ec6c 有值
  assert.equal(list.filter(r => r.jobId && r.marketCutoff).length, 1)
})

test('时间：仅日期字段保持日期，不擅自补时间', () => {
  const t = timeFieldsOf({ generated_at: null, file_mtime: '2026-09-18T15:35:36.374+08:00', summary: { market_cutoff_date: '2026-09-18' } })
  assert.equal(t.marketCutoff, '2026-09-18')
  assert.equal(t.generatedAt, null)
})

// ---------- 结论：缺失就写缺失，不做加工 ----------
test('结论：显式结论优先；缺失写"报告未提供明确结论"', () => {
  const list = rows()
  const ec6c = list.find(r => r.jobId === 'ec6c86fd856c')
  assert.equal(ec6c.conclusion.kind, 'conclusion')
  assert.equal(ec6c.conclusion.text, '样本不足，暂不下结论')

  const eod = list.find(r => r.rawId === 'eod_daily_report')
  assert.equal(eod.conclusion.kind, 'excerpt')
  assert.equal(eod.conclusion.label, '原文摘要')
  assert.match(eod.conclusion.text, /EOD success/)
})

test('结论：既无结论也无摘要时明确写缺失', () => {
  const c = conclusionOf({ summary: { disclaimer: 'x' } })
  assert.equal(c.kind, 'missing')
  assert.equal(c.text, '报告未提供明确结论')
})

// 作用域必须限定在“结论区”，不能对整页文案做此类断言：
// 页面本身含有必要说明，例如“模拟跟踪 ≠ 账户收益”“已受理 ≠ 成交”，
// 整页扫描会误伤这些免责声明。
test('结论区：缺失结论时不编造盈利判断（仅限结论区文本）', () => {
  const list = rows()
  const missing = list.filter(r => r.conclusion.kind === 'missing')
  assert.ok(missing.length > 0, '夹具应包含缺结论的行')
  missing.forEach(row => {
    assert.equal(row.conclusion.text, '报告未提供明确结论')
    assert.equal(/盈利|收益|赚/.test(row.conclusion.text), false, `结论区不得出现盈利判断：${row.title}`)
  })
  // 免责声明仍应存在于正文说明字段中（证明上一条不是靠删说明达成的）
  const eod = list.find(r => r.conclusion.kind === 'excerpt')
  assert.ok(eod, '夹具应包含摘录型结论')
})

// ---------- 筛选 ----------
test('筛选：分组、标题搜索、只看需关注可组合', () => {
  const list = rows()
  assert.equal(filterReports(list, { group: 'research' }).length, 2)
  assert.equal(filterReports(list, { group: 'close_run' }).length, 5)
  assert.equal(filterReports(list, { query: '执行摘要' }).length, 2)
  assert.equal(filterReports(list, { query: 'profit radar' }).length, 2)
  const attention = filterReports(list, { attentionOnly: true })
  assert.equal(attention.length, 4, '两份执行摘要（不可读）+ 研究摘要 JSON/MD（生成时间缺失）')
  assert.equal(filterReports(list, { group: 'research', attentionOnly: true }).length, 0)
  // 原始 label 参与检索：两个 Profit 家族都能被 "profit" 找到
  assert.equal(filterReports(list, { query: 'profit' }).length, 2)
})

// ---------- 选中项 ----------
test('选中：默认选可读取的盈利研究', () => {
  const selection = resolveSelection(rows(), null)
  assert.equal(selection.selectedKey, 'job:ec6c86fd856c')
  assert.equal(selection.fellBack, false)
})

test('选中：键失效时回退并给出提示，不静默', () => {
  const gone = resolveSelection(rows(), 'job:deadbeef')
  assert.equal(gone.fellBack, true)
  assert.match(gone.note, /已不在当前列表/)
  // 明确点选“不可读取”的报告不算失效：那是用户主动选择，应原样展示它自己的状态
  const unreadable = resolveSelection(rows(), 'family:execution_digest:json')
  assert.equal(unreadable.fellBack, false)
  assert.equal(unreadable.selectedKey, 'family:execution_digest:json')
})

test('选中：没有可读取报告时返回 null 且说明原因', () => {
  const selection = resolveSelection(normalizeReports(reportsAllUnreadable), null)
  assert.equal(selection.selectedKey, null)
  assert.match(selection.note, /没有可读取/)
})

// ---------- 竞态 ----------
test('竞态：旧响应不得覆盖新响应', () => {
  const loader = createLoader()
  const first = loader.next()
  const second = loader.next()
  assert.equal(loader.isCurrent(first), false)
  assert.equal(loader.isCurrent(second), true)
})

// ---------- 家族分组 ----------
test('分组：同家族多格式合并为一个入口但保留各变体状态', () => {
  const groups = groupByFamily(rows())
  const digest = groups.find(g => g.title === '市场研究摘要')
  assert.equal(digest.variants.length, 2)
  const exec = groups.find(g => g.title === '执行摘要')
  assert.equal(exec.variants.length, 2)
  assert.equal(exec.variants.every(v => !v.readable), true)
})

// ---------- 脏数据 ----------
test('脏数据：null 行与缺标识行不拖垮其余报告', () => {
  const list = normalizeReports(reportsWithBadRow)
  assert.equal(list.length, 9)
  const ghost = list.find(r => r.rawId === 'ghost')
  assert.equal(ghost.key, null)
  assert.equal(ghost.readable, true)
  assert.equal(ghost.needsAttention, true)
  assert.equal(list.filter(r => r.jobId).length, 2)
})

test('可读取性词表：不表达业务成功', () => {
  assert.equal(readabilityOf('ok').label, '可读取')
  assert.equal(readabilityOf('unavailable').readable, false)
  assert.equal(readabilityOf('weird').label, '状态未知')
})

// ---------- 真实文本形态回归（线上 label 为全角括号） ----------
test('稳定键：全角括号与半角括号的 label 都能取出 job id', () => {
  const full = stableKeyOf({ label: 'Profit Radar 报告（026a1083d9e0/latest.md）', format: 'markdown', status: 'ok' })
  const half = stableKeyOf({ label: 'Profit Radar 报告(026a1083d9e0/latest.md)', format: 'markdown', status: 'ok' })
  const bare = stableKeyOf({ path: 'data/hermes-workbench-allowlist/hermes-reports/ec6c86fd856c/latest.md', format: 'markdown', status: 'ok' })
  assert.equal(full.key, 'job:026a1083d9e0')
  assert.equal(half.key, 'job:026a1083d9e0')
  assert.equal(bare.key, 'job:ec6c86fd856c')
  assert.equal(full.via, 'job_id')
})

// ---------- 不可读项：可被选中看原因，但不会被默认选中 ----------
test('选中：不可读取的报告仍可被显式选中（否则看不到原因）', () => {
  const list = rows()
  const picked = resolveSelection(list, 'family:execution_digest:json')
  assert.equal(picked.selectedKey, 'family:execution_digest:json')
  assert.equal(picked.fellBack, false)
  assert.equal(findRow(list, picked.selectedKey).readable, false)
})

test('选中：默认选中绝不落在不可读取项上', () => {
  const list = rows()
  const picked = resolveSelection(list, null)
  assert.equal(findRow(list, picked.selectedKey).readable, true)
})

test('选中：键彻底消失才回退，且提示不说"不可读取"', () => {
  const gone = resolveSelection(rows(), 'job:deadbeef')
  assert.equal(gone.fellBack, true)
  assert.match(gone.note, /已不在当前列表/)
})

// ---------- 家族内显式业务日期（不跨报告借用） ----------
test('时间：收盘日报用自身 trade_date 作行情截止（显式字段，非借用）', () => {
  const eod = rows().find(r => r.rawId === 'eod_daily_report')
  assert.equal(eod.marketCutoff, '2026-09-18')
  assert.equal(eod.marketCutoffFrom, 'summary.trade_date')
})

test('时间：无该字段时仍为未知，不退化为 mtime', () => {
  const bare = normalizeReport({ id: 'eod_daily_report', format: 'json', status: 'ok', generated_at: '2026-09-18T15:50:02+08:00', file_mtime: '2026-09-18T15:50:02.1+08:00', summary: {} })
  assert.equal(bare.marketCutoff, null)
  assert.equal(bare.marketCutoffFrom, null)
  assert.equal(bare.fileMtime, '2026-09-18 15:50:02')
})

test('时间：研究摘要与 Profit 用 market_cutoff_date，来源标注正确', () => {
  const list = rows()
  const digestMd = list.find(r => r.rawId === 'research_digest_md')
  const ec6c = list.find(r => r.jobId === 'ec6c86fd856c')
  assert.equal(digestMd.marketCutoffFrom, 'summary.market_cutoff_date')
  assert.equal(ec6c.marketCutoffFrom, 'summary.market_cutoff_date')
})
