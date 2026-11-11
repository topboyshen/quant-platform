// Read-only projection of the existing V2 GET contract. No global-source fallbacks.
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
const text = value => typeof value === 'string' && value.trim() && !['unknown', 'null', 'undefined'].includes(value.trim().toLowerCase()) ? value : null
const day = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, date] = value.split('-').map(Number)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year > 0 && month >= 1 && month <= 12 && date >= 1 && date <= days[month - 1] ? value : null
}

export function researchQualityAccount (info) {
  const id = info && info.id
  return (typeof id === 'number' && Number.isSafeInteger(id) && id > 0) || (typeof id === 'string' && /^[1-9]\d*$/.test(id)) ? String(id) : ''
}

export function qualityValue (value) {
  return value === null || value === undefined || value === '' ? '未知' : value
}

export function qualityRoute (query) {
  const value = object(query)
  return Object.fromEntries(['projectId', 'branchId', 'runId'].map(key => [key, typeof value[key] === 'string' ? value[key] : '']))
}

export function qualityStatus (status) {
  return ({ succeeded: '已完成', failed: '失败', interrupted: '已中断', starting: '启动中', running: '运行中', accepted: '已受理', pending: '待执行' })[status] || '未知'
}

export function qualityError (error) {
  const response = error && error.response
  const status = (response && response.status) || (error && error.status)
  if (status === 401) return { denied: true, permission: 'login', message: '登录身份已失效；旧项目与运行数据已清除，请重新登录。' }
  if (status === 403 || status === 404) return { denied: true, permission: 'denied', message: '当前账号无权读取该研究记录，或记录/功能不存在；旧数据已清除。' }
  return { denied: false, permission: '', message: text(error && error.message) || '读取失败；这不表示没有研究记录。' }
}

export function qualityResponse (response) {
  if (!response || response.code !== 1 || !Object.prototype.hasOwnProperty.call(response, 'data')) throw new Error('研究接口未返回可确认的成功结果。')
  return response.data
}

export function recordedDataQuality (run, artifact) {
  const r = object(run); const a = object(artifact); const payload = object(a.payload)
  const data = object(r.dataQuality || payload.dataQuality)
  if (!['research.run-data-quality.v1', 'research.run-data-quality.v2'].includes(data.contractVersion)) return { available: false, status: 'unknown', reason: '这次运行没有已登记的数据质量字段；旧记录不补写、不回算。', unknownReasons: {} }
  if (data.evidenceScope !== 'selected_run' || data.runId !== r.id ||
    !Object.prototype.hasOwnProperty.call(data, 'executionId') || data.executionId !== (text(object(r.execution).id) || null) ||
    (data.dataFingerprint && payload.dataFingerprint && data.dataFingerprint !== payload.dataFingerprint)) throw new Error('数据质量证据与所选运行、执行或内容指纹不一致，未展示。')
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null
  const source = object(data.calendarSource)
  const calendar = text(data.calendarSource) || text(source.label) || text(source.url)
  const ratio = calendar && count(data.expectedBars) > 0 && typeof data.coverageRatio === 'number' && Number.isFinite(data.coverageRatio) && data.coverageRatio >= 0 && data.coverageRatio <= 1 ? data.coverageRatio : null
  const unknownReasons = Object.fromEntries(Object.entries(object(data.unknownReasons)).filter(([key, value]) => typeof key === 'string' && text(value)))
  return {
    available: true,
status: ['ok', 'verified', 'partial', 'unavailable', 'unknown', 'invalid'].includes(data.status) ? data.status : 'unknown',
    provider: text(data.provider),
adjustment: text(data.adjustmentMode),
dataVersion: text(data.dataVersion),
metricsVersion: text(data.metricsVersion),
    observedBars: count(data.observedBars),
expectedBars: calendar ? count(data.expectedBars) : null,
missingBars: calendar ? count(data.missingBars) : null,
invalidBars: count(data.invalidBars),
duplicateBars: count(data.duplicateBars),
    coverage: ratio,
missingDates: calendar && count(data.missingBars) !== null && Array.isArray(data.missingDates) && data.missingDates.every(value => day(value)) ? data.missingDates.map(day) : null,
    calendarSource: calendar,
    calendarEvidence: source,
    exchangeExpectedBars: count(data.exchangeExpectedBars),
    rawRows: count(data.rawRows),
    parsedRows: count(data.parsedRows),
    rejectedRows: count(data.rejectedRows),
    consumedRows: count(data.consumedRows),
    acquiredAt: text(data.acquiredAt),
    parserVersion: text(data.parserVersion),
    rawResponseFingerprint: text(data.rawResponseFingerprint),
    rawResponseFingerprintScope: text(data.rawResponseFingerprintScope),
    inputSnapshot: object(data.inputSnapshot),
    listingLifecycle: object(data.listingLifecycle),
    suspensionEvidence: object(data.suspensionEvidence),
    rejectionSamples: Array.isArray(data.rejectionSamples) ? data.rejectionSamples : [],
    gapDetails: Array.isArray(data.gapDetails) ? data.gapDetails : [],
    coverageStatus: text(data.coverageStatus),
marketCutoff: day(data.marketCutoff),
inputRevision: text(data.inputRevision),
dataFingerprint: text(data.dataFingerprint),
    actual: Array.isArray(data.actualRange) && data.actualRange.length === 2 ? data.actualRange.map(day) : [null, null],
    unknownReasons,
reason: text(data.evidenceReason) || (data.status === 'ok' ? '字段来自这次运行保存的数据证据；不表示策略有效。' : '部分字段未记录或缺少权威日历，不能判为数据完整。')
  }
}

