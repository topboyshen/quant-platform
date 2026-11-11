import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as logic from '../../src/views/research-lab-v2/workspaceLogic.js'
import * as mapping from '../../src/views/research-lab-v2/paramMappingLogic.js'

const vue = readFileSync(new URL('../../src/views/research-lab-v2/index.vue', import.meta.url), 'utf8')
const script = vue.split('<script>')[1].split('</script>')[0]
  .replace(/import \* as api from '@\/api\/researchLabV2'\s*/, '')
  .replace(/import \{[\s\S]*?\} from '\.\/workspaceLogic'\s*/, '')
  .replace(/import DraftBar from '\.\/DraftBar\.vue'\s*/, '')
  .replace(/import ExternalEvidencePanel from '@\/components\/ExternalEvidencePanel'\s*/, '')
  .replace(/import \{[\s\S]*?\} from '\.\/paramMappingLogic'\s*/, '')
  .replace('export default {', 'module.exports = {')

function deferred () {
  let release
  let reject
  const promise = new Promise((resolve, fail) => { release = resolve; reject = fail })
  return { promise, resolve: release, reject }
}

// Fixtures model a verified logged-in workspace. Scoped keys intentionally
// require the same account, project and branch as the component action.
const ACCOUNT = '101'
const handoffKey = (bid, pid = 'P') => logic.researchStorageKey('handoff', ACCOUNT, pid, bid)
const localKey = (bid, module, pid = 'P') => logic.researchStorageKey('local-' + module, ACCOUNT, pid, bid)

function harness ({ pendingA = { prepared: { indicatorArtifactId: 'I_A' } }, pendingB = { prepared: { indicatorArtifactId: 'I_B' } } } = {}) {
  const entries = new Map()
  if (pendingA) entries.set(handoffKey('A'), JSON.stringify(pendingA))
  if (pendingB) entries.set(handoffKey('B'), JSON.stringify(pendingB))
  const storage = {
    getItem: key => entries.get(key) || null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: key => entries.delete(key)
  }
  const calls = []
  const branches = {
    A: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' } },
    B: { id: 'B', projectId: 'P', heads: { indicator: 'I_B' } }
  }
  const api = {
    prepareHandoff (branchId, body) {
      const request = deferred()
      calls.push({ branchId, body, request })
      return request.promise
    },
    getBranch: async bid => ({ data: branches[bid] }),
    strategyParamSchema: async (bid, sourceId) => ({ data: { sourceId, codeSha256: 'code-' + sourceId, contractHash: 'contract-' + sourceId, paramSchema: [{ name: 'period', type: 'integer', default: 20, min: 0 }], params: { effective: { period: 20 } } } }),
    listArtifacts: async () => ({ data: [] }),
    listRuns: async () => ({ data: [] })
  }
  const module = { exports: {} }
  vm.runInNewContext(script, {
    module,
    api,
    sessionStorage: storage,
    ...logic,
    ...mapping,
    DraftBar: {},
    ExternalEvidencePanel: {},
    JSON
  }, { filename: 'research-lab-v2/index.vue' })
  const component = module.exports
  const messages = []
  const page = {
    ...component.data(),
    ...component.methods,
    project: { id: 'P' },
    branch: branches.A,
    pendingHandoff: pendingA,
    strat: { handoffArtifactId: null, mapped: 41, mappingConfirmed: false, code: 'draft A', sourceId: 1 },
    artifactCache: { I_A: { id: 'I_A' }, I_B: { id: 'I_B' } },
    handoffTargetProjectId: 'P',
    handoffTargetBranchId: 'A',
    handoffBranchReady: true,
    workspaceUserId: ACCOUNT,
    capsLoaded: true,
    caps: { enabled: true },
    $store: {
      state: { user: { info: { id: 101 }, token: 'offline-handoff-token' } },
      getters: { userInfo: { id: 101 }, token: 'offline-handoff-token' }
    },
    guard: logic.createLatestGuard(),
    $message: { success: msg => messages.push(msg), info: msg => messages.push(msg), warning: msg => messages.push(msg), error: msg => messages.push(msg) },
    $set: (obj, key, value) => { obj[key] = value },
    guarded: fn => fn(),
    fetchArtifact: async () => {},
    loadDrafts: async () => {},
    loadLocal () {
      this.strat = this.branch.id === 'A'
        ? { handoffArtifactId: null, mapped: 41, mappingConfirmed: false, code: 'draft A', sourceId: 1 }
        : { handoffArtifactId: null, mapped: 84, mappingConfirmed: false, code: 'draft B', sourceId: 2 }
    }
  }
  Object.defineProperty(page, 'headIndicator', { get () { return this.artifactCache[this.branch.heads.indicator] } })
  for (const [name, fn] of Object.entries(component.computed)) {
    if (name !== 'headIndicator') Object.defineProperty(page, name, { get: () => fn.call(page) })
  }
  return { page, calls, entries, messages, api, component }
}

