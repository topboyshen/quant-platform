<template>
  <n-space vertical size="large">
    <n-card title="新建服务令牌">
      <n-space align="center">
        <n-input v-model:value="name" placeholder="名称，如 hermes-readonly" style="width: 240px" />
        <n-select v-model:value="scopes" multiple :options="scopeOpts" style="width: 260px" />
        <n-button type="primary" :disabled="!name" @click="create">创建</n-button>
      </n-space>
      <n-alert v-if="created" type="success" style="margin-top: 12px" title="令牌只显示这一次，请立即保存">
        <code style="word-break: break-all">{{ created }}</code>
      </n-alert>
      <n-alert v-if="err" type="error" style="margin-top: 12px">{{ err }}</n-alert>
    </n-card>
    <n-data-table size="small" :bordered="false" :data="rows" :columns="cols" />
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NInput, NPopconfirm, NSelect, NSpace, NTag } from 'naive-ui'
import { api, ApiError } from '../api/client'
import { fmt } from '../util'
interface T { id: number; name: string; scopes: string[]; created_at: string; revoked_at: string | null; last_used_at: string | null }
const rows = ref<T[]>([]), name = ref(''), scopes = ref<string[]>(['ops:read']), created = ref(''), err = ref('')
const scopeOpts = ['ops:read', 'ops:write', 'research:read', 'signals:read', 'reports:read'].map((s) => ({ label: s, value: s }))
const cols = [
  { title: '名称', key: 'name' }, { title: '权限', key: 'scopes', render: (r: T) => r.scopes.join(', ') },
  { title: '创建 (CST)', key: 'created_at', render: (r: T) => fmt(r.created_at) },
  { title: '最近使用', key: 'last_used_at', render: (r: T) => fmt(r.last_used_at) },
  { title: '状态', key: 'revoked_at', render: (r: T) => h(NTag, { size: 'small', type: r.revoked_at ? 'default' : 'success' }, { default: () => (r.revoked_at ? '已吊销' : '有效') }) },
  { title: '', key: 'a', render: (r: T) => r.revoked_at ? null : h(NPopconfirm, { onPositiveClick: () => revoke(r.id) }, { trigger: () => h(NButton, { size: 'tiny', type: 'error', ghost: true }, { default: () => '吊销' }), default: () => `吊销 ${r.name}？` }) },
]
async function load() { rows.value = await api<T[]>('/ops/tokens') }
async function create() {
  err.value = ''; created.value = ''
  try {
    const r = await api<{ token: string }>('/ops/tokens', { method: 'POST', body: JSON.stringify({ name: name.value, scopes: scopes.value }) })
    created.value = r.token; name.value = ''; await load()
  } catch (e) { err.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
}
async function revoke(id: number) { await api(`/ops/tokens/${id}`, { method: 'DELETE' }); await load() }
onMounted(load)
</script>
