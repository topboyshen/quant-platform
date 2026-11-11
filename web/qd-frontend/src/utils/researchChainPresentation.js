export const RESEARCH_STAGES = Object.freeze([
  { path: '/ai-asset-analysis', label: 'AI 资产分析' },
  { path: '/indicator-ide', label: '指标研究' },
  { path: '/strategy-ide', label: '策略研究' },
  { path: '/backtest-center', label: '回测验证' }
])

const SOURCE_STATES = Object.freeze({
  ready: { label: '来源已核验', color: 'green' },
  temporary: { label: '仅临时引用', color: 'blue' },
  stale: { label: '来源已过期', color: 'orange' },
  unavailable: { label: '来源不可读取', color: 'red' },
  unknown: { label: '版本未知', color: '' }
})

export function researchStageIndex (path) {
  return RESEARCH_STAGES.findIndex(stage => stage.path === String(path || ''))
}

export function researchSourceState (status) {
  return SOURCE_STATES[String(status || '')] || SOURCE_STATES.unknown
}
