<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无 EOD 运行">
    <n-text depth="3">共 {{ total }} 次运行</n-text>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :row-props="rowProps" size="small" />
    <n-drawer v-model:show="show" :width="760">
      <n-drawer-content :title="current ? `运行 #${current.run_id}` : '运行'">
        <template v-if="current">
          <n-descriptions :column="3" size="small" bordered>
            <n-descriptions-item label="交易日">{{ current.decision_date }}</n-descriptions-item>
            <n-descriptions-item label="状态">{{ current.status }}</n-descriptions-item>
            <n-descriptions-item label="结论">{{ current.outcome || '—' }}</n-descriptions-item>
            <n-descriptions-item label="信号">{{ current.signal_count }}</n-descriptions-item>
            <n-descriptions-item label="放行">{{ current.allowed }}</n-descriptions-item>
            <n-descriptions-item label="拦截">{{ current.blocked }}</n-descriptions-item>
          </n-descriptions>
          <n-h4>目标决策</n-h4>
          <n-data-table :columns="decisionColumns" :data="decisions" size="small" :max-height="360" />
        </template>
      </n-drawer-content>
    </n-drawer>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent, NH4, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Run { run_id: number; decision_date: string; attempt_no: number; status: string; outcome: string | null; signal_count: number; allowed: number; blocked: number }
interface Decision { id: number; code: string; name: string; decision_state: string; risk_action: string; risk_level: string | null; reason_code: string }

const rows = ref<Run[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const show = ref(false)
const current = ref<Run | null>(null)
const decisions = ref<Decision[]>([])

const columns = [
  { title: 'ID', key: 'run_id', width: 70 },
  { title: '交易日', key: 'decision_date', width: 110 },
  { title: '次', key: 'attempt_no', width: 50 },
  { title: '状态', key: 'status', width: 100 },
  { title: '结论', key: 'outcome', width: 130 },
  { title: '信号', key: 'signal_count', width: 70 },
  { title: '放行', key: 'allowed', width: 70 },
  { title: '拦截', key: 'blocked', width: 70 },
]
const decisionColumns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '状态', key: 'decision_state', width: 100 },
  { title: '风控', key: 'risk_action', width: 90 },
  { title: '原因', key: 'reason_code', width: 140 },
]

async function load() {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Run[] }>('/signals/runs?limit=100')
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function rowProps(row: Run) { return { style: 'cursor: pointer', onClick: async () => {
  current.value = row
  decisions.value = (await api<{ decisions: Decision[] }>(`/signals/runs/${row.run_id}`)).decisions
  show.value = true
} } }
onMounted(load)
</script>
