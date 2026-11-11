<template>
  <div class="research-link">
    <button type="button" class="research-link-button" :disabled="!canOpen" data-testid="nng8-use-research" @click="open">用于研究</button>
    <span v-if="!identity" class="research-link-note">正文完整性或来源身份未确认，暂不能交接。</span>
    <a-modal
      :visible="visible"
      :title="'将这份 ' + sourceLabel + ' 用于研究'"
      :footer="null"
      :mask-closable="false"
      :closable="!saving"
      :width="680"
      @cancel="close">
      <div class="research-link-dialog" :class="{ 'research-link-dialog--dark': dark }" data-testid="nng8-research-dialog">
        <p><strong>{{ report.title }}</strong> · 报告日 {{ report.report_date || '未知' }}</p>
        <p>报告生成 {{ report.generated_at || '未知' }} · 行情截止 {{ report.data_cutoff || '未知' }}</p>
        <p class="research-link-note">服务端捕获并核对来源身份后，将保存当前可用报告副本，并新建独立分支。研究标的由你确认，原报告、已有分支和草稿均保留。</p>
        <p class="research-link-note">“用于研究”仅归档研究依据，不代表数据门禁通过或策略已具备运行资格；报告中的失败和阻断原因原样保留。</p>
        <p v-if="report.source === 'quant/etf'" class="research-link-note">保存的是 ETF 系统有效性 JSON 完整快照，不是某只股票的专属分析或报告全文；只作研究背景，不改变正式样本数。</p>
        <p v-if="report.source === 'quant/profit'" class="research-link-note">保存这份 Profit 报告原文；模拟跟踪不等于账户收益，选择研究标的不代表报告已验证该标的。</p>
        <div v-if="loading" role="status">正在读取研究项目与可用权限…</div>
        <template v-else>
          <label>研究项目</label>
          <select v-model="projectChoice" :disabled="locked" data-testid="nng8-research-project" @change="selectProject">
            <option value="">请选择…</option><option value="new">新建单标的研究项目</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }} · {{ project.subject.symbol }}</option>
          </select>
          <template v-if="projectChoice === 'new'">
            <label>项目名称<input v-model="projectName" maxlength="120" :disabled="locked" data-testid="nng8-research-project-name"></label>
            <label>A 股代码（6 位）<input v-model="symbol" maxlength="6" :disabled="locked" inputmode="numeric" data-testid="nng8-research-symbol"></label>
          </template>
          <p v-else-if="selectedProject">已确认研究对象：{{ selectedProject.subject.symbol }} · A 股日线</p>
          <label>新分支名称<input v-model="branchName" maxlength="80" :disabled="locked" data-testid="nng8-research-branch-name"></label>
          <label class="research-link-check"><input v-model="confirmed" type="checkbox" :disabled="locked" data-testid="nng8-research-confirm">我已核对报告和研究标的；本次只保存依据，不生成策略或开始回测。</label>
        </template>
        <p v-if="error" class="research-link-error" role="alert" data-testid="nng8-research-error">{{ error }}</p>
        <p v-if="workflow && workflow.branch" class="research-link-note" data-testid="nng8-research-created">已保留新分支「{{ workflow.branch.name }}」。证据{{ completed ? '已保存' : '仍待确认' }}。</p>
        <div class="research-link-actions">
          <button type="button" :disabled="saving" @click="close">{{ completed ? '关闭' : '取消 / 关闭' }}</button>
          <button v-if="completed" type="button" class="research-link-button" data-testid="nng8-research-open" @click="openWorkspace">打开这条研究分支</button>
          <button
            v-else
            type="button"
            class="research-link-button"
            :disabled="!canConfirm"
            data-testid="nng8-research-submit"
            @click="submit">{{ saving ? '保存中…' : (workflow ? '用原请求重试确认' : '确认并保存到新分支') }}</button>
        </div>
      </div>
    </a-modal>
  </div>
</template>

<script>
import * as api from '@/api/researchLabV2'
import { reportIdentity, singleStockProject, captureAvailable, linkageError } from '@/utils/nng8ResearchLink'
import { newIdempotencyKey } from '@/views/research-lab-v2/workspaceLogic'

