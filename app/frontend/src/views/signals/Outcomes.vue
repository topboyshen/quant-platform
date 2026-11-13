<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无结果记录">
    <n-text depth="3">共 {{ total }} 条</n-text>
    <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" />
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Outcome { id: number; code: string; name: string; decision_date: string; horizon_sessions: number; net_return: number | null; benchmark_return: number | null; excess_return: number | null; fill_status: string }
const rows = ref<Outcome[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '决策日', key: 'decision_date', width: 110 },
  { title: '持有', key: 'horizon_sessions', width: 70 },
  { title: '净收益', key: 'net_return', width: 90 },
  { title: '基准', key: 'benchmark_return', width: 90 },
  { title: '超额', key: 'excess_return', width: 90 },
  { title: '成交', key: 'fill_status', width: 90 },
]
onMounted(async () => {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Outcome[] }>('/signals/outcomes?limit=200')
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
