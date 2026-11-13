<template>
  <n-space vertical size="large">
    <AsyncState :loading="loading" :error="error" :empty="!data">
      <n-space vertical size="large">
        <n-card>
          <template #header>
            <n-space align="center">
              <span>服务状态（按分类，不含系统单元）</span>
              <n-tag v-for="s in totals" :key="s.state" size="small" :type="TAG[s.state]">{{ s.state }} {{ s.n }}</n-tag>
            </n-space>
          </template>
          <EChart :option="barOpt" height="320px" />
        </n-card>
        <n-card title="需要关注">
          <n-empty v-if="!attention.length" description="没有 failed / activating 的单元" />
          <n-space v-else vertical size="small">
            <div v-for="u in attention" :key="u.name" class="row">
              <n-tag size="small" :type="u.active_state === 'failed' ? 'error' : 'info'">{{ u.active_state }}</n-tag>
              <span class="name">{{ u.name }}</span>
              <span class="muted">{{ u.category }} · {{ u.kind }} · {{ u.sub_state }}{{ u.result ? ' · ' + u.result : '' }}</span>
            </div>
            <div class="muted">activating 多为任务刚启动的瞬间状态，采集时恰好撞上；持续 activating 才需要排查。</div>
          </n-space>
        </n-card>
        <n-card>
          <template #header>
            <n-space align="center">
              <span>容器健康</span>
              <n-tag v-if="containerCounts.healthy" size="small" type="success">healthy {{ containerCounts.healthy }}</n-tag>
              <n-tag v-if="containerCounts.retired" size="small" type="warning">已退役 {{ containerCounts.retired }}</n-tag>
              <n-tag v-for="(n, k) in otherContainers" :key="k" size="small" type="default">{{ k }} {{ n }}</n-tag>
            </n-space>
          </template>
          <EChart :option="pieOpt" height="260px" />
        </n-card>
      </n-space>
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NEmpty, NSpace, NTag } from 'naive-ui'
import AsyncState from '../components/AsyncState.vue'
import EChart from '../components/EChart.vue'
import { api, ApiError } from '../api/client'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
const loading = ref(false), error = ref('')
const STATES = ['active', 'inactive', 'activating', 'failed']
const COLORS: Record<string, string> = { active: '#18a058', inactive: '#a0a0a0', activating: '#2080f0', failed: '#d03050' }
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const TAG: Record<string, any> = { active: 'success', inactive: 'default', activating: 'info', failed: 'error' }
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const rows = computed<any[]>(() => (data.value?.unit_states || []).filter((r: { category: string }) => r.category !== 'os'))
const totals = computed(() => STATES.map((state) => ({
  state, n: rows.value.filter((r) => r.active_state === state).reduce((a, r) => a + r.n, 0),
})).filter((t) => t.n > 0))
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const attention = computed<any[]>(() => data.value?.attention || [])
const barOpt = computed(() => {
  const cats = [...new Set(rows.value.map((r) => r.category))].sort()
  return {
    tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 40, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: cats }, yAxis: { type: 'value' },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    series: STATES.map((s) => ({ name: s, type: 'bar', stack: 'x', itemStyle: { color: COLORS[s] },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      data: cats.map((c) => rows.value.filter((r: any) => r.category === c && r.active_state === s).reduce((a: number, r: any) => a + r.n, 0)) })),
  }
})
const containerCounts = computed(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const cs: any[] = data.value?.containers?.data || []
  const cnt: Record<string, number> = {}
  for (const c of cs) {
    if (c.retired) { cnt.retired = (cnt.retired || 0) + 1; continue }
    if (c.state !== 'running') { cnt[c.state] = (cnt[c.state] || 0) + 1; continue }
    const k = c.health || 'running'
    cnt[k] = (cnt[k] || 0) + 1
  }
  return cnt
})
const otherContainers = computed(() => Object.fromEntries(
  Object.entries(containerCounts.value).filter(([k]) => k !== 'healthy' && k !== 'retired')))
const pieOpt = computed(() => {
  const entries = Object.entries(containerCounts.value).sort(([a], [b]) => (a === 'retired' ? 1 : 0) - (b === 'retired' ? 1 : 0))
  return { tooltip: { trigger: 'item' }, legend: { bottom: 0 },
    series: [{ type: 'pie', radius: ['40%', '70%'],
      data: entries.map(([name, value]) => ({ name, value, itemStyle: name === 'retired' ? { color: '#f0a020' } : undefined })) }] }
})
onMounted(async () => {
  loading.value = true
  try { data.value = await api('/dash/services') }
  catch (e) { error.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  finally { loading.value = false }
})
</script>
<style scoped>
.muted { color: #888; font-size: 12px; }
.row { display: flex; align-items: center; gap: 8px; }
.name { font-family: ui-monospace, monospace; font-size: 13px; }
</style>
