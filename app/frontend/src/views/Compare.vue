<template>
  <n-space vertical size="large">
    <n-alert type="info" :bordered="false">每日对比新平台（影子）与旧输出；不一致写入告警表与 outbox（影子，不发送）。</n-alert>
    <n-card v-for="run in runs" :key="run.at" :title="`对比 ${fmt(run.at)}（CST）`" size="small">
      <n-data-table size="small" :bordered="false" :data="run.items" :columns="cols" />
    </n-card>
    <n-empty v-if="!runs.length" description="暂无对比结果" />
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NAlert, NCard, NDataTable, NEmpty, NSpace, NTag } from 'naive-ui'
import { api } from '../api/client'
import { fmt } from '../util'
interface C { run_at: string; item: string; status: string; summary: string; detail: Record<string, unknown> }
const rows = ref<C[]>([])
const runs = computed(() => {
  const m = new Map<string, C[]>()
  for (const r of rows.value) { if (!m.has(r.run_at)) m.set(r.run_at, []); m.get(r.run_at)!.push(r) }
  return [...m.entries()].map(([at, items]) => ({ at, items }))
})
const T: Record<string, string> = { match: 'success', mismatch: 'error', skipped: 'default' }
const L: Record<string, string> = { match: '一致', mismatch: '不一致', skipped: '跳过' }
const cols = [
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '结果', key: 'status', width: 80, render: (r: C) => h(NTag, { size: 'small', type: T[r.status] as any }, { default: () => L[r.status] || r.status }) },
  { title: '项目', key: 'item', width: 260 }, { title: '说明', key: 'summary' },
  { title: '详情', key: 'detail', ellipsis: { tooltip: true }, render: (r: C) => JSON.stringify(r.detail) },
]
onMounted(async () => { rows.value = await api<C[]>('/market/compare?runs=6') })
</script>
