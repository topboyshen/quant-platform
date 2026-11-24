export const STRATEGY_DIMENSIONS: Record<string, string> = {
  eligibility: '资格',
  sector: '板块',
  signal_type: '信号类型',
  origin: '数据来源',
  signal_state: '信号状态',
  risk_state: '风控状态',
}

export interface AttrStats {
  n: number
  eligible_n: number
  win_rate: number | null
  avg_net_return: number | null
  avg_excess_return: number | null
  sample_status: string
  sample_status_label: string
}

export interface AttrGroup extends AttrStats {
  bucket: string
}

export interface AttrReport {
  as_of: string
  horizon: number
  threshold: number
  group_by: string[]
  overall: AttrStats
  eligible: AttrStats
  sample: { matured_filled: number; eligible_matured: number }
  groups: Record<string, AttrGroup[]>
  warnings: { code: string; message: string }[]
}

export type StrategySort = 'avg_excess_return' | 'avg_net_return' | 'win_rate' | 'n'

export interface StrategyRow extends AttrGroup {
  dimension: string
  dimension_label: string
  rank: number
}

export interface StrategyOptions {
  minSamples?: number
  dimension?: string | null
  hideUnknown?: boolean
  hideBaseline?: boolean
  sortBy?: StrategySort
}

/** Flatten the six attribution dimensions into one ranked, sample-gated table. */
export function buildStrategyRows(report: AttrReport, options: StrategyOptions = {}): StrategyRow[] {
  const minSamples = options.minSamples ?? 5
  const dimension = options.dimension || null
  const hideUnknown = options.hideUnknown ?? true
  const hideBaseline = options.hideBaseline ?? true
  const sortBy = options.sortBy ?? 'avg_excess_return'
  const dimensions = dimension ? [dimension] : report.group_by
  const rows: Omit<StrategyRow, 'rank'>[] = []
  for (const dim of dimensions) {
    for (const group of report.groups[dim] || []) {
      if (group.n < minSamples) continue
      if (hideUnknown && group.bucket === 'unknown') continue
      if (hideBaseline && group.sample_status === 'baseline_only') continue
      rows.push({
        ...group,
        dimension: dim,
        dimension_label: STRATEGY_DIMENSIONS[dim] || dim,
      })
    }
  }
  rows.sort((a, b) => {
    const av = a[sortBy]
    const bv = b[sortBy]
    const an = av === null || av === undefined || Number.isNaN(av) ? -Infinity : Number(av)
    const bn = bv === null || bv === undefined || Number.isNaN(bv) ? -Infinity : Number(bv)
    if (bn !== an) return bn - an
    if (b.n !== a.n) return b.n - a.n
    return a.bucket.localeCompare(b.bucket, 'zh-CN')
  })
  return rows.map((row, index) => ({ ...row, rank: index + 1 }))
}
