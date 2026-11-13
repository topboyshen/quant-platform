<template>
  <n-space vertical size="large">
    <n-space align="center">
      <n-input v-model:value="q" placeholder="代码或名称" clearable style="width: 200px" @keyup.enter="reset" />
      <n-select v-model:value="board" :options="boardOpts" clearable placeholder="板块" style="width: 140px" />
      <n-select v-model:value="index" :options="indexOpts" clearable placeholder="指数成分(最新)" style="width: 170px" />
      <n-select v-model:value="inQlib" :options="qlibOpts" style="width: 150px" />
      <n-button @click="reset">查询</n-button>
      <span class="muted">{{ loading ? '加载中…' : '共 ' + total + ' 条' }}</span>
    </n-space>
    <n-data-table remote size="small" :bordered="false" :loading="loading" :data="rows" :columns="cols" :pagination="pagination"
      @update:page="(p: number) => { page = p; load() }" />
    <n-card title="板块分布"><EChart :option="pieOpt" height="240px" /></n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { NButton, NCard, NDataTable, NInput, NSelect, NSpace } from 'naive-ui'
import EChart from '../../components/EChart.vue'
import { api } from '../../api/client'
interface R { symbol: string; name: string | null; board: string | null; qlib_start: string | null; qlib_end: string | null; in_qlib: boolean }
const q = ref(''), board = ref<string | null>(null), index = ref<string | null>(null), inQlib = ref<string>('all')
const rows = ref<R[]>([]), total = ref(0), page = ref(1), boards = ref<Record<string, number>>({}), indexes = ref<Record<string, number>>({})
const loading = ref(true)
const PAGE = 50
const BOARD: Record<string, string> = { main: '主板', star: '科创板', chinext: '创业板', bse: '北交所', index: '指数' }
const boardOpts = Object.entries(BOARD).map(([value, label]) => ({ value, label }))
const indexOpts = computed(() => Object.keys(indexes.value).map((k) => ({ value: k, label: k.toUpperCase() })))
const qlibOpts = [{ value: 'all', label: '全部' }, { value: 'true', label: '在 Qlib 中' }, { value: 'false', label: '不在 Qlib 中' }]
const pagination = reactive({ page, pageSize: PAGE, itemCount: total })
const cols = [
  { title: '代码', key: 'symbol', width: 110 }, { title: '名称', key: 'name' },
  { title: '板块', key: 'board', render: (r: R) => (r.board ? BOARD[r.board] || r.board : '-') },
  { title: 'Qlib 起', key: 'qlib_start' }, { title: 'Qlib 止', key: 'qlib_end' },
  { title: 'Qlib', key: 'in_qlib', render: (r: R) => (r.in_qlib ? '是' : '否') },
]
const pieOpt = computed(() => ({ tooltip: { trigger: 'item' }, legend: { bottom: 0 },
  series: [{ type: 'pie', radius: ['40%', '70%'], data: Object.entries(boards.value).map(([k, v]) => ({ name: BOARD[k] || k || '未知', value: v })) }] }))
async function load() {
  loading.value = true
  try {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String((page.value - 1) * PAGE) })
    if (q.value) p.set('q', q.value)
    if (board.value) p.set('board', board.value)
    if (index.value) p.set('index', index.value)
    if (inQlib.value !== 'all') p.set('in_qlib', inQlib.value)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const r = await api<any>(`/market/instruments?${p}`)
    rows.value = r.rows; total.value = r.total; boards.value = r.boards; indexes.value = r.indexes
  } finally {
    loading.value = false
  }
}
function reset() { page.value = 1; load() }
watch([board, index, inQlib], reset)
onMounted(load)
</script>
<style scoped>.muted { color: #888; font-size: 13px; }</style>
