import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import Vue from 'vue'
import { compileTemplate } from 'vue/compiler-sfc'
import * as helpers from '../../src/utils/nng8ResearchLink.js'
import { newIdempotencyKey, researchStorageKey } from '../../src/views/research-lab-v2/workspaceLogic.js'
import { harness as workspace, clone, deferred, httpError } from './researchWorkflowBatch1Harness.mjs'

const source = readFileSync(new URL('../../src/components/Nng8ResearchLink/index.vue', import.meta.url), 'utf8')
const script = source.split('<script>')[1].split('</script>')[0]
  .replace(/import \* as api from '@\/api\/researchLabV2'\s*/, '')
  .replace(/import \{[^}]+\} from '@\/utils\/nng8ResearchLink'\s*/, '')
  .replace(/import \{[^}]+\} from '@\/views\/research-lab-v2\/workspaceLogic'\s*/, '')
  .replace('export default {', 'module.exports = {')
const hash = 'a'.repeat(64)
const stamp = '2026-10-10T09:30:00+08:00'
const report = () => ({ id: 'nng8:astock:2026-10-10:' + hash, content_sha256: hash, source: 'nng8/astock', status: 'ok', title: '合成晨报', report_date: '2026-10-10', generated_at: null, data_cutoff: null, body_truncated: false, body_block_count: 1, blocks: [{ heading: '原文', text: '合成内容' }] })
const capabilities = () => ({ enabled: true, write: true, externalEvidence: { enabled: true, schemaReady: true, capture: true, contractVersion: 'research.external-evidence.v1' } })
const project = () => ({ id: 'P', name: '合成单股', status: 'active', defaultBranchId: 'A', subject: { kind: 'single', market: 'CNStock', timeframe: '1D', symbol: '600875' } })
const reference = (artifactId = 'E') => ({ artifactId, source: 'nng8', reportId: report().id, contentSha256: hash, capturedAt: stamp })
const evidence = (id = 'E') => ({ id, branchId: 'N', kind: 'external_evidence', origin: 'server_captured', payload: { ...reference(id), sourceTime: null, generatedAt: null, marketCutoff: null, subject: project().subject, blocks: report().blocks } })

function linkHarness () {
  const calls = [], navigation = [], subscriptions = []
  const record = (name, ...args) => calls.push({ name, args: clone(args) })
  const api = {
    capabilities: async () => { record('capabilities'); return { data: capabilities() } },
    listProjects: async () => { record('listProjects'); return { data: [project()] } },
    getProject: async id => { record('getProject', id); return { data: { project: project() } } },
    createProject: async (body, key) => { record('createProject', body, key); return { data: { project: { ...project(), name: body.name, subject: body.subject } } } },
    getBranch: async id => { record('getBranch', id); return { data: { id: 'A', projectId: 'P', revision: 3 } } },
    createBranch: async (pid, body) => { record('createBranch', pid, body); return { data: { branch: { id: 'N', projectId: 'P', name: body.name, revision: 1, heads: {} } } } },
    captureExternalEvidence: async (bid, body) => { record('captureExternalEvidence', bid, body); return { data: { artifact: evidence(), branch: { id: 'N', projectId: 'P', name: '合成新分支', revision: 2, heads: { evidence: 'E' } } } } },
    externalHandoff: async body => { record('externalHandoff', body); return { data: { project: project(), artifact: evidence(), branch: { id: 'N', projectId: 'P', name: body.branchName, revision: 2, heads: { evidence: 'E' } } } } }
  }
  const module = { exports: {} }
  vm.runInNewContext(script, { module, api, ...helpers, newIdempotencyKey, JSON })
  const component = module.exports
  const page = { ...component.data(), ...component.methods, report: report(), capturedAt: stamp, accountId: '101', sourceReady: true,
    $router: { push: route => { navigation.push(clone(route)); return Promise.resolve() } },
    $store: { state: { app: { theme: 'light' } }, subscribe: callback => { subscriptions.push(callback); return () => {} } } }
  for (const [name, getter] of Object.entries(component.computed)) Object.defineProperty(page, name, { get: () => getter.call(page) })
  component.created.call(page)
  return { page, component, calls, api, navigation, subscriptions, record }
}
async function ready (h, choice = 'P') {
  await h.page.open(); h.page.projectChoice = choice; h.page.selectProject()
  h.page.projectName = '合成项目'; h.page.symbol = '600875'; h.page.confirmed = true
  assert.ok(h.page.canConfirm)
}

