import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { portfolioChartLayout } from '../../src/views/backtest-center/chartLayout.js'

const root = dirname(fileURLToPath(new URL('../../package.json', import.meta.url)))
const read = name => readFileSync(join(root, name), 'utf8')

test('narrow chart layout keeps series reachable and does not keep desktop fixed gutters', () => {
  const narrow = portfolioChartLayout(390)
  const tablet = portfolioChartLayout(768)
  const desktop = portfolioChartLayout(1440)
  assert.equal(narrow.bucket, 'narrow')
  assert.equal(narrow.legend.type, 'scroll')
  assert.equal(narrow.showAxisName, false)
  assert.ok(narrow.grid.every(item => item.containLabel === true))
  assert.ok(narrow.grid.every(item => item.left < 58 && item.right < 56))
  assert.equal(tablet.bucket, 'tablet')
  assert.equal(tablet.grid[0].left, 58)
  assert.equal(tablet.grid[0].right, 56)
  assert.equal(desktop.legend.left, 8)
  assert.equal(desktop.grid[0].height, 245)
  assert.equal(desktop.grid[1].top, 325)
  assert.equal(desktop.grid[2].top, 455)
  assert.equal(portfolioChartLayout(0).bucket, 'desktop')
})

test('history identity wraps without changing unknown values or borrowing the form', () => {
  const page = read('src/views/backtest-center/index.vue')
  assert.match(page, /class="history-identity__item"/)
  assert.match(page, /\.history-identity \{[^}]*flex-wrap:\s*wrap/)
  assert.match(page, /displayHistoryValue \(value\) \{[\s\S]*?value === '未知'/)
  assert.match(page, /symbol: symbol \? String\(symbol\) : '未知'/)
  assert.match(page, /initialCapital: capital === undefined \|\| capital === null \|\| capital === '' \? '未知'/)
  assert.match(page, /grid-template-columns: minmax\(0, 1fr\)/)
  assert.match(page, /\.result-panel \{[^}]*min-width: 0/)
})

test('history read failure has its own copy and is not shown twice', () => {
  const page = read('src/views/backtest-center/index.vue')
  assert.match(page, /v-else-if="runError \|\| historyDetailError"[\s\S]*?data-testid="backtest-error"/)
  assert.match(page, /historyDetailError \? \$t\('backtest-center\.history\.detailFailed'\) : \$t\('backtest-center\.result\.failed'\)/)
  assert.equal(page.match(/data-testid="backtest-history-detail-error"/g).length, 1)
  assert.doesNotMatch(page, /data-testid="backtest-history-detail-error"[\s\S]{0,400}data-testid="backtest-error"/)
  assert.match(page, /v-if="historyError"[\s\S]*?:message="\$t\('backtest-center\.history\.listFailed'\)"[\s\S]*?:description="historyError"/)
  assert.match(page, /v-if="historyError && history\.length"[\s\S]*?backtest-center\.history\.staleList/)
  assert.match(page, /失败时保留已有列表/)
})

test('new history copy exists in both Chinese and English', () => {
  const zh = read('src/locales/lang/zh-CN.js')
  const en = read('src/locales/lang/en-US.js')
  const keys = [
    'backtest-center.history.symbol',
    'backtest-center.history.range',
    'backtest-center.history.version',
    'backtest-center.history.initialCapital',
    'backtest-center.history.unknown',
    'backtest-center.history.detailFailed',
    'backtest-center.history.listFailed',
    'backtest-center.history.staleList'
  ]
  for (const key of keys) {
    assert.match(zh, new RegExp(`"${key}": "`))
    assert.match(en, new RegExp(`"${key}": "`))
  }
  assert.match(zh, /"backtest-center\.history\.staleList": "显示的是上次成功加载的记录，不是本次读取结果"/)
  assert.match(en, /"backtest-center\.history\.detailFailed": "Could not read this run's details"/)
  assert.doesNotMatch(zh, /"backtest-center\.history\.detailFailed": "回测失败/)
})
