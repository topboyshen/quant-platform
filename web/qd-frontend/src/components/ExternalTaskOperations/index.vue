<template>
  <section class="external-projects" :class="{ dark }" data-testid="external-task-operations">
    <header class="external-head"><div><p class="eyebrow">nnG8 · 后台继续在原服务器运行</p><h2>外部项目管理</h2><p>这里读取实时状态，并按登记权限管理调度或检查任务。每次操作都会显示影响与独立回执。</p></div><a-button :loading="state.loading" :disabled="state.saving || state.querying" data-testid="external-refresh" @click="reload">读取实时状态</a-button></header>
    <p v-if="state.loading" role="status">正在连接 nnG8 并核对项目状态…</p>
    <p v-if="state.error && !state.selected" class="external-error" role="alert">{{ state.error }}</p>
    <div v-if="state.pending.length" class="pending-requests" data-testid="external-pending"><h3>需要确认结果的请求</h3><p>先查询已有请求，再决定后续动作。离开页面或断网不会生成新的执行请求。</p><div v-for="request in state.pending" :key="request.clientRequestId"><span>{{ request.label || request.taskId }} · {{ request.actionLabel || request.action }}</span><a-button :disabled="state.saving || state.querying" :data-testid="'external-resume-' + request.clientRequestId" @click="resume(request.clientRequestId)">查询这次请求</a-button></div></div>
    <div v-if="state.catalog" key="external-catalog" class="external-catalog">
      <p class="transport-note">连接：{{ connection }}。读取时间：{{ value(state.catalog.observedAt) }}。任务运行状态与业务结果分别核对。</p>
      <p v-if="!state.catalog.enabled || state.catalog.readOnly" class="external-warning">{{ state.catalog.reason || '当前身份或运行开关未开放外部操作，仍可查看已返回的状态。' }}</p>
      <p v-if="!state.catalog.tasks.length">当前接口没有返回可读取的外部项目；不能据此判断所有项目正常。</p>
      <div class="external-grid">
        <article v-for="task in state.catalog.tasks" :key="task.taskId" class="external-task" :data-testid="'external-task-' + task.taskId">
          <div class="task-title"><div><p class="eyebrow">{{ value(task.project) }}</p><h3>{{ task.label }}</h3></div><span class="state-label" :class="tone(task.frozen ? 'frozen' : task.state)">{{ status(task.frozen ? 'frozen' : task.state) }}</span></div>
          <p v-if="task.detail">{{ task.detail }}</p>
          <p v-for="unit in task.units || []" :key="unit.Id || unit.unit || unit.id || unit.name" class="unit-state">{{ value(unit.Id || unit.unit || unit.id || unit.name) }}：{{ unitStatus(unit) }} · {{ value(unit.subState || unit.sub_state || unit.SubState) }}</p>
          <dl><div><dt>上次运行</dt><dd>{{ value(task.lastRunAt) }}</dd></div><div><dt>下次计划</dt><dd>{{ value(task.nextRunAt) }}</dd></div></dl>
          <p v-if="task.lastResultStatus">上次进程结果：{{ task.lastResultStatus === 'success' ? '进程正常结束' : task.lastResultStatus }} · {{ value(task.lastFinishedAt) }}；业务结论请查看报告。</p>
          <p v-if="task.blockedReason" class="external-warning">{{ task.blockedReason }}</p>
          <router-link v-if="resultRoute(task)" :to="resultRoute(task)" :data-testid="'external-result-' + task.taskId">查看相关报告</router-link>
          <div class="external-actions"><div v-for="action in task.actions" :key="action.id"><a-button :disabled="!action.enabled || !state.catalog.enabled || state.catalog.readOnly || state.loading || state.saving || state.querying || pendingFor(task.taskId)" :data-testid="'external-action-' + task.taskId + '-' + action.id" @click="choose(task.taskId, action.id)">{{ action.label }}</a-button><p v-if="!action.enabled" class="action-reason">{{ action.reason || '当前状态或权限未开放这个动作。' }}</p></div></div>
          <p v-if="!task.actions.length" class="readonly-reason">{{ task.reason || '该项目未登记可用操作；保持只读。' }}</p>
          <p v-if="pendingFor(task.taskId)" class="external-warning">这个任务有未确认的请求，请先查询结果。</p>
          <details class="technical"><summary>任务身份与版本</summary><p>任务 {{ task.taskId }} · 类型 {{ value(task.kind) }}</p><p>状态修订 {{ value(task.revision) }}</p><p>主服务 {{ value(task.mainService) }} · 调度 {{ value(task.mainTimer) }}</p><p>结果码 {{ value(task.lastResultCode) }}。关联入口指向同一来源，未提供本次产物身份时不称为本次执行结果。</p></details>
        </article>
      </div>
      <details v-if="state.catalog.operations.length" class="recent-operations"><summary>最近操作回执（{{ state.catalog.operations.length }}）</summary><article v-for="operation in state.catalog.operations" :key="operation.operationId"><strong>{{ operation.taskId }} · {{ operation.action }}</strong><p :class="tone(operation.status)">{{ status(operation.status) }} · {{ value(operation.requestedAt) }}</p><p>{{ value(operation.detail) }}</p></article></details>
    </div>
    <a-modal
      :visible="!!state.selected"
      title="确认外部项目操作"
      :footer="null"
      :mask-closable="false"
      :closable="!state.saving && !state.querying"
      @cancel="controller.cancel()">
      <div class="external-confirmation" :class="{ dark }" data-testid="external-confirmation">
        <template v-if="state.selected"><h3>{{ state.selected.actionLabel || state.selected.action }} · {{ state.selected.label || state.selected.taskId }}</h3><p class="operation-impact">{{ state.selected.risk || '查询本次请求已经保存的执行状态。' }}</p><p v-if="state.selected.startsExecution">这个动作会占用 nnG8 的计算资源；受理后仍需核对实际完成结果。</p><details class="technical"><summary>本次操作身份</summary><p>任务 {{ state.selected.taskId }}</p><p>已读取版本 {{ state.selected.observedRevision }}</p><p>请求 {{ state.selected.clientRequestId }}</p></details></template>
        <p v-if="state.error" class="external-error" role="alert" data-testid="external-operation-error">{{ state.error }}</p>
        <div v-if="state.receipt" class="operation-receipt" :class="tone(state.receipt.status)" role="status" data-testid="external-operation-receipt"><strong>{{ status(state.receipt.status) }}</strong><p>{{ value(state.receipt.detail) }}</p><p>操作编号 {{ state.receipt.operationId }}。{{ ['accepted', 'running'].includes(state.receipt.status) ? '这是执行进度，业务结果尚未完成。' : '' }}</p></div>
        <label v-if="!state.receipt && !state.outcomeUnknown"><input v-model="state.confirmed" type="checkbox" :disabled="state.saving || state.querying" data-testid="external-confirm-check">我已核对任务、动作影响与当前版本，确认执行这次操作。</label>
        <div class="confirmation-actions"><a-button :disabled="state.saving || state.querying" data-testid="external-confirm-close" @click="controller.cancel()">关闭</a-button><a-button v-if="state.outcomeUnknown || (state.receipt && ['accepted', 'running', 'unknown'].includes(state.receipt.status))" :loading="state.querying" :disabled="state.saving || state.querying || !!state.permission" data-testid="external-query-result" @click="controller.query()">查询原请求结果</a-button><a-button
          v-else-if="!state.receipt"
          type="primary"
          :loading="state.saving"
          :disabled="!state.confirmed || state.saving || state.querying || !!state.permission"
          data-testid="external-confirm-submit"
          @click="controller.submit()">确认执行</a-button></div>
      </div>
    </a-modal>
  </section>
