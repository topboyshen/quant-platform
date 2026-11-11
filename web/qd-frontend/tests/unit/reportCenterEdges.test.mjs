// 报告中心边界回归；只使用本地合成数据，不读取生产或网络数据。
// Vue 模板仅作源码结构断言；提取方法在隔离上下文中执行，均不能替代页面运行时验证。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  PAGE_STATE_COPY, normalizeReports, filterReports, resolveSelection, findRow,
  timeFieldsOf
} from '../../src/utils/reportCatalog.js'

const PREFERRED_KEY = 'job:ec6c86fd856c'
const SAFE_KEY = 'family:research_digest:json'
const safeReport = {
  id: 'research_digest_json', label: '独立研究摘要', format: 'json', status: 'ok',
  generated_at: '2026-09-18T15:00:00+08:00', summary: {}
}
const duplicateReports = () => [
  {
    label: '盈利研究（ec6c86fd856c/latest.md）', format: 'markdown', status: 'ok',
    generated_at: '2026-09-18T15:00:00+08:00', detail: '唯一搜索词甲', summary: {}
  },
  {
    label: '盈利研究（ec6c86fd856c/latest.md）', format: 'markdown', status: 'ok',
    generated_at: '2026-09-18T15:00:00+08:00', detail: '唯一搜索词乙', summary: {}
  }
]
const normalized = reports => normalizeReports({ reports })
const rawRow = (key, readable = true, extra = {}) => ({ key, readable, ...extra })
const vueSource = readFileSync(new URL('../../src/views/personal-reports/index.vue', import.meta.url), 'utf8')
const vueTemplate = vueSource.slice(0, vueSource.indexOf('<script>'))

// 只执行同步方法本身，不挂载 Vue，不调用组件的请求、导航或其他副作用。
function isolatedSyncSelection (resolve) {
  const method = vueSource.match(/\n {4}syncSelection \(\) \{([\s\S]*?)\n {4}\},/)
  assert.ok(method, '应能提取 syncSelection 同步方法')
  return new Function('resolveSelection', `return function () {${method[1]}\n}`)(resolve)
}

test('页面状态词表：全部六种状态提供正确的 tone', () => {
  const expected = {
    loading: 'blue', available: 'green', partial: 'orange', empty: 'grey',
    unavailable: 'red', unauthorized: 'red'
  }
  for (const [state, tone] of Object.entries(expected)) {
    assert.equal(PAGE_STATE_COPY[state].tone, tone, state)
    assert.ok(PAGE_STATE_COPY[state].label)
  }
})

test('Vue 源码结构：状态标签和横幅均绑定 stateCopy.tone（非运行时测试）', () => {
  const stateTag = vueTemplate.match(/<a-tag\b[^>]*data-testid="report-state"[^>]*>/)?.[0]
  const bannerTag = vueTemplate.match(/<div\b[^>]*data-testid="report-banner"[^>]*>/)?.[0]
  assert.ok(stateTag, '应存在页面状态标签')
  assert.ok(bannerTag, '应存在页面状态横幅')
  assert.match(stateTag, /:color="antColor\(stateCopy\.tone\)"/)
  assert.match(bannerTag, /:class="'banner-'\s*\+\s*stateCopy\.tone"/)
  assert.doesNotMatch(vueTemplate, /stateCopy\.color/)
})

test('Vue 源码结构：selectionNote 独立于 !selected 分支渲染（非运行时测试）', () => {
  const readerStart = vueTemplate.indexOf('data-testid="report-reader"')
  assert.ok(readerStart >= 0, '应存在报告阅读面板')
  const readerTemplate = vueTemplate.slice(readerStart)
  const note = readerTemplate.match(/<([a-z][\w-]*)\b[^>]*v-if="selectionNote"[^>]*>\s*\{\{\s*selectionNote\s*\}\}\s*<\/\1>/)
  assert.ok(note, '应有独立显示 selectionNote 的条件节点')
  const emptyBranch = readerTemplate.search(/v-if="!selected(?:\s*&&\s*!selectionNote)?"/)
  assert.ok(emptyBranch >= 0, '应保留未选中报告的空态')
  assert.ok(note.index < emptyBranch, '提示应位于 !selected 分支之前，有回退报告时仍可见')
})

