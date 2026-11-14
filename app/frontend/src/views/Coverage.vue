<template>
  <n-space vertical size="large">
    <span class="muted">qp 原生数据覆盖 · 生成于 {{ fmt(cov?.at) }}</span>
    <n-grid :cols="5" :x-gap="12" responsive="screen" item-responsive>
      <n-gi span="5 m:1">
        <n-card size="small">
          <template #header>
            <n-space align="center" size="small">
              <span>Qlib</span>
              <n-tag v-if="cov?.qlib?.status && cov.qlib.status !== 'current'" size="small" type="warning">{{ cov.qlib.status }}</n-tag>
              <n-tag v-else-if="cov?.qlib?.status" size="small" type="success">current</n-tag>
            </n-space>
          </template>
          <n-statistic :value="cov?.qlib?.latest || '-'" />
          <div class="muted">instruments {{ cov?.qlib?.instruments ?? '-' }} · calendar {{ cov?.qlib?.calendar_days ?? '-' }} 天 · active {{ cov?.qlib?.active ?? '-' }}</div>
          <div v-if="cov?.qlib?.planned_date || cov?.qlib?.target_date" class="muted">目标 {{ cov.qlib.planned_date || cov.qlib.target_date }}{{ cov.qlib.status !== 'current' && cov.qlib.failure_stage ? ' · 失败于 ' + cov.qlib.failure_stage : '' }}</div>
        </n-card>
      </n-gi>
      <n-gi span="5 m:1">
        <n-card size="small" title="Snapshot">
          <n-statistic :value="cov?.snapshot?.latest || '-'" />
          <div class="muted">expected {{ cov?.snapshot?.expected ?? '-' }} · lag {{ cov?.snapshot?.lag ?? '-' }}</div>
        </n-card>
      </n-gi>
      <n-gi span="5 m:1">
        <n-card size="small" title="Index Close">
          <n-statistic :value="cov?.index_close?.latest || '-'" />
          <div class="muted">{{ cov?.index_close?.symbols ?? '-' }} 个指数</div>
        </n-card>
      </n-gi>
      <n-gi span="5 m:1">
        <n-card size="small" title="Compare">
          <n-statistic :value="`${cov?.compare?.compared ?? '-'}/${cov?.compare?.expected ?? '-'}`" />
          <div class="muted">diffs {{ cov?.compare?.diffs ?? '-' }} · missing {{ cov?.compare?.missing ?? '-' }}</div>
        </n-card>
      </n-gi>
      <n-gi span="5 m:1">
        <n-card size="small" title="Calendar">
          <n-statistic :value="cov?.calendar?.qp_max || '-'" />
          <div class="muted">qp 交易日历上限</div>
        </n-card>
      </n-gi>
    </n-grid>
    <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
      <n-gi span="2 m:1">
        <n-card title="最近 20 个交易日 · index_close 覆盖">
          <EChart :option="barOpt" height="280px" />
        </n-card>
      </n-gi>
      <n-gi span="2 m:1">
        <n-card title="当前问题">
          <n-space vertical>
            <div v-for="it in issues" :key="it.code">
              <n-tag :type="it.severity === 'critical' ? 'error' : 'warning'" size="small">{{ it.severity }}</n-tag>
              {{ it.summary }}
            </div>
            <span v-if="!issues.length" class="muted">无</span>
          </n-space>
        </n-card>
      </n-gi>
    </n-grid>
    <n-card title="交易日明细">
      <n-data-table size="small" :bordered="false" :data="recent" :columns="dCols" :pagination="{ pageSize: 20 }" />
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NDataTable, NGi, NGrid, NSpace, NStatistic, NTag } from 'naive-ui'
import EChart from '../components/EChart.vue'
import { api } from '../api/client'
import { fmt } from '../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const cov = ref<any>(null)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const recent = computed<any[]>(() => [...(cov.value?.recent || [])].sort((a, b) => String(b.date).localeCompare(String(a.date))))
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const issues = computed<any[]>(() => cov.value?.issues || [])
const barOpt = computed(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const ds: any[] = [...(cov.value?.recent || [])]
  return {
    tooltip: { trigger: 'axis' }, grid: { left: 35, right: 15, bottom: 45, top: 25 },
    xAxis: { type: 'category', data: ds.map((d) => d.date), axisLabel: { rotate: 45 } },
    yAxis: { type: 'value', min: 0, max: Math.max(4, ...ds.map((d) => Number(d.index_symbols) || 0)) },
    series: [{ name: 'index_close 标的', type: 'bar', data: ds.map((d) => Number(d.index_symbols) || 0),
      itemStyle: { color: '#18a058' } }],
  }
})
const dCols = [
  { title: '日期', key: 'date' },
  { title: 'index_close 标的数', key: 'index_symbols' },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: 'qlib 覆盖', key: 'qlib', render: (r: any) => (r.qlib ? '是' : '否') },
]
onMounted(async () => { cov.value = await api('/dash/coverage') })
</script>
<style scoped>.muted { color: #888; font-size: 12px; margin-top: 6px; }</style>
