// 历史模块·真实组件调用链验收（六条重点）
// 当前 A／历史 B 不混用、缺字段不借值、详情失败有提示、
// 逆序响应不覆盖、退出后旧记录不复活、显式新回测仍受校验约束。
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import * as historyState from '../../src/views/backtest-center/historyState.js'
import * as contracts from '../../src/utils/strategyResearchContracts.mjs'
import { instantiateBacktestCenter, deferred } from './backtestCenterHarness.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SFC = path.resolve(here, '../../src/views/backtest-center/index.vue')

function build (api, overrides = {}) {
  return instantiateBacktestCenter({
    sfcPath: SFC,
    api,
    real: { historyState, contracts },
    overrides: {
      mode: 'portfolio',
      supportsFactorResearch: false,
      paramDefinitions: [{ name: 'period', default: 20 }],
      params: { period: 20 },
      runnableSources: [{ id: 7, symbol: 'A' }],
      form: { sourceId: 7, symbol: 'A', initialCapital: 12345, startDate: '2026-01-01', endDate: '2026-06-30', commission: 0.0002, slippage: 0, leverage: 1, leverageEnabled: false },
      ...overrides
    }
  })
}

const RUN_B = {
  id: 99,
  symbol: 'B-HIST',
  start_date: '2025-01-01',
  end_date: '2025-03-31',
  strategy_version: 'v4',
  initial_capital: 50000,
  source_id: 7,
  result: { equity_curve: [{ date: '2025-01-02', value: 50000 }], total_return: 0.1 }
}

test('当前 A／历史 B 不混用：查看历史不改当前草稿，页头身份取历史记录', async () => {
  const api = { getStrategyBacktestRun: async () => ({ data: RUN_B }) }
  const vm = await build(api)
  const before = JSON.stringify({ form: vm.form, params: vm.params })

  await vm.openRun({ id: 99 })

  assert.equal(JSON.stringify({ form: vm.form, params: vm.params }), before, '查看历史不得改写当前草稿/表单')
  assert.equal(vm.historyViewing, true)
  assert.equal(vm.historyIdentity.symbol, 'B-HIST', '身份必须是历史记录 B，不是当前 A')
  assert.match(vm.historyIdentity.range, /2025-01-01 ~ 2025-03-31/)
  assert.equal(vm.historyIdentity.version, 'v4')
  assert.equal(vm.historyIdentity.initialCapital, '50000')
  assert.notEqual(vm.historyIdentity.symbol, vm.form.symbol)
})

test('缺字段不借值：历史记录没有资金/区间/版本时显示未知，且不把当前表单资金传给结果区', async () => {
  const api = { getStrategyBacktestRun: async () => ({ data: { id: 100, symbol: 'B2', result: { total_return: 0.2 } } }) }
  const vm = await build(api)
  await vm.openRun({ id: 100 })

  assert.equal(vm.historyIdentity.initialCapital, '未知', '缺失必须显示未知，不得借当前表单 12345')
  assert.equal(vm.historyIdentity.range, '未知')
  assert.equal(vm.historyIdentity.version, '未知')
  assert.equal(vm.activeResultInitialCapital, undefined, '结果区不得借用当前表单资金')
  assert.notEqual(vm.activeResultInitialCapital, 12345)
  assert.notEqual(vm.historyIdentity.initialCapital, '0', '不得默认成零')
})

test('详情失败有提示：明确错误、结束加载、旧结果不冒充本次记录', async () => {
  const api = { getStrategyBacktestRun: async () => { throw new Error('detail boom') } }
  const vm = await build(api)
  vm.result = { total_return: 0.9 }   // 上一条记录的旧结果
  const ok = await vm.openRun({ id: 101, symbol: 'B3' })

  assert.equal(ok, false)
  assert.equal(vm.historyDetailError, 'detail boom', '必须有明确失败提示')
  assert.equal(vm.historyDetailLoading, false, '结束加载必须复位')
  assert.equal(vm.result, null, '旧结果不得冒充本次所选记录')
  assert.equal(vm.selectedRun.symbol, 'B3', '失败时身份仍可辨识')
})

test('详情加载中：明确表达加载状态，且不保留旧结果', async () => {
  const d = deferred()
  const vm = await build({ getStrategyBacktestRun: () => d.promise })
  vm.result = { total_return: 0.9 }
  const pending = vm.openRun({ id: 102 })
  assert.equal(vm.historyDetailLoading, true, '加载中必须明确')
  assert.equal(vm.result, null, '加载期间不得显示旧结果')
  d.resolve({ data: { id: 102, symbol: 'B4', result: { total_return: 0.3 } } })
  await pending
  assert.equal(vm.historyDetailLoading, false)
  assert.equal(vm.historyDetailError, '')
  assert.equal(vm.result.total_return, 0.3)
})

