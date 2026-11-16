<template>
  <n-space vertical size="large">
    <n-alert type="info" :bordered="false">
      研究只读评分：固定当前 ETF 池、离线模型快照；不是交易信号，不下单、不通知、不写正式记录链。
    </n-alert>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !data?.run"
                emptyText="暂无 ETF 评分快照（离线模型出分后导入）">
      <template v-if="data?.run">
        <n-space align="center" justify="space-between">
          <n-h3 style="margin: 0">ETF 研究评分</n-h3>
          <n-button size="small" @click="load">刷新</n-button>
        </n-space>
        <n-descriptions :column="3" size="small" bordered>
          <n-descriptions-item label="模型版本">{{ data.run.model_version }}</n-descriptions-item>
          <n-descriptions-item label="数据截止">{{ data.run.data_cutoff }}</n-descriptions-item>
          <n-descriptions-item label="生成时间">{{ fmt(data.run.generated_at) }}</n-descriptions-item>
          <n-descriptions-item label="训练窗口">
            {{ data.run.train_start || '—' }} ~ {{ data.run.train_end || '—' }}
          </n-descriptions-item>
          <n-descriptions-item label="标签/调仓">
            {{ data.run.label_horizon }} 交易日
          </n-descriptions-item>
          <n-descriptions-item label="模型文件 SHA256">
            <n-text code>{{ data.run.model_sha256.slice(0, 16) }}…</n-text>
          </n-descriptions-item>
        </n-descriptions>
        <n-alert v-if="data.run.stale" type="error" :bordered="false">
          快照过期：数据截止 {{ data.run.data_cutoff }}，最近应有交易日 {{ data.run.expected_cutoff }}
          （相差 {{ data.run.staleness_days }} 天）。不要把旧快照当作当日信号。
        </n-alert>
        <n-grid :cols="4" :x-gap="12" :y-gap="12" responsive="screen">
          <n-gi><n-statistic label="可评分标的" :value="eligibleCount" /></n-gi>
          <n-gi>
            <n-statistic label="组合回测 CAGR（年度重训）"
                         :value="pct(data.run.metrics?.cagr)" />
          </n-gi>
          <n-gi>
            <n-statistic label="组合回测 Sharpe（年度重训）"
                         :value="num(data.run.metrics?.sharpe)" />
          </n-gi>
          <n-gi>
            <n-statistic label="相对 EW alpha / t"
                         :value="`${pct(data.run.metrics?.alpha_vs_ew20)} / ${num(data.run.metrics?.alpha_t)}`" />
          </n-gi>
        </n-grid>
        <n-alert type="warning" :bordered="false">
          {{ data.run.notes || data.disclaimer }}
          指标口径：{{ metricPeriod }} 年度重训 walk-forward 组合回测（组合末端因标签未成熟空仓），
          不是当前冻结 ETF 模型的样本外表现，也不代表未来收益；评分过期或数据缺失时不得当作当日信号。
        </n-alert>
        <n-data-table :columns="columns" :data="data.scores" :row-props="rowProps" size="small"
                      :scroll-x="760" />
        <n-h4>历史快照</n-h4>
        <n-data-table :columns="historyColumns" :data="data.history" size="small" />
      </template>
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NDataTable, NDescriptions, NDescriptionsItem, NGi, NGrid, NH3, NH4,
  NSpace, NStatistic, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'
import { fmt } from '../../util'

interface Score {
  code: string
  name: string
  score: number | null
  rank: number | null
  eligible: boolean
  reason: string
}
interface Run {
  id: number
  model_version: string
  model_sha256: string
  data_cutoff: string
  generated_at: string | null
  label_horizon: number
  train_start: string | null
  train_end: string | null
  stale: boolean
  expected_cutoff: string
  staleness_days: number
  metrics: Record<string, number | string | boolean | null> | null
  notes: string | null
}
interface History {
  id: number
  model_version: string
  data_cutoff: string
  generated_at: string | null
  eligible_count: number
  created_at: string | null
}
interface Payload {
  read_only: boolean
  is_trade_signal: boolean
  disclaimer: string
  run: Run | null
  scores: Score[]
  history: History[]
}

const data = ref<Payload | null>(null)
const loading = ref(false)
const error = ref('')
const eligibleCount = computed(() => data.value?.scores.filter((s) => s.eligible).length || 0)
const metricPeriod = computed(() => String(data.value?.run?.metrics?.period || '2023-01-04~2026-11-13'))
const columns = [
  { title: '排名', key: 'rank', width: 64 },
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '预测 20 日超额', key: 'score', width: 130, render: (r: Score) => pct(r.score) },
  { title: '可评分', key: 'eligible', width: 80, render: (r: Score) => (r.eligible ? '是' : '否') },
  { title: '说明', key: 'reason', width: 220 },
]
const historyColumns = [
  { title: '数据截止', key: 'data_cutoff', width: 110 },
  { title: '模型', key: 'model_version' },
  { title: '可评分', key: 'eligible_count', width: 90 },
  { title: '生成时间', key: 'generated_at', width: 200, render: (r: History) => fmt(r.generated_at) },
]
function num(value: unknown): string {
  return typeof value !== 'number' || !Number.isFinite(value) ? '—' : value.toFixed(2)
}
function pct(value: unknown): string {
  return typeof value !== 'number' || !Number.isFinite(value)
    ? '—'
    : `${(value * 100).toFixed(2)}%`
}
function rowProps(row: Score) {
  return { style: row.rank != null && row.rank <= 8 ? 'background: rgba(24, 160, 88, 0.08)' : '' }
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api<Payload>('/research/etf-score')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>
