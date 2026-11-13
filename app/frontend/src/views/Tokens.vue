<template>
  <n-space vertical size="large">
    <n-card title="关于服务令牌">
      <n-space vertical size="small">
        <div>服务令牌用于让脚本/服务以 <code>Authorization: Bearer qpst_…</code> 调用只读 API，不需要登录会话。</div>
        <div>令牌只能访问其 scope 覆盖的接口组：<code>ops:read</code> 运维/总览/设置，<code>market:read</code> 行情，<code>research:read</code> 研究，<code>signals:read</code> 信号，<code>reports:read</code> 报告；越权返回 403。</div>
        <div>浏览器登录的用户会话不受 scope 限制；令牌管理接口只接受用户会话。</div>
        <div>令牌只在创建时显示一次，库里只存 sha256；泄露后请立即吊销并重建。</div>
      </n-space>
    </n-card>
    <n-card title="新建服务令牌">
      <n-space align="center">
        <n-input v-model:value="name" placeholder="名称，如 hermes-readonly" style="width: 240px" />
        <n-select v-model:value="scopes" multiple :options="scopeOpts" style="width: 320px" />
        <n-button type="primary" :disabled="!name || scopes.length === 0" :loading="creating" @click="create">创建</n-button>
      </n-space>
      <n-alert v-if="created" type="success" style="margin-top: 12px" title="令牌只显示这一次，请立即保存">
        <n-text copyable code style="word-break: break-all">{{ created }}</n-text>
      </n-alert>
      <n-alert v-if="err" type="error" style="margin-top: 12px">{{ err }}</n-alert>
    </n-card>
    <n-card title="已有令牌">
      <AsyncState :loading="loading" :error="loadError" :empty="rows.length === 0" empty-text="还没有服务令牌">
        <n-data-table size="small" :bordered="false" :data="rows" :columns="cols" />
      </AsyncState>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NInput, NPopconfirm, NSelect, NSpace, NTag, NText } from 'naive-ui'
import { api, ApiError } from '../api/client'
import AsyncState from '../components/AsyncState.vue'
import { fmt } from '../util'
interface T { id: number; name: string; scopes: string[]; created_at: string; expires_at: string | null; revoked_at: string | null; last_used_at: string | null }
const rows = ref<T[]>([]), name = ref(''), scopes = ref<string[]>(['ops:read']), created = ref(''), err = ref('')
const loading = ref(false), loadError = ref(''), creating = ref(false)
const scopeOpts = ['ops:read', 'ops:write', 'market:read', 'research:read', 'signals:read', 'reports:read']
  .map((s) => ({ label: s, value: s }))
const cols = [
  { title: '名称', key: 'name' },
  { title: '权限', key: 'scopes', render: (r: T) => (r.scopes || []).join(', ') || '—' },
  { title: '创建 (CST)', key: 'created_at', render: (r: T) => fmt(r.created_at) },
  { title: '最近使用', key: 'last_used_at', render: (r: T) => (r.last_used_at ? fmt(r.last_used_at) : '从未使用') },
  { title: '有效期', key: 'expires_at', render: (r: T) => (r.expires_at ? fmt(r.expires_at) : '长期有效') },
  { title: '状态', key: 'revoked_at', render: (r: T) => h(NTag, { size: 'small', type: r.revoked_at ? 'default' : 'success' }, { default: () => (r.revoked_at ? '已吊销' : '有效') }) },
  { title: '', key: 'a', render: (r: T) => r.revoked_at ? null : h(NPopconfirm, { onPositiveClick: () => revoke(r.id) }, { trigger: () => h(NButton, { size: 'tiny', type: 'error', ghost: true }, { default: () => '吊销' }), default: () => `吊销 ${r.name}？` }) },
]
async function load() {
  loading.value = true; loadError.value = ''
  try { rows.value = await api<T[]>('/ops/tokens') }
  catch (e) { loadError.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  finally { loading.value = false }
}
async function create() {
  err.value = ''; created.value = ''; creating.value = true
  try {
    const r = await api<{ token: string }>('/ops/tokens', { method: 'POST', body: JSON.stringify({ name: name.value, scopes: scopes.value }) })
    created.value = r.token; name.value = ''; await load()
  } catch (e) { err.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  finally { creating.value = false }
}
async function revoke(id: number) { await api(`/ops/tokens/${id}`, { method: 'DELETE' }); await load() }
onMounted(load)
</script>
