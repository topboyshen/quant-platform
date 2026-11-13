<template>
  <n-space vertical>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !projects.length" emptyText="暂无研究项目">
    <n-empty v-if="!loading && !projects.length" description="暂无研究项目" />
    <n-card v-for="p in projects" :key="p.id" :title="p.name || p.public_id || `项目 ${p.id}`">
      <template #header-extra><n-tag size="small">{{ p.status || '—' }} · rev {{ p.revision ?? '—' }}</n-tag></template>
      <n-text depth="3">{{ p.goal || '—' }}</n-text>
      <n-divider />
      <n-space vertical size="small">
        <n-text v-for="b in p.branches" :key="b.id">
          分支 {{ b.name || b.public_id || b.id }} · {{ b.status || '—' }} · rev {{ b.revision ?? '—' }}
        </n-text>
        <n-text v-if="!p.branches.length" depth="3">无分支</n-text>
      </n-space>
    </n-card>
  </AsyncState>
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NCard, NDivider, NEmpty, NSpace, NTag, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api } from '../../api/client'

interface Branch { id: number; public_id: string | null; name: string | null; status: string | null; revision: number | null }
interface Project { id: number; public_id: string | null; name: string | null; status: string | null; revision: number | null; goal: string | null; branches: Branch[] }

const projects = ref<Project[]>([])
const loading = ref(false)
const error = ref('')
onMounted(async () => {
  loading.value = true
  try { projects.value = (await api<{ rows: Project[] }>('/research/projects')).rows } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
})
</script>
