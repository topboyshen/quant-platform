// Research Lab V2 workspace: pure logic + feature-flag wiring. Offline; node --test.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  classifyError, createLatestGuard, isStrategyWorkEmpty, newIdempotencyKey, paramRows,
  parseParamComments, runDisplay, saveStateLabel, STALE_LABELS, workEquals
} from '../../src/views/research-lab-v2/workspaceLogic.js'

const ROUTER = readFileSync(new URL('../../src/config/router.config.js', import.meta.url), 'utf8')
const VIEW = readFileSync(new URL('../../src/views/research-lab-v2/index.vue', import.meta.url), 'utf8')

test('save state never claims saved while there are unsaved edits', () => {
  assert.equal(saveStateLabel({ dirty: true, draft: { draftRevision: 3 } }).tone, 'warn')
  assert.match(saveStateLabel({ dirty: true }).text, /未保存修改/)
  assert.match(saveStateLabel({ draft: { draftRevision: 2 } }).text, /草稿已保存 rev 2（未确认）/)
  assert.match(saveStateLabel({ draft: { draftRevision: 2, keptAfterConfirm: true, confirmedAsArtifactId: 'ra_x' } }).text, /已确认为快照 ra_x · 草稿 rev 2 仍保留/)
  assert.equal(saveStateLabel({ confirmedId: 'ra_1' }).text, '与已确认内容一致')
})

test('inherited review is labeled as old branch evidence, not a changed run', () => {
  assert.match(STALE_LABELS.INHERITED_REVIEW, /继承自父分支/)
  assert.notEqual(STALE_LABELS.INHERITED_REVIEW, STALE_LABELS.RUN_CHANGED)
})

test('latest-request guard drops out-of-order responses', () => {
  const g = createLatestGuard()
  const a = g.next()
  const b = g.next()
  assert.equal(g.isLatest(a), false)
  assert.equal(g.isLatest(b), true)
})

test('idempotency keys are fresh per click and within 16-100 chars', () => {
  const a = newIdempotencyKey()
  const b = newIdempotencyKey()
  assert.notEqual(a, b)
  assert.ok(a.length >= 16 && a.length <= 100)
})

test('error classification: network / conflict / disabled / forbidden', () => {
  assert.equal(classifyError(new Error('Network Error')).kind, 'network')
  assert.equal(classifyError({ response: { status: 409, data: { error: { type: 'REVISION_CONFLICT' }, data: { diff: [] } } } }).kind, 'conflict')
  assert.equal(classifyError({ response: { status: 403, data: { error: { type: 'FEATURE_DISABLED' } } } }).kind, 'disabled')
  assert.equal(classifyError({ response: { status: 403, data: {} } }).kind, 'forbidden')
})

test('run labels: pending reconcile, not adopted, late result, interrupted', () => {
  assert.ok(runDisplay({ status: 'running', displayState: 'pending_reconcile' }).some(l => /待对账：尚未证明已停止/.test(l.text)))
  assert.ok(runDisplay({ status: 'succeeded', adoption: 'not_adopted' }).some(l => l.text === '未设为当前结果'))
  assert.ok(runDisplay({ status: 'interrupted', lateResults: [{ notice: '迟到结果：已记录但未应用' }] }).some(l => /迟到结果/.test(l.text)))
  assert.ok(runDisplay({ status: 'running', reconcileSuggested: true }).some(l => /租约已过期 ≠ 已停止/.test(l.text)))
})

test('param comments parse and rows keep 0/false', () => {
  const specs = parseParamComments('# @param period int 20 周期 range=5:250:1\n# @param show bool false 显示')
  assert.deepEqual(specs.map(s => [s.name, s.default, s.min, s.max]), [['period', 20, 5, 250], ['show', false, null, null]])
  const rows = paramRows(specs, { period: 60 }, { period: 0, show: false }, {})
  assert.equal(rows[0].applied, 0)
  assert.equal(rows[0].confirmed, 60)
  assert.equal(rows[1].applied, false)
})

test('handoff arrival helpers: empty / equal work', () => {
  assert.equal(isStrategyWorkEmpty({ handoffArtifactId: null, mapped: null, mappingConfirmed: false, code: null }), true)
  assert.equal(isStrategyWorkEmpty({ mapped: 57 }), false)
  assert.equal(workEquals({ handoffArtifactId: 'a', mapped: 40 }, { handoffArtifactId: 'a', mapped: 40, mappingConfirmed: false, code: null }), true)
})

test('feature flag: route only registered when VUE_APP_RESEARCH_LAB_V2 === "1"; page falls back to legacy pages', () => {
  assert.match(ROUTER, /process\.env\.VUE_APP_RESEARCH_LAB_V2 === '1' \? RESEARCH_LAB_V2_ROUTES : \[\]/)
  assert.match(VIEW, /研究实验室 V2 未对当前账号开放/)
  for (const p of ['/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center']) assert.ok(VIEW.includes(`to="${p}"`))
})

// stage 2b (item a) deliberately changed this rule: the model is called ONLY from the explicit click handler
// (was: never called). Navigation/review-context paths are asserted model-free in researchLabV2Stage2b.test.mjs.
test('workspace calls a model only from the explicit click handler and never auto-reruns on timeout', () => {
  assert.equal((VIEW.match(/api\.aiChat\(/g) || []).length, 1)
  assert.ok(VIEW.slice(VIEW.indexOf('async callReviewModel ()'), VIEW.indexOf('async archiveModelReview ()')).includes('api.aiChat('))
  assert.match(VIEW, /按同一幂等键查询（不会重复计算）/)
  assert.match(VIEW, /对账（不会重跑）/)
})
