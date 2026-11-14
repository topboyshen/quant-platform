<template>
  <n-space vertical size="large">
    <n-alert type="warning">运维告警只写入告警表与通知 outbox（全局 shadow，不发送 Telegram）；信号类通知走独立的 live 开关。</n-alert>
    <n-space align="center">
      <n-radio-group v-model:value="state">
        <n-radio-button value="firing">进行中 {{ counts.firing }}</n-radio-button>
        <n-radio-button value="resolved">已恢复 {{ counts.resolved }}</n-radio-button>
        <n-radio-button value="">全部 {{ all.length }}</n-radio-button>
      </n-radio-group>
      <n-button size="small" :loading="loading" @click="load">刷新</n-button>
    </n-space>
    <AsyncState :loading="loading" :error="error" :empty="rows.length === 0" :empty-text="emptyText">
      <n-data-table size="small" :bordered="false" :data="rows" :columns="cols" :pagination="{ pageSize: 30 }" />
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NAlert, NButton, NDataTable, NRadioButton, NRadioGroup, NSpace, NTag } from 'naive-ui'
import { api, ApiError } from '../api/client'
import AsyncState from '../components/AsyncState.vue'
import { fmt, tagType } from '../util'
interface A { id: number; key: string; severity: string; state: string; summary: string; created_at: string; last_seen_at: string | null; resolved_at: string | null }
const all = ref<A[]>([]), state = ref('firing'), loading = ref(false), error = ref('')
const counts = computed(() => ({
  firing: all.value.filter((a) => a.state === 'firing').length,
  resolved: all.value.filter((a) => a.state === 'resolved').length,
}))
const rows = computed(() => (state.value ? all.value.filter((a) => a.state === state.value) : all.value))
const emptyText = computed(() => (state.value === 'firing' ? '没有进行中的告警' : state.value === 'resolved' ? '没有已恢复的告警' : '还没有告警记录'))
const cols = [
  { title: '级别', key: 'severity', width: 90, render: (r: A) => h(NTag, { size: 'small', type: tagType(r.severity) }, { default: () => r.severity }) },
  { title: '状态', key: 'state', width: 90 },
  { title: '摘要', key: 'summary', width: 260, ellipsis: { tooltip: true } },
  { title: '键', key: 'key', width: 260, ellipsis: { tooltip: true } },
  { title: '首次 (CST)', key: 'created_at', width: 160, render: (r: A) => fmt(r.created_at) },
  { title: '最近 (CST)', key: 'last_seen_at', width: 160, render: (r: A) => fmt(r.last_seen_at) },
  { title: '恢复 (CST)', key: 'resolved_at', width: 160, render: (r: A) => fmt(r.resolved_at) },
]
async function load() {
  loading.value = true; error.value = ''
  try { all.value = await api<A[]>('/ops/alerts?state=&limit=500') }
  catch (e) { error.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  finally { loading.value = false }
}
onMounted(load)
</script>
