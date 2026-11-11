import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const indicatorIdePath = fileURLToPath(
  new URL('../../src/views/indicator-ide/index.vue', import.meta.url)
)
const klineChartPath = fileURLToPath(
  new URL('../../src/views/indicator-analysis/components/KlineChart.vue', import.meta.url)
)
const strategyIdePath = fileURLToPath(
  new URL('../../src/views/strategy-ide/index.vue', import.meta.url)
)
const indicatorIdeSource = fs.readFileSync(indicatorIdePath, 'utf8')
const klineChartSource = fs.readFileSync(klineChartPath, 'utf8')
const strategyIdeSource = fs.readFileSync(strategyIdePath, 'utf8')

test('indicator-to-strategy conversion has a parameter parser', () => {
  assert.match(
    indicatorIdeSource,
    /parseIndicatorParamRaw \(code\) \{[\s\S]*?this\.parseIndicatorParamSpecs\(code \|\| ''\)[\s\S]*?params\[spec\.name\] = spec\.defaultValue/
  )
  assert.match(
    indicatorIdeSource,
    // gap A (§6.1 A2): the handoff carries declared ⊕ applied values, not parseIndicatorParamRaw defaults
    /buildIndicatorToStrategyContext \(\)[\s\S]*?this\.resolveIndicatorHandoff\(code \|\| ''\)/
  )
})

test('initial left-edge layout events do not trigger history loading', () => {
  assert.match(
    klineChartSource,
    /const isScrollingLeft = lastVisibleFrom !== null && lastVisibleFrom > data\.from[\s\S]*?if \(isScrollingLeft && canLoadAgain\)/
  )
  assert.doesNotMatch(klineChartSource, /isAlreadyAtEdge/)
  assert.doesNotMatch(
    klineChartSource,
    /formattedData\.length < visibleNeed[\s\S]*?loadMoreHistoryDataForScroll/
  )
})

test('indicator conversion route opens before strategy route initialization can replace it', () => {
  assert.match(
    strategyIdeSource,
    /async initPage \(\) \{[\s\S]{0,240}?await this\.loadSources\(\)[\s\S]{0,360}?syncIncomingResearchEntry\(\{ initial: true, sourcesReady: true, force: true \}\)/
  )
  const sync = strategyIdeSource.slice(
    strategyIdeSource.indexOf('    async syncIncomingResearchEntry (options = {}) {'),
    strategyIdeSource.indexOf('    async syncDecisionContextSource (providedContext = null, options = {}) {')
  )
  assert.match(
    sync,
    /if \(this\.isIndicatorConvertRoute\(\)\) \{\s*const applied = await this\.applyIndicatorConvertRouteOnce\(\{ sequence \}\)\s*if \(sequence !== this\.incomingResearchEntrySeq \|\| !this\.isActiveStrategyRoute\) return false\s*if \(applied\) \{[\s\S]*?this\.incomingResearchEntryKey = key\s*\}\s*return applied/
  )
  assert.ok(
    sync.indexOf('if (this.isIndicatorConvertRoute())') <
      sync.indexOf('const routeId = this.getInitialRouteSourceId()')
  )
  assert.ok(
    sync.indexOf('if (this.isIndicatorConvertRoute())') <
      sync.indexOf('if (this.isDraftRoute() && this.hasCopilotScriptDraft())')
  )
  assert.match(
    strategyIdeSource,
    /indicatorId: String\(raw\.indicatorId \|\| raw\.id \|\| ''\)[\s\S]*?market: String\(raw\.market \|\| ''\)[\s\S]*?timeframe: String\(raw\.timeframe \|\| ''\)/
  )
})

test('code quality reports empty or failed checks instead of claiming success', () => {
  assert.match(
    indicatorIdeSource,
    /async fetchCodeQualityHints \(code\) \{[\s\S]*?codeQualityCheckError = ''[\s\S]*?severity: 'error', code: 'EMPTY_CODE'[\s\S]*?return false[\s\S]*?catch \(e\) \{[\s\S]*?codeQualityCheckError =[\s\S]*?return false/
  )
  assert.match(
    indicatorIdeSource,
    /async runCodeQualityCheck \(\) \{[\s\S]*?const succeeded = await this\.fetchCodeQualityHints\(code\)[\s\S]*?if \(succeeded && !this\.codeQualityHints\.length\)/
  )
  assert.match(indicatorIdeSource, /v-else-if="codeQualityCheckError"[\s\S]*?:message="codeQualityCheckError"/)
})

test('AI generation locks indicator selection at both UI and method boundaries', () => {
  assert.match(
    indicatorIdeSource,
    /<a-dropdown[\s\S]{0,260}?:disabled="aiGenerating"[\s\S]{0,600}?@visibleChange="onIndicatorDropdownVisibleChange"/
  )
  assert.match(
    indicatorIdeSource,
    /selectEditorIndicator \(rawId\) \{[\s\S]*?if \(this\.aiGenerating\) \{[\s\S]*?return[\s\S]*?this\.commitEditorIndicatorSelection\(rawId\)/
  )
  assert.match(
    indicatorIdeSource,
    /const requestBody = \{[\s\S]*?executionMode: 'chart'[\s\S]*?source: 'indicator_ide'/
  )
})

test('dirty indicator switches require confirmation and commit through one helper', () => {
  assert.match(
    indicatorIdeSource,
    /selectEditorIndicator \(rawId\) \{[\s\S]*?if \(this\.codeDirty\) \{[\s\S]*?Modal\.confirm\(\{[\s\S]*?switchIndicatorUnsavedContent[\s\S]*?onOk: \(\) => this\.commitEditorIndicatorSelection\(rawId\)[\s\S]*?return[\s\S]*?this\.commitEditorIndicatorSelection\(rawId\)/
  )
  assert.match(
    indicatorIdeSource,
    /commitEditorIndicatorSelection \(rawId\) \{[\s\S]*?this\.selectedIndicatorId = rawId[\s\S]*?this\.onIndicatorChange\(rawId\)[\s\S]*?this\.persistIdeSelectionPreference\(\)/
  )
  const selectStart = indicatorIdeSource.indexOf('    selectEditorIndicator (rawId) {')
  const commitStart = indicatorIdeSource.indexOf('    commitEditorIndicatorSelection (rawId) {', selectStart)
  assert.ok(selectStart >= 0 && commitStart > selectStart)
  assert.doesNotMatch(indicatorIdeSource.slice(selectStart, commitStart), /selectedIndicatorId\s*=/)
})

test('route and browser exits protect dirty edits and in-flight AI generation', () => {
  assert.match(
    indicatorIdeSource,
    /beforeRouteLeave \(to, from, next\) \{[\s\S]*?if \(!this\.hasPendingIndicatorWork\(\)\) \{[\s\S]*?next\(\)[\s\S]*?Modal\.confirm\(\{[\s\S]*?leaveIndicatorGeneratingContent[\s\S]*?onOk: \(\) => next\(\)[\s\S]*?onCancel: \(\) => next\(false\)/
  )
  assert.match(
    indicatorIdeSource,
    /hasPendingIndicatorWork \(\) \{[\s\S]*?return !!\(this\.codeDirty \|\| this\.aiGenerating\)/
  )
  assert.match(
    indicatorIdeSource,
    /mounted \(\) \{[\s\S]*?_beforeUnloadListener = \(event\) => this\.handleBeforeUnload\(event\)[\s\S]*?window\.addEventListener\('beforeunload', this\._beforeUnloadListener\)/
  )
  assert.match(
    indicatorIdeSource,
    /beforeDestroy \(\) \{[\s\S]*?window\.removeEventListener\('beforeunload', this\._beforeUnloadListener\)[\s\S]*?this\._beforeUnloadListener = null/
  )
  assert.match(
    indicatorIdeSource,
    /handleBeforeUnload \(event\) \{[\s\S]*?if \(!event \|\| !this\.hasPendingIndicatorWork\(\)\) return undefined[\s\S]*?event\.preventDefault\(\)[\s\S]*?event\.returnValue = ''[\s\S]*?return ''/
  )
})
