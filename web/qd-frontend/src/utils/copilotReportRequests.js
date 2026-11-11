const reportTarget = value => {
  if (!value || typeof value !== 'object') return null
  const market = String(value.market || '').trim()
  const symbol = String(value.symbol || '').trim().toUpperCase()
  if (!market || !symbol) return null
  return Object.freeze({
    market,
    symbol,
    name: String(value.name || symbol).trim() || symbol
  })
}

export function sameCopilotReportTarget (left, right) {
  const a = reportTarget(left)
  const b = reportTarget(right)
  return Boolean(a && b && a.market === b.market && a.symbol === b.symbol)
}

export function createCopilotReportRequest (sequence, target, messageId, kind = 'analysis') {
  const normalizedTarget = reportTarget(target)
  if (!normalizedTarget) throw new TypeError('A valid report request target is required')
  const current = Number.isSafeInteger(Number(sequence)) ? Number(sequence) : 0
  return Object.freeze({
    id: Math.max(0, current) + 1,
    kind: kind === 'retry' ? 'retry' : 'analysis',
    messageId: String(messageId || ''),
    target: normalizedTarget
  })
}

export function isCurrentCopilotReportRequest (message, request) {
  return Boolean(
    message && request &&
    Number(message.reportRequestId) === request.id &&
    (!request.messageId || String(message.localId || message.id || '') === request.messageId)
  )
}

export function copilotTaskOwnsReportRequest (task, request) {
  return Boolean(
    task && request &&
    task.type === 'market_diagnosis' &&
    Number(task.reportRequestId) === request.id &&
    sameCopilotReportTarget(task.target, request.target)
  )
}

const normalizedRequestIds = values => Array.from(new Set(
  (Array.isArray(values) ? values : [])
    .map(value => Number(value))
    .filter(value => Number.isSafeInteger(value) && value > 0)
))

export function addCopilotReportRequestId (values, request) {
  const ids = normalizedRequestIds(values)
  const id = Number(request && request.id)
  if (Number.isSafeInteger(id) && id > 0 && !ids.includes(id)) ids.push(id)
  return ids
}

export function removeCopilotReportRequestId (values, request) {
  const id = Number(request && request.id)
  return normalizedRequestIds(values).filter(value => value !== id)
}

export async function settleCopilotReportRequest ({
  request,
  execute,
  isCurrent,
  onSuccess,
  onFailure,
  onSettled
}) {
  try {
    let result
    try {
      result = await execute(request)
    } catch (error) {
      if (!isCurrent(request)) return { status: 'stale', error }
      if (onFailure) await onFailure(error, request)
      return { status: 'failure', error }
    }

    if (!isCurrent(request)) return { status: 'stale', result }
    if (onSuccess) await onSuccess(result, request)
    return { status: 'success', result }
  } finally {
    if (onSettled) await onSettled(request)
  }
}

export default {
  addRequestId: addCopilotReportRequestId,
  create: createCopilotReportRequest,
  isCurrent: isCurrentCopilotReportRequest,
  removeRequestId: removeCopilotReportRequestId,
  taskOwns: copilotTaskOwnsReportRequest,
  sameTarget: sameCopilotReportTarget,
  settle: settleCopilotReportRequest
}
