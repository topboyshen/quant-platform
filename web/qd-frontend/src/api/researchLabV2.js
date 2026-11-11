// Research Lab V2 API client (/api/research-lab/v2, contract revision 2 §4). Feature-flagged view only.
import request from '@/utils/request'

const BASE = '/api/research-lab/v2'
const call = (method, url, data, headers) => request({ url: BASE + url, method, data, headers, timeout: url.endsWith('/runs') && method === 'post' ? 600000 : url.endsWith('/evidence-hypotheses') && method === 'post' ? 180000 : 30000 })

export const capabilities = () => call('get', '/capabilities')
export const listProjects = () => call('get', '/projects')
export const createProject = (body, key) => call('post', '/projects', body, key ? { 'Idempotency-Key': key } : undefined)
export const getProject = id => call('get', `/projects/${id}`)
export const setLastOpened = (id, body) => call('put', `/projects/${id}/last-opened`, body)
export const listArtifacts = (id, params = '') => call('get', `/projects/${id}/artifacts${params}`)
export const createBranch = (pid, body) => call('post', `/projects/${pid}/branches`, body)
export const getBranch = id => call('get', `/branches/${id}`)
export const strategyParamSchema = (bid, sourceId) => call('get', `/branches/${bid}/strategy-param-schema?sourceId=${encodeURIComponent(sourceId)}`)
export const setHeads = (id, body) => call('put', `/branches/${id}/heads`, body)
export const createArtifact = (bid, body) => call('post', `/branches/${bid}/artifacts`, body)
export const getArtifact = id => call('get', `/artifacts/${id}`)
export const captureExternalEvidence = (bid, body) => call('post', `/branches/${bid}/external-evidence`, body)
export const externalHandoff = body => call('post', '/external-handoffs', body)
export const evidenceHypotheses = (bid, body) => call('post', `/branches/${bid}/evidence-hypotheses`, body)
export const prepareHandoff = (bid, body) => call('post', `/branches/${bid}/handoffs`, body)
export const reviewContext = (bid, rid) => call('post', `/branches/${bid}/runs/${rid}/review-context`, {})
export const getDraft = (bid, m) => call('get', `/branches/${bid}/drafts/${m}`).catch(error => {
  // The server verifies the owned branch before returning this exact empty
  // draft envelope. Other NOT_FOUND responses are permission/scope failures,
  // not empty drafts; preserve them and all transport failures unchanged.
  const response = error && error.response
  const body = response && response.data
  const detail = body && body.error
  if (response && response.status === 404 && body && !Array.isArray(body) &&
      Object.keys(body).length === 4 && body.code === 0 && body.msg === '暂无草稿' && body.data === null &&
      detail && !Array.isArray(detail) && Object.keys(detail).length === 2 &&
      detail.type === 'NOT_FOUND' && detail.retriable === false) return { code: 1, msg: 'success', data: null }
  throw error
})
export const putDraft = (bid, m, body) => call('put', `/branches/${bid}/drafts/${m}`, body)
export const deleteDraft = (bid, m, rev) => call('delete', `/branches/${bid}/drafts/${m}?expectedDraftRevision=${encodeURIComponent(rev)}`)
export const startRun = (bid, body, key) => call('post', `/branches/${bid}/runs`, body, { 'Idempotency-Key': key })
export const listRuns = bid => call('get', `/branches/${bid}/runs`)
export const findRunByKey = key => call('get', `/runs?idempotencyKey=${encodeURIComponent(key)}`)
export const getRun = id => call('get', `/runs/${id}`)
export const reconcileRun = id => call('post', `/runs/${id}/reconcile`, {})
export const retryRun = id => call('post', `/runs/${id}/retry`, {})
export const adoptRun = (id, rev) => call('post', `/runs/${id}/adopt`, { expectedBranchRevision: rev })
// stage2b additive endpoints
export const indicatorPreview = (bid, body) => call('post', `/branches/${bid}/indicator-preview`, body)
export const legacyImport = (pid, body) => call('post', `/projects/${pid}/legacy-imports`, body)
// EXISTING AI chat endpoint (V1 engine). Only ever called from an explicit user click.
export const aiChat = (message, context) => request({ url: '/api/ai/chat/message', method: 'post', data: { message, context }, timeout: 180000 })
export const getAdmission = () => call('get', '/admin/admission')
export const putAdmission = body => call('put', '/admin/admission', body)
// legacy (V1) read-only lists used as capture sources
export const listIndicators = userid => request({ url: '/api/indicator/getIndicators', method: 'get', params: { userid } })
export const listScriptSources = () => request({ url: '/api/strategies/script-sources', method: 'get' })
