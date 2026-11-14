<template>
  <n-space vertical size="large">
    <n-card title="市场概览：新平台摘要 vs 旧 core-market（只读对比，上涨/下跌/平盘）"><EChart :option="breadthOpt" height="300px" /></n-card>
    <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi span="2 m:1"><n-card title="覆盖率（有效/应有）">
        <n-data-table size="small" :bordered="false" :data="covRows" :columns="covCols" :max-height="260" />
      </n-card></n-gi>
      <n-gi span="2 m:1"><n-card title="巨潮公告数（新平台，仅元数据）"><EChart :option="annOpt" height="260px" /></n-card></n-gi>
    </n-grid>
    <n-card title="旧 research-data 全量快照索引（只读，仅 sha256/大小）">
      <n-data-table size="small" :bordered="false" :data="data?.legacy_snapshots || []" :columns="snapCols" :max-height="240" />
      <div class="muted">读取器：{{ fmt(data?.reader?.at) }}，耗时 {{ data?.reader?.data?.duration_s }}s；巨潮旧暂存文件 {{ data?.cninfo_staging?.data?.files?.length ?? 0 }} 个</div>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NDataTable, NGi, NGrid, NSpace } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
import { fmt } from '../../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const sums = computed<any[]>(() => data.value?.summaries || [])
const breadthOpt = computed(() => {
  const dates = [...new Set(sums.value.map((s) => s.trade_date))].sort()
  const get = (o: string, k: string) => dates.map((d) => sums.value.find((s) => s.trade_date === d && s.origin === o)?.breadth?.[k] ?? null)
  const ser = (o: string, label: string, stack: string) => (['up', 'down', 'flat'] as const).map((k, i) => ({
    name: `${label}${['上涨', '下跌', '平盘'][i]}`, type: 'bar', stack, data: get(o, k),
    itemStyle: { color: ['#d03050', '#18a058', '#a0a0a0'][i], opacity: o === 'qp' ? 1 : 0.55 } }))
  return { tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 0, textStyle: { fontSize: 10 } }, grid: { left: 50, right: 10, top: 56, bottom: 30 },
    xAxis: { type: 'category', data: dates }, yAxis: { type: 'value' }, series: [...ser('legacy', '旧', 'legacy'), ...ser('qp', '新', 'qp')] }
})
const covRows = computed(() => sums.value.map((s) => ({ origin: s.origin === 'qp' ? '新平台' : '旧', date: s.trade_date,
  valid: s.coverage?.valid, eligible: s.coverage?.eligible, ratio: s.coverage?.ratio })).reverse())
const covCols = [{ title: '日期', key: 'date' }, { title: '来源', key: 'origin' }, { title: '有效', key: 'valid' }, { title: '应有', key: 'eligible' }, { title: '比例', key: 'ratio' }]
const annOpt = computed(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const a = [...(data.value?.announcements || [])].reverse() as any[]
  return { tooltip: { trigger: 'axis' }, grid: { left: 40, right: 10, top: 20, bottom: 30 }, xAxis: { type: 'category', data: a.map((x) => x.date) },
    yAxis: { type: 'value' }, series: [{ type: 'bar', data: a.map((x) => x.total) }] }
})
const snapCols = [{ title: '日期', key: 'date' }, { title: '生成', key: 'generated_at' }, { title: '字节', key: 'payload_bytes' },
  { title: 'sha256', key: 'payload_sha256', ellipsis: { tooltip: true } }]
onMounted(async () => { data.value = await api('/market/coverage') })
</script>
<style scoped>.muted { color: #888; font-size: 12px; margin-top: 6px; }</style>