const incomingA = { handoffArtifactId: 'I_A', mapped: 173, mappingConfirmed: true, code: 'incoming A' }

test('current branch can still apply its handoff and retain a local unsaved draft', async () => {
  const { page, calls, entries } = harness()
  const pending = page.arriveHandoff(false)
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await pending
  assert.equal(page.strat.mapped, 173)
  assert.equal(JSON.parse(entries.get(localKey('A', 'strategy'))).mapped, 173)
  assert.equal(page.stratDirty, true)
  assert.equal(page.pendingHandoff, null)
  assert.equal(entries.has(handoffKey('A')), false)
})

test('late A arrival cannot replace B draft, cache, or pending handoff', async () => {
  const { page, calls, entries, messages } = harness()
  const pending = page.arriveHandoff(false)
  assert.equal(calls[0].branchId, 'A')
  await page.loadBranch('B')
  const bPending = page.pendingHandoff
  const bCache = entries.get(handoffKey('B'))
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await pending
  assert.equal(page.strat.mapped, 84)
  assert.equal(page.strat.code, 'draft B')
  assert.equal(page.pendingHandoff, bPending)
  assert.equal(entries.get(handoffKey('B')), bCache)
  assert.equal(entries.has(localKey('B', 'strategy')), false)
  assert.equal(page.handoffDlg.visible, false)
  assert.equal(messages.length, 0)
})

test('A to B to A does not revive an old A response', async () => {
  const { page, calls, entries } = harness()
  const pending = page.arriveHandoff(false)
  await page.loadBranch('B')
  await page.loadBranch('A')
  const newAPending = page.pendingHandoff
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await pending
  assert.equal(page.strat.mapped, 41)
  assert.equal(page.pendingHandoff, newAPending)
  assert.equal(entries.has(localKey('A', 'strategy')), false)
  assert.equal(page.handoffDlg.visible, false)
})

test('replace confirmation after branch switch preserves B draft and handoff', async () => {
  const { page, calls, entries } = harness()
  const pending = page.arriveHandoff(true)
  calls[0].request.resolve({ data: { decision: 'choose', incomingWork: incomingA, diff: [{ field: 'mapped', current: 41, incoming: 173 }] } })
  await pending
  assert.equal(page.handoffDlg.visible, true)
  await page.loadBranch('B')
  const bPending = page.pendingHandoff
  page.replaceWithHandoff()
  assert.equal(page.strat.mapped, 84)
  assert.equal(page.pendingHandoff, bPending)
  assert.equal(entries.has(localKey('B', 'strategy')), false)
  assert.equal(page.handoffDlg.visible, false)
})

