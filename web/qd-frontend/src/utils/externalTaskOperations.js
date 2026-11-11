// Registered nnG8 actions only. A lost reply is resolved by request identity,
// never by dispatching a new operation from a refreshed page.
const object = value => value && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.trim() ? value : null
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value)
const terminal = status => ['succeeded', 'failed'].includes(status)
export const EXTERNAL_ACTIONS = Object.freeze(['pause', 'resume', 'run'])
export const EXTERNAL_STATUS = Object.freeze({ accepted: '已受理，等待执行', running: '正在执行', succeeded: '操作已完成', failed: '操作失败', unknown: '结果尚未确认', active: '正在运行', inactive: '未运行', failed_service: '服务失败', frozen: '已冻结', paused: '调度已暂停', scheduled: '调度已启用', available: '状态可读取', unavailable: '状态不可用' })

export function externalCatalog (response) {
  const data = response && response.code === 1 && response.data
  if (!object(data) || data.contractVersion !== 'research.external-operations.v1' || data.source !== 'nng8' || typeof data.enabled !== 'boolean' || !Array.isArray(data.tasks) || !Array.isArray(data.operations)) throw Error('外部项目操作契约未确认，未开放操作。')
  const seen = new Set()
  for (const task of data.tasks) {
    if (!object(task) || !identifier(task.taskId) || seen.has(task.taskId) || !text(task.label) || !Array.isArray(task.actions) || (!text(task.revision) && (task.state !== 'unavailable' || task.actions.some(action => action.enabled)))) throw Error('外部任务身份或版本不完整，未开放操作。')
    seen.add(task.taskId)
    const ids = new Set()
    for (const action of task.actions) {
      if (!object(action) || !EXTERNAL_ACTIONS.includes(action.id) || ids.has(action.id) || typeof action.enabled !== 'boolean' || action.requiresConfirmation !== true || typeof action.startsExecution !== 'boolean' || !text(action.label)) throw Error('外部任务动作未登记，未开放操作。')
      ids.add(action.id)
    }
  }
  return data
}

export function externalReceipt (response, request) {
  const operation = response && response.code === 1 && response.data && response.data.operation
  if (!object(operation) || operation.clientRequestId !== request.clientRequestId || operation.taskId !== request.taskId || operation.action !== request.action || !text(operation.operationId) || !['accepted', 'running', 'succeeded', 'failed', 'unknown'].includes(operation.status)) throw Error('操作回执身份或状态不完整，结果尚未确认。')
  return operation
}

export function externalFailure (error, lookup = false) {
  const status = error && error.response && error.response.status
  if (status === 401 || status === 403) return { denied: true, message: '当前登录身份无权管理这些外部项目。旧状态已清除，请重新登录或确认管理权限。' }
  if (status === 409) return { refused: true, message: '任务状态或登记版本已经变化；这次操作未按旧版本继续。请关闭确认并重新读取。' }
  if (status === 404 && lookup) return { unknown: true, message: '尚未查到这次请求的可靠回执。请继续查询原请求，不要重复发起动作。' }
  if (status && status < 500) return { refused: true, message: '服务端拒绝这次操作。请重新读取任务状态和可用动作。' }
  return { unknown: true, message: lookup ? '暂时无法查询原请求，结果仍未确认。' : '没有收到可核实的操作结果。请查询原请求；不会重新执行动作。' }
}

export function newExternalOperationState () {
  return { loading: false, saving: false, querying: false, error: '', permission: '', catalog: null, selected: null, confirmed: false, receipt: null, pending: [], outcomeUnknown: false }
}

export function pendingRequestStore (storage) {
  const prefix = 'quant.external.pending.v1.'
  return {
    read (account) {
      try {
        const rows = JSON.parse(storage.getItem(prefix + account) || '[]')
        return Array.isArray(rows) ? rows.filter(row => object(row) && identifier(row.clientRequestId) && identifier(row.taskId) && EXTERNAL_ACTIONS.includes(row.action) && text(row.observedRevision)).slice(-30) : []
      } catch (_) { return [] }
    },
    write (account, rows) { try { storage.setItem(prefix + account, JSON.stringify(rows.slice(-30))); return true } catch (_) { return false } }
  }
}

