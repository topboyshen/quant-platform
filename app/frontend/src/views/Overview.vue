<template>
  <n-space vertical size="large">
    <n-h2 style="margin: 0">总览</n-h2>
    <n-alert type="info">新平台（M2）：单账号登录、任务表、通知 outbox（影子模式，不发送）、每日备份。旧站点保持不变。</n-alert>
    <n-grid :cols="3" :x-gap="16" responsive="screen" item-responsive>
      <n-gi v-for="c in cards" :key="c.t"><n-card :title="c.t">{{ c.d }}</n-card></n-gi>
    </n-grid>
  </n-space>
</template>
<script setup lang="ts">
import { NAlert, NCard, NGi, NGrid, NH2, NSpace } from 'naive-ui'
const cards = [
  { t: '行情', d: '市场数据（规划中）' }, { t: '研究', d: '研究台账、回测（规划中）' },
  { t: '信号', d: '雷达与信号（规划中）' }, { t: '报告', d: '早报 / 日报（规划中）' },
  { t: '运维', d: '健康、监控、任务中心、告警、服务与项目、数据覆盖、服务令牌（M2 已可用）' },
]
</script>
