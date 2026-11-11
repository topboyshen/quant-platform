// An in-tab handoff receipt, not a persisted source version or an access token.
// The backend still owns authorization and the atomic backtest contract check.
const PREFIX = 'qd_research_link_source_v1:'
const MAX_AGE_MS = 30 * 60 * 1000
const SHA256 = /^[a-f0-9]{64}$/i

export function sourceHandoffKey (nonce) {
  return `${PREFIX}${String(nonce || '')}`
}

export function newSourceHandoffNonce () {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2)
  return `${Date.now()}-${random}`
}

export function sourceHandoffIdentity (sourceId, codeHash, contractHash) {
  const id = Number(sourceId)
  const code = String(codeHash || '').trim()
  const contract = String(contractHash || '').trim()
  if (!Number.isSafeInteger(id) || id <= 0 || !SHA256.test(code) || !SHA256.test(contract)) return null
  return { sourceId: id, codeHash: code.toLowerCase(), contractHash: contract.toLowerCase() }
}

export function sourceHandoffMatches (expected, actual) {
  if (!expected || !actual) return false
  const expectedIdentity = sourceHandoffIdentity(expected.sourceId, expected.codeHash, expected.contractHash)
  const actualIdentity = sourceHandoffIdentity(actual.sourceId, actual.codeHash, actual.contractHash)
  return !!expectedIdentity && !!actualIdentity &&
    expectedIdentity.sourceId === actualIdentity.sourceId &&
    expectedIdentity.codeHash === actualIdentity.codeHash &&
    expectedIdentity.contractHash === actualIdentity.contractHash
}

export function writeSourceHandoff (storage, nonce, identity, now = Date.now()) {
  const safeNonce = String(nonce || '')
  const pin = sourceHandoffIdentity(identity && identity.sourceId, identity && identity.codeHash, identity && identity.contractHash)
  if (!storage || !/^[A-Za-z0-9-]{8,100}$/.test(safeNonce) || !pin || !Number.isFinite(now)) return false
  const encoded = JSON.stringify({ version: 1, nonce: safeNonce, at: now, ...pin })
  try {
    storage.setItem(sourceHandoffKey(safeNonce), encoded)
    // A failed/quota-blocked write must not be mistaken for a valid handoff.
    return storage.getItem(sourceHandoffKey(safeNonce)) === encoded
  } catch (_) {
    return false
  }
}

export function readSourceHandoff (storage, nonce, sourceId, now = Date.now()) {
  const safeNonce = String(nonce || '')
  if (!storage || !/^[A-Za-z0-9-]{8,100}$/.test(safeNonce)) return null
  try {
    const raw = storage.getItem(sourceHandoffKey(safeNonce))
    if (!raw) return null
    const receipt = JSON.parse(raw)
    if (!receipt || receipt.version !== 1 || receipt.nonce !== safeNonce ||
      !Number.isFinite(receipt.at) || receipt.at > now || now - receipt.at > MAX_AGE_MS) return null
    const identity = sourceHandoffIdentity(receipt.sourceId, receipt.codeHash, receipt.contractHash)
    return identity && identity.sourceId === Number(sourceId) ? identity : null
  } catch (_) {
    return null
  }
}
