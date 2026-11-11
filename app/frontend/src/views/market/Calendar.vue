<template>
  <n-space vertical size="large">
    <n-space align="center">
      <n-input-number v-model:value="year" :min="2005" :max="2030" style="width: 120px" />
      <span class="muted">新平台(Dolt)：{{ meta('qp') }}；旧日历(akshare)：{{ meta('legacy') }}；旧 Qlib：{{ meta('qlib') }}</span>
    </n-space>
    <n-card :title="`${year} 交易日（新平台，绿=开市；红框=与旧日历不一致）`"><EChart :option="calOpt" height="220px" /></n-card>
    <n-card title="差异">
      <div>仅旧日历开市：{{ diff.legacyOnly.join('、') || '无' }}</div>
      <div>仅新平台开市：{{ diff.qpOnly.join('、') || '无' }}</div>
      <div>旧 Qlib 有而新平台无：{{ diff.qlibOnly.join('、') || '无' }}</div>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NCard, NInputNumber, NSpace } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
import { fmt } from '../../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null), year = ref(new Date().getFullYear())
const meta = (o: string) => { const m = data.value?.[o]?.meta; return m?.last ? `${m.first}~${m.last}（${m.open_days} 天，更新 ${fmt(m.updated_at)}）` : '无' }
const diff = computed(() => {
  const qp = new Set<string>(data.value?.qp?.open || []), lg = new Set<string>(data.value?.legacy?.open || []), ql = new Set<string>(data.value?.qlib?.open || [])
  const qpMax = [...qp].sort().pop() || '', lgMax = [...lg].sort().pop() || '', qlMax = [...ql].sort().pop() || ''
  const lim = (a: string, b: string) => (a < b ? a : b)
  return {
    legacyOnly: [...lg].filter((x) => !qp.has(x) && x <= lim(qpMax, lgMax)).sort(),
    qpOnly: [...qp].filter((x) => !lg.has(x) && x <= lim(qpMax, lgMax)).sort(),
    qlibOnly: [...ql].filter((x) => !qp.has(x) && x <= lim(qpMax, qlMax)).sort(),
  }
})
const calOpt = computed(() => {
  const open = new Set<string>(data.value?.qp?.open || []), closed = new Set<string>(data.value?.qp?.closed || [])
  const bad = new Set([...diff.value.legacyOnly, ...diff.value.qpOnly, ...diff.value.qlibOnly])
  const pts = [...open].map((d) => [d, 1]).concat([...closed].map((d) => [d, 0]))
  return {
    tooltip: { formatter: (p: { value: [string, number] }) => `${p.value[0]} ${p.value[1] ? '开市' : '休市'}${bad.has(p.value[0]) ? '（不一致）' : ''}` },
    visualMap: { show: false, min: 0, max: 1, inRange: { color: ['#eeeeee', '#18a058'] } },
    calendar: { range: String(year.value), cellSize: ['auto', 16], left: 30, right: 10, top: 30, yearLabel: { show: false }, dayLabel: { nameMap: 'ZH' }, monthLabel: { nameMap: 'ZH' } },
    series: [{ type: 'heatmap', coordinateSystem: 'calendar', data: pts,
      itemStyle: { borderColor: '#fff' } },
    { type: 'heatmap', coordinateSystem: 'calendar', data: [...bad].map((d) => [d, open.has(d) ? 1 : 0]), itemStyle: { borderColor: '#d03050', borderWidth: 2 } }],
  }
})
async function load() { data.value = await api(`/market/calendar?year=${year.value}`) }
watch(year, load)
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
