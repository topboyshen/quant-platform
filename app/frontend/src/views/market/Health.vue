<template>
  <n-space vertical size="large">
    <n-space justify="space-between" align="center">
      <span class="muted">最近检查：{{ fmt(data?.checks?.[0]?.run_at) }}（CST）</span>
      <n-button size="small" @click="load">刷新</n-button>
    </n-space>
    <n-data-table size="small" :bordered="false" :data="data?.checks || []" :columns="cols" />
    <div class="muted">状态含正常、告警、严重、未知。未知表示证据不足（例如日历覆盖不够、比较未完成或数据读取失败），不能当作已恢复或全部通过。</div>
    <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi span="2 m:1"><n-card title="数据源连通（原始探测 + qp 可用路径）">
        <n-data-table size="small" :bordered="false" :data="srcRows" :columns="srcCols" />
        <div class="muted">原始探测失败不等于 qp 无路可用；以“qp 判定”列的降级路径为准。</div>
      </n-card></n-gi>
      <n-gi span="2 m:1"><n-card title="Qlib（只读目录 + 来源对比，无数据拷贝）">
        <div>旧 Qlib 日历末日：<b>{{ cat?.calendar_last }}</b>（{{ cat?.calendar_days }} 天，标签 {{ cat?.tag }}）</div>
        <div>证券 {{ cat?.instruments }}，末日在市 {{ cat?.active_on_last }}，特征目录 {{ cat?.feature_dirs }}</div>
        <div>更新状态：{{ cat?.update_status?.status }}（{{ cat?.update_status?.targetDate }}，{{ fmt(cat?.update_status?.time) }}）</div>
        <div>采用 EOD：<b>{{ eod || '-' }}</b>（{{ eodSrc }}）；Dolt 原始：{{ srcq?.dolt_latest_eod || srcq?.dolt_error || '-' }}；GitHub 发布：{{ srcq?.github_release_tag || srcq?.github_error || '-' }}</div>
        <div class="muted">来源检查 {{ fmt(data?.qlib?.source?.at) }}，目录 {{ fmt(data?.qlib?.catalog?.at) }}</div>
      </n-card></n-gi>
    </n-grid>
    <n-card title="旧 research-data 健康（上游只读镜像；不参与 qp 判定）">
      <div>上游状态 <n-tag type="default" size="small">{{ lh?.status }}</n-tag>，模式 {{ lh?.businessMode }}，有效交易日 {{ lh?.effectiveTradeDate }}，生成 {{ fmt(lh?.generatedAt) }}</div>
      <div>核心快照最近运行：{{ lh?.core_last_run?.status }} · 目标 {{ lh?.core_last_run?.target }} · {{ lh?.core_last_run?.reason }}</div>
      <div class="muted">以上是上游自身状态，仅只读展示；qp 是否正常以上方检查表为准。</div>
      <n-space style="margin-top: 6px"><n-tag v-for="(i, k) in lh?.issues || []" :key="k" size="small" type="default">{{ i.code || 'issue' }}（{{ i.severity }}）</n-tag></n-space>
    </n-card>
    <n-card title="近 14 天检查结果"><EChart :option="histOpt" height="220px" /></n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NGi, NGrid, NSpace, NTag } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
import { fmt } from '../../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
const cat = computed(() => data.value?.qlib?.catalog?.data)
const srcq = computed(() => data.value?.qlib?.source?.data)
const lh = computed(() => data.value?.legacy_health?.data)
const eod = computed(() => srcq.value?.latest_eod || srcq.value?.dolt_latest_eod || srcq.value?.github_release_tag)
const eodSrc = computed(() => srcq.value?.latest_eod_source
  || (srcq.value?.dolt_latest_eod ? 'dolt' : (srcq.value?.github_release_tag ? 'github_release' : '-')))
const ST: Record<string, string> = { ok: 'success', warning: 'warning', critical: 'error', unknown: 'default' }
const SL: Record<string, string> = { ok: '正常', warning: '告警', critical: '严重', unknown: '未知' }
const cols = [
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '状态', key: 'status', width: 90, render: (r: any) => h(NTag, { size: 'small', type: ST[r.status] as any }, { default: () => SL[r.status] || r.status }) },
  { title: '检查项', key: 'check', width: 220 }, { title: '说明', key: 'summary' },
]
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const srcCheck = computed(() => (data.value?.checks || []).find((c: any) => c.check === 'sources.reachable'))
const srcRows = computed(() => (Object.values(data.value?.sources || {}) as any[]).map((r) => {
  const d = srcCheck.value?.detail?.[r.source] || {}
  return { ...r, covered_by: d.covered_by || [], optional: !!d.optional }
}))
const srcCols = [
  { title: '来源', key: 'source' },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '原始探测', key: 'ok', render: (r: any) => h(NTag, { size: 'small', type: r.ok ? 'success' : 'error' }, { default: () => (r.ok ? '可达' : (r.detail?.error || '失败')) }) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: 'qp 判定', key: 'verdict', render: (r: any) => {
    if (r.ok) return h(NTag, { size: 'small', type: 'success' }, { default: () => '直达' })
    if (r.covered_by?.length) return h(NTag, { size: 'small', type: 'warning' }, { default: () => `降级 → ${r.covered_by.join(', ')}` })
    if (r.optional) return h(NTag, { size: 'small' }, { default: () => '可选' })
    return h(NTag, { size: 'small', type: 'error' }, { default: () => '失败' })
  } },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '网络', key: 'via_proxy', render: (r: any) => (r.via_proxy ? '代理' : '直连') }, { title: '耗时 ms', key: 'ms' },
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
