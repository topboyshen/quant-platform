<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">信号</n-h2>
    <n-tabs type="line" :value="active" @update:value="go">
      <n-tab name="runs">EOD 运行</n-tab>
      <n-tab name="trends">趋势信号</n-tab>
      <n-tab name="outcomes">结果</n-tab>
      <n-tab name="compare">影子对比</n-tab>
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
const active = computed(() => String(route.name || 'signals-runs').replace('signals-', ''))
function go(name: string) { router.push({ name: `signals-${name}` }) }
</script>