for (const [label, failure] of [
  ['目录契约未成功读取', { contractOk: false }],
  ['未通过鉴权', { unauthorized: true }],
  ['请求返回错误', { error: 'HTTP 503' }]
]) {
  test(`Vue 隔离方法：${label}时保留选择键且不判定原报告消失`, () => {
    let resolveCalls = 0
    const syncSelection = isolatedSyncSelection(() => {
      resolveCalls += 1
      return { selectedKey: null, note: '原选中项已不在当前列表中', fellBack: true }
    })
    for (const selectedKey of [PREFERRED_KEY, null]) {
      const state = {
        contractOk: true, unauthorized: false, error: '', filteredRows: [],
        selectedKey, selectionNote: '旧选择提示', ...failure
      }
      syncSelection.call(state)
      assert.equal(resolveCalls, 0, '读取失败时不应使用空目录执行选择失效判断')
      assert.equal(state.selectedKey, selectedKey, '保留选择键以便读取恢复后继续选择')
      assert.equal(state.selectionNote, '报告目录读取未成功，暂不判定选中项是否失效。')
      assert.doesNotMatch(state.selectionNote, /已不在|已切换/)
    }
  })
}

test('Vue 隔离方法：有效空目录仍判定原选择失效，且不误报已切换', () => {
  let resolveCalls = 0
  const syncSelection = isolatedSyncSelection((rows, key) => {
    resolveCalls += 1
    return resolveSelection(rows, key)
  })
  const state = {
    contractOk: true, unauthorized: false, error: '', filteredRows: [],
    selectedKey: PREFERRED_KEY, selectionNote: '读取失败提示'
  }
  syncSelection.call(state)
  assert.equal(resolveCalls, 1)
  assert.equal(state.selectedKey, null)
  assert.match(state.selectionNote, /原选中项已不在/)
  assert.doesNotMatch(state.selectionNote, /已切换|暂不判定/)
})

test('Vue 隔离方法：读取恢复后继续原选择，并清除暂不判定提示', () => {
  const syncSelection = isolatedSyncSelection(resolveSelection)
  const state = {
    contractOk: true, unauthorized: false, error: '',
    filteredRows: [rawRow(PREFERRED_KEY), rawRow(SAFE_KEY)],
    selectedKey: PREFERRED_KEY, selectionNote: '报告目录读取未成功，暂不判定选中项是否失效。'
  }
  syncSelection.call(state)
  assert.equal(state.selectedKey, PREFERRED_KEY)
  assert.equal(state.selectionNote, '')
})

test('规范化：相同 job 键的所有条目都标注 keyAmbiguous', () => {
  const rows = normalized([...duplicateReports(), safeReport])
  const duplicates = rows.filter(row => row.key === PREFERRED_KEY)
  assert.equal(duplicates.length, 2)
  for (const row of duplicates) {
    assert.equal(row.keyAmbiguous, true)
    assert.equal(row.needsAttention, true)
    assert.ok(row.keyReason, '重复键必须解释为何不可安全识别')
  }
  assert.notEqual(rows.find(row => row.key === SAFE_KEY).keyAmbiguous, true)
})

test('规范化：家族与格式相同造成的重复键也标注歧义', () => {
  const rows = normalized([safeReport, { ...safeReport, label: '另一份同格式摘要' }])
  assert.deepEqual(rows.map(row => row.key), [SAFE_KEY, SAFE_KEY])
  assert.ok(rows.every(row => row.keyAmbiguous === true))
  assert.equal(findRow(rows, SAFE_KEY), null)
})

test('规范化：标题后备键重复时同样不可选中', () => {
  const rows = normalized([
    { label: '无稳定标识的报告', status: 'ok' },
    { label: '无稳定标识的报告', status: 'ok' }
  ])
  assert.ok(rows.every(row => row.keyAmbiguous === true))
  assert.equal(findRow(rows, 'title:无稳定标识的报告'), null)
  assert.equal(resolveSelection(rows, 'title:无稳定标识的报告').selectedKey, null)
})

test('查找：没有规范化标记的重复键也不能返回第一条', () => {
  const rows = [rawRow('duplicate'), rawRow('duplicate', false)]
  assert.equal(findRow(rows, 'duplicate'), null)
  assert.equal(findRow([...rows].reverse(), 'duplicate'), null)
})

test('查找：null、undefined、空字符串键不能匹配缺标识条目', () => {
  const rows = [rawRow(null), rawRow(undefined), rawRow(''), rawRow('safe')]
  for (const key of [null, undefined, '']) assert.equal(findRow(rows, key), null)
  assert.equal(findRow(rows, 'missing'), null)
  assert.equal(findRow(null, 'safe'), null)
  assert.equal(findRow(undefined, 'safe'), null)
})