export function qualityRunProjection (project, branch, run, artifact) {
  const p = object(project); const b = object(branch); const r = object(run); const a = object(artifact)
  if (!p.id || b.projectId !== p.id || r.branchId !== b.id) throw new Error('研究项目、分支与运行身份不一致，未展示结果。')
  if (artifact && (a.id !== r.resultArtifactId || a.kind !== 'run_result' || a.branchId !== b.id || object(a.payload).requestId !== r.id)) {
    throw new Error('结果快照与本次运行身份不一致，未展示结果。')
  }
  if (artifact && object(r.execution).id && object(a.payload).executionId !== r.execution.id) {
    throw new Error('结果快照与本次执行身份不一致，未展示结果。')
  }
  const cfg = object(r.effectiveConfig); const payload = object(a.payload); const range = object(payload.dataRange)
  const quality = recordedDataQuality(r, a)
  const actual = quality.available ? quality.actual : Array.isArray(range.actual) && range.actual.length === 2 ? range.actual.map(day) : [null, null]
  return {
    projectId: p.id,
projectName: text(p.name),
branchId: b.id,
branchName: text(b.name),
runId: r.id,
    symbol: text(object(p.subject).symbol),
name: text(object(p.subject).name),
market: text(object(p.subject).market),
timeframe: text(object(p.subject).timeframe),
    status: qualityStatus(r.status),
rawStatus: text(r.status),
requested: [day(cfg.startDate), day(cfg.endDate)],
actual,
    dataFingerprint: quality.available ? quality.dataFingerprint : text(payload.dataFingerprint),
dataVersion: quality.available ? quality.dataVersion : text(payload.dataVersion),
    metricsVersion: quality.available ? quality.metricsVersion : text(payload.metricsVersion),
adjustment: quality.available ? quality.adjustment : text(payload.adjustmentMode),
    configSha256: text(r.effectiveConfigSha256),
capturedSha256: text(r.capturedSha256),
    payloadSha256: text(a.payloadSha256),
artifactId: text(a.id),
resultSavedAt: text(a.createdAt),
    createdAt: text(r.createdAt),
finishedAt: text(r.finishedAt),
errorMessage: text(r.errorMessage),
    hasResult: !!artifact,
    // Counts and dates are owned saved-run evidence, never sampled equity points or global summaries.
    quality,
    coverage: quality.available ? quality.coverage : null,
missingBars: quality.available ? quality.missingBars : null
  }
}

