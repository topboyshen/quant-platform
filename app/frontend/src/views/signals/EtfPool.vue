<template>
  <n-tabs v-model:value="tab" type="line">
    <n-tab-pane name="pool" tab="研究池">
  <n-space vertical>
    <n-space align="center">
      <n-date-picker v-model:value="ts" type="date" clearable placeholder="最新交易日" />
      <n-button @click="load">查询</n-button>
      <n-text depth="3">{{ data?.decision_date ? `交易日 ${data.decision_date}` : '暂无 EOD 运行' }}</n-text>
      <n-tag v-for="(n, state) in data?.counts || {}" :key="state" size="small"
             :type="state === 'allowed' ? 'success' : state === 'blocked' ? 'warning' : state === 'data_failed' ? 'error' : 'default'">
        {{ state }} {{ n }}
      </n-tag>
    </n-space>
    <n-alert v-if="data?.source === 'chain'" type="info" :show-icon="true">
      读取自记录链快照
      <template v-if="data.snapshot"> {{ data.snapshot.snapshot_id }}（{{ data.snapshot.status }}）</template>
      <template v-else>：该交易日没有快照</template>
      · 收盘字段记录链不存储，显示为 —
    </n-alert>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !items.length" :emptyText="emptyText">
      <n-data-table :columns="columns" :data="items" size="small" :row-props="rowProps" />
    </AsyncState>
    <n-drawer v-model:show="show" :width="640">
      <n-drawer-content :title="current ? `${current.code} ${current.name}` : 'ETF'">
        <template v-if="current">
          <n-descriptions :column="2" size="small" bordered>
            <n-descriptions-item label="状态">{{ current.state }}</n-descriptions-item>
            <n-descriptions-item label="风控">{{ current.risk_action || '—' }}</n-descriptions-item>
            <n-descriptions-item label="风险级别">{{ current.risk_level || '—' }}</n-descriptions-item>
            <n-descriptions-item label="收盘">{{ current.reference_close ?? '—' }}</n-descriptions-item>
            <n-descriptions-item label="原因">{{ current.reason_code || '—' }}</n-descriptions-item>
            <n-descriptions-item label="触发数">{{ current.raw_trigger_count ?? '—' }}</n-descriptions-item>
          </n-descriptions>
          <n-h4>趋势信号</n-h4>
          <n-text v-if="chainRead" depth="3">
            记录链读取：信号明细不在链内，仅显示触发数 {{ current.raw_trigger_count ?? 0 }}。
          </n-text>
          <template v-else>
          <n-empty v-if="!current.signals.length" description="无信号" />
          <n-card v-for="(s, i) in current.signals" :key="i" size="small" style="margin-bottom: 8px">
            <n-space vertical size="small">
              <n-text strong>{{ s.title || s.signal_type }}</n-text>
              <n-text depth="3">{{ s.signal_type }} · {{ s.period }} · 数据日 {{ s.data_date || '—' }} · 置信 {{ s.confidence ?? '—' }}</n-text>
            </n-space>
          </n-card>
          </template>
        </template>
      </n-drawer-content>
    </n-drawer>
  </n-space>
    </n-tab-pane>
    <n-tab-pane name="chain" tab="记录链">
      <ChainPanel />
    </n-tab-pane>
  </n-tabs>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NDatePicker, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent, NEmpty, NH4, NSpace, NTabPane, NTabs, NTag, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import ChainPanel from './ChainPanel.vue'
import { api } from '../../api/client'

interface Signal { signal_type: string; period: string; data_date: string | null; title: string; confidence: number | null }
interface Item { code: string; name: string; sector: string; state: string; risk_action: string | null; risk_level: string | null; reason_code: string | null; reference_close: number | null; raw_trigger_count: number | null; signal_count: number; signals: Signal[] }
interface Snapshot { snapshot_id: string; digest: string; status: string | null }
interface Pool { decision_date: string | null; counts: Record<string, number>; items: Item[]; source: string; snapshot: Snapshot | null }

const tab = ref('pool')
const ts = ref<number | null>(null)
const data = ref<Pool | null>(null)
const chainRead = computed(() => data.value?.source === 'chain')
const emptyText = computed(() => (data.value?.source === 'chain'
  ? '该交易日没有记录链快照（QP_CHAIN_READS 已开启）'
  : '暂无 ETF 数据'))
const items = ref<Item[]>([])
const loading = ref(false)
const error = ref('')
const show = ref(false)
const current = ref<Item | null>(null)

const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '状态', key: 'state', width: 132 },
  { title: '板块', key: 'sector', width: 100 },
  { title: '风控', key: 'risk_action', width: 90 },
  { title: '原因', key: 'reason_code', width: 140 },
  { title: '收盘', key: 'reference_close', width: 90 },
  { title: '信号', key: 'signal_count', width: 70, render: (r: Item) => r.signal_count },
]
function iso(value: number) {
  const d = new Date(value)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const query = ts.value ? `?date=${iso(ts.value)}` : ''
    data.value = await api<Pool>(`/signals/etf-pool${query}`)
    items.value = data.value.items
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
function rowProps(row: Item) { return { style: 'cursor: pointer', onClick: () => { current.value = row; show.value = true } } }
onMounted(load)
</script>