</template>

<script>
import * as api from '@/api/unifiedResearch'
import { researchQualityAccount, qualityValue } from '@/utils/researchRunQuality'
import { createExternalOperationController, newExternalOperationState, pendingRequestStore, EXTERNAL_STATUS } from '@/utils/externalTaskOperations'
import { newIdempotencyKey } from '@/views/research-lab-v2/workspaceLogic'

export default {
  name: 'ExternalTaskOperations',
  data () { return { state: newExternalOperationState(), controller: null, unsubscribe: null, activatedOnce: false } },
  computed: {
    accountId () { return researchQualityAccount(this.$store && this.$store.getters.userInfo) },
    dark () { return ['dark', 'realdark'].includes(this.$store.state.app.theme) },
    connection () { const transport = this.state.catalog && this.state.catalog.transport; return transport && transport.reachable === true ? (transport.network === 'private' ? '腾讯云内网可达' : '服务通道可达') : '连接状态未确认' }
  },
  watch: { accountId () { this.controller.invalidate(); this.reload() } },
  created () {
    this.controller = createExternalOperationController({ state: this.state, api, account: () => this.accountId, key: newIdempotencyKey, pending: pendingRequestStore(window.sessionStorage) })
    this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.controller.invalidate(); this.$nextTick(() => this.reload()) } })
    this.reload()
  },
  activated () { this.controller.activate(); if (this.activatedOnce) this.reload(); this.activatedOnce = true },
  deactivated () { this.controller.deactivate() },
  beforeDestroy () { this.controller.deactivate(); if (this.unsubscribe) this.unsubscribe() },
  methods: {
    value: qualityValue,
    status (value) { return EXTERNAL_STATUS[value] || '状态未知' },
    tone (value) { return value === 'succeeded' ? 'status-complete' : ['failed', 'failed_service', 'unavailable'].includes(value) ? 'status-failed' : ['unknown', 'frozen', 'paused'].includes(value) ? 'status-attention' : 'status-neutral' },
    unitStatus (unit) {
      const id = unit.Id || unit.unit || unit.id || unit.name || ''
      const state = unit.ActiveState || unit.activeState || unit.active_state
      return id.endsWith('.timer') && state === 'active' ? '调度已启用' : this.status(state)
    },
    pendingFor (id) { return this.state.pending.some(row => row.taskId === id) },
    resultRoute (task) {
      const link = task.resultLink
      if (!link || typeof link !== 'object' || Array.isArray(link) || task.resultCorrelation !== 'source_only' ||
        Object.keys(link).sort().join(',') !== 'path,query' || link.path !== '/personal-reports') return null
      const query = link.query
      if (!query || typeof query !== 'object' || Array.isArray(query) || Object.keys(query).sort().join(',') !== 'section,source' ||
        !['reports', 'quality', 'validation'].includes(query.section) || !['astock', 'ledger', 'phase2', 'data'].includes(query.source)) return null
      return { path: link.path, query: { section: query.section, source: query.source, ...(query.source === 'astock' ? { group: 'astock' } : {}) } }
    },
    reload () { return this.controller.load() },
    choose (id, action) { this.controller.choose(id, action) },
    async resume (id) { this.controller.resume(id); await this.controller.query() }
  }
}
</script>

