<template>
  <n-result status="info" :title="`${title}（规划中）`" description="该模块将在后续里程碑（M2 起）迁入。" />
</template>
<script setup lang="ts">
import { NResult } from 'naive-ui'
defineProps<{ title: string }>()
</script>
