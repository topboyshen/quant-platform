import { effectivenessProgressView } from './effectivenessProgress.js'
import { homeProfitReports } from './homePresentation.js'
import { workbenchItems } from './workbench.js'
import { nng8Status } from './nng8Readonly.js'

export const RESEARCH_SECTIONS = Object.freeze({
  quality: { title: '数据质量', description: '先核对这一次回测的来源，再阅读全局数据摘要。两种范围不互相替代。' },
  validation: { title: '验证与复盘', description: '四套研究各自保留规则、样本与统计口径，不合并成总成绩。' },
  operations: { title: '任务与运行', description: '统一管理 nnG8 外部项目与自己的研究运行，先核对状态和影响，再确认已登记的操作。' }
})

export function researchSection (value) {
  return Object.prototype.hasOwnProperty.call(RESEARCH_SECTIONS, value) ? value : 'reports'
}

export function validationView ({ nng8, overview, radar }) {
  const calibration = (nng8 && nng8.calibration) || []
  const profitSource = workbenchItems(overview).find(row => row.id === 'profit_radar') || null
  return {
    ledger: calibration.find(row => row.id === 'ledger') || null,
    phase2: calibration.find(row => row.id === 'phase2') || null,
    etf: effectivenessProgressView(radar && radar.signals && radar.signals.effectiveness),
    profit: homeProfitReports(overview),
    profitSource
  }
}

export function operationalView (nng8, overview) {
  const states = {
    ok: ['证据可读取', 'neutral'],
failed: ['失败', 'bad'],
stale: ['证据陈旧', 'warn'],
    unavailable: ['不可用', 'bad'],
not_integrated: ['待接入', 'neutral'],
    running: ['运行中', 'neutral'],
not_due: ['尚未到验收时点', 'neutral'],
    recovered: ['业务已恢复（保留首次失败）', 'neutral'],
awaiting_retry: ['等待重试', 'warn'],
    evidence_missing: ['证据缺失', 'warn']
  }
  const external = ((nng8 && nng8.services) || []).map((row, index) => ({
    key: 'nng8:' + index,
label: row.label || '未命名任务',
source: 'nnG8 捕获副本',
    state: nng8Status(row.status),
lastRun: row.last_run,
nextRun: row.next_run,
    cutoff: null,
detail: '未提供单次执行编号或结果详情；进程运行不代表任务成功。'
  }))
  const local = workbenchItems(overview).filter(row => ['hermes_status', 'eod_status', 'execution_digest', 'release_version'].includes(row.id)).map((row, index) => {
    const [label, tone] = states[row.status] || ['未知', 'neutral']
    return {
 key: 'quant:' + index,
label: row.label || row.id,
source: 'Quant-Server 工作台',
      state: { label, tone },
lastRun: null,
nextRun: null,
cutoff: row.data_cutoff || null,
      detail: typeof row.detail === 'string' ? row.detail : '来源未提供原因。'
}
  })
  return [...local, ...external]
}

export function sourceFailure (error) {
  const status = error && error.response && error.response.status
  return {
 denied: [401, 403, 404].includes(status),
sessionLost: status === 401,
message: status === 401
    ? '登录状态已失效，请重新登录。'
    : [403, 404].includes(status)
? '当前账号没有此来源的访问权限，或该来源未开放。'
      : '本次读取失败，未取得当前证据。请稍后重试。'
}
}
