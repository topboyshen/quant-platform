<template>
  <n-space vertical size="large">
    <n-space align="center"><span>区间</span><n-select v-model:value="days" :options="dayOpts" style="width: 120px" /></n-space>
    <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi v-for="s in SYMS" :key="s.code" span="2 m:1">
        <n-card :title="`${s.name} ${s.code}`"><EChart :option="opt(s.code)" height="220px" /></n-card>
      </n-gi>
    </n-grid>
    <n-card title="明细（新平台 vs 旧缓存）">
      <n-data-table size="small" :bordered="false" :data="table" :columns="cols" :pagination="{ pageSize: 20 }" />
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue'
import { NCard, NDataTable, NGi, NGrid, NSelect, NSpace, NTag } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
interface R { origin: string; symbol: string; trade_date: string; close: number; source: string }
const SYMS = [{ code: 'sh000001', name: '上证指数' }, { code: 'sh000300', name: '沪深300' }, { code: 'sz399001', name: '深证成指' }, { code: 'sz399006', name: '创业板指' }]
const rows = ref<R[]>([]), days = ref(60)
const dayOpts = [30, 60, 120, 365].map((d) => ({ value: d, label: `${d} 天` }))
const opt = (sym: string) => {
  const qp = rows.value.filter((r) => r.symbol === sym && r.origin === 'qp')
  const lg = rows.value.filter((r) => r.symbol === sym && r.origin === 'legacy')
  const dates = [...new Set([...qp, ...lg].map((r) => r.trade_date))].sort()
  const m = (a: R[]) => { const x = new Map(a.map((r) => [r.trade_date, r.close])); return dates.map((d) => x.get(d) ?? null) }
  return { tooltip: { trigger: 'axis' }, legend: { top: 0, data: ['新平台', '旧缓存'] }, grid: { left: 50, right: 10, top: 36, bottom: 40 },
    xAxis: { type: 'category', data: dates }, yAxis: { type: 'value', scale: true },
    series: [{ name: '新平台', type: 'line', data: m(qp), showSymbol: false, connectNulls: true },
      { name: '旧缓存', type: 'line', data: m(lg), symbolSize: 8, lineStyle: { opacity: 0 } }] }
}
const table = computed(() => {
  const key = (r: R) => `${r.symbol}|${r.trade_date}`
  const lg = new Map(rows.value.filter((r) => r.origin === 'legacy').map((r) => [key(r), r]))
  return rows.value.filter((r) => r.origin === 'qp').map((r) => ({ ...r, legacy: lg.get(key(r))?.close ?? null }))
    .sort((a, b) => b.trade_date.localeCompare(a.trade_date) || a.symbol.localeCompare(b.symbol))
})
const cols = [
  { title: '日期', key: 'trade_date' }, { title: '指数', key: 'symbol' }, { title: '新平台', key: 'close' }, { title: '来源', key: 'source' },
  { title: '旧缓存', key: 'legacy', render: (r: { legacy: number | null }) => r.legacy ?? '-' },
  { title: '对比', key: 'cmp', render: (r: { legacy: number | null; close: number }) => r.legacy == null ? '-' :
    h(NTag, { size: 'small', type: Math.abs(r.legacy - r.close) <= 0.011 ? 'success' : 'error' }, { default: () => (Math.abs(r.legacy! - r.close) <= 0.011 ? '一致' : `差 ${(r.close - r.legacy!).toFixed(4)}`) }) },
]
async function load() { rows.value = await api<R[]>(`/market/index-close?days=${days.value}`) }
watch(days, load)
onMounted(load)
</script>
