<template>
  <n-space vertical size="large">
    <span class="muted">来源：旧看板导出（只读），快照 {{ fmt(cov?.at) }}；有效交易日 {{ c?.effectiveTradeDate || '-' }}</span>
    <n-grid :cols="3" :x-gap="16" responsive="screen" item-responsive>
      <n-gi span="3 m:1"><n-card title="Qlib 覆盖率"><EChart :option="gaugeOpt" height="240px" /></n-card></n-gi>
      <n-gi span="3 m:2"><n-card title="每日快照分类（最近 60 天）"><EChart :option="barOpt" height="240px" /></n-card></n-gi>
    </n-grid>
    <n-card title="当前问题">
      <n-space vertical>
        <div v-for="(it, i) in issues" :key="i">· {{ typeof it === 'string' ? it : JSON.stringify(it) }}</div>
        <span v-if="!issues.length" class="muted">无</span>
      </n-space>
    </n-card>
    <n-card title="日期明细">
      <n-data-table size="small" :bordered="false" :data="dates" :columns="dCols" :pagination="{ pageSize: 20 }" />
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NDataTable, NGi, NGrid, NSpace } from 'naive-ui'
import EChart from '../components/EChart.vue'
import { api } from '../api/client'
import { fmt } from '../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const cov = ref<any>(null)
const c = computed(() => cov.value?.data)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const dates = computed<any[]>(() => [...(c.value?.coverage?.dates || [])].sort((a, b) => String(b.date).localeCompare(String(a.date))))
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const issues = computed<any[]>(() => c.value?.currentIssues || [])
const gaugeOpt = computed(() => {
  const q = c.value?.coverage?.qlibCoverage || {}
  return { series: [{ type: 'gauge', min: 0, max: 100, progress: { show: true }, detail: { formatter: '{value}%' },
    data: [{ value: Math.round((q.ratio || 0) * 1000) / 10, name: `${q.valid ?? '-'}/${q.eligible ?? '-'} · ${q.targetDate ?? ''}` }] }] }
})
const barOpt = computed(() => {
  const ds = [...dates.value].reverse().slice(-60)
  const classes = [...new Set(ds.map((d) => d.classification || 'unknown'))]
  return { tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 30, right: 10, bottom: 40, top: 30 },
    xAxis: { type: 'category', data: ds.map((d) => d.date), axisLabel: { rotate: 45 } }, yAxis: { type: 'value', max: 1, show: false },
    series: classes.map((k) => ({ name: k, type: 'bar', stack: 'x', data: ds.map((d) => ((d.classification || 'unknown') === k ? 1 : 0)) })) }
})
const dCols = [
  { title: '日期', key: 'date' }, { title: '分类', key: 'classification' }, { title: '快照', key: 'snapshot' },
  { title: '缺口', key: 'gap' }, { title: '可恢复性', key: 'recoveryReadiness' },
]
onMounted(async () => { cov.value = await api('/dash/coverage') })
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
