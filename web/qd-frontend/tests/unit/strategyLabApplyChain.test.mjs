// 应用参数整链专项测试（离线；node --test）
//
// 评审验收点（三组收紧）：
// 1) 五种声明形式分别处理：函数调用（ctx.param / params.get / context.params.get）与注释声明
//    （# @param / # @strategy）不得混为一谈；复杂表达式无法安全修改 → 整次拒绝，不新增语法支持。
// 2) “只改已编辑参数”按**真实变化**判定：编辑器可能传入全部参数，未变化项必须逐字保留；
//    0 / false / '' 是真实值，不得当成缺失。
// 3) 整次应用失败 → 返回原文，不得留下部分修改。
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildScriptCodeWithParamValues,
  paramValueChanged,
  readParamValueFromSource
} from '../../src/views/strategy-ide/components/scriptTemplateCatalog.js'

const P = (name, def, type = 'number') => ({ name, default: def, type })

// ---------- 形式一：ctx.param（函数调用，只替换默认值，保留约束） ----------
test('ctx.param：只改默认值，min/max/step 逐字保留', () => {
  const src = "period = ctx.param('period', 20, min=5, max=120, step=1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('period', 20)], { period: 30 })
  assert.equal(r.ok, true)
  assert.equal(r.source, "period = ctx.param('period', 30, min=5, max=120, step=1)\n")
})

// ---------- 形式二：params.get（函数调用；此前被强制要求 ctx.param 而误拒） ----------
test('params.get：仅由 params.get 声明的参数可以写回，不再被要求存在 ctx.param', () => {
  const src = "thr = params.get('vol_ratio_threshold', 1.1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('vol_ratio_threshold', 1.1)], { vol_ratio_threshold: 1.5 })
  assert.equal(r.ok, true, 'params.get 声明的参数不应被拒绝')
  assert.equal(r.source, "thr = params.get('vol_ratio_threshold', 1.5)\n")
})

test('context.params.get：由更具体的 marker 处理，不被 params.get 重复扫描', () => {
  const src = "thr = context.params.get('vol_ratio_threshold', 1.1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('vol_ratio_threshold', 1.1)], { vol_ratio_threshold: 1.5 })
  assert.equal(r.ok, true)
  assert.equal(r.source, "thr = context.params.get('vol_ratio_threshold', 1.5)\n")
})

test('params.get：复杂默认值（含括号）无法安全修改 → 整次拒绝且返回原文', () => {
  const src = "thr = params.get('thr', compute_default(1.1, mode='fast'))\n"
  const r = buildScriptCodeWithParamValues(src, [P('thr', 1.1)], { thr: 1.5 })
  assert.equal(r.ok, false, '复杂表达式必须拒绝，不能改坏后返回成功')
  assert.equal(r.source, src, '拒绝时必须是原文')
  assert.match(r.reason, /简单字面量|拒绝/)
})

test('params.get：跨行调用但 token 明确 → 安全写回且保留换行缩进', () => {
  const src = "thr = params.get('thr',\n  1.1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('thr', 1.1)], { thr: 1.5 })
  assert.equal(r.ok, true)
  assert.equal(r.source, "thr = params.get('thr',\n  1.5)\n", '只替换 token，不得破坏换行与缩进')
})

