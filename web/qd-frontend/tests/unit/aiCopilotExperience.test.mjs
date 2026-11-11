import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const read = relativePath => fs.readFileSync(
  fileURLToPath(new URL(relativePath, import.meta.url)),
  'utf8'
)

const copilot = read('../../src/views/ai-analysis/components/CopilotWorkbench.vue')
const selectLang = read('../../src/components/SelectLang/index.jsx')
const zh = read('../../src/locales/lang/zh-CN.js')
const en = read('../../src/locales/lang/en-US.js')
const copilotOverrides = read('../../src/locales/copilot-overrides.js')

test('stream completion cancels the reader and releases the sending state', () => {
  assert.match(copilot, /handleStreamEvent\(part, assistantMsg\) === 'done'/)
  assert.match(copilot, /await reader\.cancel\(\)/)
  assert.match(copilot, /await this\.sendMessageStream[\s\S]*?this\.sending = false/)
})

test('strategy generation reads artifact code from the API data envelope', () => {
  assert.match(copilot, /const code = this\.extractStrategyCode\(res\)/)
  assert.match(copilot, /\[data\.code, data\.source, data\.strategy_code, res\.code\]/)
  assert.match(copilot, /typeof value === 'string' && value\.trim\(\)/)
  assert.doesNotMatch(copilot, /sessionStorage\.setItem\('qd_strategy_source', res\.code\)/)
})

