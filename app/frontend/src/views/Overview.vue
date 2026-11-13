<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">总览</n-h2>
    <AsyncState :loading="loading" :error="error">
      <n-grid :cols="3" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
        <n-gi span="3 1:1">
          <n-card title="平台">
            <n-text depth="3">版本</n-text>
            <n-h3 style="margin: 4px 0 0">{{ data?.version || '—' }}</n-h3>
          </n-card>
        </n-gi>
        <n-gi span="3 1:1">
          <n-card title="行情健康">
            <template v-if="data?.health?.run_at">
              <n-text depth="3">{{ data.health.run_at }}</n-text>
              <n-h3 style="margin: 4px 0 8px">{{ data.health.ok }} / {{ data.health.total }}</n-h3>
              <n-space size="small">
                <n-tag v-for="(status, name) in data.health.checks" :key="name"
                       :type="tagType(status)" size="small">{{ name }}: {{ status }}</n-tag>
              </n-space>
            </template>
            <n-text v-else depth="3">暂无健康数据</n-text>
          </n-card>
        </n-gi>
        <n-gi span="3 1:1">
          <n-card title="信号">
            <template v-if="data?.signals">
              <n-text depth="3">{{ data.signals.decision_date }} · 第 {{ data.signals.attempt_no }} 次</n-text>
              <n-h3 style="margin: 4px 0 8px">{{ data.signals.status }} / {{ data.signals.outcome || '—' }}</n-h3>
              <n-space size="small">
                <n-tag size="small">信号 {{ data.signals.signal_count }}</n-tag>
                <n-tag size="small" type="success">放行 {{ data.signals.allowed }}</n-tag>
                <n-tag size="small" type="warning">拦截 {{ data.signals.blocked }}</n-tag>
              </n-space>
            </template>
            <n-text v-else depth="3">暂无 EOD 运行</n-text>
          </n-card>
        </n-gi>
        <n-gi span="3 1:1">
          <n-card title="报告">
            <template v-if="data">
              <n-text depth="3">最近业务日：{{ data.reports.latest_date || '—' }}</n-text>
              <n-space size="small" style="margin-top: 8px">
                <n-tag v-for="(n, kind) in data.reports.by_kind" :key="kind" size="small">{{ kind }}: {{ n }}</n-tag>
                <n-text v-if="!Object.keys(data.reports.by_kind).length" depth="3">暂无报告</n-text>
              </n-space>
            </template>
          </n-card>
        </n-gi>
        <n-gi span="3 1:1">
          <n-card title="任务">
            <template v-if="data">
              <n-h3 style="margin: 0 0 8px">24h 失败：{{ data.jobs.failed_24h }}</n-h3>
              <n-space vertical size="small">
                <n-text v-for="j in data.jobs.recent" :key="j.id" depth="3">
                  #{{ j.id }} {{ j.job_name }} · {{ j.status }} · {{ j.started_at }}
                </n-text>
                <n-text v-if="!data.jobs.recent.length" depth="3">暂无任务记录</n-text>
              </n-space>
            </template>
          </n-card>
        </n-gi>
        <n-gi span="3 1:1">
          <n-card title="通知">
            <template v-if="data">
              <n-space size="small">
                <n-tag v-for="(n, mode) in data.notify.outbox" :key="mode" size="small">{{ mode }}: {{ n }}</n-tag>
              </n-space>
              <n-h3 style="margin: 8px 0 0">投递尝试：{{ data.notify.delivery_attempt }}</n-h3>
            </template>
          </n-card>
        </n-gi>
      </n-grid>
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NCard, NGi, NGrid, NH2, NH3, NSpace, NTag, NText } from 'naive-ui'
import AsyncState from '../components/AsyncState.vue'
import { api } from '../api/client'

interface Overview {
  version: string
  health: { run_at: string | null; checks: Record<string, string>; ok?: number; total?: number }
  signals: null | {
    decision_date: string; attempt_no: number; status: string; outcome: string | null
    signal_count: number; allowed: number; blocked: number
  }
  reports: { by_kind: Record<string, number>; latest_date: string | null }
  jobs: { failed_24h: number; recent: { id: number; job_name: string; status: string; started_at: string }[] }
  notify: { outbox: Record<string, number>; delivery_attempt: number }
}

const data = ref<Overview | null>(null)
const loading = ref(true)
const error = ref('')

function tagType(status: string): 'success' | 'warning' | 'error' | 'default' {
  if (status === 'ok' || status === 'succeeded') return 'success'
  if (status === 'warning') return 'warning'
  if (status === 'unknown') return 'default'
  return 'error'
}

onMounted(async () => {
  try {
    data.value = await api<Overview>('/overview')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
})
</script>
