import request from '@/utils/request'

// Account-scoped reads; explicit task writes require server admission and confirmation.
const read = url => request({ url, method: 'get', unifiedResearchCallerHandlesAuth: true })
const base = '/api/research-lab/v2'
export const capabilities = () => read(base + '/capabilities')
export const listProjects = () => read(base + '/projects')
export const getProject = id => read(base + '/projects/' + encodeURIComponent(id))
export const getBranch = id => read(base + '/branches/' + encodeURIComponent(id))
export const listRuns = id => read(base + '/branches/' + encodeURIComponent(id) + '/runs')
export const getRun = id => read(base + '/runs/' + encodeURIComponent(id))
export const getArtifact = id => read(base + '/artifacts/' + encodeURIComponent(id))
export const getNng8Readonly = () => read('/api/radar/workbench/nng8')
export const getWorkbenchOverview = () => read('/api/radar/workbench/overview')
export const getRadarMarketContext = () => read('/api/radar/market-context')
export const getResearchEvidenceReports = () => read('/api/radar/workbench/reports')
export const getTaskOperations = branchId => read(base + '/task-operations' + (branchId ? '?branchId=' + encodeURIComponent(branchId) : ''))
export const getExternalOperations = () => read(base + '/external-operations')
export const getResearchValidationProgress = () => read(base + '/validation/progress')
export const getExternalOperationRequest = id => read(base + '/external-operations/requests/' + encodeURIComponent(id))
export const operateExternalTask = (taskId, action, body) => request({
  url: base + '/external-operations/' + encodeURIComponent(taskId) + '/' + encodeURIComponent(action),
  method: 'post',
  data: body,
  timeout: 30000,
  externalOperationsCallerHandlesAuth: true
})
export const operateTask = (runId, action, body) => request({
  url: base + '/task-operations/' + encodeURIComponent(runId) + '/' + encodeURIComponent(action),
  method: 'post',
data: body,
timeout: action === 'retry' ? 600000 : 30000,
unifiedResearchCallerHandlesAuth: true
})
