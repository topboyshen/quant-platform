<template>
  <n-space vertical size="large">
    <n-space justify="space-between" align="center">
      <span class="muted">采集时间：{{ fmt(inv?.collector?.at) }}（快照 {{ inv?.collector?.data?.age_min ?? '-' }} 分钟前，耗时 {{ inv?.collector?.data?.duration_s ?? '-' }}s）</span>
      <n-button size="small" @click="load">刷新</n-button>
    </n-space>
    <n-grid :cols="3" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi span="3 m:1"><n-card title="证书">
        <div v-for="c in inv?.certs?.data || []" :key="c.target">
          <n-tag :type="c.ok && c.hours_left > 48 ? 'success' : 'warning'">:{{ c.target }}</n-tag>
          到期 {{ fmt(c.not_after) }}（剩 {{ c.hours_left }} 小时）
        </div>
      </n-card></n-gi>
      <n-gi span="3 m:1"><n-card title="出网（仅连通性探测，不发消息）">
        <div v-for="e in inv?.egress?.data || []" :key="e.target">
          <n-tag :type="e.ok ? 'success' : e.via_proxy ? 'error' : 'default'">{{ e.target }}</n-tag>
          {{ e.via_proxy ? '经代理' : '直连' }} · {{ e.ok ? e.status + ' · ' + e.ms + 'ms' : (e.error || '失败') }}
        </div>
      </n-card></n-gi>
      <n-gi span="3 m:1"><n-card title="主机与数据库">
        <div>根分区剩余 {{ inv?.host?.data?.disk_root?.free_gb }} GB / {{ inv?.host?.data?.disk_root?.total_gb }} GB</div>
        <div>可用内存 {{ inv?.host?.data?.mem_mb?.MemAvailable }} MB / {{ inv?.host?.data?.mem_mb?.MemTotal }} MB</div>
        <div>PG 连接 {{ inv?.pg?.data?.connections }} / {{ inv?.pg?.data?.max_connections }}</div>
        <div v-for="(v, k) in inv?.pg?.data?.databases || {}" :key="k" class="muted">{{ k }}：{{ (Number(v) / 1048576).toFixed(1) }} MB</div>
      </n-card></n-gi>
    </n-grid>
    <n-card title="错误日志摘要（最近 15 分钟，仅计数）">
      <n-space vertical>
        <div v-for="(lg, scope) in inv?.logs?.data || {}" :key="scope">
          <b>{{ scope }}</b>：共 {{ lg.total }} 条
          <n-tag v-for="(n, u) in lg.counts" :key="u" size="small" :type="n >= 50 ? 'warning' : 'default'" style="margin: 2px">{{ u }} × {{ n }}</n-tag>
        </div>
      </n-space>
    </n-card>
    <n-card title="容器">
      <n-data-table size="small" :bordered="false" :data="inv?.containers?.data || []" :columns="cCols" />
    </n-card>
    <n-card title="监听端口">
      <n-space><n-tag v-for="p in inv?.ports?.data || []" :key="p.addr + p.port" size="small">{{ p.addr }}:{{ p.port }}</n-tag></n-space>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NGi, NGrid, NSpace, NTag } from 'naive-ui'
import { api } from '../api/client'
import { fmt, tagType } from '../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const inv = ref<any>(null)
const cCols = [
  { title: '名称', key: 'name' }, { title: '镜像', key: 'image', ellipsis: { tooltip: true } },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '状态', key: 'state', render: (r: any) => h(NTag, { size: 'small', type: tagType(r.state) }, { default: () => r.state }) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '健康', key: 'health', render: (r: any) => h(NTag, { size: 'small', type: tagType(r.health) }, { default: () => r.health || '-' }) },
  { title: '说明', key: 'status' },
]
async function load() { inv.value = await api('/ops/inventory') }
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
