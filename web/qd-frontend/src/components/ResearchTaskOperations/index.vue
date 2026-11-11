<template>
  <section class="research-task-operations" :class="{ dark }" data-testid="research-task-operations">
    <header><div><h2>自己的研究运行 · 受控操作</h2><p>刷新只读取记录。对账不启动计算；重试会产生新的执行，必须逐次确认。</p></div><a-button :loading="state.loading" :disabled="state.saving" data-testid="task-operations-refresh" @click="reload">读取最新状态</a-button></header>
    <p v-if="state.loading" role="status">正在读取有权访问的运行记录…</p>
    <p v-if="state.error && !state.selected" class="task-error" role="alert" data-testid="task-operations-error">{{ state.error }}</p>
    <p v-if="!state.loading && !state.error && !state.operations.length" data-testid="task-operations-empty">当前账号尚无可读取的研究运行；本页不会为验收启动任务。</p>
    <article v-for="row in state.operations" :key="row.run.id" class="task-operation-row" data-testid="task-operation-row">
      <div><strong>运行 {{ row.run.id }}</strong><p>{{ status(row.run.status) }} · 分支 {{ value(row.run.branchId) }} · 创建 {{ value(row.run.createdAt) }}</p><p>执行 {{ value(row.run.execution && row.run.execution.id) }} · {{ value(row.run.errorMessage) }}</p><router-link :to="{ path: '/personal-reports', query: { section: 'quality', projectId: row.run.projectId || ($route.query && $route.query.projectId), branchId: row.run.branchId, runId: row.run.id } }">查看这次运行与数据证据</router-link></div>
      <div class="task-actions"><div v-for="action in row.actions" :key="action.id"><a-button :disabled="!action.enabled || state.saving || state.loading" :data-testid="'task-action-' + action.id" @click="choose(row.run.id, action.id)">{{ labels[action.id] }}</a-button><p v-if="!action.enabled">{{ action.reason || '当前状态未开放该动作。' }}</p></div><p v-if="!row.actions.length">这次运行没有可用操作。</p></div>
    </article>
    <p class="task-boundary" data-testid="task-external-boundary">本区只管理自己的研究运行。nnG8 项目请使用上方实时管理区；冻结任务不可解除。</p>
    <a-modal
      :visible="!!state.selected"
      title="确认这一次任务操作"
      :footer="null"
      :mask-closable="false"
      :closable="!state.saving"
      @cancel="controller.cancel()">
      <div class="task-confirmation" :class="{ dark }" data-testid="task-operation-confirmation">
        <template v-if="state.selected"><h3>{{ labels[state.selected.action] }}</h3><p>运行 {{ state.selected.runId }} · 执行 {{ value(state.selected.body.observedExecutionId) }} · 已读取状态 {{ status(state.selected.body.observedStatus) }}</p><p v-if="state.selected.startsExecution" class="task-boundary">重试会开始新的执行并占用计算资源，不会改写原执行。仅在没有需要保护的未保存草稿、且你确实希望重试时确认。</p><p v-else>仅对账这一运行已有的执行与持久结果；不会重跑、补算或改变策略。</p></template>
        <p v-if="state.error" class="task-error" role="alert" data-testid="task-operation-result-error">{{ state.error }}</p>
        <p v-if="state.receipt" role="status" data-testid="task-operation-receipt">服务端已返回此操作回执；请关闭确认并读取最新状态，不把操作受理当成业务成功。</p>
        <label v-if="!state.receipt"><input v-model="state.confirmed" type="checkbox" :disabled="state.saving" data-testid="task-operation-check">我已核对运行身份与动作影响，确认执行本次操作。</label>
        <div class="task-confirm-actions"><a-button :disabled="state.saving" @click="controller.cancel()">关闭</a-button><a-button
          v-if="!state.receipt"
          type="primary"
          :loading="state.saving"
          :disabled="!state.confirmed || state.saving || !!state.permission"
          data-testid="task-operation-submit"
          @click="controller.submit()">{{ state.outcomeUnknown ? '用原请求确认结果' : '确认执行' }}</a-button></div>
      </div>
    </a-modal>
  </section>
</template>

<script>
import * as api from '@/api/unifiedResearch'
import { researchQualityAccount, qualityValue, qualityStatus } from '@/utils/researchRunQuality'
import { createTaskOperationController, newTaskOperationState, TASK_ACTION_LABELS } from '@/utils/researchTaskOperations'
import { newIdempotencyKey } from '@/views/research-lab-v2/workspaceLogic'

export default {
  name: 'ResearchTaskOperations',
  data () { return { state: newTaskOperationState(), controller: null, unsubscribe: null, activatedOnce: false, labels: TASK_ACTION_LABELS } },
  computed: {
    accountId () { return researchQualityAccount(this.$store && this.$store.getters.userInfo) },
    dark () { return ['dark', 'realdark'].includes(this.$store.state.app.theme) }
  },
  watch: { accountId () { this.controller.invalidate(); this.reload() }, '$route.query.branchId' () { this.controller.invalidate(); this.reload() } },
  created () {
    this.controller = createTaskOperationController({ state: this.state, api, account: () => this.accountId, key: newIdempotencyKey })
    this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.controller.invalidate(); this.$nextTick(() => this.reload()) } })
    this.reload()
  },
  activated () { this.controller.activate(); if (this.activatedOnce) this.reload(); this.activatedOnce = true },
  deactivated () { this.controller.deactivate() },
  beforeDestroy () { this.controller.deactivate(); if (this.unsubscribe) this.unsubscribe() },
  methods: { value: qualityValue, status: qualityStatus, reload () { return this.controller.load(this.$route && this.$route.query.branchId || '') }, choose (runId, action) { this.controller.choose(runId, action) } }
}
</script>

<style scoped>
.research-task-operations, .task-confirmation { --task-ink: #26372f; --task-muted: #58675f; --task-border: #dce5df; --task-surface: #fff; --task-soft: #f1f6f3; color: var(--task-ink); }
.dark { --task-ink: #e4eee8; --task-muted: #b3c4b9; --task-border: #42574a; --task-surface: #1e2923; --task-soft: #28372e; }
.research-task-operations { padding: 22px; border: 1px solid var(--task-border); background: var(--task-surface); border-radius: 12px; margin-bottom: 22px; }
header, .task-operation-row { display: flex; justify-content: space-between; gap: 24px; } h2, h3 { color: var(--task-ink); } p { color: var(--task-muted); line-height: 1.7; overflow-wrap: anywhere; }
.task-operation-row { padding: 20px 0; border-top: 1px solid var(--task-border); } .task-operation-row > div { min-width: 0; } .task-actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; max-width: 40%; } .task-actions p { font-size: 12px; max-width: 250px; }
.task-boundary { padding: 14px; background: var(--task-soft); border-left: 3px solid var(--task-border); } .task-error { color: #aa343a; } .dark .task-error { color: #ffb2b5; }
.task-confirmation.dark { padding: 18px; background: var(--task-surface); } .task-confirm-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 22px; }
.dark /deep/ .ant-btn:not(.ant-btn-primary) { background: var(--task-soft); color: var(--task-ink); border-color: var(--task-border); }
</style>
