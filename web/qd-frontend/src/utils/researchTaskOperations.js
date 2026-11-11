// Explicit, account-bound operations. Reads never call an execution endpoint.
const plain = value => value && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.trim() ? value : null
const actions = new Set(['reconcile', 'retry'])
export const TASK_ACTION_LABELS = Object.freeze({ reconcile: '对账这次运行', retry: '重试这次中断运行' })

export function taskOperationsResponse (response) {
  const data = response && response.code === 1 && response.data
  if (!plain(data) || data.contractVersion !== 'research.task-operations.v1' || !Array.isArray(data.operations)) throw new Error('任务操作接口契约未确认，未开放操作。')
  const ids = new Set()
  for (const row of data.operations) {
    if (!plain(row) || !plain(row.run) || !text(row.run.id) || ids.has(row.run.id) || !Array.isArray(row.actions)) throw new Error('运行身份不完整或重复，未开放操作。')
    ids.add(row.run.id)
    for (const action of row.actions) {
      if (!plain(action) || !actions.has(action.id) || typeof action.enabled !== 'boolean' || action.requiresConfirmation !== true || typeof action.startsExecution !== 'boolean') throw new Error('任务动作契约未确认，未开放操作。')
    }
  }
  return data
}

export function operationSnapshot (row, actionId, clientRequestId) {
  const action = row && Array.isArray(row.actions) && row.actions.find(item => item.id === actionId)
  if (!action || action.enabled !== true || action.requiresConfirmation !== true || !actions.has(actionId) || !text(row.run && row.run.id) || !text(row.run.status) || !text(clientRequestId)) return null
  return {
    runId: row.run.id,
    action: actionId,
    startsExecution: action.startsExecution,
    body: { confirmed: true, observedExecutionId: text(row.run.execution && row.run.execution.id), observedStatus: row.run.status, clientRequestId }
  }
}

export function taskOperationFailure (error) {
  const status = error && error.response && error.response.status
  if ([401, 403, 404].includes(status)) return { denied: true, unknown: false, message: '当前身份无权操作或任务不存在；未继续执行。请重新读取。' }
  if (status === 409) return { denied: false, unknown: false, message: '运行身份或状态已变化，未按旧状态操作。请关闭确认并重新读取。' }
  if (!status || status >= 500) return { denied: false, unknown: true, message: '操作结果尚未确认。请保留原请求，先重新对账；用原请求确认不会改变操作对象。' }
  return { denied: false, unknown: false, message: '操作被服务端拒绝；未绕过权限、容量或执行规则。' }
}

export function newTaskOperationState () {
  return { loading: false, saving: false, error: '', permission: '', operations: [], externalReason: '', selected: null, confirmed: false, outcomeUnknown: false, receipt: null }
}

export function createTaskOperationController ({ state, api, account, key }) {
  let generation = 0; let active = true
  const current = ctx => active && ctx.account && account() === ctx.account && ctx.generation === generation
  const context = () => ({ account: account(), generation })
  function invalidate () { generation++; Object.assign(state, newTaskOperationState()) }
  return {
    invalidate,
    activate () { active = true },
    deactivate () { active = false; invalidate() },
    async load (branchId = '') {
      if (!active || state.saving) return
      invalidate()
      if (!account()) { state.permission = 'login'; state.error = '请先登录后读取自己的运行记录。'; return }
      const ctx = context(); state.loading = true
      try {
        const data = taskOperationsResponse(await api.getTaskOperations(branchId))
        if (!current(ctx)) return
        state.operations = data.operations
        state.externalReason = text(data.externalOperations && data.externalOperations.reason) || '远端任务未登记受控操作通道；冻结任务不可解除。'
      } catch (error) {
        if (!current(ctx)) return
        const failure = taskOperationFailure(error)
        state.error = failure.denied ? failure.message : text(error && error.message) || '任务读取失败，不代表没有任务。'
        state.permission = failure.denied ? 'denied' : ''
      } finally { if (current(ctx)) state.loading = false }
    },
    choose (runId, actionId) {
      if (!active || state.loading || state.saving || !account()) return
      const row = state.operations.find(item => item.run.id === runId)
      const snapshot = operationSnapshot(row, actionId, key())
      if (!snapshot) return
      generation++; state.selected = snapshot; state.confirmed = false; state.error = ''; state.outcomeUnknown = false; state.receipt = null
    },
    cancel () {
      if (state.saving) return
      generation++; state.selected = null; state.confirmed = false; state.error = ''; state.outcomeUnknown = false
    },
    async submit () {
      if (!active || !account() || !state.selected || state.saving || state.confirmed !== true) return
      const selected = state.selected; const ctx = context()
      state.saving = true; state.error = ''; state.receipt = null
      try {
        const response = await api.operateTask(selected.runId, selected.action, { ...selected.body })
        if (!current(ctx) || state.selected !== selected) return
        if (!response || response.code !== 1 || !plain(response.data)) throw new Error('操作回执不完整。')
        const receiptRun = response.data.run || (response.data.operation && response.data.operation.run)
        if (!receiptRun || receiptRun.id !== selected.runId) throw new Error('操作回执与所选运行身份不一致。')
        state.receipt = response.data; state.outcomeUnknown = false; state.confirmed = false
      } catch (error) {
        if (!current(ctx) || state.selected !== selected) return
        const failure = taskOperationFailure(error)
        state.error = failure.message; state.outcomeUnknown = failure.unknown
        if (!failure.unknown) state.confirmed = false
        if (failure.denied) { state.operations = []; state.permission = 'denied' }
      } finally { if (current(ctx) && state.selected === selected) state.saving = false }
    }
  }
}