test('逆序响应不覆盖：先发的详情后到，界面保持最后一次选择', async () => {
  const first = deferred()
  const second = deferred()
  const calls = [first, second]
  let index = 0
  const vm = await build({ getStrategyBacktestRun: () => calls[index++].promise })

  const p1 = vm.openRun({ id: 201, symbol: 'B-first' })
  const p2 = vm.openRun({ id: 202, symbol: 'B-second' })
  second.resolve({ data: { id: 202, symbol: 'B-second', result: { total_return: 0.2 } } })
  await p2
  first.resolve({ data: { id: 201, symbol: 'B-first', result: { total_return: 0.9 } } })
  await p1

  assert.equal(vm.selectedRun.id, 202, '迟到响应不得覆盖最后一次选择')
  assert.equal(vm.historyIdentity.symbol, 'B-second')
  assert.equal(vm.result.total_return, 0.2)
})

test('退出后旧记录不复活：退出历史后到达的详情响应不得写回界面', async () => {
  const d = deferred()
  const vm = await build({ getStrategyBacktestRun: () => d.promise })
  const pending = vm.openRun({ id: 301, symbol: 'B-late' })
  vm.closeHistoryView()
  assert.equal(vm.historyViewing, false)
  d.resolve({ data: { id: 301, symbol: 'B-late', result: { total_return: 0.7 } } })
  await pending

  assert.equal(vm.selectedRun, null, '退出后迟到响应不得复活旧记录')
  assert.equal(vm.historyViewing, false)
  assert.equal(vm.result, null)
  assert.equal(vm.historyDetailLoading, false, '过期响应不得改动加载状态')
})

test('切换对象/切换源码让旧详情请求失效', async () => {
  const d = deferred()
  const vm = await build({ getStrategyBacktestRun: () => d.promise })
  const pending = vm.openRun({ id: 302, symbol: 'B-switch' })
  await vm.handleModeChange()
  d.resolve({ data: { id: 302, symbol: 'B-switch', result: { total_return: 0.5 } } })
  await pending
  assert.notEqual(vm.selectedRun && vm.selectedRun.symbol, 'B-switch', '切换对象后旧详情不得落到界面')
  assert.equal(vm.historyViewing, false)
})

test('显式新回测仍受校验约束：应用配置 + 无源码契约时报错阻止', async () => {
  const vm = await build({ getStrategyBacktestRun: async () => ({ data: RUN_B }) })
  await vm.openRun({ id: 99 })
  const applied = vm.useRunAsNewBacktest({ ...RUN_B, source_id: 0 })
  assert.equal(vm.historyViewing, false, '显式动作才退出历史视图')
  assert.equal(vm.params.period, 20)
  assert.equal(vm.form.initialCapital, 50000, '显式动作才把历史资金写入草稿')
  await applied
  assert.ok(vm.sourceLoadError, '缺少源码契约必须明确报错')
  assert.equal(vm.__messages.warning.length > 0, true, '必须向用户提示')
})

test('显式新回测：校验走既有 selectSource 路径（失败仍阻止）', async () => {
  const calls = []
  const api = new Proxy({ getStrategyBacktestRun: async () => ({ data: RUN_B }) }, {
    get (target, prop) {
      if (prop in target) return target[prop]
      return async (...args) => { calls.push({ name: String(prop), args }); throw new Error('compile failed') }
    }
  })
  const vm = await build(api, { runnableSources: [{ id: 7, symbol: 'A' }, { id: 9, symbol: 'B' }] })
  await vm.openRun({ id: 99 })
  await vm.useRunAsNewBacktest({ ...RUN_B, source_id: 9 })
  assert.ok(calls.length > 0, '必须走既有源码校验路径（真实调用后端校验接口）')
  assert.ok(calls.some(c => JSON.stringify(c.args).includes('9')), '校验要围绕该次运行的 source_id(9)')
  assert.equal(vm.historyViewing, false)
  // 编译失败后必须确实禁止运行（不是只弹个提示）
  assert.ok(vm.sourceLoadError, '编译失败必须留下明确错误')
  assert.equal(vm.runDisabled, true, '编译失败后必须禁止发起运行')
})

test('列表读取失败：保留旧列表并显式报错，不冒充空历史', async () => {
  const vm = await build({ getStrategyBacktestHistory: async () => { throw new Error('list boom') } })
  vm.portfolioHistory = [{ id: 1 }]
  const ok = await vm.loadHistory()
  assert.equal(ok, false)
  assert.equal(vm.historyError, 'list boom')
  assert.equal(vm.portfolioHistory.length, 1, '失败不得清空既有列表')
  assert.equal(vm.historyState, 'failed', '失败优先于空态')
  assert.equal(vm.historyLoading, false)
})

