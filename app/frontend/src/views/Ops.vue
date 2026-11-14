<template>
  <n-space vertical size="large">
    <n-space align="center" justify="space-between">
      <n-h2 style="margin: 0">运维</n-h2>
      <n-button size="small" :loading="loading" @click="load">刷新</n-button>
    </n-space>
    <n-card title="健康检查（各检查最新值）">
      <n-space>
        <n-tag v-for="c in health?.checks || []" :key="c.check" :type="c.ok ? 'success' : 'error'" :title="c.at ? '观测于 ' + fmt(c.at) : undefined">
          {{ c.check }}<template v-if="c.value !== null">：{{ c.value }}</template>
        </n-tag>
        <span v-if="!health?.checks?.length" class="muted">暂无记录</span>
      </n-space>
      <p class="muted">
        通知 outbox：待处理 {{ health?.outbox.pending ?? '-' }} · 影子 {{ health?.outbox.shadowed ?? '-' }} · 已发送 {{ health?.outbox.sent ?? '-' }} · 失败 {{ health?.outbox.failed ?? '-' }}
        （全局 {{ health?.outbox.mode ?? '-' }}<template v-if="health?.outbox.signals_mode"> · 信号 {{ health.outbox.signals_mode }}</template><template v-if="health?.outbox.kill_switch"> · 紧急停止已启用</template>）
      </p>
    </n-card>
    <n-card title="任务">
      <n-data-table :columns="jobCols" :data="jobs" size="small" :bordered="false" />
    </n-card>
    <n-card title="最近任务运行">
      <n-data-table :columns="runCols" :data="runs" size="small" :bordered="false" :pagination="{ pageSize: 20 }" />
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NH2, NSpace, NTag, type DataTableColumns } from 'naive-ui'
import { api } from '../api/client'

interface Run { id: number; job_name: string; status: string; trigger: string; started_at: string; duration_s: number | null; error: string | null }
interface Job { name: string; schedule_hint: string | null; last_status: string | null; last_started_at: string | null }
interface Health { checks: { check: string; ok: boolean; value: number | null; at?: string }[]; outbox: { pending: number; shadowed: number; sent?: number; failed?: number; mode?: string; signals_mode?: string; kill_switch?: boolean } }

const runs = ref<Run[]>([]), jobs = ref<Job[]>([]), health = ref<Health | null>(null), loading = ref(false)
const fmt = (s: string | null) => (s ? new Date(s).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '-')
const statusTag = (s: string | null) => h(NTag, { size: 'small', type: s === 'succeeded' ? 'success' : s === 'failed' ? 'error' : 'default' }, { default: () => s || '-' })

const jobCols: DataTableColumns<Job> = [
  { title: '任务', key: 'name' }, { title: '调度', key: 'schedule_hint' },
  { title: '最近状态', key: 'last_status', render: (r) => statusTag(r.last_status) },
  { title: '最近开始 (CST)', key: 'last_started_at', render: (r) => fmt(r.last_started_at) },
]
const runCols: DataTableColumns<Run> = [
  { title: 'ID', key: 'id', width: 70 }, { title: '任务', key: 'job_name' },
  { title: '状态', key: 'status', render: (r) => statusTag(r.status) }, { title: '触发', key: 'trigger' },
  { title: '开始 (CST)', key: 'started_at', render: (r) => fmt(r.started_at) },
  { title: '耗时 s', key: 'duration_s' }, { title: '错误', key: 'error', ellipsis: { tooltip: true } },
]

async function load() {
  loading.value = true
  try {
    ;[runs.value, jobs.value, health.value] = await Promise.all([
      api<Run[]>('/ops/job-runs?limit=100'), api<Job[]>('/ops/jobs'), api<Health>('/ops/health'),
    ])
  } finally { loading.value = false }
}
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
