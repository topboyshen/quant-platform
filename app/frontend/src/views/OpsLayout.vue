<template>
  <n-space vertical size="large">
    <n-tabs type="line" :value="active" @update:value="go">
      <n-tab v-for="t in tabs" :key="t.name" :name="t.name">{{ t.label }}</n-tab>
    </n-tabs>
    <router-view />
  </n-space>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NSpace, NTab, NTabs } from 'naive-ui'
const tabs = [
  { name: 'ops', label: '健康与任务运行' }, { name: 'ops-monitors', label: '监控' },
  { name: 'ops-tasks', label: '任务中心' }, { name: 'ops-alerts', label: '告警' }, { name: 'ops-compare', label: '新旧对比' },
  { name: 'ops-services', label: '服务与项目' }, { name: 'ops-coverage', label: '数据覆盖' },
  { name: 'ops-tokens', label: '服务令牌' },
]
const route = useRoute(), router = useRouter()
const active = computed(() => String(route.name))
const go = (n: string) => router.push({ name: n })
</script>
