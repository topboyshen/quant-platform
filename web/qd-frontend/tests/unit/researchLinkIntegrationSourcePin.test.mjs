import test from 'node:test'
import assert from 'node:assert/strict'

import {
  readSourceHandoff,
  sourceHandoffIdentity,
  sourceHandoffKey,
  sourceHandoffMatches,
  writeSourceHandoff
} from '../../src/utils/researchLinkRun.js'

const NOW = Date.parse('2026-09-24T04:00:00Z')
const NONCE = 'handoff-abcdef1234'
const IDENTITY = {
  sourceId: 7,
  codeHash: 'a'.repeat(64),
  contractHash: 'b'.repeat(64)
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

test('strategy handoff pins both backend source revision and execution contract', () => {
  const storage = memoryStorage()
  assert.equal(writeSourceHandoff(storage, NONCE, IDENTITY, NOW), true)
  assert.deepEqual(readSourceHandoff(storage, NONCE, 7, NOW + 1000), IDENTITY)
  assert.equal(sourceHandoffMatches(IDENTITY, { ...IDENTITY, codeHash: 'c'.repeat(64) }), false)
  assert.equal(sourceHandoffMatches(IDENTITY, { ...IDENTITY, contractHash: 'c'.repeat(64) }), false)
  assert.equal(sourceHandoffMatches(IDENTITY, { ...IDENTITY, sourceId: 8 }), false)
  assert.equal(sourceHandoffMatches(IDENTITY, IDENTITY), true)
  assert.deepEqual(Object.keys(JSON.parse(storage.getItem(sourceHandoffKey(NONCE)))).sort(), [
    'at', 'codeHash', 'contractHash', 'nonce', 'sourceId', 'version'
  ])
})

test('strategy handoff cannot borrow a different source, stale receipt, or invalid hash', () => {
  const storage = memoryStorage()
  assert.equal(writeSourceHandoff(storage, NONCE, IDENTITY, NOW), true)
  assert.equal(readSourceHandoff(storage, NONCE, 8, NOW + 1), null)
  assert.equal(readSourceHandoff(storage, NONCE, 7, NOW - 1), null)
  assert.equal(readSourceHandoff(storage, NONCE, 7, NOW + 30 * 60 * 1000 + 1), null)
  assert.equal(sourceHandoffIdentity(7, 'short', IDENTITY.contractHash), null)
  assert.equal(sourceHandoffIdentity(0, IDENTITY.codeHash, IDENTITY.contractHash), null)
  assert.equal(writeSourceHandoff(storage, '../outside', IDENTITY, NOW), false)
})

test('strategy handoff fails closed when browser storage is denied or silently loses writes', () => {
  assert.equal(writeSourceHandoff({
    setItem: () => { throw new Error('storage denied') },
    getItem: () => null
  }, NONCE, IDENTITY, NOW), false)
  assert.equal(writeSourceHandoff({
    setItem: () => {},
    getItem: () => null
  }, NONCE, IDENTITY, NOW), false)
  const storage = memoryStorage()
  storage.setItem(sourceHandoffKey(NONCE), '{bad-json')
  assert.equal(readSourceHandoff(storage, NONCE, 7, NOW), null)
})
