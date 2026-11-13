<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">研究</n-h2>
    <n-tabs type="line" :value="active" @update:value="go">
      <n-tab name="backtests">回测</n-tab>
      <n-tab name="profit">盈利研究</n-tab>
      <n-tab name="ai">AI 分析</n-tab>
      <n-tab name="projects">研究项目</n-tab>
    </n-tabs>
    <router-view />
  </n-space>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NH2, NSpace, NTab, NTabs } from 'naive-ui'

const route = useRoute()
const router = useRouter()
const active = computed(() => String(route.name || 'research-backtests').replace('research-', ''))
function go(name: string) { router.push({ name: `research-${name}` }) }
</script>