export default {
  name: 'Nng8ResearchLink',
  props: { report: { type: Object, required: true }, capturedAt: { type: String, default: null }, accountId: { type: String, default: '' }, sourceReady: { type: Boolean, default: false } },
  data () {
    return { visible: false, loading: false, saving: false, projects: [], caps: {}, projectChoice: '', selectedProject: null, projectName: '', symbol: '', branchName: '', confirmed: false, workflow: null, completed: false, error: '', retry: false, blocked: false, generation: 0, unsubscribe: null }
  },
  computed: {
    identity () { return reportIdentity(this.report, this.capturedAt) },
    sourceIdentityKey () { return this.identity ? JSON.stringify({ reportId: this.identity.reportId, contentSha256: this.identity.contentSha256 }) : null },
    sourceLabel () { return this.report.source === 'quant/etf' ? 'ETF 验证快照' : this.report.source === 'quant/profit' ? 'Profit 报告' : 'nnG8 报告' },
    canOpen () { return !!(this.sourceReady && this.identity && this.accountId) },
    locked () { return this.saving || !!this.workflow || this.blocked },
    canConfirm () { return !this.loading && !this.saving && !this.completed && !this.blocked && captureAvailable(this.caps, this.report.source) && this.canOpen && (this.workflow ? this.retry : this.confirmed && this.branchName.trim() && (this.projectChoice === 'new' ? this.projectName.trim() && /^\d{6}$/.test(this.symbol) : singleStockProject(this.selectedProject))) },
    dark () { return Boolean(this.$store && this.$store.state && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme)) }
  },
  watch: { accountId () { this.invalidate() }, sourceReady (value) { if (!value) this.invalidate() }, sourceIdentityKey () { this.invalidate() } },
  created () {
    if (this.$store && this.$store.subscribe) this.unsubscribe = this.$store.subscribe(mutation => { if (/(^|\/)SET_TOKEN$/.test(mutation.type)) this.invalidate() })
  },
  beforeDestroy () { this.invalidate(); if (this.unsubscribe) this.unsubscribe() },
  deactivated () { this.invalidate() },
  methods: {
    invalidate () { this.generation += 1; this.visible = false; this.projects = []; this.selectedProject = null; this.workflow = null; this.completed = false; this.loading = false; this.saving = false; this.error = ''; this.caps = {} },
    session () {
      if (typeof window === 'undefined' || !window.localStorage) return this.accountId
      try {
        const info = JSON.parse(window.localStorage.getItem('User-Info') || 'null')
        return info && String(info.id) === this.accountId ? window.localStorage.getItem('Access-Token') : null
      } catch (_) { return null }
    },
    context () { return { generation: this.generation, account: this.accountId, session: this.session(), identity: this.sourceIdentityKey } },
    current (context) { return !!(context && context.identity && context.session && this.visible && this.sourceReady && context.generation === this.generation && context.account === this.accountId && context.session === this.session() && context.identity === this.sourceIdentityKey) },
    async open () {
      if (!this.canOpen) return
      this.generation += 1; this.visible = true; this.loading = true; this.saving = false; this.error = ''; this.blocked = false; this.retry = false; this.workflow = null; this.completed = false
      this.projects = []; this.projectChoice = ''; this.selectedProject = null; this.projectName = ''; this.symbol = ''; this.confirmed = false
      this.branchName = this.sourceLabel + '研究 ' + (this.report.report_date || '')
      const context = this.context()
      try {
        const [caps, projects] = await Promise.all([api.capabilities(), api.listProjects()])
        if (!this.current(context)) return
        this.caps = caps.data || {}
        this.projects = (projects.data || []).filter(singleStockProject)
        if (!captureAvailable(this.caps, this.report.source)) { this.blocked = true; this.error = '当前账号的外部证据交接尚未开放，或服务端版本/数据结构尚未就绪。报告仍可阅读；未创建项目或分支。' }
      } catch (error) { if (this.current(context)) this.fail(error) } finally { if (this.current(context)) this.loading = false }
    },
    selectProject () { this.selectedProject = this.projects.find(p => p.id === this.projectChoice) || null; this.confirmed = false },
    fail (error) { const state = linkageError(error); this.error = state.message; this.retry = state.retry; this.blocked = !state.retry; if (state.denied) { this.projects = []; this.selectedProject = null } },
    async submit () {
      if (!this.canConfirm) return
      const context = this.context()
      if (!this.current(context)) { this.invalidate(); return }
      if (!this.workflow) {
        this.workflow = {
          identity: { ...this.identity },
          requestKey: newIdempotencyKey(),
          choice: this.projectChoice,
          projectBody: { name: this.projectName.trim(), subject: { kind: 'single', market: 'CNStock', symbol: this.symbol, timeframe: '1D' }, startModule: 'ai' },
          branchName: this.branchName.trim(),
          project: null,
          branch: null,
          body: null
        }
      }
      const work = this.workflow
      this.saving = true; this.error = ''; this.retry = false
      try {
        if (!work.body) {
          let target = { name: work.projectBody.name, subject: work.projectBody.subject }
          if (work.choice !== 'new') {
            const response = await api.getProject(work.choice)
            if (!this.current(context)) return
            const project = response.data && response.data.project
            if (!singleStockProject(project) || project.id !== work.choice) throw new Error('Unexpected project identity')
            const parentResponse = await api.getBranch(project.defaultBranchId)
            if (!this.current(context)) return
            const parent = parentResponse.data
            if (!parent || parent.projectId !== project.id || parent.id !== project.defaultBranchId) throw new Error('Unexpected parent identity')
            target = { projectId: project.id, parentBranchId: parent.id, expectedParentRevision: parent.revision }
          }
          work.body = { ...work.identity, target, branchName: work.branchName, clientRequestId: work.requestKey }
        }
        const response = await api.externalHandoff(work.body)
        if (!this.current(context)) return
        const result = response.data
        const artifact = result && result.artifact
        const payload = artifact && artifact.payload
        const captureTimeValid = payload && (typeof payload.capturedAt === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(payload.capturedAt) && Number.isFinite(Date.parse(payload.capturedAt)) || payload.capturedAt === null && this.report.source !== 'nng8/astock')
        if (!result || !singleStockProject(result.project) || (work.choice !== 'new' && result.project.id !== work.choice) || (work.choice === 'new' && result.project.subject.symbol !== work.projectBody.subject.symbol) || !result.branch || result.branch.projectId !== result.project.id || !artifact || artifact.kind !== 'external_evidence' || artifact.branchId !== result.branch.id || !payload ||
          payload.reportId !== work.identity.reportId || payload.contentSha256 !== work.identity.contentSha256 || !captureTimeValid || result.branch.heads.evidence !== artifact.id) throw new Error('Unexpected evidence identity')
        work.project = result.project; work.branch = result.branch; this.completed = true
      } catch (error) { if (this.current(context)) this.fail(error) } finally { if (this.current(context)) this.saving = false }
    },
    close () { if (!this.saving) this.invalidate() },
    openWorkspace () {
      const context = this.context()
      if (!this.completed || !this.current(context) || !this.workflow || !this.workflow.branch) return
      const work = this.workflow
      // Opening navigates only. It does not write drafts, invoke AI, or start a run.
      this.$router.push({ path: '/research-lab', query: { project: work.project.id, branch: work.branch.id, module: 'ai' } }).catch(() => {})
    }
  }
}
</script>