test('switching conversations clears workflow and composer state', () => {
  assert.match(copilot, /async loadHistory \(sessionId\) \{\s*this\.resetComposerDraft\(\)/)
  assert.match(copilot, /newSession \(\) \{\s*this\.resetComposerDraft\(\)/)
  for (const field of ['draft', 'attachments', 'draftContextLock', 'pendingAgentTask', 'monitorSetupDraft']) {
    assert.match(copilot, new RegExp(`this\\.${field} =`))
  }
})

test('language selector is click and keyboard accessible', () => {
  assert.match(selectLang, /trigger=\{\['click'\]\}/)
  assert.match(selectLang, /role="button" tabIndex="0" onKeydown=\{handleKeydown\}/)
  assert.match(selectLang, /event\.key === 'Enter' \|\| event\.key === ' '/)
})

test('copilot action and event labels exist in both primary locales', () => {
  const keys = [
    'eventMetaSeparator',
    'impactHigh',
    'impactMedium',
    'impactLow',
    'usedThisTurn',
    'rememberPreference'
  ]
  for (const key of keys) {
    const token = `"aiAssetAnalysis.copilot.${key}"`
    assert.ok(zh.includes(token), `missing zh-CN key: ${key}`)
    assert.ok(en.includes(token), `missing en-US key: ${key}`)
  }
})

test('desktop breakpoint keeps history, chat, and watchlist in one row', () => {
  assert.match(
    copilot,
    /@media \(max-width: 1360px\)[\s\S]*?grid-template-columns: minmax\(210px, 240px\) minmax\(460px, 1fr\) minmax\(230px, 260px\)/
  )
})

test('analysis quick action is localized for every selectable language', () => {
  const localeCount = (copilotOverrides.match(/^ {2}"[a-z]{2}-[A-Z]{2}": \{/gm) || []).length
  const promptCount = (copilotOverrides.match(/"aiAssetAnalysis\.copilot\.analysisPromptTemplate"/g) || []).length

  assert.equal(localeCount, 11)
  assert.equal(promptCount, localeCount)
  assert.match(copilot, /i18nText\('aiAssetAnalysis\.copilot\.analysisPromptTemplate'/)
  assert.match(copilot, /'\$i18n\.locale' \(\) \{\s*this\.refreshLocalizedDraft\(\)/)
  assert.match(copilot, /localizedDraft: \{ type: 'analysis', target: analysisTarget \}/)
  for (const key of [
    'focusSymbol',
    'quickTasks.indicator_research.label',
    'quickTasks.strategy_research.label',
    'quickTasks.trade_plan.label',
    'quickTasks.macro_economic_data.label'
  ]) {
    const matches = copilotOverrides.match(new RegExp(`"aiAssetAnalysis\\.copilot\\.${key.replaceAll('.', '\\.')}"`, 'g')) || []
    assert.equal(matches.length, 9, `missing non-English overrides for ${key}`)
  }
  assert.match(copilot, /uploadImageLabel \(\) \{\s*return this\.text\.uploadChart/)
})

test('personal reports cannot expose or execute the unavailable PDF action', () => {
  assert.match(copilot, /if \(isCopilotActionAllowed\('export_report_pdf', this\.personalMode\)\) \{\s*actions\.push/)
  assert.doesNotMatch(copilot, /if \(!isCopilotActionAllowed\('export_report_pdf', this\.personalMode\)\) return \[\]/)
  assert.match(copilot, /async exportReportPdf \(reportId\) \{\s*if \(!isCopilotActionAllowed\('export_report_pdf', this\.personalMode\)\)/)
  assert.match(copilot, /isCopilotActionAllowed\(action\.type, this\.personalMode\)/)
  assert.match(copilot, /const blob = await exportChatReportPdf\(/)
})

test('decision research brief reaches fast analysis and both native generation prompts', () => {
  assert.match(copilot, /const decisionResearchBrief = this\.decisionResearchBriefFor\(requestTarget\)[\s\S]*?params\.decision_research_brief = decisionResearchBrief[\s\S]*?fastAnalyze\(params\)/)
  assert.match(copilot, /buildNativeStrategyGenerationPrompt \(targetType, prompt, target\)[\s\S]*?const decisionResearchBrief = this\.decisionResearchBriefFor\(target\)[\s\S]*?decisionResearchBrief,[\s\S]*?userRequirement/)
  assert.match(copilot, /buildNativeStrategyGenerationPrompt\('indicator', prompt, target\)/)
  assert.match(copilot, /buildNativeStrategyGenerationPrompt\('script', prompt, target\)/)
})

test('old generated artifacts restore their captured target before editor navigation', () => {
  const actionRunner = copilot.slice(
    copilot.indexOf('    runMessageAction (action, msg = null) {'),
    copilot.indexOf('    hydrateMessageAction (action, msg = null) {')
  )
  const validation = actionRunner.indexOf('this.messageActionArtifactTarget(normalizedAction, msg)')
  const artifactStorage = actionRunner.indexOf('sessionStorage.setItem(normalizedAction.storageKey, value)')
  const navigation = actionRunner.indexOf('this.$router.push')
  assert.ok(validation >= 0 && validation < artifactStorage)
  assert.ok(artifactStorage < navigation)
  assert.match(actionRunner, /if \(!artifactHandoff && !reportHandoff\) continueDecisionContext/)
  assert.doesNotMatch(actionRunner, /writeDecisionContext/)

  assert.match(copilot, /messageActionArtifactTarget \(action = \{\}, msg = null\)[\s\S]*?action\.artifactTarget[\s\S]*?qd_copilot_indicator_meta[\s\S]*?qd_copilot_script_strategy_meta[\s\S]*?query\.market[\s\S]*?query\.symbol/)
  assert.match(copilot, /clearMessageArtifactStorage \(action = \{\}\)[\s\S]*?sessionStorage\.removeItem\(key\)/)
  assert.match(copilot, /path: '\/indicator-ide',[\s\S]{0,260}?artifactTarget: \{ market: target\.market, symbol: target\.symbol/)
  assert.match(copilot, /path: '\/strategy-ide',[\s\S]{0,260}?artifactTarget: \{ market: target\.market, symbol: target\.symbol/)
  assert.doesNotMatch(
    copilot,
    /inferScriptDraftMetaFromMessage[\s\S]{0,260}?this\.context\.(?:market|symbol)/
  )
})

test('late A analysis completion cannot retarget a selector and canonical context already on B', () => {
  assert.match(copilot, /const reportTarget = this\.captureReportTarget\(target\)/)
  assert.match(copilot, /reportTarget: reportTarget|reportTarget,/)
  assert.match(copilot, /captureReportTarget[\s\S]{0,520}?Object\.freeze/)
  assert.match(copilot, /rememberReportAnalysisIfCurrent \(msg, result = \{\}\)[\s\S]{0,260}?currentSelectionMatchesReportTarget\(target\)[\s\S]{0,220}?rememberAnalysisDecision/)
  assert.match(copilot, /assistantMsg\.actions = this\.reportActions\(assistantMsg\)[\s\S]{0,120}?this\.rememberReportAnalysisIfCurrent\(assistantMsg, result\)/)
  assert.match(copilot, /return \{[\s\S]{0,100}?\.\.\.data,[\s\S]{0,100}?market: requestTarget\.market,[\s\S]{0,100}?symbol: requestTarget\.symbol/)
  assert.match(copilot, /const request = this\.beginReportRequest\(assistantMsg, reportTarget\)/)
  assert.match(copilot, /onSettled: \(\) => this\.finishReportRequest\(request\)/)
})

test('Radar asset name survives report completion and report actions', () => {
  const nameResolution = copilot.slice(
    copilot.indexOf('    sameLocalTarget (left, right) {'),
    copilot.indexOf('    reconcilePendingDraftTarget (previousTarget, nextTarget) {')
  )
  assert.match(nameResolution, /return sameCopilotTarget\(left, right\)/)
  assert.match(nameResolution, /return informativeCopilotTargetName\(value, symbol\)/)
  assert.match(nameResolution, /return resolveCopilotTargetName\(target, \{[\s\S]*?current: this\.context,[\s\S]*?watchlist:[\s\S]*?canonical: readDecisionContext/)

  const localTarget = copilot.slice(
    copilot.indexOf('    setLocalContextTarget (value) {'),
    copilot.indexOf('    reconcilePendingDraftTarget (previousTarget, nextTarget) {')
  )
  assert.match(localTarget, /const selectedTarget = \{[\s\S]*?name: this\.preferredTargetName\(target\)/)
  assert.match(localTarget, /this\.context = \{[\s\S]*?market: selectedTarget\.market,[\s\S]*?symbol: selectedTarget\.symbol,[\s\S]*?name: selectedTarget\.name/)
  assert.match(localTarget, /return selectedTarget/)
  assert.match(localTarget, /this\.context = \{ market: '', symbol: '', name: '' \}/)

  const applyContext = copilot.slice(
    copilot.indexOf('    applyDecisionContext (options = {}) {'),
    copilot.indexOf('    rememberWatchDecision (item) {')
  )
  assert.match(applyContext, /this\.setLocalContextTarget\(\{[\s\S]*?name: target\.name \|\| decision\.name \|\| target\.symbol/)
  assert.match(copilot, /handleSymbolChange \(value\)[\s\S]{0,520}?this\.setLocalContextTarget\(/)
  assert.match(copilot, /selectWatch \(item\)[\s\S]{0,180}?this\.setLocalContextTarget\(normalized\)/)

  const incomingPrompt = copilot.slice(
    copilot.indexOf('    applyIncomingCopilotPrompt () {'),
    copilot.indexOf('    quickTaskPromptKey (id) {')
  )
  assert.match(incomingPrompt, /this\.setLocalContextTarget\(target\)/)
  assert.doesNotMatch(incomingPrompt, /this\.context\.(?:market|symbol|name)\s*=/)

  const watchRestore = copilot.slice(
    copilot.indexOf('    restoreSelectedWatchName () {'),
    copilot.indexOf('    rememberAnalysisDecision (item) {')
  )
  assert.match(watchRestore, /this\.setLocalContextTarget\(watched\)/)
  assert.match(watchRestore, /!this\.informativeTargetName\(canonical\.name, canonical\.symbol\)/)
  assert.match(watchRestore, /writeDecisionContext\(\{[\s\S]*?name: selected\.name/)
  assert.match(copilot, /this\.watchlist = list\.map[\s\S]{0,120}?this\.restoreSelectedWatchName\(\)/)

  const rememberAnalysis = copilot.slice(
    copilot.indexOf('    rememberAnalysisDecision (item) {'),
    copilot.indexOf('    captureReportTarget (value = {}, fallback = {}) {')
  )
  assert.match(rememberAnalysis, /target\.name !== target\.symbol[\s\S]*?sameInstrument \? current\.name : target\.name/)
  assert.match(rememberAnalysis, /name: analysisName \|\| ''/)

  const commitReport = copilot.slice(
    copilot.indexOf('    commitReportDecisionSelection (msg, target, result = {}) {'),
    copilot.indexOf('    activateReportDecision (msg, result = {}) {')
  )
  assert.match(commitReport, /const selectedAsset = Object\.freeze\(\{[\s\S]*?market: reportTarget\.market,[\s\S]*?symbol: reportTarget\.symbol/)
  assert.match(commitReport, /msg\.reportTarget = selectedAsset[\s\S]*?this\.context = \{ \.\.\.selectedAsset \}[\s\S]*?this\.decisionContext = next/)

  const activateReport = copilot.slice(
    copilot.indexOf('    activateReportDecision (msg, result = {}) {'),
    copilot.indexOf('    decisionSignalLabel (value) {')
  )
  assert.match(activateReport, /return this\.commitReportDecisionSelection\(msg, target, result\)/)

  const fetchReport = copilot.slice(
    copilot.indexOf('    async fetchProfessionalAnalysis (target) {'),
    copilot.indexOf('    isInProgressError (e) {')
  )
  assert.match(fetchReport, /name: requestTarget\.name \|\| requestTarget\.symbol/)
})

test('a keep-alive AI page cannot overwrite a newer canonical target selected in Radar', () => {
  const selectionGuard = copilot.slice(
    copilot.indexOf('    currentSelectionMatchesReportTarget (target) {'),
    copilot.indexOf('    reportAnalysisAsOf (result = {}) {')
  )
  assert.match(selectionGuard, /const componentMatches = this\.sameReportTarget\(target,/)
  assert.match(selectionGuard, /if \(!componentMatches\) return false/)
  assert.match(selectionGuard, /const canonical = readDecisionContext\(\{ allowStale: true \}\)/)
  assert.match(selectionGuard, /return this\.sameReportTarget\(target, canonical\)/)

  assert.match(copilot, /activated \(\) \{\s*this\.applyDecisionContext\(\{ reconcileDraft: true \}\)/)
  const reconciliation = copilot.slice(
    copilot.indexOf('    reconcilePendingDraftTarget (previousTarget, nextTarget) {'),
    copilot.indexOf('    applyDecisionContext (options = {}) {')
  )
  assert.match(reconciliation, /task && !task\.reportRequestId/)
  assert.match(reconciliation, /task\.type === 'market_diagnosis'[\s\S]*?this\.pendingAgentTask = \{ \.\.\.task, target: next \}/)
  assert.match(reconciliation, /this\.draftContextLock = \{ \.\.\.next, locked: true \}/)
  assert.match(reconciliation, /this\.clearPendingAgentTask\(\)/)
})

test('report request ledger keeps sending locked until every independent request settles', () => {
  const begin = copilot.slice(
    copilot.indexOf('    beginReportRequest (msg, target, kind = \'analysis\') {'),
    copilot.indexOf('    isReportRequestCurrent (msg, request) {')
  )
  assert.match(begin, /this\.reportRequestsInFlight = addCopilotReportRequestId\(this\.reportRequestsInFlight, request\)/)
  assert.match(begin, /this\.sending = true/)

  const finish = copilot.slice(
    copilot.indexOf('    finishReportRequest (request) {'),
    copilot.indexOf('    async runProfessionalAnalysis () {')
  )
  assert.match(finish, /removeCopilotReportRequestId\(this\.reportRequestsInFlight, request\)/)
  assert.match(finish, /this\.sending = this\.reportRequestsInFlight\.length > 0/)
})

test('professional report terminal states persist in a serialized target-locked pair', () => {
  const persistence = copilot.slice(
    copilot.indexOf('    buildReportPersistenceContext (message, target) {'),
    copilot.indexOf('    async removeSession (session) {')
  )
  assert.match(persistence, /selected_market: reportTarget\.market[\s\S]*?selected_symbol: reportTarget\.symbol/)
  assert.match(persistence, /symbol_context_mode: 'locked_selected_context'/)
  assert.match(persistence, /queueReportPersistence \(work\)[\s\S]*?previous\.then\(work, work\)/)
  assert.match(persistence, /persistProfessionalAnalysisTerminal \(userMsg, assistantMsg, request\)/)
  assert.match(persistence, /persistCopilotMessage\(userSnapshot, 'fast_analysis_user', target\)/)
  assert.match(persistence, /persistCopilotMessage\(assistantSnapshot, 'fast_analysis_report', target\)/)

  const execute = copilot.slice(
    copilot.indexOf('    async executeProfessionalAnalysis (userMsg, target) {'),
    copilot.indexOf('    async fetchProfessionalAnalysis (target) {')
  )
  assert.match(execute, /const outcome = await settleCopilotReportRequest/)
  assert.match(execute, /outcome\.status === 'success' \|\| outcome\.status === 'failure'/)
  assert.match(execute, /persistProfessionalAnalysisTerminal\(userMsg, assistantMsg, request\)/)

  const retry = copilot.slice(
    copilot.indexOf('    async retryProfessionalAnalysis (msg) {'),
    copilot.indexOf('    handleReportGenerateStrategy (msg, result = null) {')
  )
  assert.match(retry, /persistProfessionalAnalysisTerminal\(null, msg, request\)/)
})

test('retried A reports bind indicator, strategy, and backtest actions to A instead of current B', () => {
  const retry = copilot.slice(
    copilot.indexOf('    async retryProfessionalAnalysis (msg) {'),
    copilot.indexOf('    handleReportGenerateStrategy (msg, result = null) {')
  )
  assert.match(retry, /const target = this\.reportTargetForMessage\(msg\)/)
  assert.match(retry, /const request = this\.beginReportRequest\(msg, target, 'retry'\)/)
  assert.match(retry, /msg\.reportTarget = target/)
  assert.match(retry, /settleCopilotReportRequest\(\{[\s\S]*?this\.fetchProfessionalAnalysis\(request\.target\)/)
  assert.match(retry, /isCurrent: \(\) => this\.isReportRequestCurrent\(msg, request\)/)
  assert.match(retry, /onSuccess: result => \{[\s\S]*?msg\.actions = this\.reportActions\(msg\)/)

  const actions = copilot.slice(
    copilot.indexOf('    reportActions (msg) {'),
    copilot.indexOf('    async retryProfessionalAnalysis (msg) {')
  )
  for (const path of ['/indicator-ide', '/strategy-ide', '/backtest-center']) {
    assert.match(actions, new RegExp(`path: '${path}'`))
  }
  assert.match(actions, /group: 'report_workflow'/)
  assert.match(actions, /reportTarget: target/)
  assert.match(actions, /path: '\/indicator-ide',[\s\S]{0,140}?query: \{ market: target\.market, symbol: target\.symbol, link: 'ai' \}/)
  assert.match(actions, /path: '\/backtest-center',[\s\S]{0,160}?query: buildAIBacktestQuery\(\{ market: target\.market, symbol: target\.symbol \}\)/)

  const actionRunner = copilot.slice(
    copilot.indexOf('    runMessageAction (action, msg = null) {'),
    copilot.indexOf('    hydrateMessageAction (action, msg = null) {')
  )
  assert.match(actionRunner, /const reportHandoff = normalizedAction\.group === 'report_workflow'/)
  assert.match(actionRunner, /reportHandoff && !this\.activateReportDecision\(msg/)
  assert.match(actionRunner, /if \(!artifactHandoff && !reportHandoff\) continueDecisionContext/)
  assert.match(copilot, /@generate-strategy="handleReportGenerateStrategy\(msg, \$event\)"/)
  assert.match(copilot, /@go-backtest="handleReportGoBacktest\(msg, \$event\)"/)
  assert.doesNotMatch(
    copilot,
    /handleReport(?:GenerateStrategy|GoBacktest) \([^)]*\) \{[\s\S]{0,300}?this\.context/
  )
})
