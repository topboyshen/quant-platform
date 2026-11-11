<template>
  <section class="research-evidence" :class="{ dark }" data-testid="unified-research-evidence">
    <header class="hub-head">
      <div><p class="eyebrow">统一研究 · {{ section === 'operations' ? '项目管理与运行证据' : '只读证据' }}</p><h1>{{ sectionCopy.title }}</h1><p>{{ sectionCopy.description }}</p></div>
      <button type="button" :disabled="loading || !accountId" data-testid="hub-refresh" @click="load">{{ loading ? '读取中…' : '刷新来源' }}</button>
    </header>
    <div class="source-statuses" aria-live="polite">
      <p v-for="(slot, key) in slots" :key="key" :class="{ failure: slot.status === 'failed' }" :data-testid="'hub-source-' + key">
        {{ sourceNames[key] }}：{{ slot.status === 'loading' ? '读取中' : slot.status === 'ready' ? '已读取（不代表业务成功）' : slot.error || '未读取' }}
      </p>
    </div>
    <p v-if="permission" class="failure" role="alert" data-testid="hub-permission">{{ permission }}</p>
    <p v-if="context.projectId" class="boundary" data-testid="hub-research-context">当前研究上下文：项目 {{ context.projectId }} · 分支 {{ context.branchId || '未知' }} · 运行 {{ context.runId || '尚未选择' }}。<router-link :to="{ path: '/research-lab', query: { project: context.projectId, branch: context.branchId, module: section === 'quality' ? 'backtest' : 'ai' } }">返回这条研究分支</router-link><span v-if="section !== 'quality'"> 下方统计与外部任务仍是各来源全局范围，不冒充本分支成绩。</span></p>
    <p v-if="nng8 && nng8.snapshot_stale" class="failure" role="status">nnG8 来源副本陈旧；各项数量与运行状态只能作为捕获时证据，不代表当前状态。</p>

    <template v-if="section === 'quality'">
      <research-run-quality />
      <article class="evidence-card" data-testid="hub-global-quality">
        <h2>nnG8 全局数据摘要</h2>
        <p>本区不是所选标的或回测的数据证明。覆盖范围、日期与契约必须一起阅读。</p>
        <template v-if="nng8">
          <p v-if="nng8.snapshot_stale" class="failure">来源副本已陈旧，不能当作当前行情。</p>
          <dl class="facts">
            <div><dt>来源状态</dt><dd>{{ status(nng8.data_quality.status).label }}</dd></div>
            <div><dt>行情日期</dt><dd>{{ value(nng8.data_quality.market_date) }}</dd></div>
            <div><dt>范围</dt><dd>{{ value(nng8.data_quality.counts_scope) }}</dd></div>
            <div><dt>计数日期</dt><dd>{{ value(nng8.data_quality.counts_target_date) }}</dd></div>
            <div><dt>计数契约</dt><dd>{{ value(nng8.data_quality.counts_contract) }}</dd></div>
            <div><dt>完整快照</dt><dd>{{ nng8.data_quality.full_snapshot === null ? '未知' : nng8.data_quality.full_snapshot ? '是' : '否' }}</dd></div>
            <div><dt>符合条件标的</dt><dd>{{ value(nng8.data_quality.eligible) }}</dd></div>
            <div><dt>有效标的</dt><dd>{{ value(nng8.data_quality.valid) }}</dd></div>
            <div><dt>无效标的</dt><dd>{{ value(nng8.data_quality.invalid) }}</dd></div>
          </dl>
          <p v-if="nng8.data_quality.counts_scope === 'sealed_core_qlib_summary'">封存的 QLib 核心摘要，不是完整全市场价格档案。</p>
          <p v-for="(line, index) in nng8.data_quality.limitations" :key="index">{{ line }}</p>
          <p>历史问题日：{{ nng8.data_quality.historical_issue_dates.join('、') || '来源未提供' }}。未列出不等于没有缺口。</p>
        </template>
        <p v-else>来源尚未取得；不补零，不将同步时间当作行情日期。</p>
      </article>
    </template>

    <template v-if="section === 'validation'">
      <research-validation-progress />
      <p class="boundary" data-testid="hub-validation-boundary">Ledger 与 Phase2 可能重叠；不相加，也不计入 ETF 或 Profit 样本。可读取、任务成功和策略有效性是不同结论。</p>
      <div class="validation-grid">
        <article v-for="name in ['ledger', 'phase2']" :key="name" class="evidence-card" :data-testid="'hub-' + name">
          <h2>{{ name === 'ledger' ? 'Ledger · 预测账本' : 'Phase2 · 冻结校准' }}</h2>
          <template v-if="validation[name]">
            <p>{{ status(validation[name].status).label }} · 捕获运行模式 {{ value(validation[name].runtime_mode) }}</p>
            <dl class="facts">
              <div><dt>{{ name === 'ledger' ? '已到期预测' : '名义预测' }}</dt><dd>{{ value(validation[name].nominal) }}</dd></div>
              <div><dt>已观察</dt><dd>{{ value(validation[name].observed) }}</dd></div>
              <div><dt>待复核</dt><dd>{{ value(validation[name].pending_review) }}</dd></div>
              <div><dt>正式样本</dt><dd>{{ value(validation[name].formal) }}</dd></div>
            </dl>
            <p v-if="name === 'ledger'">待复核包含在已观察中，数字不能相加。</p>
            <p v-for="(line, index) in validation[name].limitations" :key="index">{{ line }}</p>
          </template>
          <p v-else>该路来源不可用，样本与运行模式未知。</p>
          <p>业务截止：未知（当前来源未提供）；包采集时间不能替代。</p>
        </article>
        <article class="evidence-card" data-testid="hub-etf">
          <h2>ETF · 固定规则验证</h2><p>{{ validation.etf.label }}</p>
          <dl class="facts">
            <div><dt>成熟样本 / 门槛</dt><dd>{{ validation.etf.sampleSize }} / {{ validation.etf.minimumSamples }}</dd></div>
            <div><dt>主要持有期</dt><dd>{{ value(validation.etf.primaryHorizon) }} 个交易日</dd></div>
          </dl>
          <dl v-if="validation.etf.detailsAvailable" class="facts">
            <div><dt>观察记录</dt><dd>{{ validation.etf.progress.eligible }}</dd></div>
            <div><dt>待完成</dt><dd>{{ validation.etf.progress.awaiting }}</dd></div>
            <div><dt>未计入</dt><dd>{{ validation.etf.progress.excluded }}</dd></div>
            <div><dt>已保存结果截至</dt><dd>{{ value(validation.etf.progress.latestEvaluation) }}</dd></div>
          </dl>
          <p v-else>{{ validation.etf.detailMessage }}</p>
          <p>计数单位为符合规则的决策记录，不是股票数、回测次数或日历天数。</p>
          <router-link to="/personal-home?section=market">查看 ETF 信号与判定细节</router-link>
        </article>
        <article class="evidence-card" data-testid="hub-profit">
          <h2>Profit · 股票纸面研究</h2><p>模拟跟踪 ≠ 账户收益。报告状态不能替代组合收益或盈利优势验证。</p>
          <p v-if="validation.profitSource" data-testid="hub-profit-source" :class="{ failure: validation.profitSource.status !== 'ok' }">来源状态 {{ validation.profitSource.status }}：{{ validation.profitSource.detail || '未提供说明' }}。子文件可读不代表同步或业务已成功。</p>
          <div v-for="report in validation.profit" :key="report.id" class="profit-file">
            <h3>{{ report.title }}</h3><p>{{ report.conclusion }}</p>
            <p>生成 {{ report.generatedAt }} · 行情截至 {{ report.marketCutoff }}</p><p>{{ report.statusMeta.label }}（报告可读性）</p>
          </div>
          <p v-if="!validation.profit.length">报告来源不可用；不推测收益与结论。</p>
          <p>组合风险和新样本进度见上方；现金组合收益只有完整成交、持仓及公司行动证据齐备后才可评估，不用 ETF 或逐条收益代替。</p>
          <router-link to="/personal-reports">查看 Profit 报告与证据</router-link>
        </article>
      </div>
    </template>

    <template v-if="section === 'operations'">
      <external-task-operations />
      <research-task-operations />
      <details class="source-details"><summary>采集副本中的运行证据（技术对照）</summary>
        <p>副本时间不代表此刻状态；实时任务管理以上方服务端读取结果为准。</p>
        <p v-if="!operations.length">尚未取得副本证据。</p>
        <div class="operations-grid">
          <article v-for="task in operations" :key="task.key" class="evidence-card" data-testid="hub-task">
            <h2>{{ task.label }}</h2><p>{{ task.source }} · <span :class="'state-' + task.state.tone">{{ task.state.label }}</span></p>
            <dl class="facts">
              <div><dt>上次运行时间</dt><dd>{{ value(task.lastRun) }}</dd></div>
              <div><dt>下次计划时间</dt><dd>{{ value(task.nextRun) }}</dd></div>
              <div><dt>该项证据截止</dt><dd>{{ value(task.cutoff) }}</dd></div>
            </dl>
            <p>{{ task.detail }}</p>
          </article>
        </div>
      </details>
    </template>
    <details v-if="nng8" class="source-details"><summary>来源与时间边界</summary>
      <p>nnG8 包采集：{{ value(nng8.captured_at) }}；读取：{{ value(nng8.observed_at) }}。同步操作时间与这些时间均不能替代报告生成或行情截止。</p>
      <p>{{ sync.message }} {{ sync.reason }}</p>
      <p v-for="(issue, index) in nng8.issues" :key="'issue-' + index">{{ issue.source }}：{{ issue.message }}</p>
    </details>
  </section>
