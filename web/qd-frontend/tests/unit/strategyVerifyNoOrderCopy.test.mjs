import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const strategyEditorPath = fileURLToPath(
  new URL('../../src/views/strategy-ide/components/StrategyEditor.vue', import.meta.url)
)
const zhPath = fileURLToPath(new URL('../../src/locales/lang/zh-CN.js', import.meta.url))
const enPath = fileURLToPath(new URL('../../src/locales/lang/en-US.js', import.meta.url))
const strategyIdePath = fileURLToPath(new URL('../../src/views/strategy-ide/index.vue', import.meta.url))

const strategyEditor = fs.readFileSync(strategyEditorPath, 'utf8')
const zh = fs.readFileSync(zhPath, 'utf8')
const en = fs.readFileSync(enPath, 'utf8')
const strategyIde = fs.readFileSync(strategyIdePath, 'utf8')

const ZH_COPY = '未检测到交易动作；结构验证通过不代表执行审核通过。'

test('no-order-intent copy is present in both locales', () => {
  assert.match(
    zh,
    new RegExp(`"strategyIde\\.verifyHints\\.NO_ORDER_INTENT":\\s*"${ZH_COPY}"`)
  )
  assert.match(
    en,
    /"strategyIde\.verifyHints\.NO_ORDER_INTENT":\s*"No trade action detected; passing structural validation does not mean execution review approval\."/
  )
})

test('verify shows the copy as advisory info, never as a blocker', () => {
  assert.match(
    strategyEditor,
    /message\.info\(\s*this\.\$t\('strategyIde\.verifyHints\.NO_ORDER_INTENT'\)\s*\)/
  )
  assert.doesNotMatch(
    strategyEditor,
    /message\.error\([\s\S]{0,240}NO_ORDER_INTENT/
  )
})

test('advisory copy only appears after a successful structural verification', () => {
  const validBranch = strategyEditor.indexOf('message.success(this.$t(\'trading-assistant.editor.verifySuccess\'))')
  const advisory = strategyEditor.indexOf("message.info(this.$t('strategyIde.verifyHints.NO_ORDER_INTENT'))")
  const elseBranch = strategyEditor.indexOf('} else {', validBranch)
  assert.ok(validBranch > -1, 'verify success branch must exist')
  assert.ok(advisory > validBranch, 'advisory must follow the success message')
  assert.ok(elseBranch > advisory, 'advisory must stay inside the valid branch')
  assert.match(strategyEditor, /verification\.success === true \|\| verification\.valid === true/)
  assert.match(strategyEditor, /message\.error\(\s*verification\.message \|\|/)
})

test('verification remains structure-only: no order or execution gate added', () => {
  for (const forbidden of [
    'ctx.buy',
    'ctx.sell',
    'ctx.close_position',
    'order_intent_required',
    'execution_approved'
  ]) {
    assert.equal(strategyEditor.includes(forbidden), false, `${forbidden} must not gate verification`)
  }
  // The hint list is only inspected for the single advisory code.
  const inspected = strategyEditor.match(/hint\.code === '([A-Z_]+)'/g) || []
  assert.deepEqual(inspected, ["hint.code === 'NO_ORDER_INTENT'"])
  assert.match(strategyIde, /formatVerifyHint/)
})