test('查找：唯一可读取和不可读取条目都可以明确查找', () => {
  const readable = rawRow('readable')
  const unavailable = rawRow('unavailable', false)
  const rows = [readable, unavailable]
  assert.equal(findRow(rows, 'readable'), readable)
  assert.equal(findRow(rows, 'unavailable'), unavailable)
})

test('筛选：只留下原重复项之一时，歧义标记和查找拒绝仍保留', () => {
  const rows = normalized([...duplicateReports(), safeReport])
  const filtered = filterReports(rows, { query: '唯一搜索词甲' })
  assert.equal(filtered.length, 1)
  assert.equal(filtered[0].keyAmbiguous, true)
  assert.equal(findRow(filtered, PREFERRED_KEY), null)
  assert.equal(resolveSelection(filtered, PREFERRED_KEY).selectedKey, null)
  assert.equal(resolveSelection(filtered, null).selectedKey, null)
})

test('选择：即使当前只有一行，也不能选择已知 keyAmbiguous 的键', () => {
  const rows = [rawRow('ambiguous', true, { keyAmbiguous: true })]
  assert.equal(findRow(rows, 'ambiguous'), null)
  for (const wanted of ['ambiguous', 'vanished', null]) {
    const selection = resolveSelection(rows, wanted)
    assert.equal(selection.selectedKey, null)
    assert.ok(selection.note)
    assert.doesNotMatch(selection.note, /已切换/)
  }
})

test('选择：明确请求重复键时不能按数组位置选中任一重复项', () => {
  const rows = [rawRow('duplicate'), rawRow('duplicate', false)]
  for (const list of [rows, [...rows].reverse()]) {
    const selection = resolveSelection(list, 'duplicate')
    assert.equal(selection.selectedKey, null)
    assert.ok(selection.note)
    assert.doesNotMatch(selection.note, /已切换/)
  }
})

test('选择：默认优先项重复时跳过它，选择唯一可读取报告', () => {
  const rows = normalized([...duplicateReports(), safeReport])
  const selection = resolveSelection(rows, null)
  assert.equal(selection.selectedKey, SAFE_KEY)
  assert.equal(findRow(rows, selection.selectedKey)?.readable, true)
})

test('选择：默认选择也拒绝未标记的重复键和空键', () => {
  const rows = [
    rawRow(null), rawRow('duplicate'), rawRow('duplicate'),
    rawRow('unavailable', false), rawRow('safe')
  ]
  assert.equal(resolveSelection(rows, null).selectedKey, 'safe')
})

test('选择：唯一可读取的盈利研究仍为默认优先项', () => {
  const rows = [rawRow('safe'), rawRow(PREFERRED_KEY)]
  const selection = resolveSelection(rows, null)
  assert.equal(selection.selectedKey, PREFERRED_KEY)
  assert.equal(selection.fellBack, false)
})

test('选择：唯一且不可读取的显式选择仍保留，以展示错误原因', () => {
  const rows = [rawRow('safe'), rawRow('unavailable', false)]
  const selection = resolveSelection(rows, 'unavailable')
  assert.deepEqual(selection, { selectedKey: 'unavailable', note: '', fellBack: false })
})

test('选择：原选择消失时，跳过歧义和不可读取行后回退并提示', () => {
  const rows = [
    rawRow('duplicate'), rawRow('duplicate'),
    rawRow('known-ambiguous', true, { keyAmbiguous: true }),
    rawRow('unavailable', false), rawRow(null), rawRow('safe')
  ]
  const selection = resolveSelection(rows, 'vanished')
  assert.equal(selection.selectedKey, 'safe')
  assert.equal(selection.fellBack, true)
  assert.match(selection.note, /原选中项/)
  assert.match(selection.note, /已切换/)
})

test('选择：原选择消失且无安全回退时，不能声称已切换', () => {
  for (const rows of [[], [rawRow('unavailable', false)], [rawRow(null)]]) {
    const selection = resolveSelection(rows, 'vanished')
    assert.equal(selection.selectedKey, null)
    assert.match(selection.note, /原选中项/)
    assert.doesNotMatch(selection.note, /已切换/)
    assert.match(selection.note, /没有|无|无法/)
  }
})

