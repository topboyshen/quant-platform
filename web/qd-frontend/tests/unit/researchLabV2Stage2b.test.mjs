import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { chartPaths, reviewArchivable } from '../../src/views/research-lab-v2/workspaceLogic.js'

const vue = readFileSync(new URL('../../src/views/research-lab-v2/index.vue', import.meta.url), 'utf8')
const apiSrc = readFileSync(new URL('../../src/api/researchLabV2.js', import.meta.url), 'utf8')

test('chartPaths draws server series as-is, gaps (warm-up nulls) break the line', () => {
  const g = chartPaths({ close: [1, 2, 3, 4], series: { ma: [null, null, 2, 3] } }, 100, 50, 0)
  assert.deepEqual(g.lines.map(l => l.name), ['close', 'ma'])
  assert.equal(g.min, 1); assert.equal(g.max, 4)
  assert.equal(g.lines[0].d, 'M0.0,50.0L33.3,33.3L66.7,16.7L100.0,0.0')
  assert.equal(g.lines[1].d, 'M66.7,33.3L100.0,16.7')
  assert.equal(g.lines[1].last, 3)
  assert.deepEqual(chartPaths(null).lines, [])
})

test('model review is archivable only against the run + context it was produced for', () => {
  const r = { runId: 'rq_1', contextSha256: 'a', reply: 'x' }
  assert.equal(reviewArchivable(r, { runId: 'rq_1', contextSha256: 'a' }), true)
  assert.equal(reviewArchivable(r, { runId: 'rq_2', contextSha256: 'a' }), false)
  assert.equal(reviewArchivable(r, { runId: 'rq_1', contextSha256: 'b' }), false)
  assert.equal(reviewArchivable(null, { runId: 'rq_1' }), false)
})

test('model is called only from the explicit click handler, never on navigation', () => {
  const calls = [...vue.matchAll(/api\.aiChat\(/g)]
  assert.equal(calls.length, 1)
  const handler = vue.slice(vue.indexOf('async callReviewModel ()'), vue.indexOf('async archiveModelReview ()'))
  assert.ok(handler.includes('api.aiChat('))
  assert.match(vue, /data-testid="rl2-review-call" @click="callReviewModel"/)
  for (const hook of ['async mounted ()', 'async loadBranch (bid)', 'async toReview (run)', 'goModule (']) {
    const i = vue.indexOf(hook)
    if (i < 0) continue
    const body = vue.slice(i, vue.indexOf('\n    },', i))
    assert.ok(!body.includes('aiChat') && !body.includes('callReviewModel'), hook)
  }
  assert.match(apiSrc, /url: '\/api\/ai\/chat\/message', method: 'post'/)
})

test('archive of model output is labelled as model output and carries the context sha; import never re-runs', () => {
  assert.match(vue, /contentSource: 'model_output_relayed_by_client', contextSha256: r\.contextSha256/)
  assert.match(apiSrc, /legacyImport = \(pid, body\) => call\('post', `\/projects\/\$\{pid\}\/legacy-imports`/)
  const importing = vue.slice(vue.indexOf('async importLate ('), vue.indexOf('// ---------------------------------------------------------------- indicator'))
  assert.match(importing, /const context = this\.captureBranchActionContext\(\)/)
  assert.match(importing, /api\.legacyImport\(context\.projectId, \{ backtestRunId: lr\.backtestRunId \}\)/)
  assert.match(importing, /isCurrent: current/)
  assert.doesNotMatch(importing, /api\.(startRun|retryRun|aiChat)\(/)
  const previewing = vue.slice(vue.indexOf('async computeChart ('), vue.indexOf('previewIdentity ()'))
  assert.match(previewing, /const indicatorId = this\.ind\.indicatorId/)
  assert.match(previewing, /api\.indicatorPreview\(context\.branchId, \{ indicatorId, applied, bars: 250 \}\)/)
  assert.match(previewing, /isCurrent: current/)
})
