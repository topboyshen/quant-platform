<template>
  <section class="validation-progress" :class="{ dark }" data-testid="research-validation-progress">
    <header><div><h2>自然样本与组合风险</h2><p>ETF、Profit 分开统计；检查失败不算成功，历史纸面记录不冒充新样本。</p></div><a-button :loading="state.loading" @click="reader.load()">刷新进度</a-button></header>
    <p v-if="state.loading" role="status">正在读取样本与阻断证据…</p>
    <a-alert v-if="state.error" type="warning" show-icon :message="state.error" />
    <template v-if="state.data">
      <div class="families">
        <article v-for="family in ['etf', 'profit']" :key="family" :data-testid="'sample-family-' + family">
          <h3>{{ family === 'etf' ? 'ETF · 固定规则观察' : 'Profit · 股票纸面研究' }}</h3>
          <p>{{ value(state.data[family].reason) }}</p>
          <ol class="stages"><li v-for="stage in state.data[family].stages" :key="stage.key"><div><strong>{{ stage.label }}</strong><span>{{ value(stage.count) }} {{ unitLabel(stage.unit) }}</span></div><small>{{ stage.reason }}</small></li></ol>
          <template v-if="family === 'etf'">
            <details><summary>排除与等待原因</summary>
              <p v-for="(count, key) in state.data.etf.excludedReasons || {}" :key="'excluded-' + key">{{ reasonLabel(key) }}：{{ value(count) }} 条</p>
              <p v-for="(count, key) in state.data.etf.awaitingBreakdown || {}" :key="'awaiting-' + key">{{ reasonLabel(key) }}：{{ value(count) }} 条</p>
              <p>候选 = 合格 + 排除；合格 = 成熟 + 等待结果 + 未成交 + 无效结果。分项不能重复相加。</p>
            </details>
          </template>
        </article>
      </div>
      <article data-testid="profit-portfolio-risk"><h3>组合与退出 · 实际记录风险</h3>
        <p>重复持仓、待退出和名义权重是风险证据，不是实盘资金或统一组合收益。</p>
        <dl><div><dt>当前活跃记录</dt><dd>{{ value(risk.active && risk.active.records) }}</dd></div><div><dt>活跃标的</dt><dd>{{ value(risk.active && risk.active.securities) }}</dd></div><div><dt>叠加记录</dt><dd>{{ value(risk.active && risk.active.repeat_records_above_one_per_symbol) }}</dd></div><div><dt>待退出记录</dt><dd>{{ value(risk.exit_pending && risk.exit_pending.records) }}</dd></div><div><dt>组合收益</dt><dd>{{ protocol.comparison && protocol.comparison.primary !== null && protocol.comparison.primary !== undefined ? protocol.comparison.primary : '不可评估' }}</dd></div><div><dt>真实资金占用</dt><dd>{{ value(risk.portfolio && risk.portfolio.actual_cash_used) }}</dd></div></dl>
        <p v-if="state.data.profit.latestRun">最近自然研究：{{ value(state.data.profit.latestRun.status) }} · {{ value(state.data.profit.latestRun.reason) }}</p>
        <p v-for="(reason, i) in risk.integrity_issues || []" :key="i">{{ typeof reason === 'string' ? reason : value(reason.reason || reason.code) }}</p>
      </article>
      <article data-testid="profit-fixed-protocol"><h3>固定 A0 / A1 · 自然前瞻窗口</h3>
        <p>只比较“允许同标的叠加 / 不允许同标的叠加”。规则、费用、基准和观察窗口不随结果调整。</p>
        <dl><div><dt>纳入窗口</dt><dd>{{ value(protocol.enrollmentTarget) }} 个交易日</dd></div><div><dt>总观察窗口</dt><dd>{{ value(protocol.observationTarget) }} 个交易日</dd></div><div><dt>已完成窗口</dt><dd>{{ value(protocol.elapsedSessions) }} 个交易日</dd></div><div><dt>状态</dt><dd>{{ ['active', 'active_observation'].includes(protocol.status) ? '自然积累中' : '尚未满足正式启用条件' }}</dd></div></dl>
        <ul class="readiness"><li v-for="item in protocol.readiness" :key="item.key" :class="{ blocked: item.ready !== true }">{{ item.ready === true ? '已核实' : '阻断' }}：{{ item.reason || item.key }}</li></ul>
        <p v-if="protocol.calendar && protocol.calendar.reason">交易日历：{{ protocol.calendar.reason }}</p>
        <p v-for="(reason, i) in protocol.reasons || []" :key="'protocol-' + i">阻断：{{ reason }}</p>
        <p>后台持续捕获自然任务的新证据；正式启用前的记录单列留存，不追认、不补跑、不为凑数量放宽条件。</p>
        <details><summary>来源身份与采集信息</summary><p>协议 {{ value(protocol.id) }} · {{ value(protocol.manifestSha256) }}</p><p>新证据 {{ value(capture.forwardObserved) }}；历史留存 {{ value(capture.developmentOnly) }}；首次捕获 {{ value(capture.firstSeenAt) }}</p><p>{{ value(capture.reason) }}</p><p>接口读取 {{ value(state.data.capturedAt) }}（不是报告生成时间或行情截止）</p></details>
      </article>
    </template>
  </section>
