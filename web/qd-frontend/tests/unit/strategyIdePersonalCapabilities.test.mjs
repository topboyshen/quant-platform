import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const strategyIdePath = fileURLToPath(
  new URL('../../src/views/strategy-ide/index.vue', import.meta.url)
)
const strategyEditorPath = fileURLToPath(
  new URL('../../src/views/strategy-ide/components/StrategyEditor.vue', import.meta.url)
)
const factorLibraryPath = fileURLToPath(
  new URL('../../src/views/strategy-ide/FactorLibraryModal.vue', import.meta.url)
)
const strategyApiPath = fileURLToPath(
  new URL('../../src/api/strategy.js', import.meta.url)
)
const strategyIde = fs.readFileSync(strategyIdePath, 'utf8')
const strategyEditor = fs.readFileSync(strategyEditorPath, 'utf8')
const factorLibrary = fs.readFileSync(factorLibraryPath, 'utf8')
const strategyApi = fs.readFileSync(strategyApiPath, 'utf8')

test('personal strategy IDE enables completed contracts and keeps unsupported catalogs fail-closed', () => {
  for (const capability of [
    'supportsScriptSources',
    'supportsStrategyBacktest',
    'supportsCodeVerification'
  ]) {
    assert.match(strategyIde, new RegExp(`${capability}:\\s*true`))
  }

  for (const capability of [
    'supportsScriptTemplates',
    'supportsScriptVersions',
    'supportsFactorLibrary',
    'supportsUniverseLibrary'
  ]) {
    assert.match(strategyIde, new RegExp(`${capability}:\\s*false`))
  }

  assert.match(strategyIde, /:remote-templates-enabled="supportsScriptTemplates"/)
  assert.match(strategyIde, /:code-verification-enabled="supportsCodeVerification"/)
  assert.match(strategyIde, /<a-select\s+v-if="supportsScriptSources"\s+class="script-select"/)
  assert.match(strategyIde, /<a-tooltip v-if="supportsScriptSources" :title="text\.refreshScripts">/)
  assert.match(strategyIde, /v-if="supportsScriptSources && supportsScriptVersions"[\s\S]{0,420}?openVersionDrawer/)
  assert.match(strategyIde, /<a-tooltip v-if="supportsScriptSources && currentSourceId" :title="text\.saveAsNew">/)
  assert.match(strategyIde, /<a-tooltip v-if="supportsScriptSources && currentSourceId" :title="text\.deleteScript">/)
  assert.match(strategyIde, /<a-button\s+v-if="supportsScriptSources"\s+class="script-save-button"/)
  assert.match(strategyIde, /v-if="supportsFactorLibrary"\s+class="factor-library-button"/)
  assert.match(strategyIde, /v-if="supportsUniverseLibrary && currentAssetType === 'portfolio_strategy'"/)
  assert.match(strategyIde, /v-if="supportsScriptSources && currentAssetType === 'script'"[\s\S]{0,220}?openIndicatorConvertPicker/)
  assert.match(strategyIde, /v-if="supportsScriptSources && supportsStrategyBacktest && currentAssetType === 'script'"/)
  assert.match(
    strategyIde,
    /<a-modal\s+v-if="supportsScriptSources && showIndicatorConvertModal && isActiveStrategyRoute"\s+:visible="showIndicatorConvertModal"/,
    'the conversion portal exists only while the modal is open on the strategy route'
  )
  assert.match(strategyIde, /<factor-library-modal\s+v-if="supportsFactorLibrary"/)
  assert.match(strategyIde, /<universe-library-modal\s+v-if="supportsUniverseLibrary"/)
  assert.match(strategyIde, /<a-drawer\s+v-if="supportsScriptSources && supportsScriptVersions"/)
})

