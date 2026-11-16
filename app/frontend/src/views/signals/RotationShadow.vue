<template>
  <section class="rotation-shadow" :data-theme="theme" :aria-busy="loading" aria-labelledby="rotation-title">
    <header class="page-heading">
      <div>
        <div class="eyebrow">C13 · 独立模拟账户</div>
        <h3 id="rotation-title">轮动影子 <span class="paper-label">仅纸面观察</span></h3>
        <p>250 交易日动量 · Top 8 等权 · 每 20 交易日调仓 · 无市场过滤</p>
      </div>
      <button class="control" :disabled="loading" @click="load">{{ loading ? '读取中…' : '刷新记录' }}</button>
    </header>

    <div class="toolbar">
      <div class="segmented" role="group" aria-label="观察区间">
        <button :aria-pressed="phase === 'forward'" @click="phase = 'forward'">前向观察</button>
        <button :aria-pressed="phase === 'replay'" @click="phase = 'replay'">历史回放 · 仅对照</button>
      </div>
      <button class="control theme-toggle" @click="theme = theme === 'light' ? 'dark' : 'light'">
        页面配色：{{ theme === 'light' ? '浅色' : '深色' }}
      </button>
    </div>

    <aside class="phase-note" :class="{ 'replay-note': phase === 'replay' }" role="note">
      <StatusIcon :status="phase === 'replay' ? 'blocked' : 'pending'" />
      <div>
        <strong>{{ phase === 'forward' ? `前向观察自 ${forwardStart} 起独立记账` : '历史回放仅用于对照，不是前向证据' }}</strong>
        <p>{{ phase === 'forward'
          ? '前一交易日仅作资金基线，不计观察天数；不继承回放盈亏与持仓。'
          : '回放净值、调仓与交易日均属于历史区间，不计入前向观察。' }}
          本页始终为模拟，不发送通知，不进入实盘或正式记录链。
        </p>
      </div>
    </aside>

    <div v-if="error" class="notice error-notice" role="alert">
      <StatusIcon status="blocked" />
      <div><strong>加载失败</strong><p>{{ error }}</p><p v-if="snapshot">以下保留本区间上次成功读取的记录，尚未更新。</p></div>
      <button class="control" :disabled="loading" @click="load">重试</button>
    </div>
    <div v-if="loading && !snapshot" class="panel initial-state" role="status" aria-live="polite">
      <n-spin size="small" /><strong>正在读取{{ phaseLabel(phase) }}记录…</strong>
      <span>只展示已保存的估值与模拟调仓，不生成演示数据。</span>
    </div>

    <div v-if="snapshot" class="page-content" :class="{ refreshing: loading }">
      <div v-if="loading" class="refresh-note" role="status">正在更新；下方暂为本区间上次读取的记录。</div>
      <div v-if="isRotationEmpty(snapshot)" class="panel empty-panel">
        <n-empty :description="phase === 'forward' ? '尚无前向观察记录' : '尚未导入历史回放；历史行情仅用于指标预热'" size="small">
          <template #extra><span class="secondary">等待后台写入；缺失值保持为 —，不会用回放或样例填充。</span></template>
        </n-empty>
      </div>

      <dl class="kpi-row">
        <div v-for="item in kpis" :key="item.label" class="kpi">
          <dt>{{ item.label }}</dt><dd>{{ item.value }}</dd><small>{{ item.note }}</small>
        </div>
      </dl>

      <div v-if="snapshot.quality" class="notice quality-note" role="status">
        <StatusIcon :status="qualityGood ? 'executed' : 'blocked'" />
        <div>
          <strong>{{ qualityGood ? '数据检查通过' : '数据质量提示' }}</strong>
          <span v-if="snapshot.quality.status" class="quality-code">{{ snapshot.quality.status }}</span>
          <p>{{ snapshot.quality.message || '—' }}</p>
          <p v-if="snapshot.quality.missing_codes?.length">缺失标的：{{ snapshot.quality.missing_codes.join('、') }}</p>
          <p v-if="snapshot.quality.stale_date">陈旧数据日期：{{ snapshot.quality.stale_date }}</p>
        </div>
      </div>

      <article class="panel chart-panel" aria-labelledby="nav-title">
        <div class="panel-heading">
          <div><h4 id="nav-title">{{ phase === 'forward' ? '前向净值' : '历史回放净值 · 非前向' }}</h4><p>{{ periodText }} · 初始资金 {{ currency(initialCapital) }}</p></div>
          <div class="segmented small" role="group" aria-label="净值显示方式">
            <button :aria-pressed="view === 'chart'" @click="view = 'chart'">曲线</button>
            <button :aria-pressed="view === 'table'" @click="view = 'table'">数据表</button>
          </div>
        </div>
        <ul class="chart-legend" aria-label="净值图例">
          <li v-for="line in LINES" :key="line.key">
            <svg width="28" height="12" aria-hidden="true"><line x1="1" y1="6" x2="27" y2="6" :style="{ stroke: `var(${line.color})` }" :stroke-dasharray="line.dash" stroke-width="2" /></svg>
            <span>{{ line.name }}</span><small>{{ line.pattern }}</small>
          </li>
        </ul>
        <p id="rotation-chart-note" class="chart-note">净值 = 资产 ÷ 初始资金（起点 1.0000）；三条线共用同一坐标轴。缺失估值不连线。</p>

        <div v-if="view === 'chart' && hasPlot" ref="chartElement" class="chart-wrap">
          <svg class="nav-chart" :viewBox="`0 0 ${chartWidth} ${chartHeight}`" :height="chartHeight" role="group" aria-label="C13、EW-20d 与 510300 的同基准净值曲线">
            <g aria-hidden="true">
              <g v-for="tick in scale.ticks" :key="tick">
                <line :x1="plot.left" :x2="plot.right" :y1="y(tick)" :y2="y(tick)" class="grid-line" />
                <text :x="plot.left - 10" :y="y(tick) + 4" text-anchor="end" class="axis-text">{{ tick.toFixed(scale.digits) }}</text>
              </g>
              <line :x1="plot.left" :x2="plot.right" :y1="plot.bottom" :y2="plot.bottom" class="axis-line" />
              <text v-for="tick in dateTicks" :key="tick.index" :x="tick.x" :y="plot.bottom + 24" :text-anchor="tick.anchor" class="axis-text">{{ tick.label }}</text>
              <g v-for="line in plottedLines" :key="line.key">
                <path :d="line.path" :style="{ stroke: `var(${line.color})` }" :stroke-dasharray="line.dash" class="chart-line" />
                <circle v-for="dot in line.isolated" :key="dot.index" :cx="dot.x" :cy="dot.y" r="4" :style="{ fill: `var(${line.color})` }" class="data-dot" />
              </g>
              <g v-for="end in endLabels" :key="end.key">
                <path :d="`M ${end.x} ${end.y} L ${plot.right + 5} ${end.y} L ${plot.right + 12} ${end.labelY}`" class="label-leader" />
                <circle :cx="end.x" :cy="end.y" r="4" :style="{ fill: `var(${end.color})` }" class="data-dot" />
                <text :x="plot.right + 16" :y="end.labelY + 4" class="end-label">{{ end.short }}</text>
              </g>
              <g v-if="activePoint && activeIndex != null">
                <line :x1="x(activeIndex)" :x2="x(activeIndex)" :y1="plot.top" :y2="plot.bottom" class="crosshair" />
                <template v-for="line in LINES" :key="line.key">
                  <circle v-if="activePoint[line.key] != null" :cx="x(activeIndex)" :cy="y(activePoint[line.key]!)" r="4" :style="{ fill: `var(${line.color})` }" class="data-dot" />
                </template>
              </g>
            </g>
            <rect class="chart-hit" :x="plot.left" :y="plot.top" :width="plot.right - plot.left" :height="plot.bottom - plot.top" fill="transparent"
                  tabindex="0" role="slider" aria-label="净值日期；用左右方向键逐日查看，Home 和 End 跳至首末日" aria-describedby="rotation-chart-note"
                  :aria-valuemin="0" :aria-valuemax="points.length - 1" :aria-valuenow="activeIndex ?? points.length - 1" :aria-valuetext="activeDescription"
                  @pointermove="pointAt" @pointerdown="pointAt" @pointerleave="leaveChart" @focus="focusChart" @blur="blurChart" @keydown="chartKey" />
          </svg>
          <div v-if="activePoint" class="chart-tooltip" :style="tooltipPosition" role="tooltip">
            <time>{{ activePoint.date }}</time>
            <div v-for="line in LINES" :key="line.key" class="tooltip-row">
              <svg width="22" height="12" aria-hidden="true"><line x1="1" y1="6" x2="21" y2="6" :style="{ stroke: `var(${line.color})` }" :stroke-dasharray="line.dash" stroke-width="2" /></svg>
              <strong>{{ indexText(activePoint[line.key]) }}</strong><span>{{ line.short }}</span>
            </div>
          </div>
        </div>
        <div v-else-if="view === 'chart'" class="chart-empty"><n-empty size="small" description="暂无可绘制的净值记录" /></div>
        <div class="table-scroll nav-table" :class="{ 'print-only': view !== 'table' }" tabindex="0" aria-label="净值数据表，可横向滚动">
          <table>
            <caption class="sr-only">{{ phaseLabel(phase) }}净值；按初始资金归一化，最新日期在前</caption>
            <thead><tr><th scope="col">日期</th><th v-for="line in LINES" :key="line.key" scope="col" class="numeric">{{ line.short }} 净值</th><th scope="col" class="numeric">C13 资产（元）</th></tr></thead>
            <tbody><tr v-for="row in tablePoints" :key="row.date"><th scope="row">{{ row.date }}</th><td class="numeric">{{ indexText(row.c13) }}</td><td class="numeric">{{ indexText(row.ew) }}</td><td class="numeric">{{ indexText(row.benchmark) }}</td><td class="numeric">{{ currency(rawNav.get(row.date)) }}</td></tr></tbody>
          </table>
          <p v-if="!tablePoints.length" class="table-empty">暂无净值记录</p>
        </div>
        <p class="chart-note chart-footer">悬停或轻触查看同日三条净值；键盘聚焦曲线后可用方向键查阅。起始资金基线不计入前向交易日。</p>
      </article>

      <article class="panel" aria-labelledby="metrics-title">
        <div class="panel-heading"><h4 id="metrics-title">{{ phaseLabel(phase) }} · 区间指标</h4><span class="secondary">仅描述所选区间，不代表未来收益</span></div>
        <div class="table-scroll" tabindex="0" aria-label="区间指标，可横向滚动">
          <table>
            <thead><tr><th scope="col">组合 / 对照</th><th scope="col" class="numeric">累计收益</th><th scope="col" class="numeric">年化收益</th><th scope="col" class="numeric">年化波动</th><th scope="col" class="numeric">夏普</th><th scope="col" class="numeric">最大回撤</th></tr></thead>
            <tbody><tr v-for="row in comparisons" :key="row.name"><th scope="row">{{ row.name }}</th><td class="numeric">{{ percent(row.total) }}</td><td class="numeric">{{ percent(row.metrics?.cagr) }}</td><td class="numeric">{{ percent(row.metrics?.vol) }}</td><td class="numeric">{{ numberText(row.metrics?.sharpe, 2) }}</td><td class="numeric">{{ percent(row.metrics?.max_drawdown) }}</td></tr></tbody>
          </table>
        </div>
      </article>

      <div class="portfolio-grid">
        <article class="panel" aria-labelledby="positions-title">
          <div class="panel-heading"><h4 id="positions-title">{{ phase === 'forward' ? '当前模拟持仓' : '回放期末持仓 · 非当前前向' }}</h4><span class="secondary">估值日 {{ snapshot.last_date || '—' }}</span></div>
          <div v-if="snapshot.positions.length" class="table-scroll" tabindex="0" aria-label="模拟持仓，可横向滚动">
            <table class="holdings-table">
              <thead><tr><th scope="col">标的</th><th scope="col" class="numeric">份额</th><th scope="col" class="numeric">收盘价</th><th scope="col" class="numeric">市值（元）</th><th scope="col" class="numeric">权重</th></tr></thead>
              <tbody><tr v-for="(row, i) in snapshot.positions" :key="row.code || i"><th scope="row"><span>{{ row.name || '—' }}</span><small class="code">{{ row.code || '—' }}</small></th><td class="numeric">{{ numberText(row.shares) }}</td><td class="numeric">{{ numberText(row.close, 3) }}</td><td class="numeric">{{ currency(row.value) }}</td><td class="numeric">{{ weight(row.weight) }}</td></tr></tbody>
            </table>
          </div>
          <div v-else class="compact-empty">暂无模拟持仓记录</div>
          <p class="panel-footnote">缺失价格、市值或权重均显示 —；不将缺失报价视为零。</p>
        </article>

        <article class="panel plan-panel" aria-labelledby="plan-title">
          <div class="panel-heading"><h4 id="plan-title">{{ phase === 'forward' ? '调仓节奏与待执行计划' : '回放调仓记录与计划' }}</h4><span class="paper-label">仅模拟</span></div>
          <div class="schedule-grid">
            <div><span>最近信号日</span><strong>{{ latestRun?.signal_date || '—' }}</strong><span class="run-status" :class="`is-${statusTone(latestRun?.status)}`"><StatusIcon :status="latestRun?.status" />{{ statusLabel(latestRun?.status) }}</span><small>模拟执行日 {{ latestRun?.execute_date || '—' }}</small></div>
            <div><span>{{ snapshot.pending ? '待执行计划信号日' : phase === 'forward' ? '下一调仓信号日' : '回放下一信号日（非前向）' }}</span><strong>{{ snapshot.next_rebalance_date || '—' }}</strong><small>预计模拟执行 {{ snapshot.next_execute_date || '—' }}</small></div>
          </div>
          <div v-if="snapshot.pending" class="pending-plan">
            <div class="pending-heading"><h5>待执行目标</h5><span class="run-status" :class="`is-${statusTone(snapshot.pending.status)}`"><StatusIcon :status="snapshot.pending.status" />{{ statusLabel(snapshot.pending.status) }}</span></div>
            <p class="secondary">信号 {{ snapshot.pending.signal_date || '—' }} · 模拟执行 {{ snapshot.pending.execute_date || '—' }}</p>
            <p v-if="runReason(snapshot.pending)" class="reason">{{ runReason(snapshot.pending) }}</p>
            <div v-if="picks(snapshot.pending).length" class="table-scroll" tabindex="0" aria-label="待模拟执行的目标持仓">
              <table><thead><tr><th scope="col">目标标的</th><th scope="col" class="numeric">动量</th><th scope="col" class="numeric">目标权重</th></tr></thead><tbody><tr v-for="(pick, i) in picks(snapshot.pending)" :key="pick.code || i"><th scope="row">{{ pick.name || '—' }}<small class="code">{{ pick.code || '—' }}</small></th><td class="numeric">{{ percent(pick.score) }}</td><td class="numeric">{{ weight(pick.weight) }}</td></tr></tbody></table>
            </div>
            <p v-else class="compact-empty">计划尚无目标明细</p>
          </div>
          <p v-else class="compact-empty">暂无待模拟执行计划</p>
        </article>
      </div>

      <article class="panel" aria-labelledby="runs-title">
        <div class="panel-heading"><h4 id="runs-title">最近模拟调仓</h4><span class="secondary">{{ phaseLabel(phase) }} · 只读</span></div>
        <div v-if="runs.length" class="table-scroll" tabindex="0" aria-label="最近模拟调仓记录，可横向滚动">
          <table class="runs-table">
            <thead><tr><th scope="col">信号日</th><th scope="col">模拟执行日</th><th scope="col">状态</th><th scope="col" class="numeric">可用池</th><th scope="col" class="numeric">换手率</th><th scope="col" class="numeric">调仓前资产</th><th scope="col" class="numeric">调仓后资产</th><th scope="col">明细</th></tr></thead>
            <tbody>
              <template v-for="(run, i) in runs" :key="runKey(run, i)">
                <tr><th scope="row">{{ run.signal_date || '—' }}</th><td>{{ run.execute_date || '—' }}</td><td><span class="run-status" :class="`is-${statusTone(run.status)}`"><StatusIcon :status="run.status" />{{ statusLabel(run.status) }}</span></td><td class="numeric">{{ numberText(run.universe) }}</td><td class="numeric">{{ percent(run.turnover) }}</td><td class="numeric">{{ currency(run.nav_before) }}</td><td class="numeric">{{ currency(run.nav_after) }}</td><td><button class="detail-toggle" :aria-label="`${run.signal_date || '该次'}模拟调仓明细`" :aria-expanded="expandedRun === runKey(run, i)" :aria-controls="`rotation-run-${i}`" @click="toggleRun(run, i)">{{ expandedRun === runKey(run, i) ? '收起' : '查看' }}</button></td></tr>
                <tr v-if="expandedRun === runKey(run, i)" :id="`rotation-run-${i}`" class="detail-row"><td colspan="8">
                  <p v-if="runReason(run)" class="reason">记录说明：{{ runReason(run) }}</p>
                  <div class="run-detail-grid">
                    <div><h5>信号目标 · 仅模拟</h5><table><thead><tr><th scope="col">标的</th><th scope="col" class="numeric">动量</th><th scope="col" class="numeric">目标权重</th></tr></thead><tbody><tr v-for="(pick, j) in picks(run)" :key="pick.code || j"><th scope="row">{{ pick.name || '—' }}<small class="code">{{ pick.code || '—' }}</small></th><td class="numeric">{{ percent(pick.score) }}</td><td class="numeric">{{ weight(pick.weight) }}</td></tr></tbody></table><p v-if="!picks(run).length" class="compact-empty">无目标明细</p></div>
                    <div><h5>模拟成交</h5><table><thead><tr><th scope="col">代码 / 方向</th><th scope="col" class="numeric">份额</th><th scope="col" class="numeric">价格</th><th scope="col" class="numeric">费用（元）</th></tr></thead><tbody><tr v-for="(fill, j) in fills(run)" :key="j"><th scope="row">{{ fill.code || '—' }}<small class="code">{{ sideLabel(fill.side) }}</small></th><td class="numeric">{{ numberText(fill.qty) }}</td><td class="numeric">{{ numberText(fill.price, 3) }}</td><td class="numeric">{{ currency(fill.fee) }}</td></tr></tbody></table><p v-if="!fills(run).length" class="compact-empty">暂无模拟成交；不等同于已执行</p></div>
                  </div>
                </td></tr>
              </template>
            </tbody>
          </table>
        </div>
        <div v-else class="compact-empty">暂无{{ phase === 'forward' ? '前向' : '回放' }}模拟调仓记录</div>
      </article>

      <footer class="parameters">
        <span>规则版本 {{ snapshot.rule_version || '—' }}</span><span>手续费率 {{ percent(snapshot.parameters?.commission_rate) }}</span><span>滑点率 {{ percent(snapshot.parameters?.slippage_rate) }}</span><span>20 日均成交额门槛 {{ currency(snapshot.parameters?.minimum_amount) }}</span>
      </footer>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NEmpty, NSpin } from 'naive-ui'