<style scoped>
.external-projects, .external-confirmation { --ext-ink: #26372f; --ext-muted: #58675f; --ext-border: #dce5df; --ext-surface: #fff; --ext-soft: #f1f6f3; --ext-warning: #855d16; --ext-error: #aa343a; color: var(--ext-ink); }
.dark { --ext-ink: #e4eee8; --ext-muted: #b3c4b9; --ext-border: #42574a; --ext-surface: #1e2923; --ext-soft: #28372e; --ext-warning: #f5cd8b; --ext-error: #ffb2b5; }
.external-projects { padding: 24px; border: 1px solid var(--ext-border); background: var(--ext-surface); border-radius: 12px; margin-bottom: 24px; min-width: 0; }
.external-head, .task-title { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }
.external-head > div, .task-title > div { min-width: 0; }
.external-head h2 { font-size: 23px; margin: 0 0 10px; }
h2, h3, strong { color: var(--ext-ink); } p { color: var(--ext-muted); line-height: 1.7; overflow-wrap: anywhere; }
.eyebrow { font-size: 12px; margin: 0 0 7px; }
.transport-note { padding: 12px 16px; background: var(--ext-soft); border-radius: 8px; margin: 18px 0; }
.external-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.external-task { border: 1px solid var(--ext-border); border-radius: 10px; padding: 20px; min-width: 0; }
.task-title h3 { font-size: 18px; margin: 0 0 10px; } .state-label { padding: 3px 9px; border: 1px solid var(--ext-border); border-radius: 5px; font-size: 12px; white-space: nowrap; background: var(--ext-soft); }
dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 18px 0; } dt { font-weight: normal; color: var(--ext-muted); font-size: 12px; } dd { color: var(--ext-ink); margin: 5px 0 0; overflow-wrap: anywhere; }
.external-actions { display: flex; flex-wrap: wrap; gap: 12px; margin: 14px 0; } .external-actions > div { min-width: 0; max-width: 100%; } .action-reason { font-size: 12px; max-width: 260px; }
.technical { color: var(--ext-muted); margin-top: 18px; } .technical summary, .recent-operations summary { cursor: pointer; } .technical p { font-size: 12px; } .readonly-reason, .operation-impact { padding: 12px; border-left: 3px solid var(--ext-border); background: var(--ext-soft); }
.pending-requests { padding: 18px; background: var(--ext-soft); border: 1px solid var(--ext-border); border-radius: 9px; margin: 18px 0; } .pending-requests > div { display: flex; justify-content: space-between; gap: 14px; align-items: center; margin-top: 10px; }
.external-error, .status-failed { color: var(--ext-error) !important; } .external-warning, .status-attention { color: var(--ext-warning) !important; } .status-neutral { color: var(--ext-muted); } .status-complete { color: #327349; } .dark .status-complete { color: #a0dbb1; }
.recent-operations { padding: 18px; background: var(--ext-soft); margin-top: 20px; border-radius: 8px; } .recent-operations article { border-top: 1px solid var(--ext-border); padding-top: 12px; margin-top: 12px; }
.external-confirmation.dark { background: var(--ext-surface); padding: 18px; } .external-confirmation label { display: block; line-height: 1.8; } .external-confirmation input { margin-right: 8px; } .operation-receipt { padding: 14px; background: var(--ext-soft); margin: 18px 0; } .confirmation-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 22px; }
.dark /deep/ .ant-btn:not(.ant-btn-primary) { background: var(--ext-soft); color: var(--ext-ink); border-color: var(--ext-border); }
@media (min-width: 1750px) { .external-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 1100px) { .external-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