test('strategy IDE startup and deep links still guard optional source and template APIs', () => {
  assert.match(
    strategyIde,
    /async initPage \(\) \{\s*if \(this\.supportsScriptSources\) await this\.loadSources\(\)/
  )
  assert.match(
    strategyIde,
    /if \(!this\.supportsScriptSources && this\.isIndicatorConvertRoute\(\)\) \{[\s\S]{0,120}?this\.clearIndicatorConvertRoute\(\)[\s\S]{0,80}?return/
  )
  const syncStart = strategyIde.indexOf('    async syncIncomingResearchEntry (options = {}) {')
  const syncEnd = strategyIde.indexOf('    async syncDecisionContextSource (providedContext = null, options = {}) {')
  const sync = strategyIde.slice(syncStart, syncEnd)
  const sourceIntent = sync.indexOf('const routeId = this.getInitialRouteSourceId()')
  const conversionIntent = sync.indexOf('if (this.isIndicatorConvertRoute())')
  const copilotIntent = sync.indexOf('if (this.isDraftRoute() && this.hasCopilotScriptDraft())')
  const genericDraftIntent = sync.indexOf('if (this.isDraftRoute())', copilotIntent + 1)
  assert.ok(conversionIntent >= 0 && conversionIntent < sourceIntent,
    'a strict indicator payload must be checked before any incidental saved sourceId')
  assert.ok(sourceIntent < copilotIntent && copilotIntent < genericDraftIntent)
  assert.match(
    strategyIde,
    /shouldOpenTemplateFromRoute \(\) \{\s*if \(!this\.supportsScriptTemplates\) return false/
  )
  assert.match(
    strategyIde,
    /const shouldOpenTemplate = Boolean\(openTemplate && this\.supportsScriptTemplates\)/
  )
  assert.match(
    strategyIde,
    /@click="requestCreateNewDraft\(\{ openTemplate: supportsScriptTemplates \}\)"/
  )
})

test('strategy AI draft applies captured artifact context instead of stale canonical context', () => {
  const syncStart = strategyIde.indexOf('    async syncIncomingResearchEntry (options = {}) {')
  const syncEnd = strategyIde.indexOf('    async syncDecisionContextSource (providedContext = null, options = {}) {')
  const sync = strategyIde.slice(syncStart, syncEnd)
  assert.match(sync, /const artifactContext = this\.copilotScriptDraftDecisionContext\(\)/)
  assert.match(sync, /incomingStrategyOverwriteBlocked\(artifactContext, options\)/)
  assert.match(sync, /const appliedContext = readDecisionContext\(\{ allowStale: true \}\)[\s\S]{0,120}?this\.applyDecisionContext\(appliedContext\)/)
  assert.doesNotMatch(sync, /applyCopilotScriptDraft\(\)[\s\S]{0,160}?applyDecisionContext\(context\)/)

  const draft = strategyIde.slice(
    strategyIde.indexOf('    copilotScriptDraftDecisionContext () {'),
    strategyIde.indexOf('    shouldOpenTemplateFromRoute () {')
  )
  const contextWrite = draft.indexOf('writeDecisionContext(artifactContext)')
  const consumeCode = draft.indexOf("sessionStorage.removeItem('qd_strategy_source')")
  assert.match(draft, /qd_copilot_script_strategy_meta/)
  assert.match(draft, /market: query\.market \|\| ''[\s\S]{0,100}?symbol: query\.symbol \|\| ''/)
  assert.match(draft, /buildResearchArtifactDecisionContext/)
  assert.ok(contextWrite >= 0 && contextWrite < consumeCode)
  assert.match(draft, /market_category: writtenContext\.market[\s\S]{0,100}?symbol: writtenContext\.symbol/)
})

test('every script-source action has a fail-closed method guard', () => {
  for (const method of [
    'loadSources',
    'refreshSources',
    'handleScriptSelect',
    'openSource',
    'saveScript',
    'saveHiddenScriptParams',
    'deleteCurrentSource'
  ]) {
    assert.match(
      strategyIde,
      new RegExp(`(?:async )?${method} \\([^)]*\\) \\{\\s*if \\(!this\\.supportsScriptSources\\)`),
      `${method} must return before calling a script-source endpoint`
    )
  }

  assert.match(
    strategyIde,
    /handleSaveShortcut \(event\) \{[\s\S]{0,260}?event\.preventDefault\(\)[\s\S]{0,100}?if \(!this\.supportsScriptSources\) return/
  )
  assert.match(
    strategyIde,
    /openBacktestCenter \(\) \{\s*if \(!this\.supportsScriptSources \|\| !this\.supportsStrategyBacktest\) return/
  )
})

test('version and indicator-conversion paths are guarded before remote work', () => {
  for (const method of [
    'openVersionDrawer',
    'loadScriptVersions',
    'previewScriptVersion',
    'confirmRestoreScriptVersion',
    'restoreScriptVersion'
  ]) {
    assert.match(
      strategyIde,
      new RegExp(`(?:async )?${method} \\([^)]*\\) \\{\\s*if \\(!this\\.supportsScriptSources \\|\\| !this\\.supportsScriptVersions\\) return`),
      `${method} must be disabled with the version capability`
    )
  }

  for (const method of [
    'openIndicatorConvertPicker',
    'loadIndicatorOptions',
    'applyIndicatorConvertRouteOnce'
  ]) {
    assert.match(
      strategyIde,
      new RegExp(`async ${method} \\([^)]*\\) \\{\\s*if \\(!this\\.supportsScriptSources\\) return`),
      `${method} must not run without script-source persistence`
    )
  }
  assert.match(
    strategyIde,
    /async confirmIndicatorToStrategy \(\) \{\s*if \(!this\.\$route \|\| this\.\$route\.path !== '\/strategy-ide' \|\| !this\.showIndicatorConvertModal\) return\s*if \(!this\.supportsScriptSources\) return/,
    'conversion confirmation requires both an active strategy page and script-source persistence before remote work'
  )
})

test('shared strategy editor keeps opt-out props compatible and suppresses requests', () => {
  assert.match(strategyEditor, /remoteTemplatesEnabled: \{ type: Boolean, default: true \}/)
  assert.match(strategyEditor, /codeVerificationEnabled: \{ type: Boolean, default: true \}/)
  assert.match(strategyEditor, /v-if="codeVerificationEnabled"[\s\S]{0,180}?@click="handleVerify"/)
  assert.match(strategyEditor, /if \(this\.remoteTemplatesEnabled\) this\.bootstrapTemplates\(\)/)
  assert.match(strategyEditor, /async bootstrapTemplates \(\) \{\s*if \(!this\.remoteTemplatesEnabled\) return/)
  assert.match(strategyEditor, /async loadScriptTemplates \(\) \{\s*if \(!this\.remoteTemplatesEnabled\) \{[\s\S]{0,180}?return/)
  assert.match(strategyEditor, /openTemplatePicker \(\) \{\s*if \(!this\.remoteTemplatesEnabled \|\|/)
  assert.match(strategyEditor, /async handleVerify \(\) \{\s*if \(!this\.codeVerificationEnabled \|\|/)
})

test('strategy API and verification parsing match the personal backend contract', () => {
  assert.match(strategyApi, /verifyCode:\s*'\/api\/strategies\/verify-code'/)
  assert.match(strategyApi, /aiGenerate:\s*'\/api\/strategies\/ai-generate'/)
  assert.match(strategyIde, /verification\.success === true \|\| verification\.valid === true/)
  assert.match(strategyEditor, /verification\.success === true \|\| verification\.valid === true/)
  assert.match(strategyEditor, /verifyStrategyCode\(payload\)/)
  assert.match(strategyEditor, /payload\.strategyId = Number\(this\.strategyId\)/)
  assert.match(strategyEditor, /verification\.persisted !== false/)
  assert.doesNotMatch(strategyEditor, /url:\s*'\/api\/strategies\/verify'/)
})

test('default strategies, factor scaffold, and indicator conversion use on_init/on_bar ctx runtime', () => {
  const defaultStrategySection = strategyIde.slice(
    strategyIde.indexOf('const DEFAULT_SCRIPT_CODE'),
    strategyIde.indexOf('export default')
  )
  assert.match(defaultStrategySection, /def on_init\(ctx\):/)
  assert.match(defaultStrategySection, /def on_bar\(ctx, bar\):/)
  assert.match(defaultStrategySection, /ctx\.bars\(/)
  assert.match(defaultStrategySection, /ctx\.open_long\(/)
  assert.match(defaultStrategySection, /ctx\.close_long\(/)
  assert.match(defaultStrategySection, /order_type="market"/)
  assert.match(defaultStrategySection, /allocation_pct=/)
  assert.doesNotMatch(defaultStrategySection, /ctx\.(?:open_long|add_long|close_long)\([^\n]*price=/)
  assert.doesNotMatch(defaultStrategySection, /def (?:initialize|handle_data|rebalance)\(/)
  assert.doesNotMatch(defaultStrategySection, /(?:get_history|order_target_percent|set_universe)\(/)

  assert.match(strategyEditor, /_getDefaultCode \(\)[\s\S]{0,1800}?def on_init\(ctx\):/)
  assert.match(strategyEditor, /_getDefaultCode \(\)[\s\S]{0,2200}?def on_bar\(ctx, bar\):/)
  assert.match(strategyEditor, /_getDefaultCode \(\)[\s\S]{0,2600}?order_type="market"/)
  assert.doesNotMatch(strategyEditor, /ctx\.open_long\([^\n]*price=/)
  assert.match(factorLibrary, /strategySnippet \(factor\)[\s\S]{0,1800}?'def on_init\(ctx\):'/)
  assert.match(factorLibrary, /strategySnippet \(factor\)[\s\S]{0,2200}?'def on_bar\(ctx, bar\):'/)
  assert.match(factorLibrary, /ctx\.bars\(ctx\.lookback\)/)
  assert.match(factorLibrary, /ctx\.open_long\(order_type="market", allocation_pct=/)
  assert.doesNotMatch(factorLibrary, /ctx\.open_long\([^\n]*price=/)
  assert.doesNotMatch(factorLibrary, /`factor\(|`get_factors\(/)
  assert.doesNotMatch(factorLibrary, /'def (?:initialize|handle_data|rebalance)\(/)

  assert.match(strategyIde, /both on_init\(ctx\) and on_bar\(ctx, bar\)/)
  assert.match(strategyIde, /ctx\.param\(name, default\)/)
  assert.match(strategyIde, /Do not use initialize, handle_data, set_universe/)
})

test('new, refresh, source switch, workspace switch, route leave, and browser unload protect edits', () => {
  assert.match(strategyIde, /@click="requestCreateNewDraft\(/)
  assert.match(strategyIde, /refreshSources \(\) \{[\s\S]{0,220}?runAfterDiscardConfirmation/)
  assert.match(strategyIde, /handleScriptSelect \(id\) \{[\s\S]{0,320}?runAfterDiscardConfirmation/)
  assert.match(strategyIde, /handleAssetTypeChange \(assetType\) \{[\s\S]{0,700}?runAfterDiscardConfirmation\(switchWorkspace\)/)
  assert.match(strategyIde, /beforeRouteLeave \(to, from, next\)[\s\S]{0,500}?Modal\.confirm/)
  assert.match(strategyIde, /window\.addEventListener\('beforeunload', this\._beforeUnload\)/)
  assert.match(strategyIde, /window\.removeEventListener\('beforeunload', this\._beforeUnload\)/)
  assert.match(strategyIde, /handleBeforeUnload \(event\)[\s\S]{0,220}?event\.returnValue = ''/)
  assert.match(strategyEditor, /hasPendingChanges \(\) \{\s*return !!this\.templateDirty/)
})