test('current dialog confirmation applies only after the user chooses replace', async () => {
  const { page, calls, entries } = harness()
  const pending = page.arriveHandoff(true)
  calls[0].request.resolve({ data: { decision: 'choose', incomingWork: incomingA, diff: [] } })
  await pending
  assert.equal(page.strat.mapped, 41)
  page.replaceWithHandoff()
  assert.equal(page.strat.mapped, 173)
  assert.equal(JSON.parse(entries.get(localKey('A', 'strategy'))).mapped, 173)
  assert.equal(page.pendingHandoff, null)
})

test('late preparation cannot put A handoff into B session cache', async () => {
  const { page, calls, entries } = harness({ pendingA: null })
  const pending = page.handoffToStrategy()
  assert.equal(calls[0].branchId, 'A')
  await page.loadBranch('B')
  const bCache = entries.get(handoffKey('B'))
  calls[0].request.resolve({ data: { prepared: { indicatorArtifactId: 'I_A' } } })
  await pending
  assert.equal(entries.has(handoffKey('A')), false)
  assert.equal(entries.get(handoffKey('B')), bCache)
  assert.equal(page.pendingHandoff.prepared.indicatorArtifactId, 'I_B')
  assert.equal(page.strat.mapped, 84)
})

test('a branch switch blocks handoff arrival as soon as it starts', async () => {
  const { page, calls, api } = harness()
  const branchResponse = deferred()
  api.getBranch = () => branchResponse.promise
  const switching = page.loadBranch('B')
  await page.arriveHandoff(false)
  assert.equal(calls.length, 0)
  branchResponse.resolve({ data: { id: 'B', projectId: 'P', heads: { indicator: 'I_B' } } })
  await switching
  assert.equal(page.strat.mapped, 84)
})

test('a project switch invalidates an in-flight arrival before the new project loads', async () => {
  const { page, calls, api, entries } = harness()
  const arrival = page.arriveHandoff(false)
  const projectResponse = deferred()
  api.getProject = () => projectResponse.promise
  api.getBranch = async bid => ({ data: { id: bid, projectId: 'Q', heads: { indicator: 'I_' + bid } } })
  page.loadLegacyLists = () => {}
  const opening = page.openProject('Q')
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await arrival
  assert.equal(page.strat.mapped, 41)
  assert.equal(entries.has(localKey('A', 'strategy')), false)
  projectResponse.resolve({ data: { project: { id: 'Q', defaultBranchId: 'B' }, branches: [] } })
  await opening
  assert.equal(page.project.id, 'Q')
  assert.equal(page.strat.mapped, 84)
})

test('newer arrival wins and editing a draft while dialog is open requires a fresh diff', async () => {
  const { page, calls, entries } = harness()
  const older = page.arriveHandoff(true)
  const newer = page.arriveHandoff(true)
  calls[1].request.resolve({ data: { decision: 'choose', incomingWork: incomingA, diff: [] } })
  await newer
  assert.equal(page.handoffDlg.visible, true)
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await older
  assert.equal(page.handoffDlg.visible, true)
  page.setStrat('mapped', 55)
  page.replaceWithHandoff()
  assert.equal(page.strat.mapped, 55)
  assert.equal(page.pendingHandoff.prepared.indicatorArtifactId, 'I_A')
  assert.equal(entries.has(handoffKey('A')), true)
  assert.equal(page.handoffDlg.visible, false)
})

test('deactivated cached page cannot let an old preparation write sessionStorage after returning', async () => {
  const { page, calls, entries, component } = harness({ pendingA: null })
  const old = page.handoffToStrategy()
  component.deactivated.call(page)
  assert.equal(page.handoffBranchReady, false)
  const reopened = component.activated.call(page)
  await reopened
  assert.equal(page.handoffBranchReady, true)
  calls[0].request.resolve({ data: { prepared: { indicatorArtifactId: 'I_A' } } })
  await old
  assert.equal(entries.has(handoffKey('A')), false)
  assert.equal(page.pendingHandoff, null)
  page.goModule = () => {}
  const fresh = page.handoffToStrategy()
  assert.equal(calls[1].branchId, 'A')
  calls[1].request.resolve({ data: { prepared: { indicatorArtifactId: 'I_A' } } })
  await fresh
  assert.equal(entries.has(handoffKey('A')), true)
})

