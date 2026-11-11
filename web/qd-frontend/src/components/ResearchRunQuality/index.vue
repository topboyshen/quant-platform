<template>
  <section class="run-quality" :class="{ 'run-quality-dark': dark }" data-testid="research-run-quality">
    <header class="quality-header"><div><h2>关联具体回测的数据证据</h2><p>按自己的研究项目、分支和运行选择；本页只读取，不启动、对账或保存任何研究。</p></div><a-button :loading="state.loading" @click="reload">重新读取</a-button></header>
    <a-alert
      v-if="state.error"
      type="warning"
      show-icon
      :message="state.error"
      class="quality-notice"
      data-testid="run-quality-error" />
    <p v-if="state.loading" class="quality-notice" role="status" data-testid="run-quality-loading">{{ state.stage }}</p>
    <div class="quality-selectors">
      <label>研究项目<a-select :value="state.projectChoice || undefined" placeholder="选择自己的研究项目" :disabled="state.loading || !state.caps" @change="selectProject"><a-select-option v-for="project in state.projects" :key="project.id" :value="project.id">{{ project.name }} · {{ value(project.subject && project.subject.symbol) }}</a-select-option></a-select></label>
      <label>研究分支<a-select :value="state.branchChoice || undefined" placeholder="选择分支" :disabled="state.loading || !state.project" @change="selectBranch"><a-select-option v-for="branch in state.branches" :key="branch.id" :value="branch.id">{{ branch.name }}</a-select-option></a-select></label>
      <label>具体运行<a-select :value="state.runChoice || undefined" placeholder="选择运行记录" :disabled="state.loading || !state.branch" @change="selectRun"><a-select-option v-for="run in state.runs" :key="run.id" :value="run.id">{{ value(run.createdAt) }} · {{ status(run.status) }} · {{ run.id }}</a-select-option></a-select></label>
    </div>
    <p v-if="emptyMessage" class="quality-empty" data-testid="run-quality-empty">{{ emptyMessage }}</p>
    <template v-if="projection">
      <div class="quality-identity" data-testid="run-quality-identity"><strong>{{ value(projection.name) }} {{ value(projection.symbol) }}</strong><span>{{ value(projection.market) }} · {{ value(projection.timeframe) }}</span><span>{{ value(projection.projectName) }} / {{ value(projection.branchName) }}</span><span>运行 {{ projection.runId }} · {{ projection.status }}</span></div>
      <p v-if="projection.errorMessage" class="quality-notice">该次运行原因：{{ projection.errorMessage }}</p>
      <dl class="quality-grid" data-testid="run-quality-fields">
        <div><dt>请求日期范围</dt><dd>{{ value(projection.requested[0]) }} → {{ value(projection.requested[1]) }}</dd></div>
        <div><dt>结果实际日期端点</dt><dd>{{ value(projection.actual[0]) }} → {{ value(projection.actual[1]) }}</dd></div>
        <div><dt>逐日数据覆盖</dt><dd>{{ projection.coverage === null ? '未知' : (projection.coverage * 100).toFixed(2) + '%' }}</dd><small>{{ unknown('coverageRatio') }}</small></div><div><dt>缺口根数</dt><dd>{{ value(projection.missingBars) }}</dd><small>{{ unknown('missingBars') }}</small></div>
        <div><dt>本次输入有效日线</dt><dd>{{ value(projection.quality.observedBars) }}</dd><small>{{ unknown('observedBars') }}</small></div><div><dt>应有交易日日线</dt><dd>{{ value(projection.quality.expectedBars) }}</dd><small>{{ unknown('expectedBars') }}</small></div>
        <div><dt>原始 / 解析 / 拒绝 / 实际使用</dt><dd>{{ value(projection.quality.rawRows) }} / {{ value(projection.quality.parsedRows) }} / {{ value(projection.quality.rejectedRows) }} / {{ value(projection.quality.consumedRows) }}</dd><small>拒绝条目与实际输入校验分列；拒绝零不代表供应商数据完整。</small></div>
        <div><dt>交易所开市日（未扣上市/停牌）</dt><dd>{{ value(projection.quality.exchangeExpectedBars) }}</dd><small>仅凭交易所日历不能证明标的当天可交易。</small></div>
        <div><dt>输入采集时间 / 解析版本</dt><dd>{{ value(projection.quality.acquiredAt) }} / {{ value(projection.quality.parserVersion) }}</dd></div>
        <div><dt>实际输入快照</dt><dd>{{ projection.quality.inputSnapshot && projection.quality.inputSnapshot.status === 'verified' ? '已冻结并核对内容' : '未核实' }}</dd><small>{{ unknown('inputSnapshot') }}</small></div>
        <div><dt>上市生命周期</dt><dd>{{ value(projection.quality.listingLifecycle && projection.quality.listingLifecycle.firstTradingDate) }}</dd><small>{{ unknown('listingLifecycle') }}</small></div>
        <div><dt>停牌证据</dt><dd>{{ projection.quality.suspensionEvidence && projection.quality.suspensionEvidence.source ? '已记录来源及范围' : '未核实，不自动把缺口归因于停牌' }}</dd></div>
        <div><dt>无效日线 / 重复日线</dt><dd>{{ value(projection.quality.invalidBars) }} / {{ value(projection.quality.duplicateBars) }}</dd><small>{{ unknown('invalidBars') }} {{ unknown('duplicateBars') }}</small></div><div><dt>提供方 / 交易日历</dt><dd>{{ value(projection.quality.provider) }} / {{ value(projection.quality.calendarSource) }}</dd><small>{{ unknown('provider') }} {{ unknown('calendarSource') }}</small></div>
        <div><dt>这次输入行情截止</dt><dd>{{ value(projection.quality.marketCutoff) }}</dd><small>{{ unknown('marketCutoff') }}</small></div><div><dt>数据证据状态</dt><dd>{{ qualityStatusLabel }}</dd></div>
        <div><dt>行情数据版本</dt><dd>{{ value(projection.dataVersion) }}</dd></div><div><dt>复权口径</dt><dd>{{ value(projection.adjustment) }}</dd></div>
        <div><dt>统计指标版本</dt><dd>{{ value(projection.metricsVersion) }}</dd></div><div><dt>结果快照保存时间</dt><dd>{{ value(projection.resultSavedAt) }}</dd></div>
        <div class="quality-wide"><dt>行情内容指纹</dt><dd>{{ value(projection.dataFingerprint) }}</dd></div>
      </dl>
      <p v-if="projection.quality.missingDates" data-testid="run-quality-missing-dates">缺口日期：{{ projection.quality.missingDates.length ? projection.quality.missingDates.join('、') : '权威日历比对未发现缺口' }}</p>
      <p v-for="gap in projection.quality.gapDetails || []" :key="gap.date">{{ gap.date }}：{{ gap.reason }}（{{ gap.classification }}）</p>
      <details v-if="projection.quality.rawResponseFingerprint || (projection.quality.inputSnapshot && projection.quality.inputSnapshot.sha256)" class="quality-technical"><summary>原始响应与实际输入指纹</summary><p>原始响应：{{ value(projection.quality.rawResponseFingerprint) }}（{{ value(projection.quality.rawResponseFingerprintScope) }}）</p><p v-if="projection.quality.inputSnapshot">引擎实际输入：{{ value(projection.quality.inputSnapshot.sha256) }}；{{ value(projection.quality.inputSnapshot.rowCount) }} 行，{{ value(projection.quality.inputSnapshot.byteCount) }} 字节。</p><p>发行版本缺失仍是未知；内容指纹不是供应商发行版本。</p></details>
      <p class="quality-boundary" data-testid="run-quality-boundary">日期端点和内容指纹只标识本次结果，不能证明每个交易日均有有效行情；不会用 nnG8 全市场摘要、文件修改时间或同步时间补齐。{{ projection.quality.reason }} {{ projection.hasResult ? '缺失字段保持未知，不判作质量通过。' : '该次运行尚无结果快照，结果数据证据未知。' }}</p>
      <details class="quality-technical"><summary>来源与核对信息</summary><dl class="quality-grid"><div><dt>项目编号</dt><dd>{{ projection.projectId }}</dd></div><div><dt>分支编号</dt><dd>{{ projection.branchId }}</dd></div><div class="quality-wide"><dt>结果产物编号</dt><dd>{{ value(projection.artifactId) }}</dd></div><div class="quality-wide"><dt>结果产物哈希</dt><dd>{{ value(projection.payloadSha256) }}</dd></div><div class="quality-wide"><dt>有效配置哈希</dt><dd>{{ value(projection.configSha256) }}</dd></div><div class="quality-wide"><dt>捕获内容哈希</dt><dd>{{ value(projection.capturedSha256) }}</dd></div></dl></details>
    </template>
  </section>
