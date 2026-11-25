<template>
  <n-space vertical size="large">
    <n-card size="small">
      <template #header>
        <n-space align="center" :wrap="true">
          <span>策略横向对比（20 日归因）</span>
          <n-tag size="small" type="info">只读研究</n-tag>
          <n-text v-if="report" depth="3">
            截至 {{ report.as_of }} · 成熟 {{ report.sample.matured_filled }} 条 · 合格 {{ report.sample.eligible_matured }} 条（阈值 {{ report.threshold }}）
          </n-text>
        </n-space>
      </template>
      <n-alert type="warning" :show-icon="true" style="margin-bottom: 12px">
        这是分组观察排序，不是加仓/停止建议。样本不足或 unknown 分组只能作描述性参考，不能替代策略晋级门槛。
      </n-alert>
      <n-space align="center" :wrap="true" style="margin-bottom: 12px">
        <n-select v-model:value="dimension" size="small" clearable placeholder="全部维度"
                  :options="dimensionOptions" style="width: 150px" />
        <n-select v-model:value="minSamples" size="small" :options="minOptions" style="width: 140px" />
        <n-select v-model:value="sortBy" size="small" :options="sortOptions" style="width: 170px" />
        <n-space align="center" size="small">
          <n-switch v-model:value="hideUnknown" size="small" />
          <n-text depth="3">隐藏 unknown</n-text>
        </n-space>
        <n-space align="center" size="small">
          <n-switch v-model:value="showBaseline" size="small" />
          <n-text depth="3">显示仅基线</n-text>
        </n-space>
        <n-text depth="3">共 {{ rows.length }} 个分组满足样本门槛</n-text>
      </n-space>
      <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length"
                  :empty-text="showBaseline ? '没有满足样本门槛的归因分组' : '没有非基线分组满足样本门槛（仅基线分组已隐藏）'">
        <template v-if="report">
          <n-space size="small" style="margin-bottom: 12px">
            <n-tag size="small">可执行 n={{ report.executable?.n ?? 0 }}</n-tag>
            <n-tag size="small">可执行净收益 {{ pct(report.executable?.avg_net_return ?? null) }}</n-tag>
            <n-tag size="small">总体含研究样本 n={{ report.overall.n }}</n-tag>
            <n-tag size="small">总体净收益（非可执行） {{ pct(report.overall.avg_net_return) }}</n-tag>
            <n-tag size="small">总体超额 {{ pct(report.overall.avg_excess_return) }}</n-tag>
            <n-tag size="small" type="warning">合格样本 {{ report.sample.eligible_matured }}/{{ report.threshold }}</n-tag>
          </n-space>
          <n-alert v-for="warning in report.warnings" :key="warning.code" type="warning" :show-icon="true"
                   style="margin-bottom: 8px">
            {{ warning.message }}
          </n-alert>
          <n-data-table size="small" :columns="columns" :data="rows" :pagination="{ pageSize: 15 }" />
        </template>
      </AsyncState>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NAlert, NCard, NDataTable, NSelect, NSpace, NSwitch, NTag, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api, ApiError } from '../../api/client'
import {
  STRATEGY_DIMENSIONS,
  buildStrategyRows,
  type AttrReport,
  type StrategyRow,
  type StrategySort,
} from './strategyModel'

const report = ref<AttrReport | null>(null)
const loading = ref(false)
const error = ref('')
const dimension = ref<string | null>(null)
const minSamples = ref(5)
const sortBy = ref<StrategySort>('avg_excess_return')
const hideUnknown = ref(true)
const showBaseline = ref(false)

const dimensionOptions = Object.entries(STRATEGY_DIMENSIONS).map(([value, label]) => ({ value, label }))
const minOptions = [
  { label: '样本 ≥ 1', value: 1 },
  { label: '样本 ≥ 5', value: 5 },
  { label: '样本 ≥ 10', value: 10 },
  { label: '样本 ≥ 30', value: 30 },
]
const sortOptions = [
  { label: '按平均超额排序', value: 'avg_excess_return' },
  { label: '按平均净收益排序', value: 'avg_net_return' },
  { label: '按胜率排序', value: 'win_rate' },
  { label: '按样本数排序', value: 'n' },
]

const rows = computed<StrategyRow[]>(() => report.value
  ? buildStrategyRows(report.value, {
      minSamples: minSamples.value,
      dimension: dimension.value,
      hideUnknown: hideUnknown.value,
      hideBaseline: !showBaseline.value,
      sortBy: sortBy.value,
    })
  : [])

function pct(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`
}
function returnStyle(value: number | null | undefined) {
  if (value === null || value === undefined) return {}
  return { color: value > 0 ? '#18a058' : value < 0 ? '#d03050' : undefined }
}
function statusType(status: string): 'success' | 'warning' | 'default' {
  return status === 'sufficient' ? 'success' : status === 'insufficient' ? 'warning' : 'default'
}
const columns = [
  { title: '#', key: 'rank', width: 48 },
  { title: '维度', key: 'dimension_label', width: 90 },
  { title: '分组', key: 'bucket', minWidth: 130 },
  { title: '样本', key: 'n', width: 64 },
  { title: '合格', key: 'eligible_n', width: 64 },
  { title: '胜率', key: 'win_rate', width: 82, render: (r: StrategyRow) => pct(r.win_rate) },
  {
    title: '平均净收益', key: 'avg_net_return', width: 105,
    render: (r: StrategyRow) => h('span', { style: returnStyle(r.avg_net_return) }, pct(r.avg_net_return)),
  },
  {
    title: '平均超额', key: 'avg_excess_return', width: 105,
    render: (r: StrategyRow) => h('span', { style: returnStyle(r.avg_excess_return) }, pct(r.avg_excess_return)),
  },
  {
    title: '状态', key: 'sample_status', width: 95,
    render: (r: StrategyRow) => h(NTag, { size: 'small', type: statusType(r.sample_status) },
      { default: () => r.sample_status_label || r.sample_status }),
  },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const dims = Object.keys(STRATEGY_DIMENSIONS).join(',')
    report.value = await api<AttrReport>(`/signals/chain/attribution?horizon=20&group_by=${dims}`)
  } catch (e) {
    error.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