test('cached page stays fail-closed if the current account can no longer read its branch', async () => {
  const { page, calls, entries, api, component } = harness()
  const old = page.arriveHandoff(false)
  component.deactivated.call(page)
  api.getBranch = async () => { throw new Error('forbidden') }
  await component.activated.call(page)
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await old
  assert.equal(page.handoffBranchReady, false)
  assert.equal(page.handoffInactive, true)
  assert.equal(entries.has(localKey('A', 'strategy')), false)
  assert.equal(page.strat.mapped, 41)
  api.getBranch = async () => ({ data: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' } } })
  await page.loadBranch('A')
  assert.equal(page.handoffInactive, false)
  assert.equal(page.handoffBranchReady, true)
})

test('destroyed page cannot delete a newer instance handoff for the same branch', async () => {
  const { page, calls, entries, component } = harness()
  const old = page.arriveHandoff(false)
  component.beforeDestroy.call(page)
  const newer = { prepared: { indicatorArtifactId: 'I_A' }, marker: 'new-instance' }
  entries.set(handoffKey('A'), JSON.stringify(newer))
  calls[0].request.resolve({ data: { decision: 'apply', incomingWork: incomingA } })
  await old
  assert.deepEqual(JSON.parse(entries.get(handoffKey('A'))), newer)
  assert.equal(page.strat.mapped, 41)
})

test('a late branch load while deactivated cannot re-enable handoff before permission recheck', async () => {
  const { page, calls, api, component } = harness()
  component.deactivated.call(page)
  await page.loadBranch('B')
  assert.equal(page.branch.id, 'A')
  assert.equal(page.strat.code, 'draft A')
  assert.equal(page.handoffInactive, true)
  assert.equal(page.handoffViewActive, false)
  assert.equal(page.handoffBranchReady, false)
  await page.arriveHandoff(false)
  assert.equal(calls.length, 0)
  const verify = deferred()
  api.getBranch = () => verify.promise
  const reactivation = component.activated.call(page)
  assert.equal(page.handoffBranchReady, false)
  verify.resolve({ data: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' } } })
  await reactivation
  assert.equal(page.handoffInactive, false)
  assert.equal(page.handoffBranchReady, true)
  assert.equal(page.handoffTargetBranchId, 'A')
  assert.equal(page.branch.id, 'A')
  api.getBranch = async bid => ({ data: { id: bid, projectId: 'P', heads: { indicator: 'I_' + bid } } })
  await page.loadBranch('B')
  assert.equal(page.branch.id, 'B')
  assert.equal(page.strat.code, 'draft B')
})

test('late branch refresh cannot mix A identity with B draft or handoff cache', async () => {
  const { page, api, entries } = harness()
  const old = deferred()
  const getBranch = api.getBranch
  api.getBranch = bid => bid === 'A' ? old.promise : getBranch(bid)
  const refreshing = page.refreshBranch()
  await page.loadBranch('B')
  const bPending = page.pendingHandoff
  const bCache = entries.get(handoffKey('B'))
  old.resolve({ data: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' }, revision: 88 } })
  await refreshing
  assert.equal(page.branch.id, 'B')
  assert.equal(page.handoffTargetBranchId, 'B')
  assert.equal(page.strat.code, 'draft B')
  assert.equal(page.pendingHandoff, bPending)
  page.dropHandoff()
  assert.equal(entries.has(handoffKey('A')), true)
  assert.equal(entries.has(handoffKey('B')), false)
  assert.notEqual(bCache, undefined)
})

test('late artifact-list refresh cannot replace newly selected branch artifacts', async () => {
  const { page, api } = harness()
  const old = deferred()
  api.listArtifacts = (pid, query) => query === '?branchId=A' ? old.promise : Promise.resolve({ data: [{ id: 'B-new' }] })
  const refreshing = page.refreshBranch()
  // Allow getBranch and artifact fetches to reach the held list request.
  for (let i = 0; i < 6; i++) await Promise.resolve()
  await page.loadBranch('B')
  old.resolve({ data: [{ id: 'A-late' }] })
  await refreshing
  assert.equal(page.branch.id, 'B')
  assert.equal(page.branchArtifacts[0].id, 'B-new')
})

test('latest same-branch refresh wins without replacing its unsaved strategy draft', async () => {
  const { page, api } = harness()
  const first = deferred()
  const second = deferred()
  let calls = 0
  api.getBranch = () => (++calls === 1 ? first.promise : second.promise)
  const older = page.refreshBranch()
  const newer = page.refreshBranch()
  second.resolve({ data: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' }, revision: 2 } })
  await newer
  first.resolve({ data: { id: 'A', projectId: 'P', heads: { indicator: 'I_A' }, revision: 1 } })
  await older
  assert.equal(page.branch.revision, 2)
  assert.equal(page.strat.code, 'draft A')
})

test('ignore handoff refuses a target mismatch or inactive cached page', () => {
  const { page, entries, component } = harness()
  const pending = page.pendingHandoff
  page.handoffTargetBranchId = 'B'
  page.dropHandoff()
  assert.equal(page.pendingHandoff, pending)
  assert.equal(entries.has(handoffKey('A')), true)
  page.handoffTargetBranchId = 'A'
  component.deactivated.call(page)
  page.dropHandoff()
  assert.equal(page.pendingHandoff, pending)
  assert.equal(entries.has(handoffKey('A')), true)
})

function confirmationHarness (method) {
  const h = harness()
  const { page, api, component, entries } = h
  page.fetchArtifact = component.methods.fetchArtifact
  page.loadDrafts = component.methods.loadDrafts
  page.artifactCache.I_A.params = { effective: { period: 20 } }
  page.artifactCache.I_B.params = { effective: { period: 40 } }
  page.ind = { indicatorId: 1, applied: { period: 20 }, draft: { period: 20 }, specs: [] }
  page.indDirty = true
  page.stratDirty = true
  if (method === 'confirmStrategy') {
    page.strat = { ...mapping.emptyStrategyDraft(), sourceId: 1, mappings: { period: { mode: 'default', value: 20, sourceParam: null, reason: '' } } }
    page.strategySchema = { sourceId: 1, codeSha256: 'code-1', contractHash: 'contract-1', paramSchema: [{ name: 'period', type: 'integer', default: 20, min: 0 }], params: { effective: { period: 20 } } }
    page.strategySchemaStatus = 'ready'
    page.confirmMapping(true)
  }
  for (const bid of ['A', 'B']) {
    for (const module of ['indicator', 'strategy']) entries.set(localKey(bid, module), `local ${bid} ${module}`)
  }
  const head = { pickHypothesis: 'hypothesis', confirmIndicator: 'indicator', confirmStrategy: 'strategy' }[method]
  const artifact = { id: 'confirmed_A_' + head, params: { effective: { period: 20 } } }
  const branch = { id: 'A', projectId: 'P', revision: 2, heads: { indicator: 'I_A', [head]: artifact.id } }
  const response = { data: { artifact, branch } }
  const creating = deferred()
  const getBranch = api.getBranch
  api.createArtifact = (bid, body) => {
    h.confirmRequest = { bid, body }
    return creating.promise
  }
  api.getBranch = bid => bid === 'A' ? Promise.resolve({ data: branch }) : getBranch(bid)
  api.getArtifact = async id => ({ data: id === artifact.id ? artifact : { id } })
  api.getDraft = async (bid, module) => ({ data: { module, payload: { branchId: bid }, draftRevision: 2 } })
  return Object.assign(h, { creating, response, start: () => method === 'pickHypothesis' ? page[method]({ id: 'report_A' }, 0) : page[method]() })
}

function localConfirmationState ({ page, entries }) {
  return JSON.stringify({ branch: page.branch, target: page.handoffTargetBranchId, strat: page.strat, ind: page.ind,
    drafts: page.drafts, indDirty: page.indDirty, stratDirty: page.stratDirty, pending: page.pendingHandoff,
    entries: [...entries], artifacts: page.artifactCache })
}

for (const method of ['pickHypothesis', 'confirmIndicator', 'confirmStrategy']) {
  test(`${method}: late A confirmation cannot replace B draft/cache; A remains readable`, async () => {
    const h = confirmationHarness(method)
    const pending = h.start()
    assert.equal(h.confirmRequest.bid, 'A')
    await h.page.loadBranch('B')
    const before = localConfirmationState(h)
    h.creating.resolve(h.response)
    await pending
    assert.equal(localConfirmationState(h), before)
    assert.equal(h.messages.length, 0)
    await h.page.loadBranch('A')
    assert.equal(h.page.branch.revision, 2)
    assert.equal(h.page.artifactCache[h.response.data.artifact.id].id, h.response.data.artifact.id)
  })

  test(`${method}: switching during real artifact fetch drops its local commit`, async () => {
    const h = confirmationHarness(method)
    const fetching = deferred()
    const entered = deferred()
    h.api.getArtifact = () => { entered.resolve(); return fetching.promise }
    const pending = h.start()
    h.creating.resolve(h.response)
    await entered.promise
    await h.page.loadBranch('B')
    const before = localConfirmationState(h)
    fetching.resolve({ data: h.response.data.artifact })
    await pending
    assert.equal(localConfirmationState(h), before)
    assert.equal(h.messages.length, 0)
  })

  test(`${method}: deactivated page ignores a successful confirmation response`, async () => {
    const h = confirmationHarness(method)
    const pending = h.start()
    h.component.deactivated.call(h.page)
    const before = localConfirmationState(h)
    h.creating.resolve(h.response)
    await pending
    assert.equal(localConfirmationState(h), before)
    assert.equal(h.messages.length, 0)
  })

  test(`${method}: current branch still accepts a normal confirmation`, async () => {
    const h = confirmationHarness(method)
    const pending = h.start()
    h.creating.resolve(h.response)
    await pending
    assert.equal(h.page.branch.revision, 2)
    assert.equal(h.page.artifactCache[h.response.data.artifact.id].id, h.response.data.artifact.id)
    if (method === 'confirmIndicator') assert.equal(h.entries.has(localKey('A', 'indicator')), false)
    if (method === 'confirmStrategy') assert.equal(h.entries.has(localKey('A', 'strategy')), false)
    assert.equal(h.entries.get(localKey('B', 'indicator')), 'local B indicator')
    assert.equal(h.entries.get(localKey('B', 'strategy')), 'local B strategy')
  })
}

test('confirmIndicator: late real draft fetch cannot populate B drafts after A confirmation', async () => {
  const h = confirmationHarness('confirmIndicator')
  const draft = deferred()
  const entered = deferred()
  const getDraft = h.api.getDraft
  h.api.getDraft = (bid, module) => {
    if (bid === 'A' && module === 'indicator') { entered.resolve(); return draft.promise }
    return getDraft(bid, module)
  }
  const pending = h.start()
  h.creating.resolve(h.response)
  await entered.promise
  await h.page.loadBranch('B')
  const before = localConfirmationState(h)
  draft.resolve({ data: { module: 'indicator', payload: { private: 'A-only' }, draftRevision: 88 } })
  await pending
  assert.equal(localConfirmationState(h), before)
  assert.equal(h.messages.length, 0)
})

function guardedFailureHarness (method) {
  const confirms = ['pickHypothesis', 'confirmIndicator', 'confirmStrategy']
  const h = confirms.includes(method) ? confirmationHarness(method) : harness()
  // These tests exercise the real catch/finally paths, not the success-only stub.
  h.page.guarded = h.component.methods.guarded
  if (!confirms.includes(method)) h.start = () => h.page[method](false)
  h.request = () => confirms.includes(method) ? h.creating : h.calls[0].request
  return h
}

const failures = [
  { name: 'network', error: () => new Error('network interrupted'), effect: 'network' },
  { name: '409 conflict', error: () => ({ response: { status: 409, data: { msg: 'old A conflict', error: { type: 'REVISION_CONFLICT' }, data: { diff: [{ branch: 'A' }] } } } }), effect: 'conflict' },
  { name: '403 forbidden', error: () => ({ response: { status: 403, data: { msg: 'old A forbidden', error: { type: 'FORBIDDEN' } } } }), effect: 'message' },
  { name: 'UPSTREAM_LOCKED', error: () => ({ response: { status: 409, data: { msg: 'old A locked', error: { type: 'UPSTREAM_LOCKED' } } } }), effect: 'upstream' }
]

function guardedUiState (h) {
  const { page } = h
  return JSON.stringify({ local: localConfirmationState(h), busy: page.busy, lastCall: page.lastCall,
    networkIssue: page.networkIssue, conflict: page.conflict, branchDlg: page.branchDlg,
    handoffDlg: page.handoffDlg, messages: h.messages })
}

for (const method of ['handoffToStrategy', 'arriveHandoff', 'pickHypothesis', 'confirmIndicator', 'confirmStrategy']) {
  for (const failure of failures) {
    test(`${method}: real guarded ignores late ${failure.name} after switching to B`, async () => {
      const h = guardedFailureHarness(method)
      const pending = h.start()
      const request = h.request()
      await h.page.loadBranch('B')
      // An old A finally must not clear B's newer in-flight busy state.
      h.page.busy = true
      const before = guardedUiState(h)
      request.reject(failure.error())
      await pending
      assert.equal(guardedUiState(h), before)
    })

    test(`${method}: real guarded still reports current ${failure.name}`, async () => {
      const h = guardedFailureHarness(method)
      const pending = h.start()
      assert.equal(h.page.busy, true)
      h.request().reject(failure.error())
      await pending
      assert.equal(h.page.busy, false)
      assert.notEqual(h.page.lastCall, '')
      if (failure.effect === 'network') assert.notEqual(h.page.networkIssue, '')
      else if (failure.effect === 'conflict') {
        assert.equal(h.page.conflict.visible, true)
        assert.equal(h.page.conflict.diff[0].branch, 'A')
      } else if (failure.effect === 'upstream' && method === 'pickHypothesis') {
        assert.equal(h.page.branchDlg.visible, true)
        assert.equal(h.messages.length, 0)
      } else assert.equal(h.messages.length, 1)
    })
  }
}

test('real guarded discards inapplicable output but releases the same request busy state', async () => {
  const h = harness()
  h.page.guarded = h.component.methods.guarded
  const request = deferred()
  let current = true
  const pending = h.page.guarded(() => request.promise, { isCurrent: () => current })
  current = false
  const before = guardedUiState(h)
  request.resolve({ data: { old: 'A' } })
  assert.equal(await pending, null)
  const after = JSON.parse(guardedUiState(h))
  const expected = JSON.parse(before)
  expected.busy = false
  assert.deepEqual(after, expected)
  assert.equal(h.page.busy, false)
})

test('real guarded keeps default success and rethrow behavior without a context callback', async () => {
  const h = harness()
  h.page.guarded = h.component.methods.guarded
  const result = { data: { current: true } }
  assert.equal(await h.page.guarded(async () => result), result)
  assert.equal(h.page.busy, false)
  const failure = new Error('default failure')
  await assert.rejects(h.page.guarded(async () => { throw failure }, { rethrow: true }), error => error === failure)
  assert.equal(h.page.busy, false)
  assert.notEqual(h.page.networkIssue, '')
})