test('source identity only accepts complete astock report; macro/ledger/truncated/hash mismatch rejected', () => {
  assert.deepEqual(helpers.reportIdentity(report(), stamp), { reportId: report().id, contentSha256: hash, capturedAt: stamp })
  for (const edit of [{ source: 'nng8/macro' }, { id: 'ledger' }, { status: 'stale' }, { status: 'failed' }, { status: 'unknown' }, { status: null }, { blocks: [], body_block_count: 0 }, { body_truncated: true }, { body_truncated: null }, { body_block_count: 2 }, { content_sha256: 'b'.repeat(64) }]) assert.equal(helpers.reportIdentity({ ...report(), ...edit }, stamp), null)
  assert.equal(helpers.reportIdentity(report(), '2026-10-10'), null)
})
test('opening and selecting are read-only; unconfirmed submit has no writes or navigation', async () => {
  const h = linkHarness(); await h.page.open(); h.page.projectChoice = 'P'; h.page.selectProject(); await h.page.submit()
  assert.deepEqual(h.calls.map(c => c.name), ['capabilities', 'listProjects']); assert.deepEqual(h.navigation, [])
})
test('explicit existing-project confirmation atomically captures evidence into a new branch', async () => {
  const h = linkHarness(); await ready(h); await h.page.submit()
  assert.equal(h.page.completed, true)
  const captured = h.calls.find(c => c.name === 'externalHandoff').args[0]
  assert.equal(captured.target.parentBranchId, 'A'); assert.equal(captured.target.expectedParentRevision, 3)
  assert.deepEqual(Object.keys(captured).sort(), ['capturedAt', 'clientRequestId', 'contentSha256', 'target', 'branchName', 'reportId'].sort())
  assert.equal(captured.capturedAt, stamp)
  assert.ok(!h.calls.some(c => ['createProject', 'createBranch', 'captureExternalEvidence'].includes(c.name)))
  assert.deepEqual(h.navigation, []); h.page.openWorkspace()
  assert.deepEqual(h.navigation, [{ path: '/research-lab', query: { project: 'P', branch: 'N', module: 'ai' } }])
  assert.ok(!h.calls.some(c => /Draft|Run|Chat|LastOpened/.test(c.name)))
})
test('new project is explicit and server-confirmed single stock; old project never guessed', async () => {
  const h = linkHarness(); await ready(h, 'new'); await h.page.submit()
  assert.equal(h.page.completed, true)
  const call = h.calls.find(c => c.name === 'externalHandoff')
  assert.deepEqual(call.args[0].target.subject, project().subject)
  assert.equal(call.args[0].target.name, '合成项目')
  assert.equal(h.calls.filter(c => c.name === 'createProject').length, 0)
})
test('unchanged content with a newer source capture time accepts the actual archived capture receipt', async () => {
  const h = linkHarness(); await ready(h)
  const newer = '2026-10-10T09:35:00+08:00'
  h.api.externalHandoff = async body => ({ data: { project: project(), artifact: { ...evidence(), payload: { ...evidence().payload, capturedAt: newer } }, branch: { id: 'N', projectId: 'P', name: body.branchName, revision: 2, heads: { evidence: 'E' } } } })
  await h.page.submit()
  assert.equal(h.page.completed, true)
  assert.equal(h.page.workflow.body.capturedAt, stamp)
  assert.equal(h.page.error, '')
})
test('network failure at capture retries same branch and evidence identity without duplicate writes', async () => {
  const h = linkHarness(); await ready(h)
  const capture = h.api.externalHandoff
  h.api.externalHandoff = async body => { h.record('failedCapture', body); throw new Error('offline') }
  await h.page.submit(); assert.equal(h.page.completed, false); assert.equal(h.page.retry, true)
  const body = clone(h.page.workflow.body)
  h.api.externalHandoff = capture; await h.page.submit()
  assert.equal(h.page.completed, true); assert.equal(h.calls.filter(c => c.name === 'createBranch').length, 0)
  assert.deepEqual(h.calls.find(c => c.name === 'externalHandoff').args[0], body)
})
test('unknown branch response reuses original branch key and parent revision', async () => {
  const h = linkHarness(); await ready(h)
  const create = h.api.externalHandoff
  h.api.externalHandoff = async body => { h.record('unknownBranch', body); throw new Error('lost reply') }
  await h.page.submit(); const frozen = clone(h.page.workflow.body)
  h.api.externalHandoff = create; await h.page.submit()
  assert.deepEqual(h.calls.find(c => c.name === 'externalHandoff').args[0], frozen)
  assert.equal(h.calls.filter(c => c.name === 'getBranch').length, 1)
})
test('source hash/version conflict does not import latest or leave an empty branch', async () => {
  const h = linkHarness(); await ready(h); h.api.externalHandoff = async () => { throw httpError(409, 'SOURCE_CHANGED') }
  await h.page.submit(); assert.equal(h.page.blocked, true); assert.equal(h.page.retry, false); assert.equal(h.page.workflow.branch, null)
  assert.match(h.page.error, /未替换为最新内容/); assert.equal(h.page.canConfirm, false)
  const before = h.calls.length; await h.page.submit(); assert.equal(h.calls.length, before)
})
test('permission denial clears choices and disables capture continuation', async () => {
  const h = linkHarness(); await ready(h); h.api.getProject = async () => { throw httpError(403, 'FEATURE_DISABLED') }
  await h.page.submit(); assert.deepEqual(clone(h.page.projects), []); assert.equal(h.page.selectedProject, null); assert.equal(h.page.canConfirm, false)
  assert.equal(h.calls.filter(c => c.name === 'createBranch').length, 0)
})
test('capability write-off never allows submit', async () => {
  const h = linkHarness(); h.api.capabilities = async () => ({ data: { enabled: true, write: false } }); await h.page.open()
  assert.equal(h.page.blocked, true); assert.equal(h.page.canConfirm, false)
})
test('old backend or missing external admission/schema never creates project or branch', async () => {
  for (const evidence of [undefined, {}, { ...capabilities().externalEvidence, enabled: false }, { ...capabilities().externalEvidence, schemaReady: false }, { ...capabilities().externalEvidence, capture: false }, { ...capabilities().externalEvidence, contractVersion: 'unknown' }]) {
    const h = linkHarness(); h.api.capabilities = async () => ({ data: { enabled: true, write: true, externalEvidence: evidence } })
    await h.page.open(); h.page.projectChoice = 'new'; h.page.projectName = '新项目'; h.page.symbol = '600875'; h.page.confirmed = true
    await h.page.submit(); assert.equal(h.page.canConfirm, false); assert.equal(h.page.workflow, null)
    assert.equal(h.calls.filter(c => /create|capture/.test(c.name)).length, 0)
  }
})
test('late project response after account replacement does not create branch', async () => {
  const h = linkHarness(); await ready(h); const pending = deferred(); h.api.getProject = () => pending.promise
  const request = h.page.submit(); h.page.accountId = '202'; h.component.watch.accountId.call(h.page)
  pending.resolve({ data: { project: project() } }); await request
  assert.equal(h.page.workflow, null); assert.equal(h.calls.filter(c => c.name === 'createBranch').length, 0)
})
test('late response after token replacement or source refresh is ignored', async () => {
  for (const replace of [h => h.subscriptions[0]({ type: 'user/SET_TOKEN' }), h => { h.page.sourceReady = false; h.component.watch.sourceReady.call(h.page, false) }]) {
    const h = linkHarness(); await ready(h); const pending = deferred(); h.api.externalHandoff = () => pending.promise
    const request = h.page.submit(); await Promise.resolve(); await Promise.resolve(); replace(h)
    pending.resolve({ data: { branch: { id: 'N', projectId: 'P', revision: 1 } } }); await request
    assert.equal(h.calls.filter(c => c.name === 'captureExternalEvidence').length, 0)
    assert.equal(h.page.completed, false)
  }
})
test('source identity changing while request is pending prevents continuation', async () => {
  const h = linkHarness(); await ready(h); const pending = deferred(); h.api.getProject = () => pending.promise
  const request = h.page.submit(); h.page.report = { ...report(), content_sha256: 'b'.repeat(64) }
  pending.resolve({ data: { project: project() } }); await request
  assert.equal(h.calls.filter(c => c.name === 'createBranch').length, 0)
})
test('run evidence uses exact captured reference, never current branch head; unknown time stays null', () => {
  const old = evidence('OLD'); const current = evidence('CURRENT'); current.payload.reportId = 'different-report'
  const entries = helpers.evidenceEntries(current, [reference('OLD')], { OLD: old, CURRENT: current })
  assert.equal(entries[0].id, 'OLD'); assert.equal(entries[0].payload.generatedAt, null); assert.equal(entries[0].payload.marketCutoff, null)
  assert.equal(helpers.evidenceEntries(current, [], {}).length, 0)
  assert.equal(helpers.evidenceEntries(null, [reference('OLD')], { OLD: current })[0].payload, null)
})
test('V2 opens explicit new branch from route without changing parent server or local drafts', async () => {
  const key = researchStorageKey('local-strategy', 101, 'P', 'A')
  const oldDraft = JSON.stringify({ sourceId: 11, code: 'unsaved-original', mappings: {} })
  const entries = new Map([[key, oldDraft]])
  const h = await workspace({ entries })
  const before = new Map(entries)
  h.page.$route.query = { project: 'P', branch: 'N', module: 'ai' }
  await h.page.openProject('P')
  assert.equal(h.page.branch.id, 'N'); assert.equal(h.page.module, 'ai')
  assert.equal(entries.get(key), oldDraft); assert.deepEqual(entries, before)
  assert.ok(!h.calls.some(c => ['putDraft', 'deleteDraft', 'createArtifact', 'startRun', 'setLastOpened'].includes(c.method)))
})
test('V2 restore without explicit branch respects saved last-opened; explicit foreign branch rejected', async () => {
  const h = await workspace()
  h.api.getProject = async () => ({ data: { project: { ...clone(h.page.project), lastOpened: { branchId: 'B', module: 'strategy' } }, branches: clone(Object.values(h.branches)) } })
  await h.page.openProject('P'); assert.equal(h.page.branch.id, 'B'); assert.equal(h.page.module, 'strategy')
  h.page.$route.query = { project: 'P', branch: 'FOREIGN', module: 'ai' }
  await h.page.openProject('P'); assert.equal(h.page.branch, null); assert.match(h.page.networkIssue, /不属于/)
})
test('V2 loads evidence across modules and retains separate historical run references', async () => {
  const h = await workspace()
  h.branches.A.heads.evidence = 'CURRENT'; h.artifacts.CURRENT = evidence('CURRENT'); h.artifacts.OLD = evidence('OLD')
  h.runs.A[0].evidenceReferences = [reference('OLD')]
  await h.page.loadBranch('A')
  assert.equal(h.page.headEvidence.id, 'CURRENT'); assert.equal(h.page.artifactCache.OLD.id, 'OLD')
  assert.equal(h.page.currentRun.evidenceReferences[0].artifactId, 'OLD')
  h.page.module = 'indicator'; assert.equal(h.page.headEvidence.id, 'CURRENT')
  h.page.module = 'strategy'; assert.equal(h.page.headEvidence.id, 'CURRENT')
  h.page.module = 'backtest'; assert.equal(h.page.headEvidence.id, 'CURRENT')
})