test('选择：全部条目缺键、重复或不可读取时不产生默认选择', () => {
  const rows = [
    rawRow(null), rawRow(undefined), rawRow(''),
    rawRow('duplicate'), rawRow('duplicate'), rawRow('unavailable', false)
  ]
  const selection = resolveSelection(rows, null)
  assert.equal(selection.selectedKey, null)
  assert.ok(selection.note)
  assert.doesNotMatch(selection.note, /已切换/)
})

test('选择：非数组输入安全返回无选择', () => {
  for (const rows of [null, undefined, {}]) {
    const selection = resolveSelection(rows, 'vanished')
    assert.equal(selection.selectedKey, null)
    assert.ok(selection.note)
    assert.doesNotMatch(selection.note, /已切换/)
  }
})

test('行情截止：market_cutoff_date 优先于 market_cutoff 和 trade_date，来源精确', () => {
  const time = timeFieldsOf({
    id: 'eod_daily_report',
    summary: {
      market_cutoff_date: '2026-09-18',
      market_cutoff: '2026-09-17T15:00:00+08:00',
      trade_date: '2026-09-16'
    }
  })
  assert.equal(time.marketCutoff, '2026-09-18')
  assert.equal(time.marketCutoffRaw, '2026-09-18')
  assert.equal(time.marketCutoffFrom, 'summary.market_cutoff_date')
})

test('行情截止：缺少 market_cutoff_date 时使用 market_cutoff 并保留其来源', () => {
  const time = timeFieldsOf({
    id: 'eod_daily_report',
    summary: { market_cutoff: '2026-09-18T15:00:00+08:00', trade_date: '2026-09-17' }
  })
  assert.equal(time.marketCutoff, '2026-09-18 15:00:00')
  assert.equal(time.marketCutoffRaw, '2026-09-18T15:00:00+08:00')
  assert.equal(time.marketCutoffFrom, 'summary.market_cutoff')
})

test('行情截止：收盘日报仅有 trade_date 时来源为 summary.trade_date', () => {
  const time = timeFieldsOf({ id: 'eod_daily_report', summary: { trade_date: '2026-09-18' } })
  assert.equal(time.marketCutoff, '2026-09-18')
  assert.equal(time.marketCutoffFrom, 'summary.trade_date')
})

test('行情截止：非收盘日报不会把 trade_date 当作截止日', () => {
  const time = timeFieldsOf({ ...safeReport, summary: { trade_date: '2026-09-18' } })
  assert.equal(time.marketCutoff, null)
  assert.equal(time.marketCutoffRaw, null)
  assert.equal(time.marketCutoffFrom, null)
})

test('行情截止：空显式字段依次回退到有效字段且来源与值一致', () => {
  const market = timeFieldsOf({
    id: 'eod_daily_report',
    summary: { market_cutoff_date: '', market_cutoff: '2026-09-18', trade_date: '2026-09-17' }
  })
  assert.equal(market.marketCutoff, '2026-09-18')
  assert.equal(market.marketCutoffFrom, 'summary.market_cutoff')
  const trade = timeFieldsOf({
    id: 'eod_daily_report', summary: { market_cutoff_date: null, market_cutoff: '', trade_date: '2026-09-17' }
  })
  assert.equal(trade.marketCutoff, '2026-09-17')
  assert.equal(trade.marketCutoffFrom, 'summary.trade_date')
})

test('行情截止：缺少业务截止日时不借生成时间、文件时间或读取时间', () => {
  for (const summary of [null, {}, { market_cutoff_date: null, market_cutoff: null }]) {
    const time = timeFieldsOf({
      id: 'eod_daily_report', summary,
      generated_at: '2026-09-18T15:00:00+08:00',
      file_mtime: '2026-09-21T13:00:00.123+08:00',
      observed_at: '2026-09-21T14:00:00+08:00'
    })
    assert.equal(time.marketCutoff, null)
    assert.equal(time.marketCutoffRaw, null)
    assert.equal(time.marketCutoffFrom, null)
    assert.equal(time.fileMtime, '2026-09-21 13:00:00')
  }
})

test('规范化：行情截止来源精确保留到展示字段', () => {
  const [row] = normalized([{ ...safeReport, summary: { market_cutoff_date: '2026-09-18' } }])
  assert.equal(row.marketCutoff, '2026-09-18')
  assert.equal(row.marketCutoffFrom, 'summary.market_cutoff_date')
})