import { api } from '../../api/client'
import {
  FORWARD_START, INITIAL_CAPITAL, currency, dayLabel, indexedSeries, indexText, isRotationEmpty,
  navScale, normalizeRotation, numberText, percent, phaseLabel, recentRuns, runReason, sideLabel,
  statusLabel, statusTone, weight,
  type RotationPhase, type RotationResponse, type RotationRun, type RotationSnapshot,
} from './rotationModel'

const LINES = [
  { key: 'c13', name: 'C13 轮动', short: 'C13', color: '--series-1', dash: '', pattern: '实线' },
  { key: 'ew', name: 'EW-20d 等权', short: 'EW-20d', color: '--series-2', dash: '8 5', pattern: '长虚线' },
  { key: 'benchmark', name: '510300', short: '510300', color: '--series-3', dash: '2 5', pattern: '点线' },
] as const

const StatusIcon = (props: { status?: string | null }) => h('svg', {
  width: 15, height: 15, viewBox: '0 0 16 16', fill: 'none', stroke: 'currentColor',
  'stroke-width': 1.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
}, [h('circle', { cx: 8, cy: 8, r: 6.2 }), h('path', {
  d: props.status === 'executed' ? 'M4.5 8l2.3 2.3 4.7-4.7' : props.status === 'pending' ? 'M8 4.5v3.7l2.4 1.4' : 'M8 4.5v4M8 11v.1',
})])

