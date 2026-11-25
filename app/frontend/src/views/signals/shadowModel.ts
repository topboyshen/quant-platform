/** Display helpers for the shadow-allowed sample table (kept out of the SFC for tests). */
export const RISK_LABEL: Record<string, string> = {
  ok: '正常', warning: '预警', critical: '严重', growth_blocked: '成长受限', unavailable: '不可用',
}

export interface ShadowCell {
  net_return: number | null
  excess_return: number | null
  fill_status: string
  return_role?: string | null
  executable_net_return?: number | null
}

export const RETURN_ROLE_LABEL: Record<string, string> = {
  executable: '可执行',
  counterfactual_ungated: '未过闸反事实',
  research_counterfactual: '非信号研究',
  unknown: '无依据',
}

export interface ShadowSample {
  id: number
  decision_date: string
  code: string
  name: string
  risk_level: string | null
  reason_code: string
  raw_trigger_count: number
  outcomes: Record<string, ShadowCell>
}

export function riskLabel(level: string | null | undefined): string {
  if (!level) return '—'
  return RISK_LABEL[level] || level
}

export function pct(value: number | null | undefined): string {
  return value == null ? '—' : (value * 100).toFixed(2) + '%'
}

export function returnRoleLabel(role: string | null | undefined): string {
  if (!role) return '无依据'
  return RETURN_ROLE_LABEL[role] || role
}

export function horizonCell(sample: { outcomes: Record<string, ShadowCell> }, horizon: number): string {
  const value = sample.outcomes[String(horizon)]
  if (!value) return '待到期'
  if (value.fill_status !== 'filled') return '未成交'
  const text = pct(value.net_return)
  if (!value.return_role || value.return_role === 'unknown') return `无依据 ${text}`
  if (value.return_role === 'executable') return `获准未校验 ${text}`
  return `反事实 ${text}`
}
