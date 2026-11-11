// Offline harness for the actual ResearchLabV2 Vue script and API client.
// Only API and browser storage are synthetic; no network/model/execution is used.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import vm from 'node:vm'

export const sourceRoot = process.env.RESEARCH_WORKFLOW_SOURCE_ROOT
  ? resolve(process.env.RESEARCH_WORKFLOW_SOURCE_ROOT)
  : fileURLToPath(new URL('../..', import.meta.url))
const logic = await import(pathToFileURL(resolve(sourceRoot, 'src/views/research-lab-v2/workspaceLogic.js')))
const mapping = await import(pathToFileURL(resolve(sourceRoot, 'src/views/research-lab-v2/paramMappingLogic.js')))
const vue = readFileSync(resolve(sourceRoot, 'src/views/research-lab-v2/index.vue'), 'utf8')
const script = vue.split('<script>')[1].split('</script>')[0]
  .replace(/import \* as api from '@\/api\/researchLabV2'\s*/, '')
  .replace(/import \{[\s\S]*?\} from '\.\/workspaceLogic'\s*/, '')
  .replace(/import DraftBar from '\.\/DraftBar\.vue'\s*/, '')
  .replace(/import ExternalEvidencePanel from '@\/components\/ExternalEvidencePanel'\s*/, '')
  .replace(/import \{[\s\S]*?\} from '\.\/paramMappingLogic'\s*/, '')
  .replace('export default {', 'module.exports = {')

