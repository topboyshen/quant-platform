<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无报告">
    <n-space>
      <n-select v-model:value="kind" :options="kindOptions" clearable placeholder="全部类型" style="width: 260px" @update:value="load" />
      <n-text depth="3">共 {{ total }} 份</n-text>
    </n-space>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :row-props="rowProps" size="small" />
    <n-drawer v-model:show="show" :width="720">
      <n-drawer-content :title="detail ? `${detail.report.kind} ${detail.report.business_date} v${detail.report.version}` : '报告'">
        <template v-if="detail">
          <n-descriptions :column="2" size="small" bordered>
            <n-descriptions-item label="状态">{{ detail.report.status }}</n-descriptions-item>
            <n-descriptions-item label="内容 sha">{{ detail.report.content_sha256.slice(0, 16) }}…</n-descriptions-item>
            <n-descriptions-item label="冻结时间">{{ fmt(detail.report.frozen_at) }}</n-descriptions-item>
            <n-descriptions-item label="投递">{{ detail.deliveries.length }}</n-descriptions-item>
          </n-descriptions>
          <n-h4>正文</n-h4>
          <n-card size="small"><pre class="text">{{ detail.report.payload?.text || '（无文本正文）' }}</pre></n-card>
          <n-h4>来源</n-h4>
          <n-data-table :columns="sourceColumns" :data="detail.sources" size="small" />
          <n-h4>投递与回执</n-h4>
          <n-data-table :columns="deliveryColumns" :data="detail.deliveries" size="small" />
        </template>
      </n-drawer-content>
    </n-drawer>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NCard, NDataTable, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent, NH4, NSelect, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'
import { fmt } from '../../util'

interface Report { id: number; kind: string; business_date: string; version: number; status: string; content_sha256: string; frozen_at: string | null }
interface Source { id: number; source: string; locator: string | null; sha256: string | null }
interface Delivery { id: number; channel: string; target: string; mode: string; status: string; sent_at: string | null; receipts: { id: number; status: string; received_at: string | null }[] }
interface Detail { report: Report & { payload: { text?: string } }; sources: Source[]; deliveries: Delivery[] }

const kind = ref<string | null>(null)
const rows = ref<Report[]>([])
const total = ref(0)
const counts = ref<Record<string, number>>({})
const loading = ref(false)
const error = ref('')
const show = ref(false)
const detail = ref<Detail | null>(null)

const columns = [
  { title: '类型', key: 'kind' },
  { title: '业务日', key: 'business_date', width: 110 },
  { title: '版本', key: 'version', width: 70 },
  { title: '状态', key: 'status', width: 90 },
  { title: '冻结时间 (CST)', key: 'frozen_at', width: 180, render: (r: Report) => fmt(r.frozen_at) },
]
const sourceColumns = [
  { title: '来源', key: 'source', width: 160 },
  { title: '位置', key: 'locator' },
  { title: 'sha256', key: 'sha256', width: 140, render: (r: Source) => (r.sha256 || '').slice(0, 16) },
]
const deliveryColumns = [
  { title: '渠道', key: 'channel', width: 90 },
  { title: '模式', key: 'mode', width: 80 },
  { title: '状态', key: 'status', width: 100 },
  { title: '回执', key: 'receipts', render: (r: Delivery) => (r.receipts || []).map(x => x.status).join(', ') || '—' },
]

const kindOptions = ref<{ label: string; value: string }[]>([])
async function load() {
  loading.value = true
  try {
    const q = kind.value ? `?kind=${encodeURIComponent(kind.value)}&limit=200` : '?limit=200'
    const data = await api<{ total: number; counts: Record<string, number>; rows: Report[] }>(`/reports${q}`)
    rows.value = data.rows
    total.value = data.total
    counts.value = data.counts
    kindOptions.value = Object.entries(data.counts).map(([value, n]) => ({ label: `${value} (${n})`, value }))
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function rowProps(row: Report) { return { style: 'cursor: pointer', onClick: async () => {
  detail.value = await api<Detail>(`/reports/${row.id}`)
  show.value = true
} } }
onMounted(load)
</script>
<style scoped>
.text { white-space: pre-wrap; margin: 0; font-family: inherit; }
</style>
