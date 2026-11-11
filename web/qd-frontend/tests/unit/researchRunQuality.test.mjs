import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import * as quality from '../../src/utils/researchRunQuality.js'

const { createRunQualityReader, newQualityState, qualityRunProjection, qualityRoute, qualityResponse, qualityError, qualityValue, researchQualityAccount } = quality
const project = { id: 'rp1', name: '东方电气研究', subject: { symbol: '600875', name: '东方电气', market: 'CNStock', timeframe: '1D' } }
const branch = { id: 'rb1', name: '主线', projectId: 'rp1' }
const run = { id: 'rr1', branchId: 'rb1', status: 'succeeded', resultArtifactId: 'ra1', effectiveConfig: { startDate: '2026-01-01', endDate: '2026-10-01' }, effectiveConfigSha256: 'config-sha', capturedSha256: 'capture-sha' }
const artifact = { id: 'ra1', kind: 'run_result', branchId: 'rb1', payloadSha256: 'payload-sha', createdAt: '2026-10-01T12:00:00+08:00', payload: { requestId: 'rr1', dataRange: { requested: ['2025-01-01', '2025-02-01'], actual: ['2026-01-02', '2026-09-30'] }, dataFingerprint: 'data-sha', dataVersion: 'unknown' } }
const ok = data => ({ code: 1, data })
const deferred = () => { let resolve; let reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function setup (overrides = {}) {
  let account = '1'
  const state = newQualityState()
  const calls = []
  const base = {
    capabilities: async () => ok({ enabled: true, read: true, write: false, run: false }),
    listProjects: async () => ok([project]), getProject: async () => ok({ project, branches: [branch] }),
    getBranch: async () => ok(branch), listRuns: async () => ok([run]), getRun: async () => ok({ run }), getArtifact: async () => ok(artifact)
  }
  const api = Object.fromEntries(Object.entries({ ...base, ...overrides }).map(([name, fn]) => [name, (...args) => { calls.push([name, ...args]); return fn(...args) }]))
  const reader = createRunQualityReader({ state, api, account: () => account })
  return { state, reader, calls, setAccount: id => { account = id } }
}
async function chooseBranch (ctx) { await ctx.reader.load(); await ctx.reader.selectProject('rp1'); await ctx.reader.selectBranch('rb1') }

test('projects, branches and runs retain their own identity and request vs actual dates', () => {
  const view = qualityRunProjection(project, branch, run, artifact)
  assert.equal(view.symbol, '600875'); assert.equal(view.runId, 'rr1')
  assert.deepEqual(view.requested, ['2026-01-01', '2026-10-01']); assert.deepEqual(view.actual, ['2026-01-02', '2026-09-30'])
  assert.equal(view.dataFingerprint, 'data-sha'); assert.equal(view.resultSavedAt, artifact.createdAt)
})
test('range endpoints never assert complete daily coverage or zero missing bars', () => {
  const view = qualityRunProjection(project, branch, run, artifact)
  assert.equal(view.coverage, null); assert.equal(view.missingBars, null)
  assert.equal(view.dataVersion, null); assert.equal(view.metricsVersion, null); assert.equal(view.adjustment, null)
})
test('missing result fields cannot borrow global summary, sync time or request dates', () => {
  const view = qualityRunProjection(project, branch, run, { ...artifact, payload: { requestId: run.id, sync_at: 'now', globalCoverage: 100 } })
  assert.deepEqual(view.actual, [null, null]); assert.equal(view.dataFingerprint, null)
  assert.equal(view.hasResult, true); assert.equal(view.coverage, null)
})
test('run without result stays unknown even if succeeded', () => {
  const view = qualityRunProjection(project, branch, { ...run, resultArtifactId: null }, null)
  assert.equal(view.hasResult, false); assert.equal(view.dataFingerprint, null); assert.deepEqual(view.actual, [null, null])
})
test('invalid calendar dates are unknown rather than claimed actual coverage', () => {
  const view = qualityRunProjection(project, branch, { ...run, effectiveConfig: { startDate: '2026-02-29', endDate: '2026-13-01' } }, { ...artifact, payload: { requestId: run.id, dataRange: { actual: ['2026-04-31', '2026-00-01'] } } })
  assert.deepEqual(view.requested, [null, null]); assert.deepEqual(view.actual, [null, null])
})
test('cross-project branch and cross-branch run are refused', () => {
  assert.throws(() => qualityRunProjection(project, { ...branch, projectId: 'other' }, run, artifact), /身份不一致/)
  assert.throws(() => qualityRunProjection(project, branch, { ...run, branchId: 'other' }, artifact), /身份不一致/)
})
test('artifact id, kind, branch and request are all checked', () => {
  for (const changed of [{ id: 'other' }, { kind: 'legacy_import' }, { branchId: 'other' }, { payload: { requestId: 'other' } }]) {
    assert.throws(() => qualityRunProjection(project, branch, run, { ...artifact, ...changed }), /快照.*身份不一致/)
  }
})
test('URL never accepts array or object ids as identity', () => {
  assert.deepEqual(qualityRoute({ projectId: ['rp1'], branchId: {}, runId: 'rr1', symbol: 'bad' }), { projectId: '', branchId: '', runId: 'rr1' })
})
test('result artifact must match the selected execution when run supplies execution id', () => {
  const selected = { ...run, execution: { id: 'exec-original' } }
  assert.throws(() => qualityRunProjection(project, branch, selected, artifact), /执行身份不一致/)
  assert.throws(() => qualityRunProjection(project, branch, selected, { ...artifact, payload: { ...artifact.payload, executionId: 'exec-other' } }), /执行身份不一致/)
  assert.equal(qualityRunProjection(project, branch, selected, { ...artifact, payload: { ...artifact.payload, executionId: 'exec-original' } }).runId, run.id)
})
test('failed API envelope cannot become empty success', () => {
  for (const reply of [{ code: 0, data: [] }, { data: [] }, { code: 1 }, null]) assert.throws(() => qualityResponse(reply))
  assert.deepEqual(qualityResponse(ok([])), [])
})
test('account ids are checked and zero values do not become unknown', () => {
  assert.equal(researchQualityAccount({ id: 1 }), '1'); assert.equal(researchQualityAccount({ id: '2' }), '2')
  for (const id of [0, -1, '0', '01', {}, 1.5]) assert.equal(researchQualityAccount({ id }), '')
  assert.equal(qualityValue(0), 0); assert.equal(qualityValue(null), '未知')
})
test('auth failures clear data while ordinary failures remain failures', () => {
  assert.equal(qualityError({ response: { status: 401 } }).permission, 'login')
  assert.equal(qualityError({ response: { status: 403 } }).denied, true)
  assert.equal(qualityError({ response: { status: 404 } }).denied, true)
  assert.equal(qualityError(new Error('network failed')).message, 'network failed')
})
test('reader uses only capabilities and GET list APIs, no automatic selection', async () => {
  const ctx = setup(); await ctx.reader.load()
  assert.deepEqual(ctx.calls.map(row => row[0]), ['capabilities', 'listProjects'])
  assert.equal(ctx.state.project, null); assert.equal(ctx.state.branch, null); assert.equal(ctx.state.run, null)
})
test('selecting existing project -> branch -> run reads exactly that run artifact', async () => {
  const ctx = setup(); await chooseBranch(ctx); await ctx.reader.selectRun('rr1')
  assert.equal(ctx.state.run.id, 'rr1'); assert.equal(ctx.state.artifact.id, 'ra1')
  assert.deepEqual(ctx.calls.map(row => row[0]), ['capabilities', 'listProjects', 'getProject', 'listRuns', 'getRun', 'getArtifact'])
})
test('runId link derives project and branch from authorized server responses, not URL labels', async () => {
  const ctx = setup(); await ctx.reader.load({ runId: 'rr1' })
  assert.equal(ctx.state.project.id, 'rp1'); assert.equal(ctx.state.branch.id, 'rb1'); assert.equal(ctx.state.run.id, 'rr1')
  assert.deepEqual(ctx.calls.map(row => row[0]), ['capabilities', 'listProjects', 'getRun', 'getBranch', 'getProject', 'listRuns', 'getArtifact'])
})
test('conflicting project or branch in link cannot silently select another identity', async () => {
  for (const query of [{ runId: 'rr1', projectId: 'rp-other' }, { runId: 'rr1', branchId: 'rb-other' }]) {
    const ctx = setup(); await ctx.reader.load(query)
    assert.match(ctx.state.error, /不一致/); assert.equal(ctx.state.run, null); assert.equal(ctx.state.artifact, null)
  }
})
test('project A response cannot write project B after a fast switch', async () => {
  const late = deferred(); const project2 = { ...project, id: 'rp2' }; const branch2 = { ...branch, id: 'rb2', projectId: 'rp2' }
  const ctx = setup({ listProjects: async () => ok([project, project2]), getProject: id => id === 'rp1' ? late.promise : Promise.resolve(ok({ project: project2, branches: [branch2] })) })
  await ctx.reader.load(); const first = ctx.reader.selectProject('rp1'); await ctx.reader.selectProject('rp2')
  late.resolve(ok({ project, branches: [branch] })); await first
  assert.equal(ctx.state.project.id, 'rp2'); assert.equal(ctx.state.branches[0].id, 'rb2'); assert.equal(ctx.state.loading, false)
})
test('late branch failure and finally cannot overwrite the current branch request', async () => {
  const a = deferred(); const b = deferred(); const branch2 = { ...branch, id: 'rb2' }
  const ctx = setup({ getProject: async () => ok({ project, branches: [branch, branch2] }), listRuns: id => id === 'rb1' ? a.promise : b.promise })
  await ctx.reader.load(); await ctx.reader.selectProject('rp1')
  const first = ctx.reader.selectBranch('rb1'); const second = ctx.reader.selectBranch('rb2')
  a.reject(new Error('old branch failed')); await first
  assert.equal(ctx.state.error, ''); assert.equal(ctx.state.loading, true)
  b.resolve(ok([{ ...run, branchId: 'rb2', id: 'rr2' }])); await second
  assert.equal(ctx.state.branch.id, 'rb2'); assert.equal(ctx.state.error, ''); assert.equal(ctx.state.loading, false)
})
test('late run detail and artifact cannot revive a previous selection', async () => {
  const a = deferred(); const run2 = { ...run, id: 'rr2', resultArtifactId: null }
  const ctx = setup({ listRuns: async () => ok([run, run2]), getRun: id => id === 'rr1' ? a.promise : Promise.resolve(ok({ run: run2 })) })
  await chooseBranch(ctx); const first = ctx.reader.selectRun('rr1'); await ctx.reader.selectRun('rr2')
  a.resolve(ok({ run })); await first
  assert.equal(ctx.state.run.id, 'rr2'); assert.equal(ctx.state.artifact, null)
  assert.equal(ctx.calls.some(row => row[0] === 'getArtifact'), false)
})
test('deactivated page clears state and ignores late completion', async () => {
  const late = deferred(); const ctx = setup({ getRun: () => late.promise })
  await chooseBranch(ctx); const pending = ctx.reader.selectRun('rr1'); ctx.reader.deactivate()
  late.resolve(ok({ run })); await pending
  assert.equal(ctx.state.run, null); assert.equal(ctx.state.projects.length, 0); assert.equal(ctx.state.loading, false)
})
test('account replacement and invalidation clear every old-user selection', async () => {
  const late = deferred(); const ctx = setup({ getRun: () => late.promise })
  await chooseBranch(ctx); const pending = ctx.reader.selectRun('rr1'); ctx.setAccount('2'); ctx.reader.invalidate()
  late.resolve(ok({ run })); await pending
  assert.equal(ctx.state.project, null); assert.equal(ctx.state.branches.length, 0); assert.equal(ctx.state.runs.length, 0); assert.equal(ctx.state.artifact, null)
})
test('401 and 403 while loading artifact clear projects and do not show empty-success', async () => {
  for (const status of [401, 403]) {
    const ctx = setup({ getArtifact: async () => { throw { response: { status } } } })
    await chooseBranch(ctx); await ctx.reader.selectRun('rr1')
    assert.equal(ctx.state.projects.length, 0); assert.equal(ctx.state.run, null); assert.equal(ctx.state.caps, null)
    assert.equal(ctx.state.permission, status === 401 ? 'login' : 'denied'); assert.ok(ctx.state.error); assert.equal(ctx.state.loading, false)
  }
})
test('list request failure is explicit and cannot be rendered as an empty catalog', async () => {
  const ctx = setup({ listProjects: async () => { throw new Error('project reading failed') } }); await ctx.reader.load()
  assert.equal(ctx.state.error, 'project reading failed'); assert.equal(ctx.state.projects.length, 0); assert.equal(ctx.state.loading, false)
})
test('capability refusal does not call project or run reads', async () => {
  const ctx = setup({ capabilities: async () => ok({ enabled: true, read: false }) }); await ctx.reader.load({ runId: 'rr1' })
  assert.equal(ctx.calls.length, 1); assert.equal(ctx.state.permission, 'denied'); assert.equal(ctx.state.run, null)
})
test('foreign run and corrupted artifact fail closed', async () => {
  const wrong = setup({ getRun: async () => ok({ run: { ...run, branchId: 'wrong' } }) }); await chooseBranch(wrong); await wrong.reader.selectRun('rr1')
  assert.match(wrong.state.error, /身份不一致/); assert.equal(wrong.state.run, null)
  const changed = setup({ getArtifact: async () => ok({ ...artifact, payload: { requestId: 'another-run' } }) }); await chooseBranch(changed); await changed.reader.selectRun('rr1')
  assert.match(changed.state.error, /身份不一致/); assert.equal(changed.state.artifact, null)
})
test('component observes session and keep-alive changes and never calls write endpoints', () => {
  const source = fs.readFileSync(new URL('../../src/components/ResearchRunQuality/index.vue', import.meta.url), 'utf8')
  assert.match(source, /data-testid="research-run-quality"/); assert.match(source, /deactivated \(\) \{ this.reader.deactivate\(\) \}/)
  assert.match(source, /SET_TOKEN/); assert.match(source, /accountId \(\)/)
  assert.match(source, /from '@\/api\/unifiedResearch'/)
  assert.doesNotMatch(source, /setLastOpened|startRun|reconcileRun|putDraft|aiChat|createProject|setHeads/)
  assert.match(source, /日期端点和内容指纹只标识本次结果/)
})