</template>
<script>
import { getResearchValidationProgress } from '@/api/unifiedResearch'
import { createValidationReader, validationValue } from '@/utils/researchValidation'
import { researchQualityAccount } from '@/utils/researchRunQuality'
export default {
  name: 'ResearchValidationProgress',
  data () { return { state: { loading: false, data: null, error: '' }, reader: null, timer: null, unsubscribe: null } },
  computed: {
    accountId () { return researchQualityAccount(this.$store.getters.userInfo) },
    dark () { return ['dark', 'realdark'].includes(this.$store.state.app.theme) },
    risk () { return this.state.data.profit.risk },
    protocol () { return this.state.data.profit.protocol },
    capture () { return this.state.data.profit.capture }
  },
  watch: { accountId () { this.reader.invalidate(); this.reader.load() } },
  created () {
    this.reader = createValidationReader({ state: this.state, read: getResearchValidationProgress, account: () => this.accountId })
    this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.reader.invalidate(); this.$nextTick(() => this.reader.load()) } })
    this.reader.load(); this.startPolling()
  },
  activated () { this.reader.activate(); this.reader.load(); this.startPolling() },
  deactivated () { this.reader.deactivate(); this.stopPolling() },
  beforeDestroy () { this.reader.deactivate(); this.stopPolling(); if (this.unsubscribe) this.unsubscribe() },
  methods: {
    value: validationValue,
    unitLabel (key) { return ({ exchange_sessions: '个交易日', executions: '次执行', paper_records: '条纸面记录', decision_records: '条决策记录', portfolio_sessions: '个组合会话' })[key] || key },
    reasonLabel (key) { return ({ run_not_success: '原批次未成功', outside_decision_window: '不在固定决策窗口', non_oos: '不是前瞻样本', invalid_record: '记录证据无效', not_due: '尚未到期', due_uncollected: '已到期但未采集', date_unknown: '日期未知' })[key] || key },
    startPolling () { if (!this.timer) this.timer = setInterval(() => this.reader.load(), 60000) },
    stopPolling () { if (this.timer) clearInterval(this.timer); this.timer = null }
  }
}
</script>
<style scoped>
.validation-progress { --ink: #25382e; --muted: #5d7064; --bg: #fff; --line: #d5e1d9; color: var(--ink); margin-bottom: 24px; }
.validation-progress.dark { --ink: #e0ebe4; --muted: #b6c8bd; --bg: #1d2922; --line: #455c4c; }
header { display:flex; justify-content:space-between; gap:24px; margin-bottom:16px; } h2,h3 { color:var(--ink); } p,small,details { color:var(--muted); line-height:1.7; }
.families { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; } article { background:var(--bg); border:1px solid var(--line); padding:22px; border-radius:12px; margin-bottom:18px; min-width:0; overflow-wrap:anywhere; }
.stages { list-style:none; padding:0; } .stages li { border-top:1px solid var(--line); padding:12px 0; } .stages li div { display:flex; justify-content:space-between; gap:20px; } small { display:block; }
dl { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px 30px; } dt { color:var(--muted); } dd { margin:5px 0 0; } .readiness { padding-left:20px; } .blocked { color:#a66606; } .dark .blocked { color:#ffcc7c; } details { margin-top:16px; }
</style>
