<template>
  <div class="workbench-overview" :class="{ 'workbench-overview--dark': dark }" data-testid="workbench-overview">
    <a-alert v-if="error" type="error" show-icon class="workbench-alert" :message="error" />
    <a-alert
      v-else-if="summary.overall === 'unavailable' && !loading"
      type="error"
      show-icon
      class="workbench-alert"
      message="全部业务来源不可用：本页其余数字不代表当前状态，请勿据此下结论。"
    />
    <a-alert v-else-if="summary.overall === 'partial'" type="warning" show-icon class="workbench-alert" :message="attentionTitle">
      <div slot="description" class="attention-description">
        <span v-if="!attention.length">接口报告部分证据不可用，请查看来源明细。</span>
        <span v-else>{{ attention.map(item => `${item.label}：${item.status}`).join('；') }}</span>
        <button type="button" class="evidence-link" data-testid="workbench-attention-detail" @click="openEvidence">查看原因与证据</button>
      </div>
    </a-alert>

    <div class="workbench-readline">
      <span>研究摘要行情截止 <strong>{{ marketCutoff }}</strong></span>
      <span>工作台读取 {{ observedAt }}（CST）<template v-if="error"> · 最近尝试 {{ lastAttemptAt }}</template></span>
      <span v-if="loading" role="status">正在读取，保留原数据时间…</span>
    </div>
    <div class="workbench-priority-grid">
      <div class="workbench-research"><slot name="research" /></div>
      <section class="profit-summary" aria-labelledby="home-profit-title" data-testid="home-profit-summary">
        <div class="profit-heading"><h2 id="home-profit-title">股票纸面研究</h2><a-tag>模拟跟踪 ≠ 账户收益</a-tag></div>
        <div v-if="!profitReports.length" class="profit-empty">{{ loading ? '正在读取报告证据…' : '报告证据不可用；不推测收益或结论。' }}</div>
        <article v-for="report in profitReports" :key="report.id" class="profit-report" data-testid="home-profit-report">
          <div class="profit-report-title"><span>{{ report.title }}</span><a-tag :color="report.statusMeta.color">{{ report.status === 'ok' ? '可读' : report.statusMeta.label }}</a-tag></div>
          <strong class="profit-conclusion">{{ report.conclusion === '未知' ? '结论未知' : report.conclusion }}</strong>
          <div class="profit-time">生成 {{ report.generatedAt }} · 行情截止 {{ report.marketCutoff }}</div>
        </article>
        <router-link to="/personal-reports" class="report-entry">阅读报告与来源 <a-icon type="arrow-right" /></router-link>
      </section>
    </div>
    <slot name="market-summary" />
    <slot name="research-entry" />
    <slot name="market-pools" />

    <details ref="evidenceSection" class="workbench-evidence" data-testid="workbench-evidence">
      <summary>运行与审计证据 <span class="evidence-summary-status" data-testid="workbench-overall">{{ loading ? '读取中' : (error ? '读取失败' : summary.overallLabel) }}</span><small>来源、截止日期、判定规则与未验证事项</small></summary>
      <div class="workbench-block">
        <div class="workbench-evidence-head"><h2>证据明细</h2><a-button size="small" :loading="loading" data-testid="workbench-refresh" @click="load">刷新工作台证据</a-button></div>
        <p class="evidence-note">本区仅表示证据状态，不代表策略盈利。Hermes 进程可用不等于 Telegram 或模型已验证。</p>
        <a-table
          size="small"
          row-key="id"
          :columns="columns"
          :data-source="rows"
          :pagination="false"
          :scroll="{ x: 920 }"
          data-testid="workbench-items">
          <template slot="status" slot-scope="text, row"><a-tag :color="row.statusColor" :class="{ 'workbench-status-neutral': row.statusColor === 'default' }" data-testid="workbench-status">{{ text }}</a-tag></template>
        </a-table>
      </div>
      <div class="workbench-block">
        <h2>哪些结论还不能下</h2>
        <ul v-if="summary.openQuestions.length" class="question-list"><li v-for="(question, index) in summary.openQuestions" :key="index">{{ question }}</li></ul>
        <div v-else class="evidence-note">当前已接入来源未报告异常；研究有效性与实际送达仍须各自证据。</div>
      </div>
      <slot name="audit" />
    </details>
  </div>
</template>

<script>
import { getWorkbenchOverview } from '@/api/workbench'
import { expandWorkbenchRows, summarizeWorkbench, workbenchItems } from '@/utils/workbench'
import { homeAttention, homeMarketCutoff, homeProfitReports, homeTime } from '@/utils/homePresentation'

function decodeEnvelope (response) {
  if (response && response.code === 1 && response.data && Array.isArray(response.data.items)) return { data: response.data, error: '' }
  if (response && response.code === 0) return { data: null, error: response.msg || '工作台读取失败' }
  return { data: null, error: '工作台读取失败，请稍后重试。' }
}

