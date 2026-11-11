import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const read = relativePath => fs.readFileSync(
  fileURLToPath(new URL(relativePath, import.meta.url)),
  'utf8'
)

const aiAsset = read('../../src/views/ai-asset-analysis/index.vue')
const copilot = read('../../src/views/ai-analysis/components/CopilotWorkbench.vue')
const router = read('../../src/config/router.config.js')
const basicLayout = read('../../src/layouts/BasicLayout.vue')
const routeView = read('../../src/layouts/RouteView.vue')
const indicator = read('../../src/views/indicator-ide/index.vue')
const strategy = read('../../src/views/strategy-ide/index.vue')
const backtest = read('../../src/views/backtest-center/index.vue')
const personalHome = read('../../src/views/personal-home/index.vue')
const contextBar = read('../../src/components/DecisionContextBar/index.vue')

test('personal AI route mounts only the guarded workbench and retires the legacy page', () => {
  assert.match(aiAsset, /<copilot-workbench personal-mode\s*\/>/)
  assert.match(aiAsset, /import CopilotWorkbench from '@\/views\/ai-analysis\/components\/CopilotWorkbench\.vue'/)
  assert.doesNotMatch(aiAsset, /AnalysisView|@\/views\/ai-analysis(?:['"])/)
  assert.match(
    router,
    /path: '\/ai-analysis\/:pageNo\(\[1-9\]\\\\d\*\)\?'[\s\S]{0,180}?redirect: '\/ai-asset-analysis'/
  )
})

test('personal workbench startup cannot request chat-only or portfolio capabilities', () => {
  const startup = copilot.slice(copilot.indexOf('  mounted () {'), copilot.indexOf('  beforeDestroy () {'))
  assert.match(
    startup,
    /if \(!this\.personalMode\) \{[\s\S]*?this\.loadSessions\(\)[\s\S]*?this\.loadAgentPreflight\(\)[\s\S]*?this\.loadAiSkills\(\)[\s\S]*?this\.loadUserMemories\(\)[\s\S]*?\}/
  )
  assert.doesNotMatch(copilot, /from ['"]@\/api\/portfolio['"]|getPortfolio|portfolioSnapshot/)
  assert.doesNotMatch(copilot, /getMonitors|addMonitor|updateMonitor|deleteMonitor/)
  for (const method of ['loadSessions', 'loadAgentPreflight', 'loadAiSkills', 'loadUserMemories', 'persistCopilotMessage']) {
    assert.match(
      copilot,
      new RegExp(`(?:async )?${method} \\([^)]*\\) \\{\\s*if \\(this\\.personalMode\\)`),
      `${method} must fail closed before its remote request`
    )
  }
})

test('personal workbench exposes only real research workflows and hides chat-only actions', () => {
  assert.match(copilot, /const allowed = new Set\(\['diagnose', 'indicator_research', 'strategy_research'\]\)/)
  assert.match(copilot, /type === 'market_diagnosis' \|\| type === 'strategy_design'/)
  assert.match(copilot, /if \(this\.personalMode\) \{[\s\S]{0,240}?this\.messages\.pop\(\)[\s\S]{0,180}?personalCapabilityUnavailable/)
  assert.match(copilot, /isCopilotActionAllowed\(action\.type, this\.personalMode\)/)
  assert.match(copilot, /isCopilotActionAllowed\('export_report_pdf', this\.personalMode\)/)
  assert.match(copilot, /if \(!this\.personalMode\) \{\s*actions\.push\(\{[\s\S]{0,240}?type: 'ask_about_report'/)
  assert.match(copilot, /getWatchlistPrices\(\{ watchlist: this\.watchlist\.map/)
  assert.doesNotMatch(copilot, /watchlist\.slice\(0,\s*24\)/)
  assert.match(copilot, /if \(String\(key \|\| ''\) === 'qd_strategy_source'\) return true/)
  assert.doesNotMatch(copilot, /sessionStorage\.setItem\('qd_strategy_source', code\)/)
})

test('route keep-alive honors route metadata and query changes do not remount workspaces', () => {
  assert.match(basicLayout, /:keep-alive="!!multiTab \|\| !!\(\$route\.meta && \$route\.meta\.keepAlive\)"/)
  assert.match(routeView, /const routeKey = \$route\.name \|\| \(matched && matched\.path\) \|\| \$route\.path/)
  assert.match(routeView, /<keep-alive>[\s\S]{0,120}?this\.keepAlive \? <router-view key=\{routeKey\} \/> : null[\s\S]{0,40}?<\/keep-alive>/)
  assert.match(routeView, /!this\.keepAlive \? <router-view key=\{routeKey\} \/> : null/)
  assert.doesNotMatch(routeView, /\$route\.fullPath/)

  for (const source of [indicator, strategy]) {
    assert.match(source, /activated \(\) \{[\s\S]{0,100}?this\.bindSaveShortcut\(\)/)
    assert.match(source, /deactivated \(\) \{\s*this\.unbindSaveShortcut\(\)/)
  }
  assert.match(indicator, /activated \(\) \{[\s\S]{0,140}?this\.syncIncomingResearchEntry\(\)/)
  assert.match(strategy, /activated \(\) \{[\s\S]{0,180}?this\.syncIncomingResearchEntry\(\{ refreshSources: true \}\)/)
  assert.match(backtest, /activated \(\) \{[\s\S]{0,180}?this\.syncIncomingResearchEntry\(\{ force: true \}\)/)
  assert.match(indicator, /handleGlobalSaveShortcut \(event\)[\s\S]{0,220}?this\.\$route\.path !== '\/indicator-ide'/)
  assert.match(strategy, /handleSaveShortcut \(event\)[\s\S]{0,220}?this\.\$route\.path !== '\/strategy-ide'/)
})

test('legacy strategy runtime resolves an explicit source and fails closed when it cannot load', () => {
  const legacyRoute = router.slice(
    router.indexOf("    path: '/strategy-runtime'"),
    router.indexOf("    path: '/user'")
  )
  assert.match(
    legacyRoute,
    /query:\s*to\.query && to\.query\.strategy_id\s*\?\s*\{\s*sourceId:\s*to\.query\.strategy_id\s*\}/
  )
  assert.doesNotMatch(legacyRoute, /\{\s*strategy_id:/)

  const strategySync = strategy.slice(
    strategy.indexOf('    async syncIncomingResearchEntry (options = {}) {'),
    strategy.indexOf('    async syncDecisionContextSource (providedContext = null, options = {}) {')
  )
  assert.match(strategySync, /!options\.force && !options\.refreshSources && key === this\.incomingResearchEntryKey/)
  assert.match(strategySync, /if \(!source\) \{[\s\S]{0,180}?return this\.failClosedRouteSource\(routeId, key\)/)
  assert.match(strategySync, /if \(!opened\) return this\.failClosedRouteSource\(routeId, key\)/)
  assert.match(strategySync, /if \(!synchronizedContext\) \{[\s\S]{0,120}?return true/)

  const failClosed = strategy.slice(
    strategy.indexOf('    failClosedRouteSource (routeId, entryKey = \'\') {'),
    strategy.indexOf('    hasMeaningfulUnsavedScriptChanges () {')
  )
  assert.match(failClosed, /this\.createNewDraft\(\{[\s\S]{0,160}?updateRoute: false/)
  assert.match(failClosed, /已停止自动选择/)
  assert.match(failClosed, /this\.incomingResearchEntryKey = entryKey[\s\S]{0,40}?return true/)

  const init = strategy.slice(
    strategy.indexOf('    async initPage () {'),
    strategy.indexOf('    async loadSources () {')
  )
  assert.ok(
    init.indexOf('await this.syncIncomingResearchEntry') < init.indexOf('this.scriptOptions[0].id'),
    'the default first source is allowed only after the explicit entry handler declines the request'
  )

  const refresh = strategy.slice(
    strategy.indexOf('    async commitRefreshSources () {'),
    strategy.indexOf('    extractSources (res) {')
  )
  assert.match(refresh, /this\.hasRouteSourceId\(\)[\s\S]{0,100}?syncIncomingResearchEntry\(\{ sourcesReady: true, force: true \}\)/)
})

test('cached AI and indicator pages refresh watchlists through a single in-flight request', () => {
  const assertWatchlistRefreshContract = (label, source, activatedEndToken, loadEndToken) => {
    const activatedStart = source.indexOf('  activated () {')
    const activated = source.slice(activatedStart, source.indexOf(activatedEndToken, activatedStart))
    assert.match(activated, /this\.loadWatchlist\(\)/, `${label} must refresh after keep-alive activation`)

    const loadStart = source.indexOf('    async loadWatchlist ({ refreshAfterPending = false } = {}) {')
    const load = source.slice(loadStart, source.indexOf(loadEndToken, loadStart))
    assert.match(load, /const currentRequest = this\._watchlistLoadPromise/)
    assert.match(load, /if \(!refreshAfterPending\) return currentRequest/)
    assert.match(load, /this\._watchlistLoadPromise = request/)
    assert.equal((load.match(/getWatchlist\(/g) || []).length, 1, `${label} must issue one request per flight`)
    assert.match(
      load,
      /finally \{[\s\S]*?if \(this\._watchlistLoadPromise === request\) this\._watchlistLoadPromise = null/
    )
  }

  assertWatchlistRefreshContract('AI', copilot, '  beforeDestroy () {', '    async loadMonitors () {')
  assertWatchlistRefreshContract('indicator', indicator, '  deactivated () {', '    reconcileIdeMarketFromWatchlist () {')
  assert.ok((copilot.match(/loadWatchlist\(\{ refreshAfterPending: true \}\)/g) || []).length >= 2)
  assert.match(indicator, /loadWatchlist\(\{ refreshAfterPending: true \}\)/)
})

test('cached research workspaces consume new entries and retry failed source matching', () => {
  const indicatorConsume = indicator.slice(
    indicator.indexOf('    consumeIncomingResearchEntry (key, context, options = {}) {'),
    indicator.indexOf('    syncIncomingResearchEntry (options = {}) {')
  )
  const indicatorActions = [
    'acceptedContext = writeDecisionContext(context)',
    'this.applyDecisionContext(acceptedContext)',
    'this.applyIndicatorRouteSelection()',
    'this.applyCopilotDraft()',
    'this.incomingResearchEntryKey = this.getIncomingResearchEntryKey(acceptedContext) || key'
  ].map(token => indicatorConsume.indexOf(token))
  assert.ok(indicatorActions.every(index => index >= 0))
  assert.deepEqual(indicatorActions, [...indicatorActions].sort((a, b) => a - b))

  const indicatorSync = indicator.slice(
    indicator.indexOf('    syncIncomingResearchEntry (options = {}) {'),
    indicator.indexOf('    writeSelectedAssetDecisionContext (asset = {}) {')
  )
  assert.match(indicatorSync, /if \(this\.aiGenerating && \(contextChanged \|\| overwritesEditor\)\)/)
  assert.match(indicatorSync, /if \(this\.codeDirty && overwritesEditor[\s\S]*?Modal\.confirm\([\s\S]*?onOk:[\s\S]*?this\.consumeIncomingResearchEntry\([\s\S]*?latest\.context/)

  const strategySync = strategy.slice(
    strategy.indexOf('    async syncIncomingResearchEntry (options = {}) {'),
    strategy.indexOf('    async syncDecisionContextSource (providedContext = null, options = {}) {')
  )
  assert.match(strategySync, /await this\.loadSources\(\)/)
  const sourceIntent = strategySync.indexOf('const routeId = this.getInitialRouteSourceId()')
  const conversionIntent = strategySync.indexOf('if (this.isIndicatorConvertRoute())')
  const copilotIntent = strategySync.indexOf('if (this.isDraftRoute() && this.hasCopilotScriptDraft())')
  const genericDraftIntent = strategySync.indexOf('if (this.isDraftRoute())', copilotIntent + 1)
  assert.ok(conversionIntent >= 0 && conversionIntent < sourceIntent,
    'strict conversion must precede an incidental saved sourceId')
  assert.ok(sourceIntent < copilotIntent && copilotIntent < genericDraftIntent)
  assert.match(strategySync, /await this\.openSource\(routeId, \{ updateRoute: false \}\)/)

  const entrySync = backtest.slice(
    backtest.indexOf('    async syncIncomingResearchEntry (options = {}) {'),
    backtest.indexOf('    renderEquityChart () {')
  )
  assert.match(entrySync, /await this\.loadSources\(\)/)
  assert.match(entrySync, /if \(!loaded\) \{[\s\S]{0,100}?this\.sourceLoadError = this\.sourceListLoadError[\s\S]{0,60}?return false/)
  assert.match(entrySync, /await this\.selectSource\(routeSourceId\)/)
  const routePreserve = entrySync.indexOf('key === this.incomingResearchEntryKey')
  const routeSelect = entrySync.indexOf('await this.selectSource(routeSourceId)')
  assert.ok(routePreserve >= 0 && routePreserve < routeSelect)
  assert.match(entrySync.slice(routePreserve, routeSelect), /this\.manifest[\s\S]*?return true/)
  assert.match(entrySync, /if \(String\(query\.source_selection \|\| ''\) === 'required' && !routeSourceId\)/)
  assert.match(entrySync, /requireExplicitSourceSelection\([\s\S]*?请手动选择已保存策略/)
  assert.doesNotMatch(backtest, /findDecisionContextSource\(this\.sources, context\)/)
})

test('one versioned decision context is visible throughout the research chain', () => {
  assert.match(copilot, /readDecisionContext\(\{ allowStale: true \}\)/)
  for (const source of [indicator, strategy, backtest]) {
    assert.match(source, /<decision-context-bar\b/)
    assert.match(source, /DecisionContextBar/)
  }
  assert.match(indicator, /applyDecisionContext \(contextValue = null\)[\s\S]{0,420}?this\.market = context\.market[\s\S]{0,180}?this\.symbol = context\.symbol/)
  assert.match(strategy, /applyDecisionContext \(contextValue = null\)[\s\S]{0,500}?market_category: context\.market[\s\S]{0,180}?symbol: context\.symbol/)
  assert.match(strategy, /findDecisionContextSource\(this\.scriptOptions, context\)/)
  assert.match(strategy, /run_config: this\.buildTradingConfig\(this\.scriptCode\)/)
  assert.match(backtest, /研究对象只说明标的，不能确定具体策略。请选择一份已保存策略后再回测。/)
  assert.match(copilot, /evidenceUpdatedAt: hasRadarEvidence[\s\S]{0,160}?current\.evidenceUpdatedAt \|\| current\.updatedAt/)
  assert.match(copilot, /this\.rememberWatchDecision\(\{[\s\S]{0,160}?watched \|\| target/)
  assert.match(copilot, /:class="\{ active: symbolOptionValue\(item\) === selectedSymbolValue \}"/)
  assert.match(contextBar, /window\.addEventListener\(DECISION_CONTEXT_CHANGED_EVENT, this\._decisionContextListener\)/)
  assert.match(contextBar, /window\.removeEventListener\(DECISION_CONTEXT_CHANGED_EVENT, this\._decisionContextListener\)/)
})

test('strategy ContextBar delegates backtesting to the current explicit source', () => {
  assert.match(strategy, /<decision-context-bar\s+:artifact-label="strategyArtifactLabel"\s+:artifact-status="strategyArtifactStatus"\s+:artifact-detail="strategyArtifactDetail"\s+@next="openBacktestCenter"\s*\/>/)
  assert.match(contextBar, /@click="handleNext"/)
  const handleNext = contextBar.slice(
    contextBar.indexOf('    handleNext () {'),
    contextBar.indexOf('    navigate (path) {')
  )
  assert.match(handleNext, /this\.\$listeners && this\.\$listeners\.next/)
  assert.match(handleNext, /this\.\$emit\('next', this\.nextStep\)/)
  assert.match(handleNext, /this\.navigate\(this\.nextStep\.path\)/)

  const backtestEntry = strategy.slice(
    strategy.indexOf('    openBacktestCenter () {'),
    strategy.indexOf('    openVersionDrawer () {')
  )
  const verify = backtestEntry.indexOf('await this.verifyScriptCode')
  const save = backtestEntry.indexOf('await this.saveScript')
  const navigate = backtestEntry.indexOf("path: '/backtest-center'")
  const explicitSource = backtestEntry.indexOf('sourceId: String(sourceId)')
  assert.ok(verify >= 0 && verify < save)
  assert.ok(save < navigate && navigate < explicitSource)
  assert.doesNotMatch(backtestEntry, /findDecisionContextSource\(this\.scriptOptions/)
})

test('Radar failures remove stale Radar data without losing a watchlist asset identity', () => {
  assert.match(personalHome, /catch \(error\) \{[\s\S]{0,260}?this\.context = \{\}/)
  assert.match(personalHome, /stored && stored\.origin === 'radar'[\s\S]{0,100}?clearDecisionContext\(\)/)
  assert.match(
    personalHome,
    /stored && stored\.origin === 'watchlist'[\s\S]{0,520}?radar: \{ signal: \{\}, risk: \{\}, market: \{\} \}[\s\S]{0,320}?evidenceUpdatedAt: stored\.evidenceUpdatedAt[\s\S]{0,100}?updatedAt: stored\.updatedAt/
  )
})

test('Radar macro card renders completeness and missing metrics without fabricated zeroes', () => {
  const macroCard = personalHome.slice(
    personalHome.indexOf('<div class="metric-label">宏观风险</div>'),
    personalHome.indexOf('<div class="metric-label">最近收盘审计</div>')
  )
  assert.match(macroCard, /\{\{ riskLevelLabel \}\}/)
  assert.match(macroCard, /VIX \{\{ riskMetric\('vix'\) \}\}/)
  assert.match(macroCard, /美债10Y \{\{ riskMetric\('us_10y'\) \}\}/)
  assert.match(personalHome, /riskPresentation \(\) \{ return getRadarRiskPresentation\(this\.risk,/)
  assert.match(personalHome, /riskLevelLabel \(\) \{ return this\.riskPresentation\.label \}/)

  const riskMetric = personalHome.slice(
    personalHome.indexOf('    riskMetric (field) {'),
    personalHome.indexOf('    levelColor (level) {')
  )
  assert.match(riskMetric, /!this\.riskPresentation\.available/)
  assert.match(riskMetric, /this\.riskPresentation\.missingFields\.includes\(field\)/)
  assert.match(riskMetric, /return '--'/)
  assert.match(riskMetric, /return formatRadarNumber\(this\.risk\[field\]\)/)
})

test('cached Radar home follows an asset changed by AI before continuing the chain', () => {
  assert.match(personalHome, /DECISION_CONTEXT_CHANGED_EVENT/)
  assert.match(
    personalHome,
    /mounted \(\) \{[\s\S]{0,220}?window\.addEventListener\(DECISION_CONTEXT_CHANGED_EVENT, this\._decisionContextListener\)/
  )
  assert.match(
    personalHome,
    /activated \(\) \{[\s\S]{0,100}?this\.refreshSelectedContext\(\)[\s\S]{0,100}?this\.loadWatchlist\(\)/
  )
  assert.match(
    personalHome,
    /beforeDestroy \(\) \{[\s\S]{0,220}?window\.removeEventListener\(DECISION_CONTEXT_CHANGED_EVENT, this\._decisionContextListener\)/
  )
  assert.match(
    personalHome,
    /refreshSelectedContext \(\) \{\s*this\.selectedContext = readDecisionContext\(\{ allowStale: true \}\)/
  )
  const continueNext = personalHome.slice(
    personalHome.indexOf('    continueNext (path) {'),
    personalHome.indexOf('    isSelected (origin, row) {')
  )
  assert.match(continueNext, /continueDecisionContext\(\{\}, \{ allowStale: true \}\)/)
  assert.match(continueNext, /this\.selectedContext = selected[\s\S]*?this\.\$router\.push\(path\)/)
})

test('a watchlist selection made during Radar startup receives the completed global snapshot', () => {
  assert.match(personalHome, /hydrateWatchlistRadarContext/)
  const loadContext = personalHome.slice(
    personalHome.indexOf('    async loadContext () {'),
    personalHome.indexOf('    async loadWatchlist () {')
  )
  assert.match(loadContext, /this\.context = response\.data\s*this\.hydrateSelectedWatchlistRadarContext\(\)/)

  const hydrateSelection = personalHome.slice(
    personalHome.indexOf('    hydrateSelectedWatchlistRadarContext () {'),
    personalHome.indexOf('    resolveMarket (item) {')
  )
  assert.match(hydrateSelection, /readDecisionContext\(\{ allowStale: true \}\)/)
  assert.match(hydrateSelection, /hydrateWatchlistRadarContext\(stored \|\| \{\}, this\.context\)/)
  assert.match(hydrateSelection, /this\.selectedContext = writeDecisionContext\(hydrated\)/)
})

test('an explicit saved strategy source atomically replaces a mismatched asset context', () => {
  assert.match(strategy, /buildSavedSourceDecisionContext/)
  assert.match(strategy, /writeDecisionContext/)
  const openSource = strategy.slice(
    strategy.indexOf('    async openSource (id, options = {}) {'),
    strategy.indexOf('    syncDecisionContextFromSource (source = {}) {')
  )
  assert.match(openSource, /this\.applySource\(source\)[\s\S]*?this\.syncDecisionContextFromSource\(source\)/)
  assert.match(
    openSource,
    /this\.syncDecisionContextFromSource\(source\)[\s\S]*?this\.lastSavedSnapshot = this\.scriptSnapshot\(\)/
  )
  assert.ok(
    openSource.indexOf('this.lastSavedSnapshot = this.scriptSnapshot()') <
      openSource.indexOf('this.writeRouteSource(this.currentSourceId)'),
    'the normalized loaded source must become the clean baseline before route handoff'
  )

  const sourceContext = strategy.slice(
    strategy.indexOf('    syncDecisionContextFromSource (source = {}) {'),
    strategy.indexOf('    applySource (source) {')
  )
  assert.match(sourceContext, /buildSavedSourceDecisionContext\(source, current \|\| \{\}\)/)
  assert.match(sourceContext, /if \(!next\) \{[\s\S]*?clearDecisionContext\(\)/)
  assert.match(sourceContext, /writeDecisionContext\(next\)/)
  assert.doesNotMatch(sourceContext, /lastSavedSnapshot/)

  const backtestEntry = strategy.slice(
    strategy.indexOf('    openBacktestCenter () {'),
    strategy.indexOf('    openVersionDrawer () {')
  )
  assert.match(backtestEntry, /this\.syncDecisionContextFromSource\(this\.currentSource \|\| \{\}\)/)
  assert.match(backtestEntry, /!context \|\| !findDecisionContextSource/)
  assert.match(backtestEntry, /this\.saveScript\(false, \{ skipUnchanged: true, silent: true, loadingMode: 'backtest' \}\)/)
  assert.doesNotMatch(backtestEntry, /lastSavedSnapshot/)
  assert.doesNotMatch(backtestEntry, /!this\.hasRouteSourceId\(\)/)

  const saveScript = strategy.slice(
    strategy.indexOf('    async saveScript (forceCreate = false, options = {}) {'),
    strategy.indexOf('    async saveHiddenScriptParams (options = {}) {')
  )
  assert.ok(
    saveScript.indexOf('!this.hasUnsavedScriptChanges') < saveScript.indexOf('updateScriptSource'),
    'an unchanged loaded source must return its existing id before any update request'
  )
})

test('a backtest source deep link reconciles canonical context before preserving or running it', () => {
  assert.match(backtest, /buildSavedSourceDecisionContext/)
  const entrySync = backtest.slice(
    backtest.indexOf('    async syncIncomingResearchEntry (options = {}) {'),
    backtest.indexOf('    renderEquityChart () {')
  )
  assert.match(entrySync, /this\.syncDecisionContextFromSource\(this\.source \|\| routeSource\)/)
  assert.match(entrySync, /await this\.selectSource\(routeSourceId\)/)

  const sourceSync = backtest.slice(
    backtest.indexOf('    syncDecisionContextFromSource (source = {}) {'),
    backtest.indexOf('    async selectSource (sourceId, options = {}) {')
  )
  assert.match(sourceSync, /buildSavedSourceDecisionContext\(source, current \|\| \{\}\)/)
  assert.match(sourceSync, /if \(!next\) \{[\s\S]*?clearDecisionContext\(\)/)
  assert.match(sourceSync, /writeDecisionContext\(next\)/)

  const selectSource = backtest.slice(
    backtest.indexOf('    async selectSource (sourceId, options = {}) {'),
    backtest.indexOf('    sourceTypeLabel (item) {')
  )
  assert.match(selectSource, /this\.syncDecisionContextFromSource\(nextSource\)[\s\S]*?this\.source = nextSource[\s\S]*?return true/)
})
