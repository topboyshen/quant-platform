// 回测中心 · 历史列表状态与乱序保护专项测试（离线；node --test）
//
// 评审验收点：
// - 请求失败不冒充空历史（failed 优先于 empty）；
// - 迟到响应不覆盖最后一次选择。
import test from 'node:test'
import assert from 'node:assert/strict'

import { historyViewState, shouldAcceptResponse, effectiveSelection } from '../../src/views/backtest-center/historyState.js'

// ---------- 三态优先级 ----------
test('读取中优先：有旧数据也显示 loading', () => {
  assert.equal(historyViewState({ loading: true, error: '', count: 5 }), 'loading')
})

test('失败优先于空：读取失败且列表为空 → failed，不是 empty', () => {
  assert.equal(historyViewState({ loading: false, error: 'boom', count: 0 }), 'failed')
})

test('失败优先于有数据：读取失败但仍有旧列表 → failed', () => {
  assert.equal(historyViewState({ loading: false, error: 'boom', count: 7 }), 'failed')
})

test('确实为空才显示 empty', () => {
  assert.equal(historyViewState({ loading: false, error: '', count: 0 }), 'empty')
})

test('无错误且有数据 → ready', () => {
  assert.equal(historyViewState({ loading: false, error: '', count: 3 }), 'ready')
})

test('缺省输入按空态处理，不抛错', () => {
  assert.equal(historyViewState(), 'empty')
  assert.equal(historyViewState({}), 'empty')
})

// ---------- 乱序保护 ----------
test('只有最后一次请求的响应被接受', () => {
  assert.equal(shouldAcceptResponse(2, 2), true)
  assert.equal(shouldAcceptResponse(1, 2), false, '迟到的旧响应必须被丢弃')
})

test('选择相邻连点：最后一次点击胜出', () => {
  // 依次发起 1、2 两次选择，最终只有 2 生效
  assert.equal(effectiveSelection(1, 2), null, '第一次选择已被第二次取代')
  assert.equal(effectiveSelection(2, 2), 2)
})

test('无后续选择时，本次选择正常生效', () => {
  assert.equal(effectiveSelection(5, 5), 5)
})

test('序号以数字比较（避免字符串比较失误）', () => {
  assert.equal(shouldAcceptResponse('3', 3), true)
  assert.equal(shouldAcceptResponse(3, '4'), false)
})
