// The browser selects identity only. The server captures and verifies the source.
export function reportIdentity (report, capturedAt) {
  const astock = report && report.source === 'nng8/astock' && /^nng8:astock:\d{4}-\d{2}-\d{2}:[a-f0-9]{64}$/.test(report.id || '')
  const profit = report && report.source === 'quant/profit' && /^quant:profit:(?:ec6c86fd856c|026a1083d9e0):[a-f0-9]{64}$/.test(report.id || '')
  const etf = report && report.source === 'quant/etf' && /^quant:etf:effectiveness:[a-f0-9]{64}$/.test(report.id || '')
  const captured = typeof capturedAt === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(capturedAt) && Number.isFinite(Date.parse(capturedAt))
  const contentV2 = astock && report.content_contract === 'nng8.report-text.v2' && report.body_complete === true &&
    /^[a-f0-9]{64}$/.test(report.source_report_sha256 || '') && report.id.endsWith(':' + report.source_report_sha256)
  if (!report || !(astock || profit || etf) ||
    !/^[a-f0-9]{64}$/.test(report.content_sha256 || '') || (!contentV2 && !report.id.endsWith(':' + report.content_sha256)) ||
    !(contentV2 ? ['available', 'partial', 'unknown', 'ok', 'pass'].includes(report.status) : (profit || etf) ? ['ok', 'pass', 'partial', 'failed'].includes(report.status) : ['ok', 'pass', 'partial'].includes(report.status)) || report.body_truncated !== false || !Array.isArray(report.blocks) || !report.blocks.length || report.body_block_count !== report.blocks.length ||
    !(captured || ((profit || etf) && capturedAt === null))) return null
  return { reportId: report.id, contentSha256: report.content_sha256, capturedAt }
}

export function singleStockProject (project) {
  const s = project && project.subject
  return !!(project && project.status !== 'archived' && s && s.kind === 'single' && s.market === 'CNStock' && s.timeframe === '1D' && /^\d{6}$/.test(s.symbol || ''))
}

export function evidenceRequest (identity, revision, clientRequestId) {
  return { expectedRevision: revision, clientRequestId, reportId: identity.reportId, contentSha256: identity.contentSha256, capturedAt: identity.capturedAt }
}

export function captureAvailable (caps, source = 'nng8/astock') {
  const local = ['quant/profit', 'quant/etf'].includes(source)
  const evidence = caps && (local ? caps.reportEvidence : caps.externalEvidence)
  if (local && (!evidence || !Array.isArray(evidence.sources) || !evidence.sources.includes(source === 'quant/profit' ? 'profit' : 'etf'))) return false
  return !!(caps && caps.enabled === true && caps.write === true && evidence && evidence.enabled === true && evidence.schemaReady === true && evidence.capture === true && evidence.contractVersion === 'research.external-evidence.v1')
}

export function linkageError (error) {
  const response = error && error.response
  const status = response && response.status
  if ([401, 403, 404].includes(status)) return { denied: true, retry: false, message: '当前账号无权继续此交接，或来源/项目已不可用。请重新确认登录与访问权限。' }
  if (status === 409) return { denied: false, retry: false, message: '来源版本或分支状态已变化，未替换为最新内容，也未留下空项目或分支。请关闭交接、刷新报告并重新确认。' }
  if (!status || status >= 500) return { denied: false, retry: true, message: '结果尚未确认。可用原请求重试确认，已有项目和分支不会因重试重复创建。' }
  return { denied: false, retry: false, message: '本次输入未通过校验，未创建项目或分支。请核对名称、标的及报告完整性后重新确认。' }
}

export function evidenceEntries (artifact, references, cache = {}) {
  if (Array.isArray(references)) {
    return references.filter(ref => ref && typeof ref.artifactId === 'string').map(ref => {
      const found = cache[ref.artifactId]
      const p = found && found.id === ref.artifactId && found.kind === 'external_evidence' && found.payload
      const verified = p && p.reportId === ref.reportId && p.contentSha256 === ref.contentSha256 && p.capturedAt === ref.capturedAt
      return { id: ref.artifactId, payload: verified ? p : null, payloadSha256: verified ? found.payloadSha256 : null, reference: ref }
    })
  }
  return artifact && artifact.kind === 'external_evidence' ? [{ id: artifact.id, payload: artifact.payload || null, payloadSha256: artifact.payloadSha256, reference: null }] : []
}
