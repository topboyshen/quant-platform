<template>
  <n-space vertical size="large">
    <n-space justify="space-between" align="center" :wrap="true">
      <n-h2 style="margin: 0">总览</n-h2>
      <n-space align="center" size="small">
        <n-tag v-if="data" size="small">v{{ data.version }}</n-tag>
        <n-button size="small" :loading="loading" @click="load">刷新</n-button>
      </n-space>
    </n-space>

    <AsyncState :loading="loading" :error="error">
      <template v-if="data">
        <n-card size="small">
          <n-space align="center" size="small" :wrap="true">
            <n-tag size="small" type="info">{{ data.today }}</n-tag>
            <n-tag size="small">{{ data.session_label }}</n-tag>
            <n-text depth="3">业务日 {{ data.business_date }}</n-text>
            <n-text depth="3">汇总 {{ fmt(data.generated_at) }}</n-text>
            <n-text depth="3">{{ refreshLabel }}</n-text>
            <n-text depth="3">版本 {{ data.version }}</n-text>
          </n-space>
        </n-card>

        <n-card v-if="data.same_day_failures.length" title="同日曾失败（已由后续运行恢复）" size="small">
          <n-space vertical size="small">
            <n-alert v-for="item in data.same_day_failures" :key="`${item.job_name}:${item.run_id}`"
                     type="warning" :show-icon="true">
              <n-space align="center" size="small" :wrap="true">
                <n-tag size="small" type="warning">{{ item.result_failed ? 'result FAIL' : item.status }}</n-tag>
                <n-text strong>{{ item.job_name }}</n-text>
                <n-text depth="3">run {{ item.run_id }} · {{ fmt(item.started_at) }}</n-text>
                <n-text depth="3">后续成功 run {{ item.recovered_by_run_id }}</n-text>
              </n-space>
            </n-alert>
          </n-space>
        </n-card>

        <n-card title="当前待处理" size="small">
          <n-empty v-if="!data.pending.length" description="没有待处理异常" />
          <n-space v-else vertical size="small">
            <n-alert v-for="issue in data.pending" :key="issue.id" :type="issueType(issue.severity)"
                     :show-icon="true">
              <n-space align="center" size="small" :wrap="true">
                <n-tag size="small">{{ SCOPE_LABEL[issue.scope] || issue.scope }}</n-tag>
                <n-text strong>{{ issue.summary }}</n-text>
              </n-space>
              <n-text depth="3" style="display: block; margin-top: 4px">
                影响：{{ issue.impact || '—' }} · 原因：{{ issue.reason || '—' }}
                <template v-if="issue.started_at"> · 开始：{{ fmt(issue.started_at) }}</template>
              </n-text>
              <n-text v-if="evidenceText(issue.evidence)" depth="3" style="display: block">
                证据：{{ evidenceText(issue.evidence) }}
              </n-text>
            </n-alert>
          </n-space>
        </n-card>

        <n-grid :cols="3" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
          <n-gi v-for="item in BUSINESS_MODULES" :key="item.key" span="3 m:1">
            <n-card :title="item.label" size="small">
              <template v-if="module(item.key).available">
                <n-space align="center" size="small" :wrap="true">
                  <n-tag size="small" :type="statusType(module(item.key).status)">
                    {{ module(item.key).status_label || module(item.key).status }}
                  </n-tag>
                  <n-text depth="3">截止 {{ module(item.key).deadline || '—' }}</n-text>
                </n-space>
                <n-text style="display: block; margin-top: 8px">{{ module(item.key).summary }}</n-text>
                <n-text v-if="module(item.key).actual_at" depth="3" style="display: block">
                  最近完成 {{ fmt(module(item.key).actual_at) }}
                </n-text>
                <n-space v-if="module(item.key).gaps?.length" size="small" style="margin-top: 8px">
                  <n-tag v-for="gap in module(item.key).gaps" :key="gap" size="small" type="warning">
                    {{ gap }}
                  </n-tag>
                </n-space>
                <n-button v-if="evidenceApi(item.key)" text type="primary"
                          style="margin-top: 8px" @click="go(evidenceApi(item.key))">
                  查看证据
                </n-button>
              </template>
              <n-text v-else depth="3">
                无权限：需要 {{ module(item.key).required_scope || '对应领域 scope' }}
              </n-text>
            </n-card>
          </n-gi>
        </n-grid>

        <n-card title="今日日报与研究通知" size="small">
          <template v-if="module('reports').available">
            <n-space align="center" size="small" :wrap="true">
              <n-tag size="small" :type="statusType(module('reports').status)">
                {{ module('reports').status_label || module('reports').status }}
              </n-tag>
              <n-text depth="3">业务日 {{ data.business_date }} · 截止 {{ module('reports').deadline }}</n-text>
            </n-space>
            <n-grid :cols="2" :x-gap="12" responsive="screen" item-responsive style="margin-top: 8px">
              <n-gi span="2 m:1">
                <n-card title="EOD 日报" size="small">
                  <n-text>{{ deliveryText(module('reports').metrics?.daily) }}</n-text>
                </n-card>
              </n-gi>
              <n-gi span="2 m:1">
                <n-card title="ETF 研究通知" size="small">
                  <n-text>{{ deliveryText(module('reports').metrics?.etf) }}</n-text>
                </n-card>
              </n-gi>
            </n-grid>
            <n-text v-if="module('reports').gaps?.length" depth="3" type="warning"
                    style="display: block; margin-top: 8px">
              缺口：{{ gapsText('reports') }}
            </n-text>
          </template>
          <n-text v-else depth="3">无权限：需要 {{ module('reports').required_scope }}</n-text>
        </n-card>

        <n-card title="近期自然运行质量" size="small">
          <template v-if="data.recent_quality.length">
            <n-data-table size="small" :columns="qualityCols" :data="data.recent_quality" :pagination="false" />
            <n-text depth="3" style="display: block; margin-top: 8px">
              verdict 只表示复评结果；natural_pass 只有首次投递成功才为真。手工恢复不计入自然通过率。
            </n-text>
          </template>
          <n-text v-else depth="3">无可用验收记录</n-text>
        </n-card>

        <n-card v-if="module('ops').available" title="运维摘要" size="small">
          <n-space size="small" :wrap="true">
            <n-tag :type="statusType(module('ops').status)" size="small">
              {{ module('ops').status_label || module('ops').status }}
            </n-tag>
            <n-text depth="3">{{ module('ops').summary }}</n-text>
            <n-button text type="primary" @click="go('/ops')">打开运维页</n-button>
          </n-space>
        </n-card>
      </template>
    </AsyncState>
  </n-space>