<style scoped>
.research-link { margin: 14px 0; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }.research-link-button { background: #256745; color: #fff; border: 1px solid #256745; border-radius: 6px; padding: 7px 14px; cursor: pointer; }.research-link button:disabled, .research-link-dialog button:disabled { opacity: .5; cursor: not-allowed; }.research-link-note { color: #66756b; font-size: 12px; }.research-link-dialog { color: #283b30; overflow-wrap: anywhere; padding: 4px; }.research-link-dialog label { display: block; margin-top: 14px; }.research-link-dialog input:not([type=checkbox]), .research-link-dialog select { display: block; width: 100%; background: #fff; color: #283b30; border: 1px solid #b5c9bb; border-radius: 5px; padding: 8px; margin-top: 5px; }.research-link-check { line-height: 1.8; }.research-link-error { color: #af3232; background: #fff0ef; padding: 10px; margin-top: 12px; border-radius: 5px; }.research-link-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; margin-top: 20px; }.research-link-actions button { border-radius: 6px; padding: 7px 14px; border: 1px solid #8aa694; cursor: pointer; }.research-link-dialog--dark { color: #dfece4; background: #202923; padding: 14px; border-radius: 6px; }.research-link-dialog--dark .research-link-note { color: #b7c8bd; }.research-link-dialog--dark input:not([type=checkbox]), .research-link-dialog--dark select { background: #29372e; color: #dfece4; border-color: #5c7866; }.research-link-dialog--dark .research-link-error { color: #ffc0bb; background: #4a2929; }
</style>
