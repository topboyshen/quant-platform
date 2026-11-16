/** Pure display helpers for the independent C13 paper portfolio. */
export const INITIAL_CAPITAL = 1_000_000
export const FORWARD_START = '2026-11-16'
export type RotationPhase = 'forward' | 'replay'
type MaybeNumber = number | null | undefined

export interface RotationMetrics {
  total_return?: number | null
  cagr?: number | null
  vol?: number | null
  sharpe?: number | null
  max_drawdown?: number | null
}

export interface RotationPosition {
  code?: string
  name?: string
  shares?: number | null
  close?: number | null
  value?: number | null
  weight?: number | null
}

export interface RotationPick {
  code?: string
  name?: string
  score?: number | null
  weight?: number | null
}

export interface RotationFill {
  code?: string
  side?: string
  qty?: number | null
  price?: number | null
  fee?: number | null
}

export interface RotationRun {
  id?: number
  signal_date?: string | null
  execute_date?: string | null
  status?: string | null
  universe?: number | null
  nav_before?: number | null
  nav_after?: number | null
  turnover?: number | null
  selected?: RotationPick[] | null
  fills?: RotationFill[] | null
  detail?: Record<string, unknown> | null
}

export interface RotationSeriesPoint {
  date: string
  nav?: number | null
  benchmark_nav?: number | null
  ew_nav?: number | null
}

export interface RotationResponse {
  rule_version?: string | null
  parameters?: {
    lookback?: number | null
    top_k?: number | null
    rebalance?: number | null
    initial?: number | null
    commission_rate?: number | null
    slippage_rate?: number | null
    minimum_amount?: number | null
  } | null
  phase?: RotationPhase
  forward_start?: string | null
  days?: number | null
  start?: string | null
  last_date?: string | null
  nav?: number | null
  cash?: number | null
  total_return?: number | null
  metrics?: RotationMetrics | null
  benchmark?: { nav?: number | null; metrics?: RotationMetrics | null } | null
  ew?: { nav?: number | null; metrics?: RotationMetrics | null } | null
  positions?: RotationPosition[] | null
  runs?: RotationRun[] | null
  last_run?: RotationRun | null
  next_rebalance_date?: string | null
  next_execute_date?: string | null
  pending?: RotationRun | null
  series?: RotationSeriesPoint[] | null
  quality?: {
    status?: string | null
    message?: string | null
    missing_codes?: string[] | null
    stale_date?: string | null
  } | null
}

export interface RotationSnapshot extends RotationResponse {
  phase: RotationPhase
  positions: RotationPosition[]
  runs: RotationRun[]
  series: RotationSeriesPoint[]
}

export interface IndexedPoint {
  date: string
  c13: number | null
  ew: number | null
  benchmark: number | null
}

export function finite(value: MaybeNumber): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function numberText(value: MaybeNumber, digits = 0): string {
  const n = finite(value)
  return n == null ? '—' : n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  })
}

export function currency(value: MaybeNumber): string {
  return finite(value) == null ? '—' : `¥${numberText(value, 2)}`
}

export function percent(value: MaybeNumber): string {
  const n = finite(value)
  return n == null ? '—' : `${numberText(n * 100, 2)}%`
}

export function weight(value: MaybeNumber): string { return percent(value) }
export function indexText(value: MaybeNumber): string { return numberText(value, 4) }

export function phaseLabel(phase: RotationPhase): string {
  return phase === 'replay' ? '历史回放 · 仅对照' : '前向观察'
}

export function dayLabel(phase: RotationPhase): string {
  return phase === 'replay' ? '回放交易日（不计前向）' : '前向交易日'
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return ({ pending: '待模拟执行', executed: '已模拟执行', blocked: '已阻断' } as Record<string, string>)[status] || status
}

export function statusTone(status: string | null | undefined): 'pending' | 'executed' | 'blocked' | 'neutral' {
  return status === 'pending' || status === 'executed' || status === 'blocked' ? status : 'neutral'
}

export function sideLabel(side: string | null | undefined): string {
  if (!side) return '—'
  return ({ buy: '模拟买入', sell: '模拟卖出' } as Record<string, string>)[side.toLowerCase()] || side
}

export function normalizeRotation(data: RotationResponse | null | undefined, phase: RotationPhase): RotationSnapshot {
  // Never display a replay payload under the forward label, even on a stale response.
  if (data?.phase != null && data.phase !== phase) throw new Error('返回区间与所选区间不一致，请重试')
  return {
    ...data,
    phase,
    positions: Array.isArray(data?.positions) ? data.positions : [],
    runs: Array.isArray(data?.runs) ? data.runs : [],
    series: Array.isArray(data?.series) ? data.series : [],
  }
}

/** All three capital series use the same initial capital, NOT their first visible value.
 * Missing quotes remain gaps; an absent benchmark is never silently rebased later. */
export function indexedSeries(series: RotationSeriesPoint[] | null | undefined, initial?: number | null): IndexedPoint[] {
  const capital = initial == null ? INITIAL_CAPITAL : finite(initial)
  const index = (value: MaybeNumber) => {
    const n = finite(value)
    return n == null || capital == null || capital <= 0 ? null : n / capital
  }
  const dates = new Map<string, RotationSeriesPoint>()
  for (const row of series || []) {
    if (row && typeof row.date === 'string' && row.date) dates.set(row.date, row)
  }
  return [...dates.values()].sort((a, b) => a.date.localeCompare(b.date)).map((row) => ({
    date: row.date, c13: index(row.nav), ew: index(row.ew_nav), benchmark: index(row.benchmark_nav),
  }))
}

export function isRotationEmpty(data: RotationSnapshot): boolean {
  return finite(data.nav) == null && !data.series.length && !data.positions.length &&
    !data.runs.length && !data.last_run && !data.pending
}

export function recentRuns(data: RotationSnapshot | null | undefined): RotationRun[] {
  const rows = [...(data?.runs || [])]
  if (data?.last_run && !rows.some((r) => r === data.last_run ||
    (r.id != null && r.id === data.last_run?.id))) rows.push(data.last_run)
  return rows.sort((a, b) => (b.signal_date || '').localeCompare(a.signal_date || '') || (b.id ?? 0) - (a.id ?? 0))
}

export function runReason(run: RotationRun | null | undefined): string {
  for (const key of ['reason', 'message', 'blocked_reason']) {
    const value = run?.detail?.[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

/** Shared single-axis bounds, with tidy ticks even for an initial flat baseline. */
export function navScale(points: IndexedPoint[]): { min: number; max: number; ticks: number[]; digits: number } {
  const values = points.flatMap((p) => [p.c13, p.ew, p.benchmark]).filter((n): n is number => n != null && Number.isFinite(n))
  if (!values.length) return { min: 0.99, max: 1.01, ticks: [], digits: 2 }
  let low = Math.min(...values), high = Math.max(...values)
  const pad = high === low ? Math.max(Math.abs(high) * 0.01, 0.01) : (high - low) * 0.1
  low -= pad
  high += pad
  const raw = (high - low) / 4
  const power = 10 ** Math.floor(Math.log10(raw))
  const factor = [1, 2, 2.5, 5, 10].find((n) => n * power >= raw) || 10
  const step = factor * power
  const min = Math.floor(low / step) * step
  const max = Math.ceil(high / step) * step
  const count = Math.round((max - min) / step)
  const ticks = Array.from({ length: count + 1 }, (_, i) => Number((min + i * step).toPrecision(12)))
  const digits = Math.min(8, Math.max(2, Math.ceil(-Math.log10(step)) + (factor === 2.5 ? 1 : 0)))
  return { min, max, ticks, digits }
}
