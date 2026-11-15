<template>
  <n-space vertical size="large">
    <n-card size="small">
      <template #header>
        <n-space align="center" :wrap="true">
          <span>记录链 5 日窗口</span>
          <n-tag v-if="win" size="small" :type="win.verdict === 'PASS' ? 'success' : win.verdict === 'FAIL' ? 'error' : 'default'">
            {{ WIN_LABEL[win.verdict] || win.verdict }}
          </n-tag>
          <n-text v-if="win && win.days_total" depth="3">
            {{ win.days_pass }}/{{ win.days_total }} 天 · {{ win.window_start }} → {{ win.window_end }}
          </n-text>
          <n-text v-else depth="3">窗口从首个 native 批次（周一 EOD）开始</n-text>
        </n-space>
      </template>
      <n-data-table size="small" :columns="windowCols" :data="win?.rows || []" :pagination="false" />
    </n-card>
    <n-space align="center">
      <n-date-picker v-model:value="ts" type="date" clearable placeholder="最新快照" />
      <n-button size="small" @click="load">查询</n-button>
      <template v-if="snap">
        <n-tag size="small" :type="snap.status === 'complete' ? 'success' : 'warning'">{{ snap.status }}</n-tag>
        <n-text depth="3">{{ snap.business_date }} · 截止 15:50 · {{ snap.snapshot_id }}</n-text>
      </template>
    </n-space>
    <AsyncState :loading="loading" :error="error" :empty="!loading && !error && !snap" :empty-text="emptyText">
      <template v-if="snap">
        <n-grid :cols="5" :x-gap="12" responsive="screen" item-responsive>
          <n-gi v-for="(value, key) in snap.body.funnel" :key="key" span="5 1:1">
            <n-card size="small">
              <n-statistic :label="FUNNEL[key] || key" :value="value" />
            </n-card>
          </n-gi>
        </n-grid>
        <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
          <n-gi span="2 m:1">
            <n-card title="样本与进度" size="small">
              <n-space size="small" style="margin-bottom: 8px">
                <n-tag v-for="(value, key) in snap.body.sample" :key="key" size="small"
                       :type="key === 'qualified_matured' ? 'success' : key === 'invalid' || key === 'expired_uncollected' ? 'error' : 'default'">
                  {{ SAMPLE[key] || key }} {{ value }}
                </n-tag>
              </n-space>
              <n-text depth="3">
                已成熟 {{ snap.body.progress.matured }} / {{ snap.body.progress.threshold }}（主周期 {{ snap.body.progress.primary_horizon_sessions }} 日）
                · 最近合格日 {{ snap.body.progress.last_new_eligible_date || '—' }}
              </n-text>
            </n-card>
          </n-gi>
          <n-gi span="2 m:1">
            <n-card title="工程损失" size="small">
              <n-space size="small">
                <n-tag v-for="(value, key) in snap.body.loss" :key="key" size="small" :type="value ? 'error' : 'success'">
                  {{ LOSS[key] || key }} {{ value }}
                </n-tag>
              </n-space>
              <n-text v-if="lossTotal" depth="3" type="error" style="display: block; margin-top: 8px">
                有 {{ lossTotal }} 条工程丢记录，快照数字可能偏小，展开下方明细定位。
              </n-text>
            </n-card>
          </n-gi>
        </n-grid>
        <n-card title="决策明细" size="small">
          <template #header>
            <n-space align="center">
              <span>决策明细</span>
              <n-select v-model:value="stateFilter" size="small" clearable placeholder="全部状态" style="width: 190px"
                        :options="stateOptions" />
              <n-select v-model:value="eligibilityFilter" size="small" clearable placeholder="全部资格" style="width: 160px"
                        :options="eligibilityOptions" />
              <n-text depth="3">共 {{ total }} 条</n-text>
            </n-space>
          </template>
          <AsyncState :loading="rowsLoading" :error="rowsError" :empty="!rows.length" empty-text="该快照没有决策明细">
            <n-data-table :columns="columns" :data="rows" size="small" :row-props="rowProps" />
          </AsyncState>
        </n-card>
      </template>
    </AsyncState>
    <n-drawer v-model:show="show" :width="720">
      <n-drawer-content :title="detail ? `${detail.decision.code} ${detail.decision.name || ''}` : '决策'">
        <template v-if="detail">
          <n-descriptions :column="2" size="small" bordered>
            <n-descriptions-item label="身份">{{ detail.decision.decision_uid }}</n-descriptions-item>
            <n-descriptions-item label="批次">{{ detail.decision.batch_key }}</n-descriptions-item>
            <n-descriptions-item label="规则">{{ detail.decision.rule_version }}</n-descriptions-item>
            <n-descriptions-item label="标的池">{{ detail.decision.universe_version }}</n-descriptions-item>
          </n-descriptions>
          <n-h4>尝试（{{ detail.attempts.length }}）</n-h4>
          <n-space vertical size="small">
            <n-text v-for="a in detail.attempts" :key="a.attempt_id" depth="3">
              run {{ a.run_id }} · {{ fmt(a.started_at) }} → {{ fmt(a.completed_at) }}{{ a.error_code ? ' · ' + a.error_code : '' }}
            </n-text>
          </n-space>
          <n-h4>状态修订</n-h4>
          <n-data-table size="small" :bordered="false" :data="detail.states" :columns="stateCols" :pagination="false" />
          <n-h4>结果（按持有期）</n-h4>
          <n-data-table size="small" :bordered="false" :data="detail.outcomes" :columns="outcomeCols" :pagination="false" />
          <n-h4>资格</n-h4>
          <n-data-table size="small" :bordered="false" :data="detail.eligibility" :columns="eligCols" :pagination="false" />
        </template>
      </n-drawer-content>
    </n-drawer>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue'
