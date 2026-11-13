<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !tasks.length && !sessions.length" emptyText="暂无 AI 记录">
    <n-tabs type="segment" v-model:value="tab">
      <n-tab-pane name="tasks" tab="分析任务">
        <n-data-table :columns="taskColumns" :data="tasks" :loading="loading" size="small" />
      </n-tab-pane>
      <n-tab-pane name="sessions" tab="对话">
        <n-data-table :columns="sessionColumns" :data="sessions" :loading="loading" :row-props="sessionProps" size="small" />
      </n-tab-pane>
    </n-tabs>
    <n-drawer v-model:show="show" :width="620">
      <n-drawer-content :title="current?.title || current?.id || '对话'">
        <n-space vertical>
          <n-card v-for="m in messages" :key="m.id" size="small" :title="m.role || '—'">
            <n-text>{{ m.content }}</n-text>
          </n-card>
        </n-space>
      </n-drawer-content>
    </n-drawer>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NCard, NDataTable, NDrawer, NDrawerContent, NSpace, NTabPane, NTabs, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Task { id: number; market: string | null; symbol: string | null; model: string | null; status: string | null; decision: string | null; confidence: number | null; created_at: string | null }
interface Session { id: string; title: string | null; market: string | null; symbol: string | null; updated_at: string | null }
interface Message { id: number; role: string | null; content: string | null }

const tab = ref('tasks')
const tasks = ref<Task[]>([])
const sessions = ref<Session[]>([])
const loading = ref(false)
const error = ref('')
const show = ref(false)
const current = ref<Session | null>(null)
const messages = ref<Message[]>([])

const taskColumns = [
  { title: 'ID', key: 'id', width: 70 },
  { title: '市场', key: 'market', width: 80 },
  { title: '代码', key: 'symbol', width: 100 },
  { title: '模型', key: 'model', width: 110 },
  { title: '状态', key: 'status', width: 100 },
  { title: '结论', key: 'decision', width: 100 },
  { title: '置信', key: 'confidence', width: 70 },
]
const sessionColumns = [
  { title: 'ID', key: 'id', width: 220 },
  { title: '标题', key: 'title' },
  { title: '代码', key: 'symbol', width: 100 },
  { title: '更新时间', key: 'updated_at', width: 180 },
]
function sessionProps(row: Session) { return { style: 'cursor: pointer', onClick: async () => {
  current.value = row
  messages.value = (await api<{ rows: Message[] }>(`/research/ai/sessions/${encodeURIComponent(row.id)}/messages`)).rows
  show.value = true
} } }
onMounted(async () => {
  loading.value = true
  try {
    tasks.value = (await api<{ rows: Task[] }>('/research/ai/tasks?limit=100')).rows
    sessions.value = (await api<{ rows: Session[] }>('/research/ai/sessions?limit=100')).rows
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
