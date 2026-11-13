<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">设置</n-h2>
    <AsyncState :loading="loading" :error="error" :empty="!data">
      <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
        <n-gi span="2 1:1">
          <n-card title="账号">
            <n-descriptions :column="1" size="small" bordered>
              <n-descriptions-item label="用户名">{{ data?.account.username }}</n-descriptions-item>
              <n-descriptions-item label="类型">{{ data?.account.kind }}</n-descriptions-item>
              <n-descriptions-item label="角色">{{ data?.account.role || '—' }}</n-descriptions-item>
              <n-descriptions-item label="口令方案">{{ data?.account.hash_scheme || '—' }}</n-descriptions-item>
              <n-descriptions-item label="上次登录">{{ data?.account.last_login_at || '—' }}</n-descriptions-item>
            </n-descriptions>
          </n-card>
        </n-gi>
        <n-gi span="2 1:1">
          <n-card title="平台">
            <n-descriptions :column="1" size="small" bordered>
              <n-descriptions-item label="版本">{{ data?.platform.version }}</n-descriptions-item>
              <n-descriptions-item label="通知模式">
                <n-tag :type="data?.platform.shadow_only ? 'success' : 'warning'" size="small">
                  {{ data?.platform.notify_mode }}
                </n-tag>
              </n-descriptions-item>
            </n-descriptions>
            <n-alert type="info" style="margin-top: 12px">
              单账号模式。服务令牌在「运维 · 服务令牌」管理；修改口令需管理员操作。
            </n-alert>
          </n-card>
        </n-gi>
      </n-grid>
    </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NCard, NDescriptions, NDescriptionsItem, NGi, NGrid, NH2, NSpace, NTag } from 'naive-ui'
import AsyncState from '../components/AsyncState.vue'
import { api } from '../api/client'

interface Settings {
  account: { username: string; kind: string; role: string | null; hash_scheme: string | null; last_login_at: string | null }
  platform: { version: string; notify_mode: string; shadow_only: boolean }
}

const data = ref<Settings | null>(null)
const loading = ref(true)
const error = ref('')
onMounted(async () => {
  try { data.value = await api<Settings>('/settings') } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
