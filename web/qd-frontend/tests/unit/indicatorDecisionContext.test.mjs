import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const indicator = fs.readFileSync(
  fileURLToPath(new URL('../../src/views/indicator-ide/index.vue', import.meta.url)),
  'utf8'
)

test('indicator manual symbol changes atomically continue the canonical decision context', () => {
  assert.match(indicator, /buildWatchlistDecisionContext/)
  assert.match(indicator, /writeSelectedAssetDecisionContext \(asset = \{\}\)[\s\S]{0,520}?writeDecisionContext\(next\)/)
  assert.match(
    indicator,
    /handleWatchlistChange \(val\)[\s\S]{0,900}?writeSelectedAssetDecisionContext\(selectedAsset\)[\s\S]{0,260}?this\.market = \(context && context\.market\)[\s\S]{0,180}?this\.symbol = \(context && context\.symbol\)/
  )
})

test('indicator conversion freezes one explicit target for session and route handoff', () => {
  const conversion = indicator.slice(
    indicator.indexOf('    buildIndicatorToStrategyContext () {'),
    indicator.indexOf('    // ===== Equity chart =====')
  )
  assert.match(conversion, /market: this\.market \|\| ''[\s\S]{0,100}?symbol: this\.symbol \|\| ''/)
  assert.match(conversion, /const context = this\.buildIndicatorToStrategyContext\(\)/)
  // The temporary payload must be written and read back before navigation;
  // a failed or lost write may not fall through to a saved indicator instead.
  assert.match(conversion, /if \(!writeTemporaryIndicatorHandoff\(session, storageKey, context\)\) \{[\s\S]*?return[\s\S]*?this\.\$router\.push/)
  assert.match(conversion, /convert_required: '1'/)
  assert.match(conversion, /link: 'indicator'/)
  for (const field of ['market', 'symbol', 'exchange_id', 'market_type', 'timeframe']) {
    assert.match(conversion, new RegExp(`${field}: context\\.${field}`))
  }
})

test('indicator AI artifact metadata overrides a later canonical symbol before draft apply', () => {
  const incoming = indicator.slice(
    indicator.indexOf('    incomingIndicatorArtifactTarget () {'),
    indicator.indexOf('    writeSelectedAssetDecisionContext (asset = {}) {')
  )
  assert.match(incoming, /qd_copilot_indicator_meta/)
  assert.match(incoming, /market: query\.market \|\| ''[\s\S]{0,100}?symbol: query\.symbol \|\| ''/)
  const builder = incoming.slice(
    incoming.indexOf('    buildDecisionContextFromIncomingArtifact () {'),
    incoming.indexOf('    resolveIncomingIndicatorDecisionContext () {')
  )
  assert.match(builder, /buildResearchArtifactDecisionContext/)
  assert.doesNotMatch(builder, /writeDecisionContext/)
  const consumer = incoming.slice(
    incoming.indexOf('    consumeIncomingResearchEntry (key, context, options = {}) {'),
    incoming.indexOf('    syncIncomingResearchEntry (options = {}) {')
  )
  assert.match(consumer, /acceptedContext = writeDecisionContext\(context\)[\s\S]*?this\.applyDecisionContext\(acceptedContext\)/)
  const sync = incoming.slice(incoming.indexOf('    syncIncomingResearchEntry (options = {}) {'))
  const reconcile = sync.indexOf('this.resolveIncomingIndicatorDecisionContext()')
  const key = sync.indexOf('this.getIncomingResearchEntryKey(context)')
  const modal = sync.indexOf('Modal.confirm({')
  assert.ok(reconcile >= 0 && reconcile < key && key < modal)
  assert.doesNotMatch(sync.slice(0, modal), /writeDecisionContext/)
  assert.match(sync.slice(0, modal), /if \(this\.aiGenerating[\s\S]*?return false/)
})

test('A artifact over dirty B commits only after one signature-guarded confirmation', () => {
  const sync = indicator.slice(
    indicator.indexOf('    syncIncomingResearchEntry (options = {}) {'),
    indicator.indexOf('    writeSelectedAssetDecisionContext (asset = {}) {')
  )
  assert.match(sync, /const artifactSignature = incoming\.artifact[\s\S]*?incomingIndicatorArtifactSignature\(\)/)
  assert.match(sync, /const confirmSeq = this\.incomingResearchConfirmSeq \+ 1/)
  assert.match(sync, /if \(confirmSeq !== this\.incomingResearchConfirmSeq\) return/)
  assert.match(sync, /this\.incomingIndicatorArtifactSignature\(\) !== artifactSignature/)
  assert.match(sync, /const latest = this\.resolveIncomingIndicatorDecisionContext\(\)/)
  assert.match(sync, /this\.consumeIncomingResearchEntry\([\s\S]*?commitArtifact: latest\.artifact/)
})

test('canceling A artifact over dirty B clears only confirmation state and never canonical context', () => {
  const sync = indicator.slice(
    indicator.indexOf('    syncIncomingResearchEntry (options = {}) {'),
    indicator.indexOf('    writeSelectedAssetDecisionContext (asset = {}) {')
  )
  const cancelStart = sync.indexOf('          onCancel: () => {')
  const cancel = sync.slice(cancelStart, sync.indexOf('        })', cancelStart))
  assert.match(cancel, /confirmSeq !== this\.incomingResearchConfirmSeq/)
  assert.match(cancel, /this\.incomingResearchConfirmKey = ''/)
  assert.match(cancel, /this\.incomingResearchConfirmSeq = 0/)
  assert.doesNotMatch(cancel, /writeDecisionContext|consumeIncomingResearchEntry|applyDecisionContext/)
})
