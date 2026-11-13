<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="当日无趋势信号">
    <n-space>
      <n-date-picker v-model:value="ts" type="date" />
      <n-button @click="load">查询</n-button>
    </n-space>
    <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" />
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NDataTable, NDatePicker, NSpace } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Signal { id: number; code: string; name: string; signal_type: string; period: string; data_date: string | null; title: string; confidence: number | null }
const ts = ref<number>(Date.now())
const rows = ref<Signal[]>([])
const loading = ref(false)
const error = ref('')
const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '类型', key: 'signal_type' },
  { title: '周期', key: 'period', width: 60 },
  { title: '数据日', key: 'data_date', width: 110 },
  { title: '标题', key: 'title' },
  { title: '置信', key: 'confidence', width: 80 },
]
function iso(value: number) {
  const d = new Date(value)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
async function load() {
  loading.value = true
  try { rows.value = (await api<{ rows: Signal[] }>(`/signals/trends?date=${iso(ts.value)}`)).rows } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
onMounted(load)
</script>
