<template>
  <n-space vertical size="large">
    <n-card title="旧服务状态（按分类，不含系统单元）"><EChart :option="barOpt" height="320px" /></n-card>
    <n-card title="容器健康"><EChart :option="pieOpt" height="260px" /></n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NSpace } from 'naive-ui'
import EChart from '../components/EChart.vue'
import { api } from '../api/client'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
const STATES = ['active', 'inactive', 'activating', 'failed']
const COLORS: Record<string, string> = { active: '#18a058', inactive: '#a0a0a0', activating: '#2080f0', failed: '#d03050' }
const barOpt = computed(() => {
  const rows: { category: string; active_state: string; n: number }[] = (data.value?.unit_states || []).filter((r: { category: string }) => r.category !== 'os')
  const cats = [...new Set(rows.map((r) => r.category))].sort()
  return {
    tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 40, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: cats }, yAxis: { type: 'value' },
    series: STATES.map((s) => ({ name: s, type: 'bar', stack: 'x', itemStyle: { color: COLORS[s] },
      data: cats.map((c) => rows.filter((r) => r.category === c && r.active_state === s).reduce((a, r) => a + r.n, 0)) })),
  }
})
const pieOpt = computed(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const cs: any[] = data.value?.containers?.data || []
  const cnt: Record<string, number> = {}
  for (const c of cs) {
    const k = c.retired ? 'retired' : (c.state !== 'running' ? c.state : (c.health || 'running'))
    cnt[k] = (cnt[k] || 0) + 1
  }
  const entries = Object.entries(cnt).sort(([a], [b]) => (a === 'retired' ? 1 : 0) - (b === 'retired' ? 1 : 0))
  return { tooltip: { trigger: 'item' }, legend: { bottom: 0 },
    series: [{ type: 'pie', radius: ['40%', '70%'],
      data: entries.map(([name, value]) => ({ name, value, itemStyle: name === 'retired' ? { color: '#f0a020' } : undefined })) }] }
})
onMounted(async () => { data.value = await api('/dash/services') })
</script>
<style scoped>.muted { color: #888; font-size: 12px; margin-top: 6px; }</style>