</template>

<script setup lang="ts">
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NDataTable, NEmpty, NGi, NGrid, NH2, NSpace, NTag, NText } from 'naive-ui'
import AsyncState from '../components/AsyncState.vue'
import { api } from '../api/client'
import { fmt } from '../util'

interface ModuleData {
  available: boolean
  required_scope?: string
  status?: string
  status_label?: string
  deadline?: string
  actual_at?: string | null
  summary?: string
  metrics?: Record<string, any>
  gaps?: string[]
  evidence?: Record<string, any>
}
interface Issue {
  id: string
  scope: string
  severity: string
  summary: string
  impact?: string
  reason?: string
  started_at?: string | null
  evidence?: any
}
interface QualityRow {
  business_date: string
  eod: string | null
  acceptance: string | null
  run_quality: string
  natural_pass: boolean
  chain_window: string | null
  notification: string
}
interface Overview {
  generated_at: string
  version: string
  release_sha?: string
  today: string
  business_date: string
  session: string
  session_label: string
  access: { kind: string; name: string; scopes: string[] }
  modules: Record<string, ModuleData>
  pending: Issue[]
  same_day_failures: { job_name: string; run_id: number; status: string; result_failed?: boolean;
    started_at: string | null; recovered_by_run_id: number }[]
  recent_quality: QualityRow[]
}