test('离开缓存页面后迟到响应不得复活：deactivated → 返回后仍为空', async () => {
  const d = deferred()
  const vm = await build({ getStrategyBacktestRun: () => d.promise })
  const pending = vm.openRun({ id: 401, symbol: 'B-row' })
  vm.deactivated()   // keep-alive 路由切换：不会执行 beforeDestroy
  assert.equal(vm.historyViewing, false)
  // 迟到的详情响应：载荷与所选行可区分，用于证明它没有被写回
  d.resolve({ data: { id: 401, symbol: 'B-late-response', result: { total_return: 0.9 } } })
  await pending
  if (typeof vm.activated === 'function') vm.activated()
  assert.equal(vm.result, null, '返回页面后迟到响应不得复活旧结果')
  assert.equal(vm.historyViewing, false, '迟到响应不得把界面切回历史视图')
  assert.equal(vm.selectedRun, null, '离开历史后清除历史身份，迟到响应不得复活它')
})

test('已加载历史离开再返回：旧结果和身份一并清除，不借当前资金重新展示', async () => {
  const history = { id: 100, symbol: 'B-NO-CAPITAL', result: { totalReturn: 12 } }
  const vm = await build({
    getStrategyBacktestRun: async () => ({ data: history }),
    getScriptSourceList: async () => ({ data: [{ id: 7, asset_type: 'script', symbol: 'A' }] })
  }, {
    decisionContextReady: true,
    incomingResearchEntryKey: 'source:7:',
    manifest: { strategyType: 'cta' },
    source: { id: 7, symbol: 'A' },
    $route: { query: { sourceId: 7 }, params: {} },
    syncDecisionContextFromSource: () => ({ symbol: 'A' })
  })
  await vm.openRun({ id: 100, symbol: 'B-NO-CAPITAL' })
  assert.equal(vm.result.totalReturn, 12)
  assert.equal(vm.activeResultInitialCapital, undefined)

  vm.deactivated()
  await vm.syncIncomingResearchEntry({ force: true }) // activated 的真实调用路径（同源码快捷分支）

  assert.equal(vm.historyViewing, false)
  assert.equal(vm.result, null, '缓存页返回后不得把历史结果当成当前结果')
  assert.equal(vm.selectedRun, null, '历史编号也不得挂到当前视图')
  assert.equal(vm.form.initialCapital, 12345, '当前草稿资金不受历史查看影响')
  assert.equal(vm.runError, '')
})

test('非历史当前结果离开缓存页：失效历史请求不删除当前回测结果', async () => {
  const vm = await build({})
  const currentResult = { totalReturn: 33 }
  const currentRun = { id: 200 }
  vm.result = currentResult
  vm.selectedRun = currentRun
  vm.historyViewing = false
  vm.deactivated()
  assert.equal(vm.result, currentResult)
  assert.equal(vm.selectedRun, currentRun)
})

test('显式使用历史配置：先捕获配置再清旧历史结果，失败仍留草稿并禁止运行', async () => {
  const vm = await build({ getStrategyBacktestRun: async () => ({ data: { ...RUN_B, source_id: 0 } }) })
  await vm.openRun({ id: 99 })
  await vm.useRunAsNewBacktest(vm.selectedRun)
  assert.equal(vm.form.initialCapital, 50000, '历史配置仍可显式应用，不能因清理而丢失')
  assert.equal(vm.historyViewing, false)
  assert.equal(vm.result, null, '旧历史结果不能挂在新的回测草稿下')
  assert.equal(vm.selectedRun, null)
  assert.ok(vm.sourceLoadError)
  assert.equal(vm.runDisabled, true)
})

test('成功详情无结果：进入历史空态，不虚构运行失败；真实失败仍保留错误', async () => {
  let response = { id: 103, symbol: 'B-EMPTY', status: 'success', result: {} }
  const vm = await build({ getStrategyBacktestRun: async () => ({ data: response }) })
  await vm.openRun({ id: 103 })
  assert.equal(vm.historyViewing, true)
  assert.equal(vm.result, null)
  assert.equal(vm.runError, '', '读取成功但无结果应走历史空态，不应进入失败分支')
  assert.equal(vm.historyDetailError, '')

  response = { id: 104, symbol: 'B-FAILED', status: 'failed', error_message: 'recorded run failed', result: {} }
  await vm.openRun({ id: 104 })
  assert.equal(vm.result, null)
  assert.equal(vm.runError, 'recorded run failed', '记录本身失败仍必须显示错误')
})

test('模板分区：历史空态必须“确实无结果”，且失败与空态互斥', () => {
  const raw = fs.readFileSync(SFC, 'utf8')
  // 有结果的历史必须能走到结果组件：空态要求 !activeResult
  assert.match(raw, /v-else-if="historyViewing && !activeResult"/)
  // 失败（含详情失败）单独成支，不能落到“没有结果”
  assert.match(raw, /v-else-if="runError \|\| historyDetailError"/)
  // 顶部当前研究对象是 decision-context-bar，且只在非历史视图显示
  assert.match(raw, /<decision-context-bar[\s\S]{0,160}?v-if="!historyViewing"/)
  // 配置面板不再被当成身份隔离手段
  assert.doesNotMatch(raw, /class="panel config-panel" v-if="!historyViewing"/)
})