export function createExternalOperationController ({ state, api, account, key, pending }) {
  let generation = 0; let active = true
  const context = () => ({ account: account(), generation })
  const current = ctx => active && !!ctx.account && ctx.account === account() && ctx.generation === generation
  function invalidate () { generation++; Object.assign(state, newExternalOperationState()) }
  function remember (request, ctx) {
    const rows = pending.read(ctx.account).filter(row => row.clientRequestId !== request.clientRequestId)
    rows.push(request)
    if (!pending.write(ctx.account, rows)) throw Error('无法保存本次请求编号，操作未提交。请检查浏览器存储后再试。')
    state.pending = rows
  }
  function forget (request, ctx) {
    const rows = pending.read(ctx.account).filter(row => row.clientRequestId !== request.clientRequestId)
    pending.write(ctx.account, rows); state.pending = rows
  }
  function clearDenied () {
    state.permission = 'denied'; state.catalog = null; state.selected = null; state.receipt = null; state.pending = []; state.confirmed = false
  }
  return {
    invalidate,
    activate () { active = true },
    deactivate () { active = false; invalidate() },
    async load () {
      if (!active || state.saving || state.querying) return
      invalidate()
      if (!account()) { state.permission = 'login'; state.error = '请登录后读取外部项目。'; return }
      const ctx = context(); state.loading = true; state.pending = pending.read(ctx.account)
      try {
        const catalog = externalCatalog(await api.getExternalOperations())
        if (!current(ctx)) return
        state.catalog = catalog; state.pending = pending.read(ctx.account)
      } catch (error) {
        if (!current(ctx)) return
        const failure = externalFailure(error)
        if (failure.denied) clearDenied()
        state.error = failure.denied ? failure.message : text(error.message) || '外部项目读取失败；不能据此认定全部正常。'
      } finally { if (current(ctx)) state.loading = false }
    },
    choose (taskId, actionId) {
      if (!active || !account() || state.loading || state.saving || state.querying || state.permission || !state.catalog || state.catalog.enabled !== true || state.catalog.readOnly === true) return
      const task = state.catalog.tasks.find(row => row.taskId === taskId)
      const action = task && task.actions.find(row => row.id === actionId)
      if (!action || !action.enabled || state.pending.some(row => row.taskId === taskId)) return
      const clientRequestId = key()
      if (!identifier(clientRequestId)) return
      generation++; state.selected = { taskId, action: actionId, label: task.label, actionLabel: action.label, risk: text(action.effect) || text(action.risk) || text(action.reason) || '服务端将核对本次任务状态并执行已登记动作。', startsExecution: action.startsExecution, observedRevision: task.revision, clientRequestId }
      state.confirmed = false; state.receipt = null; state.error = ''; state.outcomeUnknown = false
    },
    cancel () { if (state.saving || state.querying) return; generation++; state.selected = null; state.confirmed = false; state.receipt = null; state.error = ''; state.outcomeUnknown = false },
    resume (clientRequestId) {
      if (!active || state.saving || state.querying || !account()) return
      const request = state.pending.find(row => row.clientRequestId === clientRequestId)
      if (!request) return
      generation++; state.selected = { ...request }; state.receipt = null; state.error = ''; state.confirmed = false; state.outcomeUnknown = true
    },
    async submit () {
      if (!active || !account() || !state.selected || state.saving || state.querying || !state.confirmed || state.permission || state.outcomeUnknown || state.receipt || state.pending.some(row => row.taskId === state.selected.taskId)) return
      const request = state.selected; const ctx = context(); state.saving = true; state.error = ''; let dispatched = false
      try {
        remember(request, ctx)
        dispatched = true
        const receipt = externalReceipt(await api.operateExternalTask(request.taskId, request.action, { confirmed: true, clientRequestId: request.clientRequestId, observedRevision: request.observedRevision }), request)
        if (!current(ctx) || state.selected !== request) return
        state.receipt = receipt; state.confirmed = false; state.outcomeUnknown = !terminal(receipt.status)
        if (terminal(receipt.status)) forget(request, ctx)
      } catch (error) {
        if (!current(ctx) || state.selected !== request) return
        const failure = dispatched ? externalFailure(error) : { refused: true, message: error.message }; state.error = failure.message; state.confirmed = false; state.outcomeUnknown = !!failure.unknown
        if (failure.refused) forget(request, ctx)
        if (failure.denied) clearDenied()
      } finally { if (current(ctx)) state.saving = false }
    },
    async query () {
      if (!active || !account() || !state.selected || state.saving || state.querying || state.permission) return
      const request = state.selected; const ctx = context(); state.querying = true; state.error = ''
      try {
        const receipt = externalReceipt(await api.getExternalOperationRequest(request.clientRequestId), request)
        if (!current(ctx) || state.selected !== request) return
        state.receipt = receipt; state.outcomeUnknown = !terminal(receipt.status)
        if (terminal(receipt.status)) forget(request, ctx)
      } catch (error) {
        if (!current(ctx) || state.selected !== request) return
        const failure = externalFailure(error, true); state.error = failure.message; state.outcomeUnknown = true
        if (failure.denied) clearDenied()
      } finally { if (current(ctx)) state.querying = false }
    }
  }
}
