<template>
  <section
    v-if="enabled"
    id="nng8-readonly"
    ref="region"
    class="nng8-readonly"
    :class="{ 'nng8-readonly--dark': dark }"
    tabindex="-1"
    data-testid="nng8-section">
    <div class="nng8-head">
      <div><span class="nng8-eyebrow">外部只读来源</span><h2>AStock · 外部报告与依据</h2><p>阅读原文，明确确认后用于研究；可读取不等于业务成功。</p></div>
      <button type="button" class="nng8-refresh" :disabled="loading || !accountId" data-testid="nng8-refresh" @click="load">{{ loading ? '读取中…' : '刷新 nnG8' }}</button>
    </div>
    <div class="nng8-state" :class="'nng8-state--' + pageState" role="status" aria-live="polite" data-testid="nng8-state">{{ stateText }}</div>
    <div v-if="snapshot && snapshot.snapshot_stale" class="nng8-notice" data-testid="nng8-stale">来源快照陈旧：采集时间 {{ value(snapshot.captured_at) }}。本次读取不会更新来源时效。</div>
    <div v-if="snapshot && (cached || loading)" class="nng8-notice" data-testid="nng8-cached">当前正文为上次成功读取（{{ value(lastSuccessAt) }}）的副本。{{ cached ? '本次刷新失败，未获得新证据。' : '本次刷新尚未完成。' }}</div>
    <template v-if="snapshot">
      <section class="nng8-sync" :class="'nng8-sync--' + syncView.tone" aria-live="polite" data-testid="nng8-sync-health">
        <h3>来源同步</h3>
        <p class="nng8-sync-message" data-testid="nng8-sync-status">{{ syncView.message }}</p>
        <p v-if="syncView.reason" class="nng8-small" data-testid="nng8-sync-reason">{{ syncView.reason }}</p>
        <p v-if="cached || loading" class="nng8-small" data-testid="nng8-sync-cached">健康信息来自上次成功读取，未确认本次同步状态。</p>
        <dl class="nng8-sync-times">
          <div><dt>同步操作开始时间</dt><dd data-testid="nng8-sync-started">{{ value(snapshot.sync.started_at) }}</dd></div>
          <div><dt>同步操作结束时间</dt><dd data-testid="nng8-sync-finished">{{ value(snapshot.sync.finished_at) }}</dd></div>
          <div><dt>最近同步成功操作时间</dt><dd data-testid="nng8-sync-last-success">{{ value(snapshot.sync.last_success_at) }}</dd></div>
          <div><dt>连续同步失败次数</dt><dd>{{ value(snapshot.sync.consecutive_failures) }}</dd></div>
        </dl>
        <p class="nng8-small">以上为同步操作时间，不是报告生成时间或行情截止。本次页面读取成功不等于同步成功，同步成功也不代表业务成功或数据新鲜。</p>
        <p class="nng8-small">同步变化在下一次页面读取或刷新时可见；此页没有自动轮询或推送保证。</p>
        <details class="nng8-fingerprint"><summary>同步关联信息</summary><p>同步写入协议：{{ value(snapshot.sync.writer_version) }}</p><p>关联副本字节 SHA256：{{ value(snapshot.sync.bundle_sha256) }}</p><p>关联哈希是副本一致性证据，不是原生执行或业务成功身份。</p></details>
      </section>
      <dl class="nng8-capture-times">
        <div><dt>来源</dt><dd>nnG8 外部只读包</dd></div>
        <div><dt>包采集时间</dt><dd>{{ value(snapshot.captured_at) }}</dd></div>
        <div><dt>成功读取时间</dt><dd>{{ value(snapshot.observed_at) }}</dd></div>
      </dl>
      <div class="nng8-layout" :class="{ 'nng8-layout--reports': view === 'reports' }">
        <div class="nng8-reports" data-testid="nng8-report-reader">
          <div v-if="!visibleReports.length" class="nng8-empty">{{ snapshot.reports.length ? '没有匹配搜索的 AStock 报告。' : '暂无可展示的晨报正文；缺少报告不代表任务成功。' }}</div>
          <article v-for="report in visibleReports" :key="report.render_key" class="nng8-report">
            <div class="nng8-row-head"><h3>{{ report.title }}</h3><span class="nng8-badge" :class="'tone-' + status(report.status).tone">{{ status(report.status).label }}</span></div>
            <dl class="nng8-report-times">
              <div><dt>报告日期</dt><dd>{{ value(report.report_date) }}</dd></div>
              <div><dt>报告生成时间</dt><dd data-testid="nng8-generated-at">{{ value(report.generated_at) }}</dd></div>
              <div><dt>行情截止</dt><dd data-testid="nng8-data-cutoff">{{ value(report.data_cutoff) }}</dd></div>
              <div><dt>源时间</dt><dd>{{ value(report.source_time) }}</dd></div>
              <div><dt>报告证据观察截止</dt><dd>{{ value(report.evidence_cutoff) }}</dd></div>
              <div><dt>来源标识</dt><dd>{{ report.source }}</dd></div>
            </dl>
            <p class="nng8-small">发布门禁：{{ status(report.publication_gate).label }} · 复核：{{ status(report.review_status).label }}。门禁通过不自动代表业务成功。</p>
            <nng8-research-link :report="report" :captured-at="snapshot.captured_at" :account-id="accountId" :source-ready="active && !loading && !cached && !permission && !snapshot.snapshot_stale" />
            <p class="nng8-small" data-testid="nng8-body-count">正文显示 {{ report.blocks.length }} 段 / 来源原始 {{ value(report.body_block_count) }} 段。</p>
            <div v-if="report.body_truncated === true" class="nng8-notice" role="note" data-testid="nng8-body-truncated">正文未完整保留：显示受 128 段、单段 2000 字等安全边界限制；以下内容不能当作完整报告。</div>
            <p v-else-if="report.body_truncated === null" class="nng8-small" data-testid="nng8-body-completeness-unknown">来源未提供正文完整性标记，不能确认内容是否完整。</p>
            <div v-if="report.conclusion.length" class="nng8-prose"><h4>报告结论</h4><p v-for="(line, index) in report.conclusion" :key="index">{{ line }}</p></div>
            <div v-if="report.risks.length" class="nng8-prose nng8-risks"><h4>风险与限制</h4><p v-for="(line, index) in report.risks" :key="index">{{ line }}</p></div>
            <div v-for="(block, index) in report.blocks" :key="index" class="nng8-prose" data-testid="nng8-body-block"><h4 v-if="block.heading">{{ block.heading }}</h4><p>{{ value(block.text) }}</p></div>
            <div v-if="!report.blocks.length" class="nng8-empty">来源未提供正文分段。</div>
            <details class="nng8-fingerprint"><summary>外部内容标识</summary><p>{{ report.id }}</p><p>内容 SHA256：{{ value(report.content_sha256) }}</p><p>哈希仅表示内容一致性，不是来源认证或原生运行身份。</p></details>
          </article>
        </div>
        <aside v-if="view !== 'reports'" class="nng8-evidence">
          <section class="nng8-card" data-testid="nng8-quality">
            <div class="nng8-row-head"><h3>数据质量</h3><span class="nng8-badge" :class="'tone-' + status(snapshot.data_quality.status).tone">{{ status(snapshot.data_quality.status).label }}</span></div>
            <dl class="nng8-meta">
              <div><dt>源时间</dt><dd>{{ value(snapshot.data_quality.source_time) }}</dd></div>
              <div><dt>行情日期</dt><dd>{{ value(snapshot.data_quality.market_date) }}</dd></div>
              <div><dt>当前契约</dt><dd>{{ value(snapshot.data_quality.active_contract) }}</dd></div>
              <div><dt>计数范围</dt><dd>{{ value(snapshot.data_quality.counts_scope) }}</dd></div>
              <div><dt>计数契约</dt><dd>{{ value(snapshot.data_quality.counts_contract) }}</dd></div>
              <div><dt>计数行情日</dt><dd>{{ value(snapshot.data_quality.counts_target_date) }}</dd></div>
              <div><dt>当前运行状态</dt><dd>{{ value(snapshot.data_quality.current_run_status) }}</dd></div>
              <div><dt>完整快照</dt><dd>{{ snapshot.data_quality.full_snapshot === null ? '未知' : (snapshot.data_quality.full_snapshot ? '是' : '否') }}</dd></div>
            </dl>
            <dl class="nng8-counts"><div><dt>符合条件标的</dt><dd>{{ value(snapshot.data_quality.eligible) }}</dd></div><div><dt>有效标的</dt><dd>{{ value(snapshot.data_quality.valid) }}</dd></div><div><dt>无效标的</dt><dd>{{ value(snapshot.data_quality.invalid) }}</dd></div></dl>
            <p class="nng8-small">同一 scope、行情日与契约的标的数；缺少可核验口径时显示未知。</p>
            <p v-if="snapshot.data_quality.counts_scope === 'sealed_core_qlib_summary'" class="nng8-small" data-testid="nng8-quality-scope-limit">封存的 QLib 核心摘要，不是完整全市场价格档案。</p>
            <p v-if="snapshot.data_quality.historical_issue_dates.length" class="nng8-small">历史问题日：{{ snapshot.data_quality.historical_issue_dates.join('、') }}</p>
            <p v-for="(line, index) in snapshot.data_quality.limitations" :key="index" class="nng8-small">{{ line }}</p>
          </section>
          <section class="nng8-card" data-testid="nng8-delivery">
            <h3>投递与封存</h3><p v-if="!snapshot.delivery.length" class="nng8-empty">投递来源不可用。</p>
            <div v-for="(row, index) in snapshot.delivery" :key="index" class="nng8-evidence-row">
              <div class="nng8-row-head"><strong>{{ value(row.label) }}</strong><span class="nng8-badge" :class="'tone-' + status(row.status).tone">{{ status(row.status).label }}</span></div>
              <p class="nng8-small">该层源时间：{{ value(row.source_time) }}</p><p v-if="!row.source_time" class="nng8-small">缺少该层时间证据，不能证明当前投递时效。</p>
              <p v-for="(line, lineIndex) in row.limitations" :key="lineIndex" class="nng8-small">{{ line }}</p>
            </div>
          </section>
          <section class="nng8-card" data-testid="nng8-services">
            <h3>服务与任务</h3><p v-if="!snapshot.services.length" class="nng8-empty">服务来源不可用。</p>
            <div v-for="(row, index) in snapshot.services" :key="index" class="nng8-evidence-row">
              <div class="nng8-row-head"><strong>{{ value(row.label) }}</strong><span class="nng8-badge" :class="'tone-' + status(row.status).tone">{{ status(row.status).label }}</span></div>
              <p class="nng8-small">上次运行：{{ value(row.last_run) }}<br />下次计划：{{ value(row.next_run) }}</p>
            </div>
          </section>
          <section class="nng8-card" data-testid="nng8-calibration">
            <h3>外部冻结校准</h3><p class="nng8-small">Ledger 与 Phase2 分别展示，可能重叠；不合计，不计入 ETF / Profit 样本。</p>
            <p v-if="!snapshot.calibration.length" class="nng8-empty">校准来源不可用，运行模式未知。</p>
            <div v-for="row in snapshot.calibration" :key="row.id" class="nng8-evidence-row">
              <div class="nng8-row-head"><strong>{{ value(row.label) }}</strong><span class="nng8-badge" :class="'tone-' + status(row.status).tone">{{ status(row.status).label }}</span></div>
              <p class="nng8-small">捕获的运行模式：{{ value(row.runtime_mode) }}</p>
              <dl class="nng8-counts"><div><dt>{{ row.id === 'ledger' ? '已到期预测' : '名义预测' }}</dt><dd>{{ value(row.nominal) }}</dd></div><div><dt>已观察</dt><dd>{{ value(row.observed) }}</dd></div><div><dt>待复核</dt><dd>{{ value(row.pending_review) }}</dd></div><div><dt>正式样本</dt><dd>{{ value(row.formal) }}</dd></div></dl>
              <p v-if="row.id === 'ledger'" class="nng8-small">待复核包含在已观察中，上述数字不相加。</p>
              <p v-for="(line, index) in row.limitations" :key="index" class="nng8-small">{{ line }}</p>
            </div>
          </section>
        </aside>
      </div>
      <div v-if="snapshot.issues.length" class="nng8-issues" data-testid="nng8-issues"><h3>缺失与待核验证据</h3><p v-for="(issue, index) in snapshot.issues" :key="index">{{ issue.source }}：{{ value(issue.message) }}</p></div>
      <p v-for="(line, index) in snapshot.limitations" :key="index" class="nng8-small">{{ line }}</p>
      <p class="nng8-boundary">页面读取只展示外部捕获副本，不是 QuantDinger 原生回测或 Profit 执行记录。只有明确确认“用于研究”才保存到你自己的项目与新分支；不会恢复冻结任务、发送通知、调用 AI 或开始回测。</p>
    </template>
  </section>
