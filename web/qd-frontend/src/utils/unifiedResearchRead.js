// Transport exception for account-bound, read-only consumers only. Not permission granting.
export function isUnifiedResearchRead (config) {
  if (!config || config.unifiedResearchCallerHandlesAuth !== true || (config.method || 'get').toLowerCase() !== 'get') return false
  if (config.baseURL && config.baseURL !== '/') return false
  // request.js adds its own numeric cache-buster to every GET before transport.
  // Accept that one operational value only, never arbitrary API/query options.
  if (config.params) {
    if (typeof config.params !== 'object' || Array.isArray(config.params)) return false
    const keys = Object.keys(config.params)
    if (keys.some(key => key !== '_t')) return false
    if (keys.length && (!Number.isSafeInteger(config.params._t) || config.params._t <= 0)) return false
  }
  const url = config.url || ''
  return /^\/api\/radar\/(?:market-context|workbench\/(?:overview|nng8|reports))$/.test(url) ||
    /^\/api\/research-lab\/v2\/(?:capabilities|projects|task-operations(?:\?branchId=[A-Za-z0-9_-]+)?|external-operations(?:\/requests\/[A-Za-z0-9_-]{1,128})?|(?:projects|branches|runs|artifacts)\/[A-Za-z0-9_-]+|branches\/[A-Za-z0-9_-]+\/runs)$/.test(url)
}

// Exact registered external-operation write paths handle account changes locally.
// This affects stale authentication responses only; server admission remains required.
export function isExternalOperationWrite (config) {
  return !!config && config.externalOperationsCallerHandlesAuth === true &&
    (config.method || '').toLowerCase() === 'post' && (!config.baseURL || config.baseURL === '/') &&
    /^\/api\/research-lab\/v2\/external-operations\/[A-Za-z0-9_-]{1,128}\/(?:pause|resume|run)$/.test(config.url || '') &&
    !config.params
}
