import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  AI_REFERENCE_STORAGE_KEY,
  INDICATOR_HANDOFF_CONTRACT,
  buildAIBacktestQuery,
  isAIResearchEntry,
  readTemporaryAIReference,
  temporaryCodeDigest,
  writeTemporaryAIReference,
  writeTemporaryIndicatorHandoff
} from '../../src/utils/researchLinkAI.js'

const AI_PAGE = readFileSync(new URL('../../src/views/ai-analysis/components/CopilotWorkbench.vue', import.meta.url), 'utf8')
const INDICATOR_PAGE = readFileSync(new URL('../../src/views/indicator-ide/index.vue', import.meta.url), 'utf8')

function memoryStorage () {
  const items = new Map()
  return {
    getItem: key => items.has(key) ? items.get(key) : null,
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: key => items.delete(key)
  }
}

test('AI backtest query carries only object identity and mandatory source selection', () => {
  assert.deepEqual(buildAIBacktestQuery({
    market: 'CNStock',
    symbol: '300476',
    handoff: 123,
    linked: true,
    code: 'secret strategy',
    prompt: 'private report',
    token: 'private token'
  }), {
    source_selection: 'required',
    market: 'CNStock',
    symbol: '300476',
    handoff: '123',
    link: 'ai'
  })
})

test('an AI research-object entry is explicit even without any report reference', () => {
  assert.equal(isAIResearchEntry({ link: 'ai' }), true)
  assert.equal(isAIResearchEntry({ link: 'indicator' }), false)
  assert.equal(isAIResearchEntry({}), false)
  assert.match(AI_PAGE, /if \(path === '\/indicator-ide' \|\| this\.activeDecisionArtifactReference\) query\.link = 'ai'/)
  assert.match(AI_PAGE, /reportHandoff \|\| normalizedAction\.path === '\/indicator-ide'/)
  assert.match(AI_PAGE, /if \(artifactHandoff && normalizedAction\.path === '\/indicator-ide'\) normalizedAction\.query\.aiDraft = '1'/)
})

test('AI reference is temporary, target-scoped and never contains a report body', () => {
  const storage = memoryStorage()
  const selectedAt = '2026-09-24T03:00:00.000Z'
  const reference = {
    market: 'CNStock',
symbol: '300476',
localRef: 'local-9',
    generatedAt: '2026-09-24T10:00:00+08:00',
selectedAt,
    body: 'private report text',
token: 'secret'
  }
  assert.equal(writeTemporaryAIReference(storage, reference), true)
  const raw = storage.getItem(AI_REFERENCE_STORAGE_KEY)
  assert.ok(!raw.includes('private report text'))
  assert.ok(!raw.includes('secret'))
  assert.deepEqual(readTemporaryAIReference(storage, { market: 'CNStock', symbol: '300476' }, Date.parse(selectedAt) + 1000), {
    market: 'CNStock',
symbol: '300476',
localRef: 'local-9',
    generatedAt: '2026-09-24T10:00:00+08:00',
selectedAt
  })
  assert.equal(readTemporaryAIReference(storage, { market: 'CNStock', symbol: '600519' }, Date.parse(selectedAt) + 1000), null)
  assert.equal(readTemporaryAIReference(storage, { market: 'CNStock', symbol: '300476' }, Date.parse(selectedAt) + 31 * 60 * 1000), null)
})

test('AI reference handoff fails when storage silently loses the value', () => {
  const storage = {
    setItem () {},
    getItem () { return null },
    removeItem () {}
  }
  assert.equal(writeTemporaryAIReference(storage, {
    market: 'CNStock',
symbol: '300476',
localRef: 'local-9',
    selectedAt: '2026-09-24T03:00:00.000Z'
  }), false)
})

test('indicator handoff records distinct saved and draft checksums', () => {
  const storage = memoryStorage()
  const key = 'qd_indicator_to_strategy_1727140000000_abc'
  const payload = {
    indicatorId: 17,
    code: 'def indicator():\n  return 2\n',
    handoff_contract: INDICATOR_HANDOFF_CONTRACT,
    code_digest: temporaryCodeDigest('def indicator():\n  return 2\n'),
    saved_code_digest: temporaryCodeDigest('def indicator():\n  return 1\n'),
    is_draft: true
  }
  assert.notEqual(payload.code_digest, payload.saved_code_digest)
  assert.equal(writeTemporaryIndicatorHandoff(storage, key, payload), true)
  assert.deepEqual(JSON.parse(storage.getItem(key)), payload)
})

