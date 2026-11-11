import test from 'node:test'
import assert from 'node:assert/strict'
import {
  readSourceHandoff,
  sourceHandoffIdentity,
  sourceHandoffKey,
  sourceHandoffMatches,
  writeSourceHandoff
} from '../../src/utils/researchLinkRun.js'

function memoryStorage () {
  const values = new Map()
  return {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)
  }
}

const pin = { sourceId: 17, codeHash: 'a'.repeat(64), contractHash: 'b'.repeat(64) }
const nonce = '20260924-linked-run-17'

test('strategy source handoff pins exact backend source and compiler identities', () => {
  const storage = memoryStorage()
  assert.equal(writeSourceHandoff(storage, nonce, pin, 10000), true)
  assert.deepEqual(readSourceHandoff(storage, nonce, 17, 10001), pin)
  assert.equal(readSourceHandoff(storage, nonce, 18, 10001), null)
  assert.equal(sourceHandoffMatches(pin, { ...pin, codeHash: 'c'.repeat(64) }), false)
  assert.equal(sourceHandoffMatches(pin, { ...pin, contractHash: 'c'.repeat(64) }), false)
  assert.equal(sourceHandoffMatches(pin, { ...pin, sourceId: 18 }), false)
  assert.equal(sourceHandoffMatches(pin, pin), true)
  assert.doesNotMatch(storage.getItem(sourceHandoffKey(nonce)), /sourceCode|password|token/)
})

test('missing, malformed, stale or future handoff cannot silently select latest source', () => {
  const storage = memoryStorage()
  assert.equal(readSourceHandoff(storage, nonce, 17, 10000), null)
  storage.setItem(sourceHandoffKey(nonce), '{invalid')
  assert.equal(readSourceHandoff(storage, nonce, 17, 10000), null)
  writeSourceHandoff(storage, nonce, pin, 10000)
  assert.equal(readSourceHandoff(storage, nonce, 17, 10000 + 30 * 60 * 1000 + 1), null)
  assert.equal(readSourceHandoff(storage, nonce, 17, 9999), null)
  assert.equal(readSourceHandoff(storage, 'wrong-nonce', 17, 10001), null)
  assert.equal(sourceHandoffIdentity(17, 'short', pin.contractHash), null)
  assert.equal(sourceHandoffIdentity(17, pin.codeHash, 'short'), null)
})

test('failed or truncated sessionStorage writes stop navigation', () => {
  const denied = { setItem () { throw new Error('quota') }, getItem () { return null } }
  const silentFailure = { setItem () {}, getItem () { return null } }
  assert.equal(writeSourceHandoff(denied, nonce, pin), false)
  assert.equal(writeSourceHandoff(silentFailure, nonce, pin), false)
  assert.equal(readSourceHandoff(denied, nonce, 17), null)
})
