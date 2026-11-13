<template>
  <n-space vertical size="large">
    <n-alert type="info" :bordered="false">qp 独立抓取行情数据并写入 qp 数据库；旧链已停止，旧数据仅用于只读对比。</n-alert>
    <n-tabs type="line" :value="active" @update:value="go">
      <n-tab v-for="t in tabs" :key="t.name" :name="t.name">{{ t.label }}</n-tab>
    </n-tabs>
    <router-view />
  </n-space>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NSpace, NTab, NTabs } from 'naive-ui'
const tabs = [
  { name: 'market', label: '数据健康' }, { name: 'market-instruments', label: '证券主数据' },
  { name: 'market-calendar', label: '交易日历' }, { name: 'market-indexclose', label: '指数收盘' },
  { name: 'market-coverage', label: '数据覆盖' }, { name: 'market-imports', label: '导入对账' },
]
const route = useRoute(), router = useRouter()
const active = computed(() => String(route.name))
const go = (n: string) => router.push({ name: n })
</script>
