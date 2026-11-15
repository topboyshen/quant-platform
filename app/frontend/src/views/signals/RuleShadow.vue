<template>
  <n-space vertical>
    <n-text depth="3">
      实验规则 qp-swing.v2：日线放量突破 MA60（成交量 ≥ 2×20 日均量）。只写影子表，
      不影响实盘决策、通知或记录链。共 {{ total }} 条 · 结果 {{ summary?.outcomes ?? 0 }} 条
      <template v-if="summary?.last_sample_date"> · 最后样本 {{ summary.last_sample_date }}</template>
    </n-text>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length"
                emptyText="暂无规则影子样本（15:37 任务运行后生成）">
      <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" :scroll-x="960" />
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'
import { horizonCell, riskLabel } from './shadowModel'

const HORIZONS = [1, 3, 5, 10, 20]
interface Cell { net_return: number | null; excess_return: number | null; fill_status: string }
interface Summary { outcomes: number; last_sample_date: string | null }
interface RuleRow {
  id: number; decision_date: string; code: string; name: string; sector: string
  risk_level: string | null; volume_ratio: number | null
  outcomes: Record<string, Cell>
}
const rows = ref<RuleRow[]>([])
const total = ref(0)
const summary = ref<Summary | null>(null)
const loading = ref(false)
const error = ref('')

const columns = [
  { title: '日期', key: 'decision_date', width: 110 },
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name', minWidth: 150 },
  { title: '板块', key: 'sector', width: 90 },
  { title: '风控', key: 'risk_level', width: 100, render: (r: RuleRow) => riskLabel(r.risk_level) },
  { title: '量比', key: 'volume_ratio', width: 80, render: (r: RuleRow) => (r.volume_ratio == null ? '—' : r.volume_ratio.toFixed(2)) },
  ...HORIZONS.map((h) => ({
    title: `${h}日`, key: `h${h}`, width: 90,
    render: (r: RuleRow) => horizonCell(r, h),
  })),
]
onMounted(async () => {
  loading.value = true
  try {
    const data = await api<{ total: number; summary: Summary; rows: RuleRow[] }>(
      '/signals/rule-shadow?limit=200')
    rows.value = data.rows
    total.value = data.total
    summary.value = data.summary
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