import { NButton, NCard, NDataTable, NDatePicker, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent,
  NGi, NGrid, NH4, NSelect, NSpace, NStatistic, NTag, NText } from 'naive-ui'
import AsyncState from '../../components/AsyncState.vue'
import { api, ApiError } from '../../api/client'
import { fmt } from '../../util'

const FUNNEL: Record<string, string> = { checked: '检查', triggered: '触发', risk_blocked: '风控拦截',
  risk_allowed: '风控放行', admitted: '进入样本' }
const SAMPLE: Record<string, string> = { waiting_maturity: '待成熟', qualified_matured: '合格且成熟',
  historical_excluded: '历史排除', other_excluded: '其他排除', attribution_pending: '归属待定',
  expired_uncollected: '到期未采集', invalid: '结果无效' }
const LOSS: Record<string, string> = { universe_members_without_decision: '成员无决策',
  signal_decisions_without_eligibility: '触发缺资格', matured_without_result: '成熟无结果' }

interface Snap { snapshot_id: string; digest: string; business_date: string; status: string; generated_at: string;
  body: { funnel: Record<string, number>; sample: Record<string, number>;
    progress: { matured: number; threshold: number; primary_horizon_sessions: number; last_new_eligible_date: string | null };
    loss: Record<string, number> } }
interface Row { decision_uid: string; code: string; name: string | null; data_state: string | null;
  signal_state: string | null; risk_state: string | null; verdict: string | null;
  eligibility_reason: string | null; execution_state: string | null; result_state: string | null;
  expected_maturity_date: string | null }
interface Detail { decision: { decision_uid: string; code: string; name: string | null; batch_key: string;
  rule_version: string; universe_version: string }; attempts: { attempt_id: string; run_id: number;
    started_at: string | null; completed_at: string | null; error_code: string | null }[];
  states: { aspect: string; state: string; reason_code: string | null; state_seq: number; observed_at: string }[];
  outcomes: { horizon_sessions: number; revision: number; execution_state: string; result_state: string;
    expected_maturity_date: string | null; entry_date: string | null; exit_date: string | null;
    net_return: number | null; revision_reason: string | null }[];
  eligibility: { verdict: string; reason_code: string; policy_version: string; evaluation_seq: number;
    evaluated_at: string }[] }

interface WindowRow { date: string; status: string; run_status: string | null;
  snapshot_status: string | null; compare_ok: boolean | null; decisions: number;
  allowed: number; blocked: number; shadow_samples: number; shadow_outcomes: number;
  reason: string }
interface WindowData { verdict: string; days_total: number; days_pass: number;
  window_start: string | null; window_end: string | null; rows: WindowRow[] }

const WIN_LABEL: Record<string, string> = { PASS: '通过', FAIL: '未通过', NOT_STARTED: '未开始' }
const win = ref<WindowData | null>(null)
const windowCols = [
  { title: '日期', key: 'date', width: 110 },
  { title: '结果', key: 'status', width: 90,
    render: (r: WindowRow) => h(NTag, { size: 'small', type: r.status === 'PASS' ? 'success' : 'error' },
      { default: () => (r.status === 'PASS' ? '通过' : '未通过') }) },
  { title: 'EOD', key: 'run_status', width: 90 },
  { title: '快照', key: 'snapshot_status', width: 90 },
  { title: '一致', key: 'compare_ok', width: 70,
    render: (r: WindowRow) => (r.compare_ok === null ? '—' : r.compare_ok ? '是' : '否') },
  { title: '决策', key: 'decisions', width: 70 },
  { title: '放行', key: 'allowed', width: 70 },
  { title: '拦截', key: 'blocked', width: 70 },
  { title: '样本/结果', key: 'shadow_samples', width: 110,
    render: (r: WindowRow) => `${r.shadow_samples}/${r.shadow_outcomes}` },
  { title: '说明', key: 'reason', minWidth: 120 },
]

