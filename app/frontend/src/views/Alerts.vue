<template>
  <n-space vertical size="large">
    <n-alert type="warning">告警只写入告警表与通知 outbox（影子模式），不会发送 Telegram。</n-alert>
    <n-radio-group v-model:value="state"><n-radio-button value="firing">进行中</n-radio-button><n-radio-button value="resolved">已恢复</n-radio-button><n-radio-button value="">全部</n-radio-button></n-radio-group>
    <n-data-table size="small" :bordered="false" :data="rows" :columns="cols" :pagination="{ pageSize: 30 }" />
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref, watch } from 'vue'
import { NAlert, NDataTable, NRadioButton, NRadioGroup, NSpace, NTag } from 'naive-ui'
import { api } from '../api/client'
import { fmt, tagType } from '../util'
interface A { id: number; key: string; severity: string; state: string; summary: string; created_at: string; last_seen_at: string | null; resolved_at: string | null }
const rows = ref<A[]>([]), state = ref('firing')
const cols = [
  { title: '级别', key: 'severity', width: 90, render: (r: A) => h(NTag, { size: 'small', type: tagType(r.severity) }, { default: () => r.severity }) },
  { title: '键', key: 'key' }, { title: '摘要', key: 'summary' }, { title: '状态', key: 'state', width: 90 },
  { title: '首次 (CST)', key: 'created_at', render: (r: A) => fmt(r.created_at) },
  { title: '最近 (CST)', key: 'last_seen_at', render: (r: A) => fmt(r.last_seen_at) },
  { title: '恢复 (CST)', key: 'resolved_at', render: (r: A) => fmt(r.resolved_at) },
]
async function load() { rows.value = await api<A[]>(`/ops/alerts?state=${state.value}`) }
watch(state, load)
onMounted(load)
</script>
