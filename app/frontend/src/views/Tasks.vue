<template>
  <n-space vertical size="large">
    <n-alert type="info">只读登记 systemd 定时器与服务（不接管、不启停）。分类仅用于展示。</n-alert>
    <n-space align="center">
      <n-select v-model:value="scope" :options="scopeOpts" style="width: 140px" />
      <n-select v-model:value="kind" :options="kindOpts" style="width: 140px" />
      <n-select v-model:value="category" :options="catOpts" style="width: 180px" clearable placeholder="分类" />
      <n-switch v-model:value="includeOs">
        <template #checked>含系统单元</template><template #unchecked>不含系统单元</template>
      </n-switch>
      <n-select v-model:value="stateFilter" :options="stateOpts" style="width: 150px" />
      <n-input v-model:value="q" placeholder="搜索名称" clearable style="width: 220px" />
      <span class="muted">共 {{ rows.length }} 条</span>
    </n-space>
    <n-data-table size="small" :bordered="false" :data="rows" :columns="cols" :pagination="{ pageSize: 30 }" />
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue'
import { NAlert, NDataTable, NInput, NSelect, NSpace, NSwitch, NTag } from 'naive-ui'
import { api } from '../api/client'
import { fmt, tagType } from '../util'
interface U { scope: string; name: string; kind: string; description: string; active_state: string; result: string | null; category: string; activates: string | null; triggered_by: string | null; last_trigger: string | null; next_elapse: string | null; last_exit: string | null; run_user: string | null }
const all = ref<U[]>([]), scope = ref(''), kind = ref('timer'), category = ref<string | null>(null), includeOs = ref(false), q = ref('')
const stateFilter = ref('')
const stateOpts = [{ label: '全部状态', value: '' }, { label: 'active', value: 'active' }, { label: 'inactive', value: 'inactive' }, { label: 'failed', value: 'failed' }, { label: 'activating', value: 'activating' }]
const scopeOpts = [{ label: '全部范围', value: '' }, { label: '系统', value: 'system' }, { label: '用户(ubuntu)', value: 'user' }]
const kindOpts = [{ label: '全部类型', value: '' }, { label: '定时器', value: 'timer' }, { label: '服务', value: 'service' }]
const catOpts = ['morning_chain', 'research', 'radar', 'quantdinger', 'data', 'hermes', 'qp', 'ops', 'other', 'os'].map((c) => ({ label: c, value: c }))
const rows = computed(() => all.value.filter((u) =>
  (!q.value || u.name.includes(q.value)) && (!stateFilter.value || u.active_state === stateFilter.value)))
const now = Date.now()
const nextCell = (r: U) => {
  if (!r.next_elapse) return '-'
  const overdue = r.active_state === 'active' && new Date(r.next_elapse).getTime() < now
  return overdue
    ? h(NTag, { size: 'small', type: 'warning' }, { default: () => `${fmt(r.next_elapse)}（已过期）` })
    : fmt(r.next_elapse)
}
const cols = computed(() => [
  { title: '范围', key: 'scope', width: 70 }, { title: '名称', key: 'name' }, { title: '分类', key: 'category', width: 120 },
  { title: '状态', key: 'active_state', render: (r: U) => h(NTag, { size: 'small', type: tagType(r.active_state) }, { default: () => r.active_state }) },
  ...(kind.value === 'timer' ? [] : [{ title: '结果', key: 'result', render: (r: U) => r.result ? h(NTag, { size: 'small', type: tagType(r.result) }, { default: () => r.result }) : '-' }]),
  { title: '触发/被触发', key: 'activates', render: (r: U) => r.activates || r.triggered_by || '-' },
  { title: '上次触发', key: 'last_trigger', render: (r: U) => fmt(r.last_trigger) },
  { title: '下次触发', key: 'next_elapse', render: nextCell },
])
async function load() {
  const p = new URLSearchParams()
  if (scope.value) p.set('scope', scope.value)
  if (kind.value) p.set('kind', kind.value)
  if (category.value) p.set('category', category.value)
  if (includeOs.value) p.set('include_os', 'true')
  all.value = await api<U[]>(`/ops/units?${p}`)
}
watch([scope, kind, category, includeOs], load)
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
