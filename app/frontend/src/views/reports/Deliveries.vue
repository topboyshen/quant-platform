<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无投递记录">
    <n-text depth="3">共 {{ total }} 次投递（影子期 mode 应为 shadow，回执单独记录）</n-text>
    <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" />
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Row { id: number; kind: string; business_date: string; version: number; channel: string; mode: string; status: string; sent_at: string | null; receipt_status: string | null; received_at: string | null }
const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const columns = [
  { title: '类型', key: 'kind' },
  { title: '业务日', key: 'business_date', width: 110 },
  { title: '版本', key: 'version', width: 70 },
  { title: '渠道', key: 'channel', width: 90 },
  { title: '模式', key: 'mode', width: 80 },
  { title: '发送状态', key: 'status', width: 100 },
  { title: '回执状态', key: 'receipt_status', width: 100 },
  { title: '回执时间', key: 'received_at', width: 180 },
]
onMounted(async () => {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Row[] }>('/reports/deliveries?limit=200')
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
