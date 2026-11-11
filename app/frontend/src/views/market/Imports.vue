<template>
  <n-space vertical size="large">
    <n-alert type="info" :bordered="false">旧数据只读导入：逐项核对行数与规范化 sha256（旧文件 vs 回读 qp 数据库）。首次导入：{{ fmt(data?.first_import_at) }}</n-alert>
    <n-data-table size="small" :bordered="false" :data="data?.latest || []" :columns="cols" />
  </n-space>
</template>
<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NAlert, NDataTable, NSpace, NTag } from 'naive-ui'
import { api } from '../../api/client'
import { fmt } from '../../util'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const data = ref<any>(null)
const short = (s: string | null) => (s ? s.slice(0, 12) + '…' : '-')
const cols = [
  { title: '项目', key: 'item' }, { title: '旧路径', key: 'legacy_path', ellipsis: { tooltip: true } },
  { title: '旧行数', key: 'legacy_rows' }, { title: '库行数', key: 'db_rows' },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '旧校验', key: 'legacy_checksum', render: (r: any) => short(r.legacy_checksum) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '库校验', key: 'db_checksum', render: (r: any) => short(r.db_checksum) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '结果', key: 'status', render: (r: any) => h(NTag, { size: 'small', type: r.status === 'match' ? 'success' : 'error' }, { default: () => (r.status === 'match' ? '一致' : '不一致') }) },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  { title: '时间', key: 'at', render: (r: any) => fmt(r.at) },
]
onMounted(async () => { data.value = await api('/market/imports') })
</script>
