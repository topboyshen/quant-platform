<template>
  <n-space vertical>
    <n-pagination v-if="total > PAGE_SIZE" :page="page" :item-count="total" :page-size="PAGE_SIZE"
                  :page-slot="5" @update:page="loadPage" />
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无结果记录">
      <n-text depth="3">共 {{ total }} 条</n-text>
      <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" :scroll-x="1120" />
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NDataTable, NPagination, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

const BASIS_FIELDS = { entry_date: '入场日', entry_open: '入场开盘价', entry_fill: '含滑点入场价' }
interface EntryBasis {
  status: 'conflict' | 'consistent' | 'insufficient'
  horizons: number[]
  conflicts: { field: keyof typeof BASIS_FIELDS; values_by_horizon: Record<string, string | number | null> }[]
}
interface Outcome {
  id: number; code: string; name: string; decision_date: string; horizon_sessions: number
  net_return: number | null; benchmark_return: number | null; excess_return: number | null; fill_status: string
  entry_basis?: EntryBasis | null
}
const rows = ref<Outcome[]>([])
const PAGE_SIZE = 200
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const error = ref('')
let latestRequest = 0
function pct(v: number | null) { return v == null ? '—' : (v * 100).toFixed(2) + '%' }
function entryBasisCell(row: Outcome) {
  const basis = row.entry_basis
  if (!basis) return '暂无核对结果'
  if (basis.status === 'conflict') {
    return h('div', [
      h(NText, { type: 'warning', strong: true }, { default: () => '入场依据冲突' }),
      ...basis.conflicts.map(({ field, values_by_horizon }) => {
        const values = Object.entries(values_by_horizon)
          .sort(([a], [b]) => Number(a) - Number(b))
          .map(([horizon, value]) => `${horizon}日 ${value ?? '缺失'}`)
        return h('div', { key: field }, `${BASIS_FIELDS[field]} ${values.join(' · ')}`)
      }),
    ])
  }
  const periods = basis.horizons.map(horizon => `${horizon}日`).join('、')
  const verdict = basis.status === 'consistent' ? '一致' : '依据不足'
  return periods ? `${verdict} · 已成交持有期 ${periods}` : '依据不足 · 暂无已成交持有期'
}
const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '决策日', key: 'decision_date', width: 110 },
  { title: '持有', key: 'horizon_sessions', width: 70 },
  { title: '净收益', key: 'net_return', width: 100, render: (r: Outcome) => pct(r.net_return) },
  { title: '基准', key: 'benchmark_return', width: 100, render: (r: Outcome) => pct(r.benchmark_return) },
  { title: '超额', key: 'excess_return', width: 100, render: (r: Outcome) => pct(r.excess_return) },
  { title: '成交', key: 'fill_status', width: 90 },
  { title: '已成交持有期的入场依据', key: 'entry_basis', minWidth: 340, render: entryBasisCell },
]
async function loadPage(nextPage: number): Promise<void> {
  const request = ++latestRequest
  page.value = nextPage
  rows.value = []
  error.value = ''
  loading.value = true
  try {
    const offset = (nextPage - 1) * PAGE_SIZE
    const data = await api<{ total: number; rows: Outcome[] }>(`/signals/outcomes?limit=${PAGE_SIZE}&offset=${offset}`)
    if (request !== latestRequest) return
    total.value = data.total
    const lastPage = Math.max(1, Math.ceil(data.total / PAGE_SIZE))
    if (nextPage > lastPage) return loadPage(lastPage)
    rows.value = data.rows
  } catch (e) {
    if (request === latestRequest) error.value = (e as Error).message
  } finally {
    if (request === latestRequest) loading.value = false
  }
}
onMounted(() => loadPage(1))
</script>