export default {
  name: 'WorkbenchOverview',
  props: { dark: { type: Boolean, default: false } },
  data () {
    return { loading: false, error: '', overview: null, observedAt: '—', lastAttemptAt: '—', loadSeq: 0 }
  },
  computed: {
    summary () { return summarizeWorkbench(this.overview) },
    attention () { return homeAttention(this.overview) },
    attentionTitle () { return this.attention.length ? `${this.attention.length} 项证据需要注意` : '部分业务来源不可用' },
    profitReports () { return homeProfitReports(this.overview) },
    marketCutoff () { return homeMarketCutoff(this.overview) },
    rows () {
      return expandWorkbenchRows(workbenchItems(this.overview))
    },
    columns () {
      return [
        { title: '项目', dataIndex: 'label', width: 180 },
        { title: '状态', dataIndex: 'statusLabel', width: 145, scopedSlots: { customRender: 'status' } },
        { title: '数据截止', dataIndex: 'cutoff', width: 165 },
        { title: '来源', dataIndex: 'source', width: 195 },
        { title: '判定规则 / 说明', dataIndex: 'detail', width: 285 }
      ]
    }
  },
  created () { this.load() },
  beforeDestroy () { this.loadSeq += 1 },
  methods: {
    openEvidence () {
      const target = this.$refs.evidenceSection
      if (target) {
        target.open = true
        this.$nextTick(() => target.scrollIntoView({ block: 'start' }))
      }
    },
    async load () {
      const seq = ++this.loadSeq
      this.loading = true
      this.lastAttemptAt = homeTime(new Date().toISOString())
      this.$emit('loading-change', true)
      try {
        const decoded = decodeEnvelope(await getWorkbenchOverview())
        if (seq !== this.loadSeq) return
        this.overview = decoded.data
        this.error = decoded.error
        this.observedAt = decoded.data ? homeTime(new Date().toISOString()) : '—'
      } catch (error) {
        if (seq !== this.loadSeq) return
        const response = error && error.response
        const decoded = decodeEnvelope(response && response.data)
        // An explicit all-unavailable 503 is valid evidence, not an empty page.
        // Other transport errors or code:0 cannot revive attached old data.
        if (response && response.status === 503 && !decoded.error && decoded.data.all_unavailable === true) {
          this.overview = decoded.data
          this.observedAt = homeTime(new Date().toISOString())
          this.error = ''
        } else {
          this.overview = null
          this.observedAt = '—'
          this.error = '工作台读取失败，请稍后重试。'
        }
      } finally {
        if (seq === this.loadSeq) {
          this.loading = false
          this.$emit('loading-change', false)
        }
      }
    }
  }
}
</script>

<style scoped>
.workbench-overview { --wb-ink: #203c31; --wb-muted: #65766d; --wb-surface: #fff; --wb-border: #dfe8e2; color: var(--wb-ink); }
.workbench-overview--dark { --wb-ink: #e3ece7; --wb-muted: #abbcb2; --wb-surface: #202923; --wb-border: #3b493f; }
.workbench-alert { margin-bottom: 14px; }
.attention-description { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.evidence-link { background: transparent; border: 0; color: inherit; padding: 0; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
.workbench-readline { display: flex; flex-wrap: wrap; gap: 6px 20px; margin: 12px 0 18px; font-size: 12px; color: var(--wb-muted); }
.workbench-readline strong { color: var(--wb-ink); font-weight: 600; }
.workbench-priority-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.workbench-research { min-width: 0; }
.profit-summary { padding: 19px; background: var(--wb-surface); border: 1px solid var(--wb-border); border-radius: 10px; min-width: 0; }
.profit-heading, .profit-report-title { display: flex; justify-content: space-between; flex-wrap: wrap; align-items: center; gap: 8px; }
.profit-heading h2, .workbench-block h2 { color: var(--wb-ink); font-size: 16px; margin: 0; font-weight: 600; }
.profit-heading .ant-tag, .profit-report-title .ant-tag { margin: 0; }
.profit-report { padding: 13px 0; border-bottom: 1px solid var(--wb-border); }
.profit-report-title { font-size: 12px; color: var(--wb-muted); }
.profit-conclusion { display: block; font-size: 17px; font-weight: 600; margin: 7px 0; overflow-wrap: anywhere; }
.profit-time, .profit-empty, .evidence-note { color: var(--wb-muted); font-size: 12px; line-height: 1.7; }
.profit-empty { padding: 20px 0; }
.report-entry { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; margin-top: 13px; }
.workbench-evidence { scroll-margin-top: 88px; margin-top: 20px; padding: 16px 18px; border: 1px solid var(--wb-border); border-radius: 10px; background: var(--wb-surface); }
.workbench-evidence > summary { cursor: pointer; font-size: 15px; font-weight: 600; color: var(--wb-ink); }
.workbench-evidence > summary small { display: block; font-size: 12px; font-weight: 400; margin: 5px 0 0 17px; color: var(--wb-muted); }
.evidence-summary-status { font-size: 12px; font-weight: 400; margin-left: 10px; color: var(--wb-muted); }
.workbench-evidence-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.workbench-block { margin-top: 20px; overflow-wrap: anywhere; }
.evidence-note { margin: 9px 0 14px; }
.workbench-status-neutral { color: var(--wb-ink) !important; background: var(--wb-surface) !important; border-color: var(--wb-border) !important; }
.question-list { margin: 12px 0 0; padding-left: 18px; color: var(--wb-muted); line-height: 1.8; }
@media (max-width: 760px) { .workbench-priority-grid { grid-template-columns: minmax(0, 1fr); gap: 14px; } .profit-summary { padding: 16px; } }
</style>
