<template>
  <n-space vertical size="large">
    <n-space justify="space-between" align="center">
      <span class="muted">最近检查：{{ fmt(data?.checks?.[0]?.run_at) }}（CST）</span>
      <n-button size="small" @click="load">刷新</n-button>
    </n-space>
    <n-data-table size="small" :bordered="false" :data="data?.checks || []" :columns="cols" />
    <div class="muted">状态含正常、告警、严重、未知。未知表示证据不足（例如日历覆盖不够、比较未完成或数据读取失败），不能当作已恢复或全部通过。</div>
    <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi span="2 m:1"><n-card title="数据源连通（最近一次探测）">
        <n-data-table size="small" :bordered="false" :data="srcRows" :columns="srcCols" />
      </n-card></n-gi>
      <n-gi span="2 m:1"><n-card title="Qlib（只读目录 + 来源对比，无数据拷贝）">
        <div>旧 Qlib 日历末日：<b>{{ cat?.calendar_last }}</b>（{{ cat?.calendar_days }} 天，标签 {{ cat?.tag }}）</div>
        <div>证券 {{ cat?.instruments }}，末日在市 {{ cat?.active_on_last }}，特征目录 {{ cat?.feature_dirs }}</div>
        <div>更新状态：{{ cat?.update_status?.status }}（{{ cat?.update_status?.targetDate }}，{{ fmt(cat?.update_status?.time) }}）</div>
        <div>Dolt 最新日线：<b>{{ srcq?.dolt_latest_eod || srcq?.dolt_error || '-' }}</b>；GitHub 发布：{{ srcq?.github_release_tag || srcq?.github_error || '-' }}</div>
        <div class="muted">来源检查 {{ fmt(data?.qlib?.source?.at) }}，目录 {{ fmt(data?.qlib?.catalog?.at) }}</div>
      </n-card></n-gi>
    </n-grid>
    <n-card title="旧 research-data 健康（只读镜像）">
      <div>状态 <n-tag :type="tagType(lh?.status)" size="small">{{ lh?.status }}</n-tag>，模式 {{ lh?.businessMode }}，有效交易日 {{ lh?.effectiveTradeDate }}，生成 {{ fmt(lh?.generatedAt) }}</div>
      <div>核心快照最近运行：{{ lh?.core_last_run?.status }} · 目标 {{ lh?.core_last_run?.target }} · {{ lh?.core_last_run?.reason }}</div>
      <n-space style="margin-top: 6px"><n-tag v-for="(i, k) in lh?.issues || []" :key="k" size="small" :type="tagType(i.severity)">{{ i.code || 'issue' }}</n-tag></n-space>
    </n-card>
    <n-card title="近 14 天检查结果"><EChart :option="histOpt" height="220px" /></n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NGi, NGrid, NSpace, NTag } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
import { fmt, tagType } from '../../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
const cat = computed(() => data.value?.qlib?.catalog?.data)
const srcq = computed(() => data.value?.qlib?.source?.data)
const lh = computed(() => data.value?.legacy_health?.data)
const ST: Record<string, string> = { ok: 'success', warning: 'warning', critical: 'error', unknown: 'default' }
const SL: Record<string, string> = { ok: '正常', warning: '告警', critical: '严重', unknown: '未知' }
const cols = [
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '状态', key: 'status', width: 90, render: (r: any) => h(NTag, { size: 'small', type: ST[r.status] as any }, { default: () => SL[r.status] || r.status }) },
  { title: '检查项', key: 'check', width: 220 }, { title: '说明', key: 'summary' },
]
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const srcRows = computed(() => Object.values(data.value?.sources || {}) as any[])
const srcCols = [
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '来源', key: 'source' }, { title: '结果', key: 'ok', render: (r: any) => h(NTag, { size: 'small', type: r.ok ? 'success' : 'error' }, { default: () => (r.ok ? '可达' : (r.detail?.error || '失败')) }) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '路径', key: 'via_proxy', render: (r: any) => (r.via_proxy ? '代理' : '直连') }, { title: '耗时 ms', key: 'ms' },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '时间', key: 'at', render: (r: any) => fmt(r.at) },
]
const histOpt = computed(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const hist: any[] = data.value?.history || []
  const runs = [...new Set(hist.map((x) => x.run_at))]
  const states = ['ok', 'warning', 'critical', 'unknown']
  const colors: Record<string, string> = { ok: '#18a058', warning: '#f0a020', critical: '#d03050', unknown: '#a0a0a0' }
  return { tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 30, right: 10, top: 30, bottom: 50 },
    xAxis: { type: 'category', data: runs.map((r) => fmt(r)), axisLabel: { rotate: 30 } }, yAxis: { type: 'value' },
    series: states.map((s) => ({ name: s, type: 'bar', stack: 'x', itemStyle: { color: colors[s] },
      data: runs.map((r) => hist.filter((x) => x.run_at === r && x.status === s).reduce((a, x) => a + x.n, 0)) })) }
})
async function load() { data.value = await api('/market/health') }
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