const SCOPE_LABEL: Record<string, string> = {
  market: '行情', signals: '信号', research: '研究', reports: '报告', ops: '运维',
}
const BUSINESS_MODULES = [
  { key: 'market', label: '行情链' },
  { key: 'signals', label: '信号链' },
  { key: 'research', label: 'ETF 研究' },
]
const QUALITY_LABEL: Record<string, string> = {
  natural: '自然通过', retry_recovered: '重试恢复', manual_recovered: '手工恢复',
  warn: '警告', failed: '失败', incomplete: '未完成', pending: '待运行',
}
const data = ref<Overview | null>(null)
const loading = ref(true)
const error = ref('')
const now = ref(Date.now())
let refreshTimer: ReturnType<typeof setInterval> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
const router = useRouter()

const refreshLabel = computed(() => {
  const at = data.value?.generated_at
  if (!at) return ''
  const min = Math.max(0, Math.round((now.value - new Date(at).getTime()) / 60000))
  return min < 1 ? '刚刚刷新' : `${min} 分钟前刷新`
})

function module(key: string): ModuleData {
  return data.value?.modules[key] || { available: false, status: 'unknown' }
}
function statusType(status?: string): 'success' | 'warning' | 'error' | 'default' {
  if (status === 'ok') return 'success'
  if (status === 'warning' || status === 'pending') return 'warning'
  if (status === 'failed') return 'error'
  return 'default'
}
function issueType(severity: string): 'error' | 'warning' | 'info' {
  return severity === 'critical' ? 'error' : severity === 'warning' ? 'warning' : 'info'
}
function evidenceText(value: any): string {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (value.api) return String(value.api)
  return JSON.stringify(value)
}
function evidenceApi(key: string): string {
  return String(module(key).evidence?.api || '')
}
function gapsText(key: string): string {
  return (module(key).gaps || []).join('、')
}
function deliveryText(row: any): string {
  if (!row) return '没有记录'
  const bits = [`${row.mode}/${row.status}`]
  if (row.message_id) bits.push(`msg ${row.message_id}`)
  if (row.recovery) bits.push(`恢复 ${QUALITY_LABEL[row.recovery] || row.recovery}`)
  if (row.attempts) bits.push(`${row.attempts} 次尝试`)
  if (row.receipt_status) bits.push(`回执 ${row.receipt_status}`)
  return bits.join(' · ')
}
function go(path: string) {
  if (path?.startsWith('/')) void router.push(path)
}

const qualityCols = [
  { title: '业务日', key: 'business_date', width: 110 },
  { title: 'EOD', key: 'eod', width: 150 },
  { title: '验收', key: 'acceptance', width: 80 },
  { title: '质量', key: 'run_quality', width: 110,
    render: (row: QualityRow) => h(NTag, {
      size: 'small',
      type: row.run_quality === 'natural' ? 'success'
        : row.run_quality === 'manual_recovered' ? 'warning'
        : row.run_quality === 'failed' ? 'error' : 'default',
    }, { default: () => QUALITY_LABEL[row.run_quality] || row.run_quality }) },
  { title: '自然通过', key: 'natural_pass', width: 90,
    render: (row: QualityRow) => (row.natural_pass ? '是' : '否') },
  { title: '链窗口', key: 'chain_window', width: 90 },
  { title: '通知', key: 'notification', width: 110,
    render: (row: QualityRow) => QUALITY_LABEL[row.notification] || row.notification },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api<Overview>('/overview')
    now.value = Date.now()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  clockTimer = setInterval(() => { now.value = Date.now() }, 30000)
  refreshTimer = setInterval(() => { void load() }, 60000)
})
onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>
