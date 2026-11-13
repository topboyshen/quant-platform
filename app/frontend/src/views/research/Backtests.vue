<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无回测">
    <n-space>
      <n-input v-model:value="q" placeholder="策略或代码" clearable style="width: 220px" @keyup.enter="load" />
      <n-button @click="load">查询</n-button>
      <n-text depth="3">共 {{ total }} 条</n-text>
    </n-space>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :row-props="rowProps" size="small" />
    <n-drawer v-model:show="show" :width="640">
      <n-drawer-content :title="detail ? `回测 #${detail.run.run_id}` : '回测'">
        <template v-if="detail">
          <n-descriptions :column="2" size="small" bordered>
            <n-descriptions-item label="策略">{{ detail.run.strategy_name || '—' }}</n-descriptions-item>
            <n-descriptions-item label="代码">{{ detail.run.symbol || '—' }}</n-descriptions-item>
            <n-descriptions-item label="周期">{{ detail.run.timeframe || '—' }}</n-descriptions-item>
            <n-descriptions-item label="状态">{{ detail.run.status || '—' }}</n-descriptions-item>
            <n-descriptions-item label="权益点">{{ detail.counts.equity_points }}</n-descriptions-item>
            <n-descriptions-item label="成交">{{ detail.counts.trades }}</n-descriptions-item>
          </n-descriptions>
          <n-h4>最近成交</n-h4>
          <n-data-table :columns="tradeColumns" :data="detail.trades" size="small" :max-height="260" />
        </template>
      </n-drawer-content>
    </n-drawer>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NDataTable, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent, NH4, NInput, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Run { run_id: number; strategy_name: string | null; symbol: string | null; timeframe: string | null; status: string | null; created_at: string | null }
interface Detail { run: Run; equity: { index: number; time: string | null; value: number | null }[]; trades: { index: number; time: string | null; side: string | null; price: number | null; amount: number | null; profit: number | null; reason: string | null }[]; counts: { equity_points: number; trades: number } }

const q = ref('')
const rows = ref<Run[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const show = ref(false)
const detail = ref<Detail | null>(null)

const columns = [
  { title: 'ID', key: 'run_id', width: 70 },
  { title: '策略', key: 'strategy_name' },
  { title: '代码', key: 'symbol', width: 100 },
  { title: '周期', key: 'timeframe', width: 80 },
  { title: '状态', key: 'status', width: 100 },
  { title: '创建时间', key: 'created_at', width: 180 },
]
const tradeColumns = [
  { title: '#', key: 'index', width: 50 },
  { title: '时间', key: 'time', width: 160 },
  { title: '方向', key: 'side', width: 70 },
  { title: '价格', key: 'price', width: 90 },
  { title: '盈亏', key: 'profit', width: 90 },
]

async function load() {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Run[] }>(`/research/backtests?limit=100&q=${encodeURIComponent(q.value)}`)
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function rowProps(row: Run) { return { style: 'cursor: pointer', onClick: async () => {
  detail.value = await api<Detail>(`/research/backtests/${row.run_id}`)
  show.value = true
} } }
onMounted(load)
</script>