const ts = ref<number | null>(null)
const snap = ref<Snap | null>(null)
const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false), error = ref(''), rowsLoading = ref(false), rowsError = ref('')
const stateFilter = ref<string | null>(null), eligibilityFilter = ref<string | null>(null)
const show = ref(false), detail = ref<Detail | null>(null)
const emptyText = '还没有记录链快照（QP_RECORD_CHAIN 未开启，或 15:55 任务尚未运行）'
const lossTotal = computed(() => snap.value ? Object.values(snap.value.body.loss).reduce((a, b) => a + b, 0) : 0)

const stateOptions = [
  { label: '触发', value: 'triggered' }, { label: '未触发', value: 'not_triggered' },
  { label: '风控放行', value: 'allowed' }, { label: '风控拦截', value: 'blocked' },
  { label: '数据完整', value: 'complete' }, { label: '采集失败', value: 'collection_failed' },
]
const eligibilityOptions = [
  { label: '合格', value: 'eligible' }, { label: '排除', value: 'excluded' }, { label: '未知', value: 'unknown' },
]
const columns = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name' },
  { title: '数据', key: 'data_state', width: 120 },
  { title: '信号', key: 'signal_state', width: 110 },
  { title: '风控', key: 'risk_state', width: 90 },
  { title: '资格', key: 'verdict', width: 100 },
  { title: '原因', key: 'eligibility_reason', width: 150 },
  { title: '结果', key: 'result_state', width: 130 },
]
const stateCols = [
  { title: '类别', key: 'aspect', width: 80 }, { title: '状态', key: 'state', width: 150 },
  { title: '序号', key: 'state_seq', width: 70 }, { title: '原因', key: 'reason_code' },
  { title: '观测', key: 'observed_at', render: (r: { observed_at: string }) => fmt(r.observed_at) },
]
const outcomeCols = [
  { title: '持有期', key: 'horizon_sessions', width: 80 }, { title: '修订', key: 'revision', width: 70 },
  { title: '执行', key: 'execution_state', width: 110 }, { title: '结果', key: 'result_state', width: 130 },
  { title: '到期日', key: 'expected_maturity_date', width: 110 },
  { title: '净收益', key: 'net_return', render: (r: { net_return: number | null }) =>
    r.net_return === null ? '—' : `${(r.net_return * 100).toFixed(2)}%` },
]
const eligCols = [
  { title: '判定', key: 'verdict', width: 100 }, { title: '原因', key: 'reason_code' },
  { title: '版本', key: 'policy_version', width: 130 }, { title: '序号', key: 'evaluation_seq', width: 70 },
  { title: '评估时间', key: 'evaluated_at', render: (r: { evaluated_at: string }) => fmt(r.evaluated_at) },
]

function iso(value: number) {
  const d = new Date(value)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

async function loadRows() {
  if (!snap.value) return
  rowsLoading.value = true; rowsError.value = ''
  try {
    const params = new URLSearchParams({ date: snap.value.business_date })
    if (stateFilter.value) params.set('state', stateFilter.value)
    if (eligibilityFilter.value) params.set('eligibility', eligibilityFilter.value)
    const out = await api<{ total: number; rows: Row[] }>(`/signals/chain/decisions?${params}`)
    rows.value = out.rows
    total.value = out.total
  } catch (e) { rowsError.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  finally { rowsLoading.value = false }
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const query = ts.value ? `?date=${iso(ts.value)}` : ''
    snap.value = await api<Snap>(`/signals/chain/snapshot${query}`)
    await loadRows()
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) { snap.value = null; rows.value = []; total.value = 0 }
    else error.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e)
  } finally { loading.value = false }
}

watch([stateFilter, eligibilityFilter], loadRows)

function rowProps(row: Row) {
  return { style: 'cursor: pointer', onClick: async () => {
    try { detail.value = await api<Detail>(`/signals/chain/decision/${row.decision_uid}`); show.value = true }
    catch (e) { rowsError.value = e instanceof ApiError ? `${e.status} ${e.message}` : String(e) }
  } }
}
async function loadWindow() {
  try { win.value = await api<WindowData>('/signals/chain/window') } catch { /* 非关键：快照区域仍可用 */ }
}

onMounted(() => { void loadWindow(); void load() })
</script>
