<template>
  <n-spin :show="loading">
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <n-empty v-else-if="empty" :description="emptyText" style="padding: 32px 0" />
    <slot v-else />
  </n-spin>
</template>
<script setup lang="ts">
import { NAlert, NEmpty, NSpin } from 'naive-ui'

withDefaults(defineProps<{ loading?: boolean; error?: string; empty?: boolean; emptyText?: string }>(), {
  loading: false,
  error: '',
  empty: false,
  emptyText: '暂无数据',
})
</script>
