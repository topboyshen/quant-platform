// Counts retain their source unit. A missing count never becomes zero.
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
export const validationValue = value => value === null || value === undefined || value === '' ? '未知' : value
export const validationCount = value => Number.isSafeInteger(value) && value >= 0 ? value : null
export function validationProgress (raw) {
  if (!raw || raw.contractVersion !== 'research.validation-progress.v1') throw Error('验证进度契约不一致，未展示旧数据。')
  const stages = value => Array.isArray(value)
? value.map(row => ({
    key: String(row.key || ''),
label: String(row.label || '未命名阶段'),
    count: validationCount(row.count),
unit: String(row.unit || '来源未提供单位'),
    status: String(row.status || 'unknown'),
reason: String(row.reason || '')
  }))
: []
  const profit = object(raw.profit); const etf = object(raw.etf)
  return {
 ...raw,
profit: {
 ...profit,
stages: stages(profit.stages),
risk: object(profit.risk),
    protocol: { ...object(profit.protocol), readiness: Array.isArray(object(profit.protocol).readiness) ? profit.protocol.readiness : [] },
capture: object(profit.capture)
},
    etf: { ...etf, stages: stages(etf.stages) }
}
}
export function createValidationReader ({ state, read, account }) {
  let generation = 0; let active = true
  const invalidate = () => { generation++; state.loading = false; state.data = null; state.error = '' }
  return {
    invalidate,
    activate () { active = true },
    deactivate () { active = false; invalidate() },
    async load () {
      if (!active || state.loading) return
      if (!account()) { invalidate(); state.error = '请登录后读取验证进度。'; return }
      const seq = ++generation; const owner = account()
      const current = () => active && seq === generation && account() === owner
      state.loading = true; state.error = ''; state.data = null
      try {
        const response = await read()
        if (!current()) return
        if (!response || response.code !== 1) throw Error('验证进度读取失败。')
        state.data = validationProgress(response.data)
      } catch (error) {
        if (current()) state.error = '未能读取验证进度；不能据此认定样本为零或全部正常。'
      } finally { if (current()) state.loading = false }
    }
  }
}
