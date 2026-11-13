<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无盈利研究运行">
    <n-text depth="3">共 {{ total }} 次运行</n-text>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :row-props="rowProps" size="small" />
    <n-drawer v-model:show="show" :width="720">
      <n-drawer-content :title="`运行 #${current?.id ?? ''}`">
        <n-data-table :columns="pickColumns" :data="picks" size="small" :max-height="420" />
      </n-drawer-content>
    </n-drawer>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NDrawer, NDrawerContent, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Run { id: number; signal_date: string | null; status: string | null; candidate_count: number | null; universe_count: number | null; data_ready: boolean | null; reason: string | null }
interface Pick { id: number; symbol: string | null; name: string | null; rank: number | null; state: string | null; realized_return: number | null; latest_unrealized_return: number | null; excess_return: number | null }

const rows = ref<Run[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const show = ref(false)
const current = ref<Run | null>(null)
const picks = ref<Pick[]>([])

const columns = [
  { title: 'ID', key: 'id', width: 70 },
  { title: '信号日', key: 'signal_date', width: 110 },
  { title: '状态', key: 'status' },
  { title: '候选', key: 'candidate_count', width: 70 },
  { title: '股票池', key: 'universe_count', width: 80 },
  { title: '原因', key: 'reason' },
]
const pickColumns = [
  { title: '#', key: 'rank', width: 50 },
  { title: '代码', key: 'symbol', width: 90 },
  { title: '名称', key: 'name' },
  { title: '状态', key: 'state', width: 90 },
  { title: '已实现', key: 'realized_return', width: 90 },
  { title: '未实现', key: 'latest_unrealized_return', width: 90 },
  { title: '超额', key: 'excess_return', width: 90 },
]

async function load() {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Run[] }>('/research/profit/runs?limit=100')
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function rowProps(row: Run) { return { style: 'cursor: pointer', onClick: async () => {
  current.value = row
  picks.value = (await api<{ rows: Pick[] }>(`/research/profit/runs/${row.id}/picks`)).rows
  show.value = true
} } }
onMounted(load)
</script>