const phase = ref<RotationPhase>('forward')
const theme = ref<'light' | 'dark'>(window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
const view = ref<'chart' | 'table'>('chart')
const cache = ref<Partial<Record<RotationPhase, RotationSnapshot>>>({})
const snapshot = computed(() => cache.value[phase.value] || null)
const loading = ref(false), error = ref('')
let request = 0
let controller: AbortController | null = null
const expandedRun = ref('')

async function load() {
  const id = ++request
  const targetPhase = phase.value
  controller?.abort()
  const current = new AbortController()
  controller = current
  loading.value = true
  error.value = ''
  try {
    const payload = await api<RotationResponse | null>(`/signals/rotation-shadow?phase=${targetPhase}`, { signal: current.signal })
    if (id !== request) return
    cache.value = { ...cache.value, [targetPhase]: normalizeRotation(payload, targetPhase) }
  } catch (e) {
    if (id === request && !current.signal.aborted) error.value = e instanceof Error ? e.message : '读取记录时发生错误'
  } finally {
    if (id === request) loading.value = false
  }
}
watch(phase, () => { activeIndex.value = null; expandedRun.value = ''; load() })
onMounted(load)
onBeforeUnmount(() => { request++; controller?.abort(); observer?.disconnect() })

const forwardStart = computed(() => snapshot.value?.forward_start || FORWARD_START)
const initialCapital = computed(() => snapshot.value?.parameters?.initial ?? INITIAL_CAPITAL)
const periodText = computed(() => `基线 ${snapshot.value?.start || '—'} → 最近估值 ${snapshot.value?.last_date || '—'}`)
const kpis = computed(() => [
  { label: '模拟组合资产', value: currency(snapshot.value?.nav), note: `初始资金 ${currency(initialCapital.value)}` },
  { label: phase.value === 'forward' ? '前向累计收益' : '回放累计收益', value: percent(snapshot.value?.total_return ?? snapshot.value?.metrics?.total_return), note: '不与另一观察区间拼接' },
  { label: '可用模拟现金', value: currency(snapshot.value?.cash), note: `估值日 ${snapshot.value?.last_date || '—'}` },
  { label: dayLabel(phase.value), value: numberText(snapshot.value?.days), note: phase.value === 'forward' ? `观察起点 ${forwardStart.value} · 不含基线` : '仅历史区间，不计入前向' },
])
const qualityGood = computed(() => ['ok', 'ready', 'complete'].includes(snapshot.value?.quality?.status || ''))
const comparisons = computed(() => [
  { name: 'C13 轮动', total: snapshot.value?.total_return ?? snapshot.value?.metrics?.total_return, metrics: snapshot.value?.metrics },
  { name: 'EW-20d 等权', total: snapshot.value?.ew?.metrics?.total_return, metrics: snapshot.value?.ew?.metrics },
  { name: '510300', total: snapshot.value?.benchmark?.metrics?.total_return, metrics: snapshot.value?.benchmark?.metrics },
])
const runs = computed(() => recentRuns(snapshot.value))
const latestRun = computed(() => snapshot.value?.last_run || runs.value[0] || null)
const picks = (run: RotationRun) => Array.isArray(run.selected) ? run.selected : []
const fills = (run: RotationRun) => Array.isArray(run.fills) ? run.fills : []
const runKey = (run: RotationRun, i: number) => run.id != null ? `id-${run.id}` : `${run.signal_date || 'unknown'}-${i}`
function toggleRun(run: RotationRun, i: number) { const key = runKey(run, i); expandedRun.value = expandedRun.value === key ? '' : key }

const points = computed(() => indexedSeries(snapshot.value?.series, initialCapital.value))
const tablePoints = computed(() => [...points.value].reverse())
const rawNav = computed(() => new Map((snapshot.value?.series || []).map((row) => [row.date, row.nav])))
const hasPlot = computed(() => points.value.some((p) => p.c13 != null || p.ew != null || p.benchmark != null))
const scale = computed(() => navScale(points.value))
const chartElement = ref<HTMLDivElement | null>(null)
const chartWidth = ref(800)
const chartHeight = computed(() => chartWidth.value < 520 ? 260 : 300)
const plot = computed(() => ({ left: 54, right: chartWidth.value - 76, top: 18, bottom: chartHeight.value - 38 }))
let observer: ResizeObserver | null = null
watch(chartElement, (el) => {
  observer?.disconnect()
  if (!el) return
  chartWidth.value = Math.max(280, Math.round(el.getBoundingClientRect().width))
  observer = new ResizeObserver(([entry]) => { if (entry) chartWidth.value = Math.max(280, Math.round(entry.contentRect.width)) })
  observer.observe(el)
}, { flush: 'post' })
function x(index: number) { return plot.value.left + (plot.value.right - plot.value.left) * (points.value.length > 1 ? index / (points.value.length - 1) : 0.5) }
function y(value: number) { return plot.value.bottom - (value - scale.value.min) / (scale.value.max - scale.value.min) * (plot.value.bottom - plot.value.top) }
const plottedLines = computed(() => LINES.map((line) => {
  let drawing = false
  const path = points.value.map((point, i) => {
    const value = point[line.key]
    if (value == null) { drawing = false; return '' }
    const command = `${drawing ? 'L' : 'M'} ${x(i)} ${y(value)}`
    drawing = true
    return command
  }).join(' ')
  const isolated = points.value.flatMap((point, i, rows) => point[line.key] != null &&
    rows[i - 1]?.[line.key] == null && rows[i + 1]?.[line.key] == null
    ? [{ index: i, x: x(i), y: y(point[line.key]!) }] : [])
  return { ...line, path, isolated }
}))
const endLabels = computed(() => {
  const ends = LINES.flatMap((line) => {
    for (let i = points.value.length - 1; i >= 0; i--) {
      const value = points.value[i][line.key]
      if (value != null) return [{ ...line, x: x(i), y: y(value), labelY: y(value) }]
    }
    return []
  }).sort((a, b) => a.y - b.y)
  const min = plot.value.top + 8, max = plot.value.bottom - 8, gap = 20
  ends.forEach((end, i) => { end.labelY = Math.max(min, end.y, i ? ends[i - 1].labelY + gap : min) })
  if (ends.length && ends[ends.length - 1].labelY > max) {
    ends[ends.length - 1].labelY = max
    for (let i = ends.length - 2; i >= 0; i--) ends[i].labelY = Math.min(ends[i].labelY, ends[i + 1].labelY - gap)
  }
  return ends
})
const dateTicks = computed(() => {
  const count = Math.min(points.value.length, chartWidth.value < 520 ? 3 : 5)
  return [...new Set(Array.from({ length: count }, (_, i) => count === 1 ? 0 : Math.round(i * (points.value.length - 1) / (count - 1))))].map((index, i, all) => ({
    index, x: x(index), label: chartWidth.value < 720 ? points.value[index].date.slice(5) : points.value[index].date,
    anchor: all.length === 1 ? 'middle' : i === 0 ? 'start' : i === all.length - 1 ? 'end' : 'middle',
  }))
})
const activeIndex = ref<number | null>(null)
const pointerY = ref(0)
const chartFocused = ref(false)
const activePoint = computed(() => activeIndex.value == null ? null : points.value[activeIndex.value] || null)
const activeDescription = computed(() => {
  const point = activePoint.value || points.value[points.value.length - 1]
  return point ? `${point.date}，${LINES.map((line) => `${line.short} ${indexText(point[line.key])}`).join('，')}` : '暂无净值'
})
const tooltipPosition = computed(() => ({
  left: `${Math.max(8, Math.min(chartWidth.value - 214, x(activeIndex.value ?? 0) + 16))}px`,
  top: `${Math.max(8, Math.min(chartHeight.value - 158, pointerY.value + 14))}px`,
}))
function pointAt(event: PointerEvent) {
  const bounds = chartElement.value?.getBoundingClientRect()
  if (!bounds || !points.value.length) return
  const px = (event.clientX - bounds.left) * chartWidth.value / bounds.width
  pointerY.value = event.clientY - bounds.top
  activeIndex.value = points.value.length === 1 ? 0 : Math.max(0, Math.min(points.value.length - 1, Math.round((px - plot.value.left) / (plot.value.right - plot.value.left) * (points.value.length - 1))))
}
function focusChart() { chartFocused.value = true; activeIndex.value ??= points.value.length - 1; pointerY.value = 0 }
function blurChart() { chartFocused.value = false; activeIndex.value = null }
function leaveChart(event: PointerEvent) { if (!chartFocused.value && event.pointerType !== 'touch') activeIndex.value = null }
function chartKey(event: KeyboardEvent) {
  const last = points.value.length - 1
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return
  event.preventDefault()
  if (event.key === 'Escape') { activeIndex.value = null; return }
  if (event.key === 'Home') activeIndex.value = 0
  else if (event.key === 'End') activeIndex.value = last
  else activeIndex.value = Math.max(0, Math.min(last, (activeIndex.value ?? last) + (event.key === 'ArrowLeft' ? -1 : 1)))
  pointerY.value = 0
}
watch(points, () => { activeIndex.value = null })
</script>

<style scoped>
.rotation-shadow {
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7; --ink: #0b0b0b; --secondary: #52514e;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11, 11, 11, .1);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --good: #0ca30c; --critical: #d03b3b;
  display: grid; gap: 16px; min-width: 0; color: var(--ink); background: var(--plane);
  padding: 20px; border: 1px solid var(--border); border-radius: 14px;
}
.rotation-shadow[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --plane: #0d0d0d; --ink: #f0efec; --secondary: #c3c2b7;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255, 255, 255, .1);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --good: #0ca30c; --critical: #d03b3b;
}
* { box-sizing: border-box; }
h3, h4, h5, p, dl, dd { margin: 0; }
h3 { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 24px; line-height: 1.5; letter-spacing: -.5px; }
h4 { font-size: 15px; font-weight: 650; }
h5 { font-size: 13px; font-weight: 650; }
button { font: inherit; cursor: pointer; }
button:disabled { opacity: .55; cursor: wait; }
button:focus-visible, .table-scroll:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }
.page-heading, .panel-heading, .toolbar, .pending-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-heading p { color: var(--secondary); margin-top: 5px; font-size: 13px; line-height: 1.7; }
.eyebrow { color: var(--secondary); font-size: 11px; font-weight: 600; letter-spacing: .07em; margin-bottom: 3px; }
.paper-label { display: inline-flex; align-items: center; color: var(--secondary); border: 1px solid var(--border); border-radius: 5px; padding: 3px 7px; font-size: 11px; font-weight: 500; letter-spacing: 0; white-space: nowrap; }
.control { border: 1px solid var(--border); border-radius: 7px; background: var(--surface); color: var(--ink); min-height: 40px; padding: 7px 12px; white-space: nowrap; font-size: 12px; }
.control:hover:not(:disabled) { border-color: var(--secondary); }
.segmented { display: inline-flex; padding: 3px; gap: 3px; border: 1px solid var(--border); background: var(--surface); border-radius: 9px; }
.segmented button { min-height: 38px; padding: 7px 16px; border: 0; background: transparent; color: var(--secondary); border-radius: 6px; font-size: 13px; white-space: nowrap; }
.segmented button[aria-pressed="true"] { background: var(--ink); color: var(--surface); font-weight: 600; }
.segmented.small { padding: 2px; border-radius: 7px; }
.segmented.small button { min-height: 34px; padding: 5px 12px; font-size: 12px; }
.phase-note, .notice { display: flex; gap: 10px; align-items: flex-start; padding: 13px 15px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); font-size: 12px; }
.phase-note > svg, .notice > svg { flex: 0 0 15px; margin-top: 2px; }
.phase-note strong, .notice strong { font-weight: 600; }
.phase-note p, .notice p { margin-top: 4px; line-height: 1.7; color: var(--secondary); overflow-wrap: anywhere; }
.replay-note { border-left: 3px solid var(--secondary); }
.error-notice { border-color: var(--critical); }
.error-notice > svg { color: var(--critical); }
.error-notice > div { flex: 1; min-width: 0; }
.page-content { display: grid; gap: 16px; min-width: 0; transition: opacity .15s; }
.refreshing { opacity: .6; }
.refresh-note, .secondary, .parameters, .panel-footnote, .chart-note { color: var(--secondary); font-size: 12px; line-height: 1.7; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; min-width: 0; overflow: hidden; }
.initial-state { min-height: 220px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 13px; }
.initial-state span { color: var(--secondary); font-size: 12px; }
.empty-panel { padding: 24px 12px; }
:deep(.n-empty__description), :deep(.n-empty__extra) { color: var(--secondary); }
.panel-heading { padding: 15px 17px 11px; flex-wrap: wrap; }
.panel-heading p { margin-top: 3px; color: var(--secondary); font-size: 11px; }
.kpi-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }
.kpi { min-width: 0; padding: 16px 18px; }
.kpi + .kpi { border-left: 1px solid var(--border); }
.kpi dt { color: var(--secondary); font-size: 12px; }
.kpi dd { font-size: clamp(19px, 2.1vw, 28px); font-weight: 600; letter-spacing: -.6px; line-height: 1.5; margin: 4px 0; overflow-wrap: anywhere; }
.kpi small { display: block; color: var(--secondary); font-size: 11px; line-height: 1.6; }
.quality-code { margin-left: 8px; color: var(--secondary); }
.chart-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 11px 22px; list-style: none; padding: 0 17px; margin: 2px 0 6px; }
.chart-legend li { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; }
.chart-legend small { color: var(--secondary); font-size: 10px; }
.chart-note { font-size: 11px; padding: 0 17px; }
.chart-wrap { position: relative; width: 100%; min-width: 0; }
.nav-chart { display: block; width: 100%; overflow: visible; }
.grid-line { stroke: var(--grid); stroke-width: 1; }
.axis-line { stroke: var(--axis); stroke-width: 1; }
.axis-text { fill: var(--secondary); font-size: 10px; font-variant-numeric: tabular-nums; }
.chart-line { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.data-dot { stroke: var(--surface); stroke-width: 2; }
.label-leader { fill: none; stroke: var(--axis); stroke-width: 1; }
.end-label { fill: var(--ink); font-size: 11px; font-weight: 600; }
.crosshair { stroke: var(--secondary); stroke-width: 1; opacity: .6; }
.chart-hit:focus-visible { outline: 1px solid var(--secondary); outline-offset: 3px; }
.chart-tooltip { position: absolute; width: 206px; max-width: calc(100% - 16px); padding: 12px; background: var(--surface); color: var(--ink); border: 1px solid var(--axis); border-radius: 8px; box-shadow: 0 5px 18px #00000012; pointer-events: none; font-size: 12px; z-index: 1; }
.chart-tooltip time { display: block; color: var(--secondary); margin-bottom: 7px; font-size: 11px; }
.tooltip-row { display: grid; grid-template-columns: 22px 1fr auto; align-items: center; gap: 6px; padding: 3px 0; }
.tooltip-row span { color: var(--secondary); font-size: 11px; }
.chart-footer { padding-bottom: 13px; }
.chart-empty { padding: 48px 16px; min-height: 230px; }
.table-scroll { width: 100%; overflow-x: auto; scrollbar-width: thin; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; white-space: nowrap; }
th, td { padding: 11px 16px; border-bottom: 1px solid var(--border); }
thead th { color: var(--secondary); font-size: 11px; font-weight: 500; background: var(--plane); }
tbody th { font-weight: 500; }
tbody tr:last-child > th, tbody tr:last-child > td { border-bottom: 0; }
.numeric { text-align: right; font-variant-numeric: tabular-nums; }
.nav-table { max-height: 390px; }
.nav-table thead { position: sticky; top: 0; }
.print-only { display: none; }
.portfolio-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.code { display: block; color: var(--secondary); margin-top: 3px; font-size: 11px; font-weight: 400; }
.panel-footnote { padding: 10px 16px 13px; font-size: 11px; }
.compact-empty, .table-empty { color: var(--secondary); padding: 25px 16px; text-align: center; font-size: 12px; }
.schedule-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; padding: 3px 17px 16px; }
.schedule-grid > div { display: flex; align-items: flex-start; flex-direction: column; gap: 6px; }
.schedule-grid > div > span:first-child, .schedule-grid small { font-size: 11px; color: var(--secondary); }
.schedule-grid strong { font-size: 17px; font-weight: 600; }
.run-status { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--secondary); white-space: nowrap; }
.run-status.is-executed > svg { color: var(--good); }
.run-status.is-blocked > svg { color: var(--critical); }
.pending-plan { border-top: 1px solid var(--border); }
.pending-heading { padding: 13px 17px 5px; }
.pending-plan > p { padding: 0 17px 10px; }
.reason { font-size: 12px; line-height: 1.7; white-space: normal; overflow-wrap: anywhere; padding-bottom: 10px; }
.detail-toggle { min-width: 44px; min-height: 36px; padding: 4px 7px; background: transparent; border: 1px solid var(--border); color: var(--ink); border-radius: 5px; font-size: 11px; }
.detail-row > td { background: var(--plane); padding: 16px; }
.run-detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); gap: 20px; }
.run-detail-grid h5 { margin-bottom: 9px; }
.run-detail-grid table { background: var(--surface); }
.run-detail-grid th, .run-detail-grid td { padding: 9px 12px; }
.parameters { display: flex; flex-wrap: wrap; gap: 5px 22px; padding: 0 2px; font-size: 11px; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 1100px) { .portfolio-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 760px) {
  .rotation-shadow { padding: 14px; gap: 13px; }
  .kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi { padding: 13px; }
  .kpi:nth-child(3) { border-left: 0; }
  .kpi:nth-child(n+3) { border-top: 1px solid var(--border); }
  .kpi dd { font-size: 22px; }
  .run-detail-grid { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .rotation-shadow { padding: 12px; border: 0; border-radius: 8px; background: var(--plane); gap: 12px; }
  .page-heading { align-items: flex-start; }
  h3 { font-size: 22px; gap: 7px; }
  .page-heading p { font-size: 12px; max-width: 230px; }
  .eyebrow { font-size: 10px; }
  .page-heading > .control { padding: 5px 9px; margin-top: 8px; }
  .toolbar { flex-wrap: wrap; gap: 8px; }
  .segmented { flex: 1; }
  .segmented button { flex: 1; min-height: 40px; padding: 7px 9px; font-size: 12px; }
  .theme-toggle { min-height: 36px; padding: 5px 9px; font-size: 11px; margin-left: auto; }
  .control, .detail-toggle { min-height: 44px; }
  .phase-note, .notice { padding: 11px; font-size: 11px; }
  .panel-heading { padding: 13px 12px 10px; gap: 8px; }
  .panel-heading p { font-size: 10px; }
  .panel-heading .segmented { flex: 0 0 auto; }
  .segmented.small button { min-height: 40px; padding: 5px 10px; }
  .chart-legend { gap: 8px 12px; padding: 0 12px; }
  .chart-legend li { gap: 4px; font-size: 10px; }
  .chart-legend svg { width: 22px; }
  .chart-legend small { display: none; }
  .chart-note { padding-left: 12px; padding-right: 12px; font-size: 10px; }
  .kpi { padding: 12px 10px; }
  .kpi dt { font-size: 11px; }
  .kpi dd { font-size: clamp(17px, 5.3vw, 23px); }
  .kpi small { font-size: 10px; }
  .schedule-grid { padding: 3px 12px 14px; gap: 10px; }
  .schedule-grid strong { font-size: 15px; }
  .parameters { gap: 4px 14px; font-size: 10px; padding-bottom: 5px; }
  .error-notice { flex-wrap: wrap; }
}
@media (prefers-reduced-motion: reduce) { .page-content { transition: none; } }
@media (forced-colors: active) {
  .rotation-shadow[data-theme] { --surface: Canvas; --plane: Canvas; --ink: CanvasText; --secondary: CanvasText; --grid: GrayText; --axis: CanvasText; --border: GrayText; --series-1: CanvasText; --series-2: CanvasText; --series-3: CanvasText; }
  .chart-line, .chart-legend line, .tooltip-row line { stroke: CanvasText; }
  .data-dot { fill: CanvasText !important; stroke: Canvas; }
}
@media print {
  .rotation-shadow[data-theme] { --surface: #ffffff; --plane: #ffffff; --ink: #0b0b0b; --secondary: #52514e; --grid: #e1e0d9; --axis: #c3c2b7; --border: #c3c2b7; border: 0; padding: 0; }
  .toolbar, .page-heading > button, .panel-heading .segmented, .detail-toggle, .chart-footer { display: none; }
  .print-only { display: block; }
  .table-scroll { max-height: none; overflow: visible; }
  .portfolio-grid { display: block; }
  .panel { break-inside: avoid; margin-bottom: 12px; }
  .chart-line { stroke: #0b0b0b !important; }
}
</style>