test('indicator handoff fails closed on missing, corrupt or silently truncated storage', () => {
  const key = 'qd_indicator_to_strategy_1727140000000_abc'
  const payload = {
    code: 'code',
handoff_contract: INDICATOR_HANDOFF_CONTRACT,
    code_digest: temporaryCodeDigest('code')
  }
  assert.equal(writeTemporaryIndicatorHandoff(null, key, payload), false)
  assert.equal(writeTemporaryIndicatorHandoff(memoryStorage(), key, { ...payload, code_digest: 'wrong' }), false)
  let removed = false
  const truncatingStorage = {
    setItem (name, value) { this.value = String(value).slice(0, 10) },
    getItem () { return this.value },
    removeItem () { removed = true }
  }
  assert.equal(writeTemporaryIndicatorHandoff(truncatingStorage, key, payload), false)
  assert.equal(removed, true)
})

test('indicator handoff never overwrites an existing session reference', () => {
  const storage = memoryStorage()
  const key = 'qd_indicator_to_strategy_1727140000000_collision'
  storage.setItem(key, 'earlier draft')
  assert.equal(writeTemporaryIndicatorHandoff(storage, key, {
    code: 'later draft',
handoff_contract: INDICATOR_HANDOFF_CONTRACT,
    code_digest: temporaryCodeDigest('later draft')
  }), false)
  assert.equal(storage.getItem(key), 'earlier draft')
})

test('AI page marks every backtest navigation as explicit source selection', () => {
  assert.match(AI_PAGE, /if \(path === '\/backtest-center'\) \{\s*query = buildAIBacktestQuery/s)
  assert.match(AI_PAGE, /if \(normalizedAction\.path === '\/backtest-center'\) \{\s*normalizedAction\.query = buildAIBacktestQuery/s)
  assert.match(AI_PAGE, /handleReportGoBacktest \(msg, result = null\)[\s\S]*?query: buildAIBacktestQuery/)
  assert.match(AI_PAGE, /已停止交接；当前报告仍在本页/)
})

test('indicator page only routes after verified storage and uses strict marker', () => {
  assert.match(INDICATOR_PAGE, /if \(!writeTemporaryIndicatorHandoff\(session, storageKey, context\)\) \{/)
  assert.match(INDICATOR_PAGE, /convert_required: '1'/)
  assert.match(INDICATOR_PAGE, /link: 'indicator'/)
  assert.match(INDICATOR_PAGE, /if \(isAIResearchEntry\(query\) \|\| query\.aiDraft === '1' \|\| this\.hasIncomingIndicatorArtifact\(\) \|\| this\.codeDirty\) return/)
  assert.match(INDICATOR_PAGE, /当前代码是未保存草稿；跨页仅传本次会话的临时摘要，不是指标版本/)
})

test('AI object-only arrival cannot inherit a saved preference or advance without explicit indicator selection', () => {
  assert.match(INDICATOR_PAGE, /restoreIdeUiState \(\) \{[\s\S]*?if \(isAIResearchEntry\(\(this\.\$route && this\.\$route\.query\) \|\| \{\}\)\) return/)
  assert.match(INDICATOR_PAGE, /restoreIdeSelectionPreference \(\) \{[\s\S]*?if \(isAIResearchEntry\(\(this\.\$route && this\.\$route\.query\) \|\| \{\}\)\) return/)
  assert.match(INDICATOR_PAGE, /applyIndicatorRouteSelection \(\) \{[\s\S]*?if \(isAIResearchEntry\(query\)\) return false/)
  assert.doesNotMatch(INDICATOR_PAGE, /const selected = this\.applyIndicatorRouteSelection\(\)\s*if \(selected && this\.isAIResearchObjectEntry\) this\.indicatorResearchSelectionConfirmed = true/)
  assert.match(INDICATOR_PAGE, /handleCreateStrategyFromIndicator \(\) \{[\s\S]*?if \(this\.isAIResearchObjectEntry && !this\.hasConfirmedAIIndicatorSelection\) \{[\s\S]*?return/)
  assert.match(INDICATOR_PAGE, /confirmAIIndicatorSelection \(\) \{[\s\S]*?this\.indicatorResearchConfirmedIdentity = identity/)
  assert.match(INDICATOR_PAGE, /aiIndicatorSelectionIdentity \(\) \{[\s\S]*?indicatorId: String\(this\.selectedIndicatorId\)/)
  assert.match(INDICATOR_PAGE, /confirmDeleteIndicator \(\) \{[\s\S]*?if \(this\.indicators\.length > 0 && !this\.isAIResearchObjectEntry\)/)
})