</template>

<script>
import * as api from '@/api/unifiedResearch'
import { createRunQualityReader, newQualityState, qualityRoute, qualityRunProjection, qualityStatus, qualityValue, researchQualityAccount } from '@/utils/researchRunQuality'

export default {
  name: 'ResearchRunQuality',
  data () { return { state: newQualityState(), reader: null, activationSeen: false, sessionUnsubscribe: null } },
  computed: {
    accountId () { return researchQualityAccount(this.$store && this.$store.getters && this.$store.getters.userInfo) },
    dark () { return Boolean(this.$store && this.$store.state && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme)) },
    routeIdentity () { return JSON.stringify(qualityRoute(this.$route && this.$route.query)) },
    projection () { return this.state.project && this.state.branch && this.state.run ? qualityRunProjection(this.state.project, this.state.branch, this.state.run, this.state.artifact) : null },
    qualityStatusLabel () { return this.projection && ({ ok: '已记录证据字段（非策略有效性）', verified: '已记录证据字段（非策略有效性）', partial: '部分字段未核实', invalid: '输入证据异常', unavailable: '证据不可用', unknown: '未知' })[this.projection.quality.status] || '未知' },
    emptyMessage () {
      if (this.state.loading || this.state.error || this.state.permission || !this.state.caps) return ''
      if (!this.state.projects.length) return '当前账号没有已保存的研究项目；本页不会创建项目。'
      if (!this.state.project) return '请选择研究项目，查看与该项目关联的数据证据。'
      if (!this.state.branches.length) return '该项目没有可读取的分支。'
      if (!this.state.branch) return '请选择该项目的研究分支。'
      if (!this.state.runs.length) return '该分支尚无运行记录；本页不会为验收启动回测。'
      return this.state.run ? '' : '请选择具体运行；以下证据不会借用当前表单或其他分支。'
    }
  },
  watch: {
    accountId () { if (this.reader) { this.reader.invalidate(); this.reload() } },
    routeIdentity () { this.reload() }
  },
  created () {
    this.reader = createRunQualityReader({ state: this.state, api, account: () => this.accountId })
    if (this.$store && typeof this.$store.subscribe === 'function') {
 this.sessionUnsubscribe = this.$store.subscribe(mutation => {
      if (/(^|\/)SET_TOKEN$/.test(mutation.type)) this.reader.invalidate()
    })
}
    this.reload()
  },
  activated () { this.reader.activate(); if (this.activationSeen) this.reload(); this.activationSeen = true },
  deactivated () { this.reader.deactivate() },
  beforeDestroy () { this.reader.deactivate(); if (this.sessionUnsubscribe) this.sessionUnsubscribe() },
  methods: {
    value: qualityValue,
    status: qualityStatus,
    unknown (field) { return this.projection && this.projection.quality.unknownReasons[field] || '' },
    reload () { return this.reader && this.reader.load(qualityRoute(this.$route && this.$route.query)) },
    selectProject (id) { return this.reader.selectProject(id) },
    selectBranch (id) { return this.reader.selectBranch(id) },
    selectRun (id) { return this.reader.selectRun(id) }
  }
}
</script>

