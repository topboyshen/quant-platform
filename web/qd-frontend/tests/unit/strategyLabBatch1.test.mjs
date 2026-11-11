// 研究实验室第一批 · 参数链路专项测试（离线；node --test）
//
// 覆盖评审要求的验收点：
// - 参数一致性：量比周期 20 / 量比阈值 1.1 的分类与取值
// - 安全写回：只改目标默认值；约束、未编辑参数与其他源码逐字不变
// - 解析失败：明确拒绝，且**整次应用全或无**（不出现部分写回）
//
// 说明：断言都要能区分对错 —— 成功必须比对精确输出，失败必须比对原因与"原文未变"。
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isPercentParamName,
  isPeriodLikeParamName,
  isMultiplierLikeParamName,
  isExplicitPercentName,
  inferParamType,
  inferParamDefaults,
  normalizePercentParamValue,
  rewriteCtxParamDefault,
  buildScriptCodeWithParamValues,
  buildTemplateCode
} from '../../src/views/strategy-ide/components/scriptTemplateCatalog.js'

// ---------- 参数一致性 ----------
test('量比周期 20：不是百分比，取值为 20（值本身不被改写）', () => {
  assert.equal(isPeriodLikeParamName('vol_ratio_period'), true)
  assert.equal(isPercentParamName('vol_ratio_period', 20), false)
  assert.equal(inferParamType('vol_ratio_period', 20), 'integer')
  assert.equal(normalizePercentParamValue(20), 20)
  const defaults = inferParamDefaults('vol_ratio_period', 20, 'integer')
  assert.equal(defaults.default, 20)
  assert.notEqual(defaults.max, 1)          // 不得被塞进 0～1 区间
})

test('量比阈值 1.1：倍数语义，可大于 1', () => {
  assert.equal(isMultiplierLikeParamName('vol_ratio_threshold'), true)
  assert.equal(isPercentParamName('vol_ratio_threshold', 1.1), false)
  assert.equal(inferParamType('vol_ratio_threshold', 1.1), 'number')
})

test('allocation_pct 0.95：仍按既有契约处理为百分比（0～1）', () => {
  assert.equal(isExplicitPercentName('allocation_pct'), true)
  assert.equal(isPercentParamName('allocation_pct', 0.95), true)
  assert.equal(inferParamType('allocation_pct', 0.95), 'percent')
  const defaults = inferParamDefaults('allocation_pct', 0.95, 'percent')
  assert.equal(defaults.min, 0)
  assert.equal(defaults.max, 1)
})

// ---------- 安全写回：只改默认值 ----------
test('只替换默认值，min/max/step 逐字保留', () => {
  const source = "ctx.param('period', 20, min=2, max=100, step=2)"
  const result = rewriteCtxParamDefault(source, 'period', '30')
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, "ctx.param('period', 30, min=2, max=100, step=2)")
})

test('未编辑参数与其他源码逐字不变（仅目标默认值变化）', () => {
  const source = [
    'import math',
    "ctx.param('period', 20, min=2, max=100, step=2)",
    "ctx.param('ratio', 1.1)",
    'x = ctx.position_pct',
  ].join('\n')
  const result = rewriteCtxParamDefault(source, 'period', '50')
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, [
    'import math',
    "ctx.param('period', 50, min=2, max=100, step=2)",
    "ctx.param('ratio', 1.1)",
    'x = ctx.position_pct',
  ].join('\n'))
})

test('字符串默认值可替换，函数体不受影响', () => {
  const source = [
    "ctx.param('mode', 'auto')",
    'y = run(mode)',
  ].join('\n')
  const result = rewriteCtxParamDefault(source, 'mode', "'manual'")
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, [
    "ctx.param('mode', 'manual')",
    'y = run(mode)',
  ].join('\n'))
})

test('布尔默认值可替换', () => {
  const result = rewriteCtxParamDefault("ctx.param('armed', True)", 'armed', 'False')
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, "ctx.param('armed', False)")
})

// ---------- 解析失败：明确拒绝，且不截断 ----------
test('嵌套/复合默认值 → 拒绝，且原文逐字返回', () => {
  const nested = "ctx.param('price', max(price_a, price_b))"
  const r1 = rewriteCtxParamDefault(nested, 'price', '1.0')
  assert.equal(r1.ok, false)
  assert.ok(r1.reason.length > 0, '必须给出原因')
  assert.equal(r1.source, nested, '拒绝时必须返回原文')

  const composite = "ctx.param('period', 20 if x else 30, min=2)"
  const r2 = rewriteCtxParamDefault(composite, 'period', '30')
  assert.equal(r2.ok, false)
  assert.equal(r2.source, composite)
})

