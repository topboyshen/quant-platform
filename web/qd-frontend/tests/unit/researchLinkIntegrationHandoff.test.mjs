import test from 'node:test'
import assert from 'node:assert/strict'

import {
  AI_REFERENCE_STORAGE_KEY,
  INDICATOR_HANDOFF_CONTRACT,
  buildAIBacktestQuery,
  readTemporaryAIReference,
  temporaryCodeDigest,
  writeTemporaryAIReference,
  writeTemporaryIndicatorHandoff
} from '../../src/utils/researchLinkAI.js'

const KEY = 'qd_indicator_to_strategy_case_1'
const CODE = 'def indicator(ctx):\n    return ctx.close'

function payload (code = CODE) {
  return {
    handoff_contract: INDICATOR_HANDOFF_CONTRACT,
    source_indicator_id: 12,
    code,
    code_digest: temporaryCodeDigest(code),
    saved_code_digest: temporaryCodeDigest('previous saved code'),
    is_draft: true
  }
}

function memoryStorage () {
  const values = new Map()
  return {
    values,
    setItem: (key, value) => values.set(key, String(value)),
    getItem: key => values.get(key) ?? null,
    removeItem: key => values.delete(key)
  }
}

test('temporary indicator payload can be read back byte-for-byte without claiming a persistent version', () => {
  const storage = memoryStorage()
  const entry = payload()
  assert.equal(writeTemporaryIndicatorHandoff(storage, KEY, entry), true)
  assert.deepEqual(JSON.parse(storage.getItem(KEY)), entry)
  assert.match(entry.code_digest, /^fnv1a-32:[0-9a-f]{8}:\d+$/)
  assert.notEqual(temporaryCodeDigest(CODE + ' '), entry.code_digest)
})

test('indicator handoff refuses an altered code payload or an unscoped storage key', () => {
  const storage = memoryStorage()
  const staleDigest = { ...payload(), code: CODE + '\n# changed after digest' }
  assert.equal(writeTemporaryIndicatorHandoff(storage, KEY, staleDigest), false)
  assert.equal(storage.getItem(KEY), null)
  assert.equal(writeTemporaryIndicatorHandoff(storage, 'Authorization', payload()), false)
  assert.equal(writeTemporaryIndicatorHandoff(storage, '../qd_indicator_to_strategy_x', payload()), false)
  assert.equal(writeTemporaryIndicatorHandoff(storage, KEY, { ...payload(), handoff_contract: 'other-version' }), false)
})

test('indicator handoff refuses to overwrite a preexisting opaque key', () => {
  const storage = memoryStorage()
  storage.setItem(KEY, 'older user draft')
  assert.equal(writeTemporaryIndicatorHandoff(storage, KEY, payload()), false)
  assert.equal(storage.getItem(KEY), 'older user draft')
})

test('indicator handoff fails closed on write denial, silent write loss, or truncated readback', () => {
  let removed = 0
  const denied = {
    setItem: () => { throw new Error('quota') },
    getItem: () => null,
    removeItem: () => { removed += 1 }
  }
  assert.equal(writeTemporaryIndicatorHandoff(denied, KEY, payload()), false)
  assert.equal(removed, 1)

  const dropped = {
    setItem: () => {},
    getItem: () => null,
    removeItem: () => { removed += 1 }
  }
  assert.equal(writeTemporaryIndicatorHandoff(dropped, KEY, payload()), false)
  assert.equal(removed, 2)

  let wrote = false
  const truncated = {
    setItem: () => { wrote = true },
    getItem: () => wrote ? '{"handoff_contract":' : null,
    removeItem: () => { removed += 1 }
  }
  assert.equal(writeTemporaryIndicatorHandoff(truncated, KEY, payload()), false)
  assert.equal(removed, 3)
})

test('AI reference stores only a temporary label and timestamps, never report body or credentials', () => {
  const storage = memoryStorage()
  const selectedAt = '2026-09-24T03:30:00.000Z'
  assert.equal(writeTemporaryAIReference(storage, {
    market: 'CNStock',
    symbol: '300476',
    localRef: '本次快速诊断',
    generatedAt: '2026-09-24T03:29:00.000Z',
    selectedAt,
    reportBody: 'private report body',
    token: 'secret token',
    code: 'private source code'
  }), true)
  const raw = storage.getItem(AI_REFERENCE_STORAGE_KEY)
  assert.ok(raw)
  for (const privateValue of ['private report body', 'secret token', 'private source code']) {
    assert.equal(raw.includes(privateValue), false)
  }
  assert.deepEqual(
    readTemporaryAIReference(storage, { market: 'CNStock', symbol: '300476' }, Date.parse(selectedAt) + 1),
    JSON.parse(raw)
  )
})

test('AI reference cannot be borrowed across symbols, future clocks, or expired sessions', () => {
  const storage = memoryStorage()
  const selectedAt = '2026-09-24T03:30:00.000Z'
  const target = { market: 'CNStock', symbol: '300476' }
  assert.equal(writeTemporaryAIReference(storage, { ...target, localRef: 'local-only', selectedAt }), true)
  assert.equal(readTemporaryAIReference(storage, { ...target, symbol: '600519' }, Date.parse(selectedAt) + 1), null)
  assert.equal(readTemporaryAIReference(storage, { market: 'USStock', symbol: target.symbol }, Date.parse(selectedAt) + 1), null)
  assert.equal(readTemporaryAIReference(storage, target, Date.parse(selectedAt) - 1), null)
  assert.equal(readTemporaryAIReference(storage, target, Date.parse(selectedAt) + 30 * 60 * 1000 + 1), null)
  storage.setItem(AI_REFERENCE_STORAGE_KEY, '{not json')
  assert.equal(readTemporaryAIReference(storage, target, Date.parse(selectedAt) + 1), null)
})

test('AI-to-backtest URL contains only target identity and requires manual strategy selection', () => {
  const query = buildAIBacktestQuery({
    market: 'CNStock',
    symbol: '300476',
    handoff: 'opaque-1234',
    linked: true,
    sourceId: 7,
    token: 'private token',
    code: 'private source code',
    reportBody: 'private report body'
  })
  assert.deepEqual(query, {
    source_selection: 'required',
    market: 'CNStock',
    symbol: '300476',
    handoff: 'opaque-1234',
    link: 'ai'
  })
  const url = new URL(`https://example.invalid/#/backtest-center?${new URLSearchParams(query)}`)
  for (const privateValue of ['private token', 'private source code', 'private report body']) {
    assert.equal(url.href.includes(privateValue), false)
  }
})