test('direct hypothesis is user text referencing evidence, no invented AI report or model call', async () => {
  const h = await workspace(); h.branches.A.hasDownstream = false; h.branches.A.heads.evidence = 'E'; h.artifacts.E = evidence()
  await h.page.loadBranch('A'); h.page.setEvidenceHypothesis('我的待验证假设')
  let request
  h.api.createArtifact = async (bid, body) => {
    request = clone(body); const artifact = { id: 'H', kind: 'hypothesis', payload: { title: body.from.title }, inputs: [{ role: 'evidence', artifactId: body.from.evidenceArtifactId }] }
    h.artifacts.H = artifact; h.branches.A.heads.hypothesis = 'H'
    return { data: { artifact, branch: clone(h.branches.A) } }
  }
  await h.page.confirmEvidenceHypothesis()
  assert.equal(request.kind, 'hypothesis'); assert.deepEqual(request.from, { evidenceArtifactId: 'E', title: '我的待验证假设' })
  assert.ok(request.clientRequestId); assert.equal(h.page.headHypothesis.id, 'H'); assert.equal(h.page.evidenceHypothesisText, '')
  assert.ok(!h.calls.some(c => /aiChat|startRun/.test(c.method)))
})
test('late hypothesis confirmation cannot clear another branch draft', async () => {
  const h = await workspace(); h.branches.A.hasDownstream = false; h.branches.A.heads.evidence = 'E'; h.artifacts.E = evidence()
  await h.page.loadBranch('A'); h.page.setEvidenceHypothesis('A 草稿')
  const pending = deferred(); h.api.createArtifact = () => pending.promise
  const request = h.page.confirmEvidenceHypothesis(); await h.page.loadBranch('B'); h.page.setEvidenceHypothesis('B 草稿')
  pending.resolve({ data: { artifact: { id: 'HA' }, branch: clone(h.branches.A) } }); await request
  assert.equal(h.page.evidenceHypothesisText, 'B 草稿')
  await h.page.loadBranch('A'); assert.equal(h.page.evidenceHypothesisText, 'A 草稿')
})
test('parameter and conflict branches carry external evidence by default', async () => {
  const h = await workspace(); h.page.artifactCache.E = evidence(); h.page.branch.heads.evidence = 'E'
  h.page.openBranchDialog({ indicatorApplied: { period: 40 } }); assert.ok(h.page.branchDlg.carry.includes('evidence'))
  h.page.openBranchDialog({ carry: ['indicator', 'strategy', 'run', 'review'] }); assert.ok(h.page.branchDlg.carry.includes('evidence'))
})
test('actual API client sends external capture to scoped endpoint with no source text or URL', async () => {
  const apiSource = readFileSync(new URL('../../src/api/researchLabV2.js', import.meta.url), 'utf8').replace(/import request from '@\/utils\/request'\s*/, '').replace(/export const /g, 'const ')
  const requests = []; const module = { exports: {} }
  vm.runInNewContext(apiSource + '\nmodule.exports = { captureExternalEvidence }', { module, request: options => { requests.push(clone(options)); return Promise.resolve({}) } })
  const body = helpers.evidenceRequest(helpers.reportIdentity(report(), stamp), 1, 'request-key')
  await module.exports.captureExternalEvidence('rb_example', body)
  assert.deepEqual(requests, [{ url: '/api/research-lab/v2/branches/rb_example/external-evidence', method: 'post', data: body, timeout: 30000 }])
})
test('changed Vue templates compile using the actual Vue 2 compiler', () => {
  for (const relative of ['components/Nng8ResearchLink/index.vue', 'components/ExternalEvidencePanel/index.vue', 'components/Nng8Readonly/index.vue', 'views/research-lab-v2/index.vue']) {
    const content = readFileSync(new URL('../../src/' + relative, import.meta.url), 'utf8')
    const template = content.slice(content.indexOf('<template>') + 10, content.lastIndexOf('</template>'))
    const result = compileTemplate({ source: template, filename: relative })
    assert.deepEqual(result.errors, [], relative)
  }
})
test('real Vue reactivity invalidates pending handoff when account prop changes', async () => {
  const h = linkHarness()
  const actual = new Vue({ ...h.component, propsData: { report: report(), capturedAt: stamp, accountId: '101', sourceReady: true }, beforeCreate () { this.$store = h.page.$store; this.$router = h.page.$router } })
  await actual.open(); actual.projectChoice = 'P'; actual.selectProject(); actual.confirmed = true
  await Vue.nextTick(); assert.ok(actual.canConfirm)
  const pending = deferred(); h.api.getProject = () => pending.promise
  const request = actual.submit(); actual.accountId = '202'; await Vue.nextTick()
  assert.equal(actual.visible, false); assert.equal(actual.workflow, null)
  pending.resolve({ data: { project: project() } }); await request
  assert.equal(h.calls.filter(c => c.name === 'createBranch').length, 0)
  actual.$destroy()
})
test('real Vue retains atomic request identity across harmless synchronized capture time updates', async () => {
  const h = linkHarness()
  const actual = new Vue({ ...h.component, propsData: { report: report(), capturedAt: stamp, accountId: '101', sourceReady: true }, beforeCreate () { this.$store = h.page.$store; this.$router = h.page.$router } })
  await actual.open(); actual.projectChoice = 'P'; actual.selectProject(); actual.confirmed = true
  await Vue.nextTick()
  const pending = deferred(); h.api.externalHandoff = body => { h.record('pendingAtomic', body); return pending.promise }
  const request = actual.submit(); await Promise.resolve(); await Promise.resolve(); await Vue.nextTick()
  const key = actual.workflow.requestKey
  const newer = '2026-10-10T09:35:00+08:00'
  actual.capturedAt = newer; actual.report = { ...report() }; await Vue.nextTick()
  assert.equal(actual.visible, true); assert.equal(actual.workflow.requestKey, key)
  pending.resolve({ data: { project: project(), artifact: { ...evidence(), payload: { ...evidence().payload, capturedAt: newer } }, branch: { id: 'N', projectId: 'P', name: '合成新分支', revision: 2, heads: { evidence: 'E' } } } })
  await request
  assert.equal(actual.completed, true); assert.equal(actual.workflow.requestKey, key)
  assert.equal(h.calls.find(row => row.name === 'pendingAtomic').args[0].capturedAt, stamp)
  assert.equal(h.calls.filter(row => row.name === 'pendingAtomic').length, 1)
  actual.$destroy()
})