</template>

<script>
import { getNng8Readonly, getWorkbenchOverview, getRadarMarketContext } from '@/api/unifiedResearch'
import ResearchRunQuality from '@/components/ResearchRunQuality'
import ResearchValidationProgress from '@/components/ResearchValidationProgress'
import ResearchTaskOperations from '@/components/ResearchTaskOperations'
import ExternalTaskOperations from '@/components/ExternalTaskOperations'
import { NNG8_READONLY_ENABLED, normalizeNng8, nng8AccountId, nng8Value, nng8Status, nng8SyncPresentation } from '@/utils/nng8Readonly'
import { RESEARCH_SECTIONS, validationView, operationalView, sourceFailure } from '@/utils/unifiedResearch'

export default {
  name: 'UnifiedResearchEvidence',
  components: { ResearchRunQuality, ResearchValidationProgress, ResearchTaskOperations, ExternalTaskOperations },
  props: { section: { type: String, required: true } },
  data () { return { slots: {}, loading: false, permission: '', seq: 0, active: true, unsubscribe: null } },
  computed: {
    accountId () { return nng8AccountId(this.$store && this.$store.getters.userInfo) },
    context () { const q = this.$route && this.$route.query || {}; return { projectId: q.projectId || null, branchId: q.branchId || null, runId: q.runId || null } },
    dark () { return ['dark', 'realdark'].includes(this.$store.state.app.theme) },
    sectionCopy () { return RESEARCH_SECTIONS[this.section] || RESEARCH_SECTIONS.quality },
    sourceNames () { return { nng8: 'nnG8 外部副本', overview: 'Quant-Server 工作台', radar: 'ETF 验证记录' } },
    nng8 () { return this.ready('nng8') },
    overview () { return this.ready('overview') },
    radar () { return this.ready('radar') },
    validation () { return validationView({ nng8: this.nng8, overview: this.overview, radar: this.radar }) },
    operations () { return operationalView(this.nng8, this.overview) },
    sync () { return nng8SyncPresentation(this.nng8 && this.nng8.sync) }
  },
  watch: { section () { this.invalidate(); this.load() }, accountId () { this.invalidate(); this.load() } },
  created () {
    this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.invalidate(); this.$nextTick(() => this.load()) } })
    this.load()
  },
  activated () { this.active = true; if (!this.loading) this.load() },
  deactivated () { this.active = false; this.invalidate() },
  beforeDestroy () { this.active = false; this.invalidate(); if (this.unsubscribe) this.unsubscribe() },
  methods: {
    value: nng8Value,
    status: nng8Status,
    ready (key) { return this.slots[key] && this.slots[key].status === 'ready' ? this.slots[key].data : null },
    invalidate () { this.seq++; this.slots = {}; this.loading = false; this.permission = '' },
    async load () {
      if (!this.active || this.loading) return
      if (!this.accountId) { this.invalidate(); this.permission = '请先登录后读取研究证据。'; return }
      const seq = ++this.seq; const account = this.accountId; const section = this.section
      const current = () => this.active && seq === this.seq && account === this.accountId && section === this.section
      const sources = {
 ...(NNG8_READONLY_ENABLED ? { nng8: getNng8Readonly } : {}),
        ...(section !== 'quality' ? { overview: getWorkbenchOverview } : {}),
        ...(section === 'validation' ? { radar: getRadarMarketContext } : {})
}
      this.slots = Object.fromEntries(Object.keys(sources).map(key => [key, { status: 'loading', data: null, error: '' }]))
      if (!NNG8_READONLY_ENABLED) this.$set(this.slots, 'nng8', { status: 'disabled', data: null, error: '此候选构建未开放外部来源。' })
      this.loading = true; this.permission = ''
      await Promise.all(Object.entries(sources).map(async ([key, read]) => {
        try {
          const response = await read()
          if (!current()) return
          const raw = response && response.code === 1 ? response.data : null
          const data = key === 'nng8' ? normalizeNng8(raw) : raw
          if (!data || (key === 'overview' && (data.contract_version !== 'workbench.overview.v1' || !Array.isArray(data.items))) || (key === 'radar' && !data.signals)) throw Error('invalid_contract')
          this.$set(this.slots, key, { status: 'ready', data, error: '' })
        } catch (error) {
          if (!current()) return
          const failure = sourceFailure(error)
          if (failure.sessionLost) { this.invalidate(); this.permission = failure.message; return }
          this.$set(this.slots, key, { status: 'failed', data: null, error: failure.message })
        }
      }))
      if (current()) this.loading = false
    }
  }
}
</script>