test('跨行调用：默认值 token 明确时可安全写回，且保留跨行版式', () => {
  const multiline = ["ctx.param('period',", '  20,', '  min=2)'].join('\n')
  const result = rewriteCtxParamDefault(multiline, 'period', '30')
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, ["ctx.param('period',", '  30,', '  min=2)'].join('\n'))
})

test('默认值本身跨行/含换行 → 拒绝，原文不变', () => {
  const ambiguous = ["ctx.param('period', 20 +", '  30, min=2)'].join('\n')
  const result = rewriteCtxParamDefault(ambiguous, 'period', '30')
  assert.equal(result.ok, false)
  assert.equal(result.source, ambiguous)
})

test('重复声明 → 拒绝（避免只改第一处），原文不变', () => {
  const dup = ["ctx.param('period', 20)", "ctx.param('period', 30)"].join('\n')
  const result = rewriteCtxParamDefault(dup, 'period', '40')
  assert.equal(result.ok, false)
  assert.match(result.reason, /重复|多次|声明/)
  assert.equal(result.source, dup)
})

test('目标不存在 → 拒绝并说明', () => {
  const source = "ctx.param('other', 1)"
  const result = rewriteCtxParamDefault(source, 'period', '30')
  assert.equal(result.ok, false)
  assert.match(result.reason, /未找到/)
  assert.equal(result.source, source)
})

// ---------- 整次应用：全或无 ----------
test('整次应用：全部成功 → 两个参数都被写入', () => {
  const params = [
    { name: 'vol_ratio_period', type: 'integer', default: 20 },
    { name: 'vol_ratio_threshold', type: 'number', default: 1.1 },
  ]
  const source = [
    "ctx.param('vol_ratio_period', 20, min=1)",
    "ctx.param('vol_ratio_threshold', 1.1)",
  ].join('\n')
  const result = buildScriptCodeWithParamValues(source, params, {
    vol_ratio_period: 30,
    vol_ratio_threshold: 1.2,
  })
  assert.equal(result.ok, true, result.reason)
  assert.equal(result.source, [
    "ctx.param('vol_ratio_period', 30, min=1)",
    "ctx.param('vol_ratio_threshold', 1.2)",
  ].join('\n'))
})

test('整次应用：后续参数失败 → 返回原文，绝不部分写回', () => {
  const params = [
    { name: 'vol_ratio_period', type: 'integer', default: 20 },
    { name: 'missing_param', type: 'number', default: 1 },
  ]
  const source = [
    "ctx.param('vol_ratio_period', 20, min=1)",
    "ctx.param('other', 1)",
  ].join('\n')
  const result = buildScriptCodeWithParamValues(source, params, { vol_ratio_period: 30, missing_param: 5 })
  assert.equal(result.ok, false)
  assert.ok(result.reason.length > 0)
  // 关键：第一个参数虽可成功，但整体失败时必须回到原文
  assert.equal(result.source, source, '失败必须完全不变，不得只改了一部分')
})

test('buildTemplateCode：失败返回 ok:false（不是空串冒充成功）', () => {
  // 源码里缺少 ghost_param 的 ctx.param 声明 → 整体拒绝
  const code = ["ctx.param('vol_ratio_period', 20, min=1)"].join('\n')
  const template = {
    key: 't',
    code,
    params: [
      { name: 'vol_ratio_period', type: 'integer', default: 20 },
      { name: 'ghost_param', type: 'number', default: 1 },
    ],
  }
  const failed = buildTemplateCode(template, { vol_ratio_period: 30 })
  assert.equal(failed.ok, false)
  assert.notEqual(failed.source, '', '失败不得返回空串冒充成功')
  assert.equal(failed.source, code, '失败必须返回原始模板源码')
  assert.match(failed.reason, /ghost_param/)

  const okTemplate = {
    key: 't2',
    code: "ctx.param('vol_ratio_period', 20, min=1)",
    params: [{ name: 'vol_ratio_period', type: 'integer', default: 20 }],
  }
  const ok = buildTemplateCode(okTemplate, { vol_ratio_period: 30 })
  assert.equal(ok.ok, true, ok.reason)
  assert.equal(ok.source, "ctx.param('vol_ratio_period', 30, min=1)")
})