export const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
export function deferred () {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
export const httpError = (status, type = 'SYNTHETIC_ERROR') => Object.assign(new Error('offline synthetic response'), {
  response: { status, data: { error: { type }, msg: `Synthetic HTTP ${status}` } }
})
export function recordedRun (id, branchId, changes = {}) {
  const params = branchId === 'B' ? { window: 40 } : { ma_period: 40 }
  return {
    id, branchId, status: 'succeeded', actions: [],
    execution: { id: 'exec-' + id, attempt: 1 }, strategyArtifactId: 'S_' + branchId,
    effectiveConfig: {
      params, startDate: '2023-01-03', endDate: '2024-08-30', initialCapital: 234567,
      commission: { rate: 0.0002, unit: 'ratio' }, slippage: { rate: 0.0007, unit: 'ratio' }, leverage: 1
    },
    config: {
      paramsOverride: params, startDate: '2023-01-03', endDate: '2024-08-30', initialCapital: 234567,
      commissionPct: 0.02, slippagePct: 0.07, leverage: 1
    },
    ...clone(changes)
  }
}

export async function harness ({ entries = new Map(), userId = 101, bootstrap = true } = {}) {
  const calls = [], messages = [], storageWrites = []
  const project = { id: 'P', defaultBranchId: 'A', subject: { market: 'CNStock', symbol: '600875' } }
  const branches = Object.fromEntries(['A', 'B', 'N'].map(id => [id, {
    id, projectId: 'P', name: id, revision: 7,
    heads: { indicator: 'I_' + id, strategy: 'S_' + id, run: 'R_' + id }, stale: {}
  }]))
  const artifacts = Object.fromEntries(['A', 'B', 'N'].flatMap((id, index) => [
    ['I_' + id, {
      id: 'I_' + id, kind: 'indicator_snapshot', inputs: [], params: { effective: { period: 40 } },
      payload: { indicator: { legacyId: index + 1 }, code: '# @param period int 20', params: { specs: [{ name: 'period', type: 'int', default: 20 }] } }
    }],
    ['S_' + id, {
      id: 'S_' + id, kind: 'strategy_snapshot',
      params: { declared: id === 'B' ? { window: 20 } : { ma_period: 20 }, effective: id === 'B' ? { window: 40 } : { ma_period: 40 } },
      inputs: [{ role: 'indicator', artifactId: 'I_' + id }],
      payload: { source: { legacyId: 11 + index }, paramSchema: [{ name: id === 'B' ? 'window' : 'ma_period', type: 'integer', default: 20, min: 0, max: 1000 }], paramMapping: [{ value: 40 }] }
    }]
  ]))
  const runs = Object.fromEntries(['A', 'B', 'N'].map(id => [id, [recordedRun('R_' + id, id)]]))
  const accepted = new Map()
  const record = (method, fields = {}) => calls.push({ method, ...clone(fields) })
  const api = {
    capabilities: async () => ({ data: { enabled: true, write: true, run: true } }),
    listProjects: async () => ({ data: [clone(project)] }),
    getBranch: async bid => { record('getBranch', { bid }); return { data: clone(branches[bid]) } },
    getProject: async pid => { record('getProject', { pid }); return { data: { project: clone(project), branches: clone(Object.values(branches)) } } },
    createBranch: async (pid, body) => { record('createBranch', { pid, body }); return { data: { branch: clone(branches.N) } } },
    listArtifacts: async (pid, query) => { record('listArtifacts', { pid, query }); return { data: [] } },
    listRuns: async bid => { record('listRuns', { bid }); return { data: clone(runs[bid] || []) } },
    getDraft: async (bid, module) => { record('getDraft', { bid, module }); return { data: null } },
    getArtifact: async id => { record('getArtifact', { id }); return { data: clone(artifacts[id]) } },
    strategyParamSchema: async (bid, sourceId) => {
      record('strategyParamSchema', { bid, sourceId })
      const artifact = artifacts['S_' + bid]
      return { data: { sourceId, codeSha256: 'code-' + sourceId, contractHash: 'contract-' + sourceId, paramSchema: clone(artifact.payload.paramSchema), params: clone(artifact.params) } }
    },
    setLastOpened: async (pid, body) => { record('setLastOpened', { pid, body }); return {} },
    startRun: async (bid, body, key) => {
      record('startRun', { bid, body, key })
      const run = recordedRun('NEW_' + bid, bid, { strategyArtifactId: body.strategyArtifactId })
      runs[bid] = [run, ...runs[bid]]
      branches[bid].heads.run = run.id
      accepted.set(key, run)
      return { data: { run: clone(run) } }
    },
    findRunByKey: async key => { record('findRunByKey', { key }); return { data: { run: clone(accepted.get(key) || recordedRun('RECOVERED_A', 'A')) } } },
    reviewContext: async (bid, rid) => { record('reviewContext', { bid, rid }); return { data: { runId: rid, branchId: bid, contextSha256: 'ctx-' + rid, prompt: 'offline fixture prompt' } } },
    indicatorPreview: async (bid, body) => { record('indicatorPreview', { bid, body }); return { data: { marker: 'chart-' + bid, params: { effective: body.applied }, close: [1, 2], series: {}, dataFingerprint: 'data-' + bid } } },
    listIndicators: async () => ({ data: [] }),
    listScriptSources: async () => ({ data: [] })
  }
  const storage = {
    getItem: key => entries.has(key) ? entries.get(key) : null,
    setItem: (key, value) => { storageWrites.push({ op: 'set', key, value }); entries.set(key, String(value)) },
    removeItem: key => { storageWrites.push({ op: 'remove', key }); entries.delete(key) }
  }
  const module = { exports: {} }
  vm.runInNewContext(script, {
    module, api, ...logic, ...mapping, DraftBar: {}, ExternalEvidencePanel: {}, sessionStorage: storage,
    navigator: { userAgent: 'OfflineRegression' }, JSON
  }, { filename: resolve(sourceRoot, 'src/views/research-lab-v2/index.vue') })
  const component = module.exports
  const userInfo = { id: userId }
  const page = {
    ...component.data(), ...component.methods,
    project: clone(project), branch: clone(branches.A), artifactCache: clone(artifacts),
    handoffTargetProjectId: 'P', handoffTargetBranchId: 'A', handoffBranchReady: true,
    capsLoaded: true, caps: { enabled: true, write: true, run: true },
    $store: { state: { user: { info: userInfo, token: 'offline-token-' + userId } }, getters: { userInfo, token: 'offline-token-' + userId } },
    $route: { query: {} },
    $set: (obj, key, val) => { obj[key] = val },
    $nextTick: callback => callback ? Promise.resolve().then(callback) : Promise.resolve(),
    $message: Object.fromEntries(['success', 'error', 'info', 'warning'].map(kind => [kind, msg => messages.push({ kind, msg })]))
  }
  for (const [name, fn] of Object.entries(component.computed || {})) Object.defineProperty(page, name, { get: () => fn.call(page) })
  if (bootstrap) await page.loadBranch('A')
  return { page, api, calls, entries, messages, component, storage, storageWrites, branches, artifacts, runs, record }
}

// Set the same form values an explicit user edit would provide, invoking the
// component's normal persistence entry point when available in the source tree.
export function editBacktest (h, values) {
  for (const [key, value] of Object.entries(values)) {
    if (key === 'override') {
      for (const [name, raw] of Object.entries(value)) h.page.setOverride(name, raw)
    } else if (typeof h.page.setBacktest === 'function') h.page.setBacktest(key, value)
    else h.page.bt = { ...h.page.bt, [key]: value }
  }
  if (typeof h.page.persistBacktest === 'function') h.page.persistBacktest()
}
export function changeAccount (h, id) {
  const before = h.page.sessionIdentity
  const info = { id }
  h.page.$store.state.user.info = info
  h.page.$store.getters.userInfo = info
  h.page.$store.state.user.token = 'offline-token-' + id
  h.page.$store.getters.token = 'offline-token-' + id
  // Vue invokes this watcher automatically. In the framework-free VM, drive
  // the actual watcher so a store mutation has the same lifecycle semantics.
  const watcher = h.component.watch && h.component.watch.sessionIdentity
  if (watcher && before !== h.page.sessionIdentity) watcher.call(h.page, h.page.sessionIdentity, before)
}
export function actualApiClient () {
  const source = readFileSync(resolve(sourceRoot, 'src/api/researchLabV2.js'), 'utf8')
    .replace(/import request from '@\/utils\/request'\s*/, '')
    .replace(/export const /g, 'const ')
  const requests = []
  const module = { exports: {} }
  vm.runInNewContext(source + '\nmodule.exports = { findRunByKey, startRun }', {
    module, request: options => { requests.push(clone(options)); return Promise.resolve({ data: {} }) }, encodeURIComponent
  })
  return { ...module.exports, requests }
}
