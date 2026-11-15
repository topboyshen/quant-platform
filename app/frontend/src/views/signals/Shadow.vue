<template>
  <n-space vertical>
    <n-text depth="3">
      趋势触发但被风控拦截的“影子 allowed”样本，仅用于验证策略，不进入实盘决策、通知或记录链。
      共 {{ total }} 条
    </n-text>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !rows.length" emptyText="暂无影子样本（风控放行或未触发时不会产生）">
      <n-data-table :columns="columns" :data="rows" :loading="loading" size="small" :scroll-x="960" />
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NDataTable, NSpace, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'
import { horizonCell, riskLabel, type ShadowSample as Sample } from './shadowModel'

const HORIZONS = [1, 3, 5, 10, 20]
const rows = ref<Sample[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name', minWidth: 160 },
  { title: '决策日', key: 'decision_date', width: 110 },
  { title: '风控', key: 'risk_level', width: 110, render: (r: Sample) => riskLabel(r.risk_level) },
  ...HORIZONS.map((h) => ({
    title: `${h}日`, key: `h${h}`, width: 90,
    render: (r: Sample) => horizonCell(r, h),
  })),
]
onMounted(async () => {
  loading.value = true
  try {
    const data = await api<{ total: number; rows: Sample[] }>('/signals/shadow?limit=200')
    rows.value = data.rows
    total.value = data.total
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
