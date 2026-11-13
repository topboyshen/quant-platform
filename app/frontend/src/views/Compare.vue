<template>
  <n-space vertical size="large">
    <n-alert type="info" :bordered="false">每日对比 qp 与旧系统的行情输出；不一致写入告警表（不再写 outbox）。部分比较或缺数据记为跳过/证据不足，不能当作全部通过。</n-alert>
    <n-alert v-if="meta" type="warning" :bordered="false">
      <div>旧链已于 {{ meta.legacy_chain_stopped_on }} 停止，legacy 侧数据不再更新，下面的“一致”只代表历史数据对得上，不代表当前仍在实时对账。</div>
      <div class="muted" style="margin-top: 6px">
        最近一次对比 {{ meta.last_compare_at ? fmt(meta.last_compare_at) : '—' }} ·
        index_close：legacy {{ meta.index_close.legacy || '—' }} / qp {{ meta.index_close.qp || '—' }} ·
        market_summary：legacy {{ meta.market_summary.legacy || '—' }} / qp {{ meta.market_summary.qp || '—' }}
      </div>
    </n-alert>
    <n-card v-for="run in runs" :key="run.at" size="small">
      <template #header>
        <n-space align="center" justify="space-between">
          <span>对比 {{ fmt(run.at) }}（CST）</span>
          <n-space size="small">
            <n-tag :type="run.verdict.type" size="small">{{ run.verdict.label }}</n-tag>
            <span class="muted">应比 {{ run.cov.expected }}，实比 {{ run.cov.compared }}，缺失 {{ run.cov.missing }}，差异 {{ run.cov.diffs }}</span>
          </n-space>
        </n-space>
      </template>
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
interface Meta { last_compare_at: string | null; index_close: { legacy: string | null; qp: string | null }; market_summary: { legacy: string | null; qp: string | null }; legacy_chain_stopped_on: string }
const rows = ref<C[]>([])
const meta = ref<Meta | null>(null)
function covOf(items: C[]) {
  const expected = items.length
  const compared = items.filter((r) => r.status === 'match' || r.status === 'mismatch').length
  const missing = items.filter((r) => r.status === 'skipped' || r.status === 'insufficient').length
  const diffs = items.filter((r) => r.status === 'mismatch').length
  return { expected, compared, missing, diffs }
}
function verdictOf(c: { expected: number; compared: number; missing: number; diffs: number }) {
  if (c.diffs) return { type: 'error' as const, label: '有不一致' }
  if (c.missing || c.compared < c.expected) return { type: 'warning' as const, label: '证据不足' }
  return { type: 'success' as const, label: '全部完成且一致' }
}
const runs = computed(() => {
  const m = new Map<string, C[]>()
  for (const r of rows.value) { if (!m.has(r.run_at)) m.set(r.run_at, []); m.get(r.run_at)!.push(r) }
  return [...m.entries()].map(([at, items]) => {
    const cov = covOf(items)
    return { at, items, cov, verdict: verdictOf(cov) }
  })
})
const T: Record<string, string> = { match: 'success', mismatch: 'error', skipped: 'default', insufficient: 'warning' }
const L: Record<string, string> = { match: '一致', mismatch: '不一致', skipped: '跳过', insufficient: '证据不足' }
const num = (r: C, k: string) => (typeof r.detail?.[k] === 'number' ? r.detail[k] : '—')
const cols = [
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '结果', key: 'status', width: 100, render: (r: C) => h(NTag, { size: 'small', type: T[r.status] as any }, { default: () => L[r.status] || r.status }) },
  { title: '项目', key: 'item', width: 260 },
  { title: '应比', key: 'expected', width: 60, render: (r: C) => num(r, 'expected') },
  { title: '实比', key: 'compared', width: 60, render: (r: C) => num(r, 'compared') },
  { title: '缺失', key: 'missing', width: 60, render: (r: C) => num(r, 'missing') },
  { title: '差异', key: 'diffs', width: 60, render: (r: C) => num(r, 'diffs') },
  { title: '说明', key: 'summary' },
]
onMounted(async () => {
  const [r, m] = await Promise.all([api<C[]>('/market/compare?runs=6'), api<Meta>('/market/compare/meta')])
  rows.value = r; meta.value = m
})
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