</template>

<script>
import { getNng8Readonly } from '@/api/workbench'
import Nng8ResearchLink from '@/components/Nng8ResearchLink'
import { NNG8_READONLY_ENABLED, normalizeNng8, nng8AccountId, nng8PageState, nng8Status, nng8Value, nng8SyncPresentation } from '@/utils/nng8Readonly'

export default {
  name: 'Nng8Readonly',
  components: { Nng8ResearchLink },
  props: { enabled: { type: Boolean, default: NNG8_READONLY_ENABLED }, view: { type: String, default: 'all' }, query: { type: String, default: '' } },
  data () {
    return {
 payload: null,
snapshotAccount: '',
lastSuccessAt: null,
loading: false,
error: '',
permission: '',
      cached: false,
requestVersion: 0,
active: true,
activationSeen: false,
sessionUnsubscribe: null,
reloadQueued: false
}
  },
  computed: {
    visibleReports () {
      const needle = (this.query || '').trim().toLocaleLowerCase()
      const reports = this.snapshot ? this.snapshot.reports : []
      return needle ? reports.filter(report => [report.title, ...report.conclusion, ...report.risks, ...report.blocks.map(block => block.text)].join('\n').toLocaleLowerCase().includes(needle)) : reports
    },
    accountId () { return nng8AccountId(this.$store && this.$store.getters && this.$store.getters.userInfo) },
    dark () { return Boolean(this.$store && this.$store.state && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme)) },
    snapshot () { return this.snapshotAccount === this.accountId ? this.payload : null },
    syncView () {
      const sync = this.snapshot && this.snapshot.sync
      return nng8SyncPresentation((this.cached || this.loading) ? { status: 'unknown', reason_code: 'unknown' } : sync)
    },
    pageState () { return nng8PageState({ enabled: this.enabled, loading: this.loading, permission: this.permission, error: this.error, snapshot: this.snapshot }) },
    stateText () {
      if (this.permission === 'login') return '登录身份不可用，请重新登录后读取 nnG8。'
      return {
 disabled: '',
loading: '正在读取 nnG8 来源副本…',
denied: '当前账号未获此只读区的访问权限，或功能未开放。',
        unavailable: this.error || 'nnG8 同步/读取失败，暂无本次可用副本。',
empty: '当前副本没有可展示内容。',
        partial: '部分来源可读或证据陈旧；请结合逐项限制阅读。',
available: '外部来源可读取；这不代表业务成功或研究有效。'
}[this.pageState]
    }
  },
  watch: {
    enabled (value) { this.clearSnapshot(); if (value) this.queueReload() },
    accountId () { this.clearSnapshot(); this.queueReload() },
    '$route.query.section' () { this.focusSection() }
  },
  created () {
    if (this.$store && typeof this.$store.subscribe === 'function') {
      this.sessionUnsubscribe = this.$store.subscribe(mutation => this.sessionMutation(mutation.type))
    }
    this.load()
  },
  mounted () { this.focusSection() },
  activated () {
    this.active = true
    if (this.activationSeen && !this.loading) this.load()
    this.activationSeen = true
    this.focusSection()
  },
  deactivated () { this.active = false; this.requestVersion += 1; this.loading = false },
  beforeDestroy () {
    this.active = false
    this.clearSnapshot()
    if (this.sessionUnsubscribe) this.sessionUnsubscribe()
  },
  methods: {
    value: nng8Value,
    status: nng8Status,
    clearSnapshot () {
      this.requestVersion += 1
      this.payload = null
      this.snapshotAccount = ''
      this.lastSuccessAt = null
      this.loading = false
      this.cached = false
      this.error = ''
      this.permission = ''
    },
    sessionMutation (type) {
      // Observe session replacement without reading, retaining or printing a token.
      if (/(^|\/)SET_TOKEN$/.test(type) || (/(^|\/)SET_INFO$/.test(type) && this.snapshotAccount !== this.accountId)) {
        this.clearSnapshot()
        this.queueReload()
      }
    },
    queueReload () {
      if (this.reloadQueued) return
      this.reloadQueued = true
      this.$nextTick(() => { this.reloadQueued = false; if (this.enabled && this.active && this.accountId && !this.loading) this.load() })
    },
    focusSection () {
      if (!this.enabled || !this.active || !this.$route || this.$route.query.section !== 'nng8') return
      this.$nextTick(() => {
        const region = this.$refs.region
        if (!this.enabled || !this.active || !region) return
        region.scrollIntoView({ block: 'start' })
        region.focus({ preventScroll: true })
      })
    },
    async load () {
      if (!this.enabled || !this.active) return
      const account = this.accountId
      if (!account) { this.clearSnapshot(); this.permission = 'login'; return }
      const version = ++this.requestVersion
      const current = () => this.enabled && this.active && version === this.requestVersion && account === this.accountId
      this.loading = true
      this.error = ''
      this.permission = ''
      try {
        const response = await getNng8Readonly()
        if (!current()) return
        const snapshot = response && response.code === 1 ? normalizeNng8(response.data) : null
        if (!snapshot) throw new Error('invalid_readonly_contract')
        this.payload = snapshot
        this.snapshotAccount = account
        this.lastSuccessAt = snapshot.observed_at
        this.cached = false
      } catch (error) {
        if (!current()) return
        const httpStatus = error && error.response ? error.response.status : 0
        if ([401, 403, 404].includes(httpStatus)) {
          this.clearSnapshot()
          this.permission = httpStatus === 401 ? 'login' : 'denied'
        } else {
          this.cached = Boolean(this.snapshot)
          this.error = this.cached
            ? 'nnG8 本次读取失败，未获得本次有效证据。显示上次成功读取的副本，同步状态未再次确认。'
            : 'nnG8 同步/读取失败，暂无本次可用副本。可稍后重试。'
        }
      } finally {
        if (current()) this.loading = false
      }
    }
  }
}
</script>