export function newQualityState () {
  return {
 projects: [],
project: null,
branches: [],
branch: null,
runs: [],
run: null,
artifact: null,
    projectChoice: '',
branchChoice: '',
runChoice: '',
loading: false,
stage: '',
error: '',
permission: '',
caps: null
}
}

// One generation covers success, failure and finally. The account is rechecked
// without reading credentials; the component also observes session replacement.
export function createRunQualityReader ({ state, api, account }) {
  let generation = 0
  let active = true
  function clear () { Object.assign(state, newQualityState()) }
  function invalidate () { generation += 1; clear() }
  function begin (stage) {
    const context = { generation: ++generation, account: account() }
    state.loading = true; state.stage = stage; state.error = ''; state.permission = ''
    return context
  }
  const current = ctx => active && ctx.account && ctx.account === account() && ctx.generation === generation
  function fail (ctx, error) {
    if (!current(ctx)) return
    const failure = qualityError(error)
    if (failure.denied) { clear(); generation += 1 }
    state.error = failure.message; state.permission = failure.permission
  }
  function finish (ctx) { if (current(ctx)) { state.loading = false; state.stage = '' } }
  function ready () { return active && account() && state.caps && state.caps.enabled === true && state.caps.read === true }
  function array (value, label) {
    if (!Array.isArray(value)) throw new Error(label + '读取结果不符合接口契约。')
    return value
  }
  function clearRun () { state.run = null; state.artifact = null; state.runChoice = '' }
  function clearBranch () { state.branch = null; state.branches = []; state.branchChoice = ''; state.runs = []; clearRun() }
  async function routeSelection (ctx, raw) {
    const route = qualityRoute(raw)
    if (!route.projectId && !route.branchId && !route.runId) return
    let run = null; let branch = null; let projectId = route.projectId
    if (route.runId) {
      run = object(qualityResponse(await api.getRun(route.runId))).run
      if (!current(ctx)) return
      if (!run || run.id !== route.runId || !run.branchId || (route.branchId && route.branchId !== run.branchId)) throw new Error('回测链接与服务端运行归属不一致。')
    }
    if (route.branchId || run) {
      const branchId = route.branchId || run.branchId
      branch = qualityResponse(await api.getBranch(branchId))
      if (!current(ctx)) return
      if (!branch || branch.id !== branchId || !branch.projectId || (projectId && projectId !== branch.projectId)) throw new Error('分支链接与服务端项目归属不一致。')
      projectId = branch.projectId
    }
    const details = qualityResponse(await api.getProject(projectId))
    if (!current(ctx)) return
    if (!details || !details.project || details.project.id !== projectId) throw new Error('研究项目响应身份不一致。')
    const branches = array(details.branches, '分支列表')
    if (branches.some(row => !row || row.projectId !== projectId)) throw new Error('分支列表包含不一致的项目身份。')
    const selected = branch && branches.find(row => row.id === branch.id)
    if (branch && !selected) throw new Error('运行所属分支不在项目返回的分支列表内。')
    let runs = []; let artifact = null
    if (selected) {
      runs = array(qualityResponse(await api.listRuns(selected.id)), '运行列表')
      if (!current(ctx)) return
      if (runs.some(row => !row || row.branchId !== selected.id)) throw new Error('运行列表包含不一致的分支身份。')
    }
    if (run && run.resultArtifactId) {
      artifact = qualityResponse(await api.getArtifact(run.resultArtifactId))
      if (!current(ctx)) return
    }
    if (run) qualityRunProjection(details.project, selected, run, artifact)
    state.project = details.project; state.projectChoice = projectId; state.branches = branches
    state.branch = selected || null; state.branchChoice = selected ? selected.id : ''; state.runs = runs
    state.run = run; state.runChoice = run ? run.id : ''; state.artifact = artifact
    if (!state.projects.some(row => row.id === projectId)) state.projects = [...state.projects, details.project]
  }
  return {
    invalidate,
    deactivate () { active = false; invalidate() },
    activate () { active = true },
    async load (route = {}) {
      invalidate()
      if (!active) return
      if (!account()) { state.permission = 'login'; state.error = '请登录后读取自己的研究项目。'; return }
      const ctx = begin('正在读取研究权限和项目…')
      try {
        const caps = qualityResponse(await api.capabilities())
        if (!current(ctx)) return
        if (!caps || caps.enabled !== true || caps.read !== true) { state.permission = 'denied'; state.error = '研究工作区读取权限尚未开放。'; return }
        state.caps = caps
        const projects = array(qualityResponse(await api.listProjects()), '研究项目列表')
        if (!current(ctx)) return
        if (projects.some(row => !row || typeof row.id !== 'string')) throw new Error('研究项目列表包含不合法身份。')
        state.projects = projects
        await routeSelection(ctx, route)
      } catch (error) { fail(ctx, error) } finally { finish(ctx) }
    },
    async selectProject (id) {
      if (!ready()) return
      state.project = null; state.projectChoice = id || ''; clearBranch()
      const ctx = begin('正在读取该项目的分支…')
      try {
        if (!id) return
        if (!state.projects.some(row => row.id === id)) throw new Error('请选择当前账号的研究项目。')
        const details = qualityResponse(await api.getProject(id))
        if (!current(ctx)) return
        if (!details || !details.project || details.project.id !== id) throw new Error('研究项目响应身份不一致。')
        const branches = array(details.branches, '分支列表')
        if (branches.some(row => !row || row.projectId !== id)) throw new Error('分支响应身份不一致。')
        state.project = details.project; state.branches = branches
      } catch (error) { fail(ctx, error) } finally { finish(ctx) }
    },
    async selectBranch (id) {
      if (!ready() || !state.project) return
      state.branch = null; state.branchChoice = id || ''; state.runs = []; clearRun()
      const ctx = begin('正在读取该分支的运行记录…')
      const projectId = state.project.id
      try {
        if (!id) return
        const branch = state.branches.find(row => row.id === id && row.projectId === projectId)
        if (!branch) throw new Error('请选择当前项目的分支。')
        const runs = array(qualityResponse(await api.listRuns(id)), '运行列表')
        if (!current(ctx)) return
        if (runs.some(row => !row || row.branchId !== id)) throw new Error('运行列表与所选分支不一致。')
        state.branch = branch; state.runs = runs
      } catch (error) { fail(ctx, error) } finally { finish(ctx) }
    },
    async selectRun (id) {
      if (!ready() || !state.project || !state.branch) return
      clearRun(); state.runChoice = id || ''
      const ctx = begin('正在读取该次运行的数据证据…')
      const project = state.project; const branch = state.branch
      try {
        if (!id) return
        if (!state.runs.some(row => row.id === id && row.branchId === branch.id)) throw new Error('请选择当前分支的运行记录。')
        const run = object(qualityResponse(await api.getRun(id))).run
        if (!current(ctx)) return
        if (!run || run.id !== id || run.branchId !== branch.id) throw new Error('运行响应身份不一致。')
        let artifact = null
        if (run.resultArtifactId) {
          artifact = qualityResponse(await api.getArtifact(run.resultArtifactId))
          if (!current(ctx)) return
        }
        qualityRunProjection(project, branch, run, artifact)
        state.run = run; state.artifact = artifact
      } catch (error) { fail(ctx, error) } finally { finish(ctx) }
    }
  }
}