// ---------- 形式三/四：注释声明（保持既有支持范围，尾随内容保留） ----------
test('# @param 注释声明：仅由注释声明的参数可写回，行尾注释逐字保留', () => {
  const src = '# @param fast_period int 7  # 快速均线周期\nclose.rolling(fast_period)\n'
  const r = buildScriptCodeWithParamValues(src, [P('fast_period', 7, 'integer')], { fast_period: 9 })
  assert.equal(r.ok, true, '注释声明的参数不应被拒绝')
  assert.match(r.source, /# @param fast_period int 9  # 快速均线周期/)
})

test('# @strategy 注释声明：冒号形式可写回，首尾不变', () => {
  const src = '"""x"""\n# @strategy fast_period: 7\nbody()\n'
  const r = buildScriptCodeWithParamValues(src, [P('fast_period', 7, 'integer')], { fast_period: 9 })
  assert.equal(r.ok, true)
  assert.match(r.source, /# @strategy fast_period: 9/)
  assert.match(r.source, /^"""x"""\n/)
})

test('注释声明形式不改动其他行（含括号、引号、中文注释）', () => {
  const src = '# @param p int 1\nx = f("a)b", [1, 2])  # 保留我\n'
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, true)
  assert.equal(r.source, '# @param p int 2\nx = f("a)b", [1, 2])  # 保留我\n')
})

// ---------- 真实变化判定：未编辑项逐字保留 ----------
test('编辑器传入全部参数时，只有真实变化的那一项被改写', () => {
  const src = "a = ctx.param('a', 20.0)\nb = ctx.param('b', 1.1)\n"
  // 两个参数都作为“已编辑值”传入，但只有 b 真的变了
  const r = buildScriptCodeWithParamValues(src, [P('a', 20), P('b', 1.1)], { a: 20, b: 1.5 })
  assert.equal(r.ok, true)
  assert.equal(r.source, "a = ctx.param('a', 20.0)\nb = ctx.param('b', 1.5)\n", '未变化的 a 必须逐字保留（20.0 不得被改写成 20）')
})

test('未提供覆盖值的参数完全不参与写回（不做默认值回填）', () => {
  const src = "a = ctx.param('a', 20.0)\n"
  const r = buildScriptCodeWithParamValues(src, [P('a', 20)], {})
  assert.equal(r.ok, true)
  assert.equal(r.source, src)
})

test('未编辑参数的空白与书写风格逐字保留', () => {
  const src = "a = ctx.param( 'a' ,  20 , min = 5 )\nb = ctx.param('b', 1.1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('a', 20), P('b', 1.1)], { a: 20, b: 1.5 })
  assert.equal(r.ok, true)
  assert.match(r.source, /a = ctx\.param\( 'a' ,  20 , min = 5 \)/, '未编辑参数的内部空白必须保留')
})

test('0 / false / 空字符串是真实值，不得当成缺失', () => {
  assert.equal(paramValueChanged('5', 0), true, '0 是真实值')
  assert.equal(paramValueChanged('True', false), true, 'false 是真实值')
  assert.equal(paramValueChanged("'x'", ''), true, "'' 是真实值")
  assert.equal(paramValueChanged('0', 0), false)
  assert.equal(paramValueChanged('False', false), false)
  assert.equal(paramValueChanged("''", ''), false)
})

test('参数值为 0 时确实写回（不被当作缺失跳过）', () => {
  const src = "m = ctx.param('mode_flag', 5)\n"
  const r = buildScriptCodeWithParamValues(src, [P('mode_flag', 5, 'integer')], { mode_flag: 0 })
  assert.equal(r.ok, true)
  assert.equal(r.source, "m = ctx.param('mode_flag', 0)\n")
})

test('读取当前声明值：函数调用优先于注释声明', () => {
  const src = "# @param p int 7\nv = ctx.param('p', 9)\n"
  const cur = readParamValueFromSource(src, 'p')
  assert.equal(cur.ok, true)
  assert.equal(cur.form, 'ctx.param')
  assert.equal(cur.text, '9')
})

// ---------- 全或无 ----------
test('后续参数失败时，前面参数的部分修改不得残留（返回原文）', () => {
  const src = "a = ctx.param('a', 1)\nb = params.get('b', compute(2, k=3))\nc = ctx.param('c', 3)\n"
  const r = buildScriptCodeWithParamValues(src, [P('a', 1, 'integer'), P('b', 2), P('c', 3, 'integer')], { a: 9, b: 8, c: 7 })
  assert.equal(r.ok, false)
  assert.equal(r.source, src, '整次失败必须返回原文，a 的修改不得残留')
  assert.equal(r.source.includes("ctx.param('a', 9)"), false)
})

test('参数在源码中完全没有受支持的声明形式 → 整次拒绝（不静默跳过）', () => {
  const src = "a = ctx.param('a', 1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('a', 1, 'integer'), P('ghost', 2, 'integer')], { a: 1, ghost: 2 })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
  assert.match(r.reason, /ghost/)
})

// ---------- 安全边界:定位阶段不得误改普通文本 ----------
test('字符串字面量里的 ctx.param(...) 不是代码,不得被改写', () => {
  const src = "doc = \"ctx.param('p', 1)\"\nv = ctx.param('p', 1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, true, r.reason)
  assert.equal(r.source, "doc = \"ctx.param('p', 1)\"\nv = ctx.param('p', 2)\n")
})

test('普通注释示例里的 ctx.param(...) 不得被当成可写声明', () => {
  const src = "# 示例：ctx.param('p', 1)\nz = 1\n"
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, false, '注释示例不是声明,应整次拒绝')
  assert.equal(r.source, src)
})

