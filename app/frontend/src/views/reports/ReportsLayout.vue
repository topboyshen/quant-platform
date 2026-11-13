<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">报告</n-h2>
    <n-alert type="info" :bordered="false">报告自动生成链已于 2026-11-13 删除；本页仅保留历史归档，不再新增报告或投递。</n-alert>
    <n-tabs type="line" :value="active" @update:value="go">
      <n-tab name="center">报告中心</n-tab>
      <n-tab name="deliveries">投递与回执</n-tab>
    </n-tabs>
    <router-view />
  </n-space>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NH2, NSpace, NTab, NTabs } from 'naive-ui'

const route = useRoute()
const router = useRouter()
const active = computed(() => String(route.name || 'reports-center').replace('reports-', ''))
function go(name: string) { router.push({ name: `reports-${name}` }) }
</script>
