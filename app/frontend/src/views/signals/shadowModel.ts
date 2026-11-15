/** Display helpers for the shadow-allowed sample table (kept out of the SFC for tests). */
export const RISK_LABEL: Record<string, string> = {
  ok: '正常', warning: '预警', critical: '严重', growth_blocked: '成长受限', unavailable: '不可用',
}

export interface ShadowCell {
  net_return: number | null
  excess_return: number | null
  fill_status: string
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

export function horizonCell(sample: ShadowSample, horizon: number): string {
  const value = sample.outcomes[String(horizon)]
  if (!value) return '待到期'
  return value.fill_status === 'filled' ? pct(value.net_return) : '未成交'
}