<style scoped>
.research-evidence { --ink: #26372f; --muted: #58675f; --surface: #fff; --border: #dce5df; --soft: #f1f6f3; color: var(--ink); }
.research-evidence.dark { --ink: #e4eee8; --muted: #b3c4b9; --surface: #1e2923; --border: #42574a; --soft: #28372e; }
.hub-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 18px; }
.hub-head h1 { font-size: 26px; color: var(--ink); margin: 0 0 9px; }
.hub-head p, .evidence-card p, .source-details p { color: var(--muted); line-height: 1.7; }
.eyebrow { font-size: 12px; margin-bottom: 5px; }
.research-evidence button { padding: 9px 18px; color: var(--ink); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; white-space: nowrap; }
.research-evidence button:disabled { opacity: .6; cursor: wait; }
.source-statuses { display: flex; flex-wrap: wrap; gap: 6px 24px; padding: 12px 16px; margin-bottom: 18px; background: var(--soft); border-radius: 9px; }
.source-statuses p { margin: 0; color: var(--muted); }
.validation-grid, .operations-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 18px; }
.evidence-card { padding: 22px; margin-bottom: 18px; min-width: 0; border: 1px solid var(--border); background: var(--surface); border-radius: 12px; overflow-wrap: anywhere; }
.evidence-card h2 { color: var(--ink); font-size: 19px; margin-bottom: 10px; }
.evidence-card h3 { color: var(--ink); font-size: 15px; }
.facts { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 15px 20px; margin: 20px 0; }
.facts dt { color: var(--muted); font-size: 12px; font-weight: normal; }
.facts dd { color: var(--ink); margin: 5px 0 0; font-size: 16px; }
.profit-file { border-top: 1px solid var(--border); padding-top: 14px; margin-top: 14px; }
.boundary { padding: 14px 18px; background: var(--soft); color: var(--muted); border-left: 3px solid #69967b; }
.source-details { padding: 16px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); }
.source-details summary { cursor: pointer; color: var(--ink); }
.failure, .state-bad { color: #b93d42 !important; }
.dark .failure, .dark .state-bad { color: #ffb2b5 !important; }
.state-warn { color: #9b630d; }
.dark .state-warn { color: #f5cd8b; }
</style>