<style scoped lang="less">
.run-quality { --q-bg: #fff; --q-text: #23362e; --q-muted: #63746c; --q-border: #dce5df; --q-subtle: #f5f8f6; padding: 24px; background: var(--q-bg); color: var(--q-text); border: 1px solid var(--q-border); border-radius: 12px; min-width: 0; }
.run-quality-dark { --q-bg: #18221e; --q-text: #e0e9e3; --q-muted: #a7b9ad; --q-border: #35433b; --q-subtle: #202c25; }
.quality-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; h2 { margin: 0 0 8px; font-size: 20px; color: var(--q-text); } p { margin: 0; color: var(--q-muted); } }
.quality-notice { margin: 18px 0; }
.quality-selectors { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.4fr); gap: 20px; margin-top: 24px; label { display: flex; flex-direction: column; gap: 8px; min-width: 0; font-weight: 600; } .ant-select { width: 100%; font-weight: 400; } }
.quality-empty { color: var(--q-muted); padding: 24px 0 0; margin: 0; }
.quality-identity { display: flex; flex-wrap: wrap; gap: 12px 24px; margin: 24px 0 20px; padding: 16px; background: var(--q-subtle); border: 1px solid var(--q-border); border-radius: 8px; overflow-wrap: anywhere; }
.quality-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 32px; margin: 0; div { min-width: 0; } dt { color: var(--q-muted); font-weight: 400; margin-bottom: 6px; } dd { margin: 0; color: var(--q-text); overflow-wrap: anywhere; } .quality-wide { grid-column: 1 / -1; } }
.quality-grid small { display: block; color: var(--q-muted); line-height: 1.6; margin-top: 5px; }
.quality-boundary { margin: 20px 0 0; padding: 14px 16px; background: var(--q-subtle); color: var(--q-muted); line-height: 1.7; border-left: 3px solid var(--q-border); }
.quality-technical { margin-top: 20px; summary { color: var(--q-muted); cursor: pointer; } dl { padding: 18px 0 0; } }
.run-quality-dark /deep/ .ant-select-selection { color: var(--q-text); background: var(--q-subtle); border-color: var(--q-border); }
.run-quality-dark /deep/ .ant-select-arrow { color: var(--q-muted); }
.run-quality-dark /deep/ .ant-btn { color: var(--q-text); background: var(--q-subtle); border-color: var(--q-border); }
@media (max-width: 1100px) { .quality-selectors { grid-template-columns: repeat(2, minmax(0, 1fr)); label:last-child { grid-column: 1 / -1; } } }
</style>
