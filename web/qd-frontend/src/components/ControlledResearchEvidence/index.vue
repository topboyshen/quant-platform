<template>
  <section class="controlled-evidence" :class="{ dark }" data-testid="controlled-research-evidence">
    <header><div><p class="evidence-eyebrow">Quant-Server · 完整内容白名单</p><h2>Profit / ETF · 可归档研究依据</h2><p>先查看完整来源，再明确选择研究项目；归档是新增不可变证据，不改报告、规则或样本统计。</p></div><a-button :loading="loading" data-testid="controlled-evidence-refresh" @click="load">刷新证据来源</a-button></header>
    <p v-if="error" class="evidence-error" role="alert" data-testid="controlled-evidence-error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取白名单来源内容…</p>
    <p v-if="!loading && !error && !visibleRows.length">{{ rows.length ? '没有匹配的可归档来源。' : '服务端未提供可归档来源；不以摘要填补。' }}</p>
    <article v-for="row in visibleRows" :key="row.key" class="controlled-evidence-card" data-testid="controlled-evidence-report">
      <h3>{{ row.report.title || '来源标题未知' }}</h3><p>{{ row.report.source === 'quant/etf' ? 'ETF 系统有效性 · 完整 JSON 快照' : 'Profit · 原报告内容' }}</p>
      <dl><div><dt>报告生成时间</dt><dd>{{ value(row.report.generated_at) }}</dd></div><div><dt>行情 / 评价截止</dt><dd>{{ value(row.report.data_cutoff) }}</dd></div><div><dt>读取状态</dt><dd>{{ value(row.report.status) }}（不代表盈利或有效性通过）</dd></div><div><dt>归档内容</dt><dd>{{ row.identity ? '来源身份与完整内容已提供' : '尚不能确认' }}</dd></div></dl>
      <p v-if="row.reason" class="evidence-error">{{ row.reason }}</p>
      <p v-if="row.report.source === 'quant/etf'">该快照记录系统验证口径；关联到单标的研究只作背景证据，不表示这只标的已通过验证。</p>
      <details data-testid="controlled-evidence-original"><summary>阅读全文 / 完整快照</summary><section v-for="(block, index) in row.report.blocks || []" :key="index"><h4 v-if="block.heading">{{ block.heading }}</h4><p>{{ block.text }}</p></section></details>
      <nng8-research-link :report="row.report" :captured-at="row.capturedAt === undefined ? null : row.capturedAt" :account-id="accountId" :source-ready="!!row.identity && !loading && !error" />
      <details class="evidence-identity"><summary>来源身份与限制</summary><p>来源键 {{ value(row.report.id) }}</p><p>内容 SHA256 {{ value(row.report.content_sha256) }}</p><p>来源捕获身份时间 {{ value(row.capturedAt) }}；未知时不借用同步时间、文件时间或归档时间。</p><p v-for="(line, index) in row.report.limitations || []" :key="index">{{ line }}</p></details>
    </article>
  </section>
</template>

<script>
import { getResearchEvidenceReports } from '@/api/unifiedResearch'
import Nng8ResearchLink from '@/components/Nng8ResearchLink'
import { researchQualityAccount, qualityValue } from '@/utils/researchRunQuality'
import { evidenceSourcesResponse, filterEvidenceSources } from '@/utils/controlledEvidence'
import { sourceFailure } from '@/utils/unifiedResearch'

export default {
  name: 'ControlledResearchEvidence',
  components: { Nng8ResearchLink },
  props: { query: { type: String, default: '' } },
  data () { return { rows: [], loading: false, error: '', generation: 0, active: true, unsubscribe: null, activatedOnce: false } },
  computed: {
    accountId () { return researchQualityAccount(this.$store && this.$store.getters.userInfo) },
    dark () { return ['dark', 'realdark'].includes(this.$store.state.app.theme) },
    visibleRows () { return filterEvidenceSources(this.rows, this.query) }
  },
  watch: { accountId () { this.invalidate(); this.load() } },
  created () {
    this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.invalidate(); this.$nextTick(() => this.load()) } })
    this.load()
  },
  activated () { this.active = true; if (this.activatedOnce) this.load(); this.activatedOnce = true },
  deactivated () { this.active = false; this.invalidate() },
  beforeDestroy () { this.active = false; this.invalidate(); if (this.unsubscribe) this.unsubscribe() },
  methods: {
    value: qualityValue,
    invalidate () { this.generation++; this.rows = []; this.loading = false; this.error = '' },
    async load () {
      if (!this.active || this.loading) return
      this.invalidate()
      if (!this.accountId) { this.error = '请登录后读取和归档自己的研究依据。'; return }
      const context = { generation: this.generation, account: this.accountId }
      const current = () => this.active && context.generation === this.generation && context.account === this.accountId
      this.loading = true
      try {
        const rows = evidenceSourcesResponse(await getResearchEvidenceReports())
        if (current()) this.rows = rows
      } catch (error) { if (current()) this.error = error && error.response ? sourceFailure(error).message : error.message || '归档来源读取失败。' } finally { if (current()) this.loading = false }
    }
  }
}
</script>

<style scoped>
.controlled-evidence { --e-bg: #fff; --e-text: #26372f; --e-muted: #58675f; --e-border: #dce5df; --e-soft: #f1f6f3; color: var(--e-text); margin: 28px 0; }
.controlled-evidence.dark { --e-bg: #1e2923; --e-text: #e4eee8; --e-muted: #b3c4b9; --e-border: #42574a; --e-soft: #28372e; }
header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; } h2, h3, h4 { color: var(--e-text); } h2 { font-size: 22px; } h3 { font-size: 18px; } p { line-height: 1.7; color: var(--e-muted); overflow-wrap: anywhere; } .evidence-eyebrow { font-size: 12px; margin: 0 0 5px; }
.controlled-evidence-card { margin: 18px 0; padding: 22px; border: 1px solid var(--e-border); border-radius: 10px; background: var(--e-bg); min-width: 0; }
dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 28px; } dt { font-size: 12px; color: var(--e-muted); font-weight: normal; } dd { margin: 5px 0 0; color: var(--e-text); overflow-wrap: anywhere; }
details { margin: 18px 0; padding: 14px; background: var(--e-soft); border-radius: 7px; } summary { color: var(--e-text); cursor: pointer; } details section { border-top: 1px solid var(--e-border); margin-top: 18px; padding-top: 14px; } details section p { white-space: pre-wrap; } .evidence-identity { font-size: 12px; }
.evidence-error { color: #aa343a; } .dark .evidence-error { color: #ffb2b5; } .dark /deep/ .ant-btn { background: var(--e-soft); color: var(--e-text); border-color: var(--e-border); }
</style>