<style scoped>
.nng8-layout.nng8-layout--reports { grid-template-columns: minmax(0, 1fr); }
.nng8-readonly { --ng-ink: #24382e; --ng-muted: #59695f; --ng-surface: #fff; --ng-soft: #f4f7f5; --ng-border: #dce5df; --ng-warn: #78521c; --ng-warn-bg: #fff4de; --ng-bad: #9b3939; --ng-bad-bg: #fff0ef; color: var(--ng-ink); background: var(--ng-surface); padding: 24px; margin-top: 24px; border: 1px solid var(--ng-border); border-radius: 12px; scroll-margin-top: 88px; min-width: 0; overflow-wrap: anywhere; }
.nng8-readonly--dark { --ng-ink: #e3ede7; --ng-muted: #adbbb2; --ng-surface: #202923; --ng-soft: #26332b; --ng-border: #3d4b42; --ng-warn: #e8c58a; --ng-warn-bg: #3d3323; --ng-bad: #f2aaa3; --ng-bad-bg: #422a29; }
.nng8-head, .nng8-row-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
.nng8-head { align-items: flex-start; margin-bottom: 16px; }
.nng8-eyebrow { color: var(--ng-muted); font-size: 11px; letter-spacing: 0.08em; }
.nng8-readonly h2, .nng8-readonly h3, .nng8-readonly h4 { color: var(--ng-ink); font-weight: 600; }
.nng8-readonly h2 { font-size: 20px; margin: 3px 0 8px; }
.nng8-readonly h3 { font-size: 15px; margin: 0; }
.nng8-readonly h4 { font-size: 13px; margin: 0 0 7px; }
.nng8-head p, .nng8-small { font-size: 12px; color: var(--ng-muted); margin: 7px 0; line-height: 1.7; }
.nng8-refresh { flex-shrink: 0; border: 1px solid var(--ng-border); border-radius: 7px; padding: 7px 12px; color: var(--ng-ink); background: var(--ng-soft); cursor: pointer; }
.nng8-refresh:disabled { opacity: 0.6; cursor: default; }
.nng8-state, .nng8-notice { padding: 10px 12px; margin: 10px 0; border-radius: 7px; background: var(--ng-soft); font-size: 13px; }
.nng8-state--denied, .nng8-state--unavailable { background: var(--ng-bad-bg); color: var(--ng-bad); }
.nng8-state--partial, .nng8-notice { background: var(--ng-warn-bg); color: var(--ng-warn); }
.nng8-sync { background: var(--ng-soft); border: 1px solid var(--ng-border); border-radius: 9px; padding: 16px; margin: 16px 0; min-width: 0; }
.nng8-sync--bad { background: var(--ng-bad-bg); border-color: var(--ng-bad); }
.nng8-sync--warn { background: var(--ng-warn-bg); border-color: var(--ng-warn); }
.nng8-sync-message { margin: 8px 0; font-size: 13px; line-height: 1.7; }
.nng8-sync--bad .nng8-sync-message { color: var(--ng-bad); }
.nng8-sync--warn .nng8-sync-message { color: var(--ng-warn); }
.nng8-sync-times { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px 16px; margin: 16px 0; }
.nng8-capture-times, .nng8-report-times, .nng8-meta, .nng8-counts { margin: 14px 0; display: grid; gap: 11px 16px; }
.nng8-capture-times { grid-template-columns: repeat(3, minmax(0, 1fr)); padding-bottom: 15px; border-bottom: 1px solid var(--ng-border); }
.nng8-readonly dl > div { min-width: 0; }
.nng8-readonly dt { color: var(--ng-muted); font-weight: 400; font-size: 11px; margin-bottom: 4px; }
.nng8-readonly dd { color: var(--ng-ink); margin: 0; font-size: 12px; line-height: 1.6; }
.nng8-layout { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); align-items: start; gap: 22px; }
.nng8-reports, .nng8-evidence { min-width: 0; }
.nng8-report, .nng8-card { border: 1px solid var(--ng-border); border-radius: 9px; padding: 18px; margin-bottom: 16px; min-width: 0; }
.nng8-report-times, .nng8-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.nng8-counts { grid-template-columns: repeat(2, minmax(0, 1fr)); background: var(--ng-soft); padding: 12px; border-radius: 7px; }
.nng8-counts dd { font-size: 19px; font-variant-numeric: tabular-nums; }
.nng8-badge { display: inline-block; border: 1px solid var(--ng-border); border-radius: 5px; padding: 2px 6px; font-size: 11px; background: var(--ng-soft); color: var(--ng-muted); }
.tone-warn { color: var(--ng-warn); background: var(--ng-warn-bg); }
.tone-bad { color: var(--ng-bad); background: var(--ng-bad-bg); }
.nng8-prose { padding-top: 17px; margin-top: 16px; border-top: 1px solid var(--ng-border); }
.nng8-prose p { margin: 0 0 9px; font-size: 13px; line-height: 1.9; white-space: pre-wrap; overflow-wrap: anywhere; }
.nng8-risks { border-left: 3px solid var(--ng-warn); padding: 12px; border-top: 0; background: var(--ng-warn-bg); }
.nng8-fingerprint { margin-top: 18px; font-size: 11px; color: var(--ng-muted); }
.nng8-fingerprint summary { cursor: pointer; }
.nng8-fingerprint p { margin: 7px 0; }
.nng8-evidence-row + .nng8-evidence-row { border-top: 1px solid var(--ng-border); padding-top: 13px; margin-top: 13px; }
.nng8-empty { color: var(--ng-muted); font-size: 13px; padding: 17px 0; }
.nng8-issues { background: var(--ng-soft); padding: 16px; border-radius: 7px; }
.nng8-issues p, .nng8-boundary { font-size: 12px; line-height: 1.7; color: var(--ng-muted); margin: 10px 0 0; }
</style>
