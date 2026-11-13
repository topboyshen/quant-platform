<template>
  <n-space vertical>
    <n-alert type="info" :bordered="false">M4 影子验收历史报告；旧链已停止，报告中的差异属历史对比，不代表 qp 当前信号故障。</n-alert>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !checks.length" emptyText="未生成对比报告">
    <n-space>
      <n-date-picker v-model:value="ts" type="date" />
      <n-button @click="load">查询</n-button>
      <n-tag v-if="overall" :type="overall === 'PASS' ? 'success' : overall === 'WARN' ? 'warning' : overall === 'FAIL' ? 'error' : 'default'">
        {{ overall }}
      </n-tag>
      <n-text v-if="error" depth="3">{{ error }}</n-text>
    </n-space>
    <n-data-table v-if="checks.length" :columns="columns" :data="checks" size="small" />
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NDataTable, NDatePicker, NSpace, NTag, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Check { id: string; status: string; summary: string; diffs: string[] }
const ts = ref<number>(Date.now())
const checks = ref<Check[]>([])
const loading = ref(false)
const overall = ref('')
const error = ref('')
const columns = [
  { title: '检查', key: 'id', width: 90 },
  { title: '状态', key: 'status', width: 90 },
  { title: '摘要', key: 'summary' },
  { title: '差异', key: 'diffs', render: (row: Check) => (row.diffs || []).join('; ') },
]
function iso(value: number) {
  const d = new Date(value)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
async function load() {
  error.value = ''
  loading.value = true
  try {
    const data = await api<{ overall: string; checks: Check[] }>(`/signals/compare?date=${iso(ts.value)}`)
    overall.value = data.overall
    checks.value = data.checks
  } catch (e) {
    overall.value = ''
    checks.value = []
    error.value = (e as Error).message
  } finally { loading.value = false }
}
onMounted(load)
</script>