test('otherctx.param(...) 等非完整对象名不得被改写', () => {
  const src = 'otherctx.param(\'p\', 1)\n'
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
  assert.equal(r.source.includes("otherctx.param('p', 2)"), false)
})

test('xxx.params.get(...) 不属于 params.get 声明,不得被改写', () => {
  const src = "a = other.params.get('p', 1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
})

// ---------- 安全边界:“找到了但边界坏”不得跳过 ----------
test('前面目标调用未闭合 + 后面另有同名调用 → 整次拒绝,不改后面那个', () => {
  const src = "ctx.param('p', 1\nv = ctx.param('p', 1)\n"
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, false, '未闭合调用必须整次拒绝,不得跳过后只改后者')
  assert.equal(r.source, src, '拒绝时必须返回原文')
  assert.match(r.reason, /未闭合|边界/)
})

test('目标调用没有默认值参数 → 明确拒绝(不静默跳过)', () => {
  const src = "ctx.param('p')\n"
  const r = buildScriptCodeWithParamValues(src, [P('p', 1, 'integer')], { p: 2 })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
})

// ---------- 安全边界:注释形式无法表达的值 ----------
test('# @param 注释声明写入空字符串 → 明确拒绝,不删除值 token', () => {
  const src = '# @param mode str auto\nx()\n'
  const r = buildScriptCodeWithParamValues(src, [{ name: 'mode', type: 'str', default: 'auto' }], { mode: '' })
  assert.equal(r.ok, false, '注释语法无法表达空字符串,必须拒绝')
  assert.equal(r.source, src)
  assert.match(r.source, /# @param mode str auto/, '值 token 不得被删除')
})

test('# @strategy 注释声明写入空字符串 → 明确拒绝', () => {
  const src = '# @strategy mode: auto\nx()\n'
  const r = buildScriptCodeWithParamValues(src, [{ name: 'mode', type: 'str', default: 'auto' }], { mode: '' })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
})

test('含空白/# 的文本同样无法用注释 token 表达 → 拒绝', () => {
  const src = '# @param mode str auto\nx()\n'
  const r = buildScriptCodeWithParamValues(src, [{ name: 'mode', type: 'str', default: 'auto' }], { mode: 'my mode' })
  assert.equal(r.ok, false)
  assert.equal(r.source, src)
})

test('# @param 注释声明写入普通 token 仍然正常', () => {
  const src = '# @param mode str auto\nx()\n'
  const r = buildScriptCodeWithParamValues(src, [{ name: 'mode', type: 'str', default: 'auto' }], { mode: 'fast' })
  assert.equal(r.ok, true, r.reason)
  assert.equal(r.source, '# @param mode str fast\nx()\n')
})

test('函数调用形式写入空字符串仍可正确表达', () => {
  const src = "m = ctx.param('mode', 'auto')\n"
  const r = buildScriptCodeWithParamValues(src, [{ name: 'mode', type: 'str', default: 'auto' }], { mode: '' })
  assert.equal(r.ok, true, r.reason)
  assert.equal(r.source, "m = ctx.param('mode', '')\n")
})
