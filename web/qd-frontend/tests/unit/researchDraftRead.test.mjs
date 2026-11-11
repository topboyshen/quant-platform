// Actual API transport and real component methods; only transport/storage are
// synthetic. No network, authentication, model or production data is read.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { harness, clone } from './researchWorkflowBatch1Harness.mjs'

const directory = mkdtempSync(join(tmpdir(), 'qd-draft-api-test-'))
const requestPath = join(directory, 'request.mjs')
const apiPath = join(directory, 'researchLabV2.mjs')
writeFileSync(requestPath, 'export let handler; export const use = fn => { handler = fn }; export default config => handler(config)\n')
const apiSource = readFileSync(process.env.RESEARCH_DRAFT_API_SOURCE || new URL('../../src/api/researchLabV2.js', import.meta.url), 'utf8')
assert.ok(apiSource.includes("import request from '@/utils/request'"))
writeFileSync(apiPath, apiSource.replace("import request from '@/utils/request'", `import request from ${JSON.stringify(pathToFileURL(requestPath).href)}`))
const transport = await import(pathToFileURL(requestPath).href)
const client = await import(pathToFileURL(apiPath).href)
test.after(() => rmSync(directory, { recursive: true, force: true }))

const missingBody = () => ({ code: 0, msg: '暂无草稿', data: null, error: { type: 'NOT_FOUND', retriable: false } })
const failure = (status, body = missingBody()) => Object.assign(new Error('synthetic HTTP response'), { response: { status, data: body } })
const rejectWith = error => transport.use(async () => { throw error })

test('actual getDraft normalizes only the owned-branch empty draft HTTP envelope', async () => {
  const calls = []
  transport.use(async config => { calls.push(config); throw failure(404) })
  assert.deepEqual(await client.getDraft('rb_OWNED', 'indicator'), { code: 1, msg: 'success', data: null })
  assert.deepEqual(calls, [{ method: 'get', url: '/api/research-lab/v2/branches/rb_OWNED/drafts/indicator', data: undefined, headers: undefined, timeout: 30000 }])
})

for (const [name, error] of [
  ['foreign or missing branch', failure(404, { ...missingBody(), msg: '分支不存在' })],
  ['other missing object', failure(404, { ...missingBody(), msg: '产物不存在' })],
  ['unidentified 404', failure(404, { code: 0, msg: 'Not Found', data: null })],
  ['HTML 404', failure(404, '<html>not found</html>')],
  ['authentication denied', failure(401)],
  ['permission denied', failure(403)],
  ['invalid module', failure(400, { ...missingBody(), error: { type: 'VALIDATION_ERROR', retriable: false } })],
  ['server failure', failure(500)],
  ['retriable failure', failure(404, { ...missingBody(), error: { type: 'NOT_FOUND', retriable: true } })],
  ['nonempty error data', failure(404, { ...missingBody(), data: { arbitrary: true } })],
  ['unverified additive envelope', failure(404, { ...missingBody(), scope: 'foreign' })],
  ['malformed error detail', failure(404, { ...missingBody(), error: { type: 'NOT_FOUND' } })],
  ['network interruption', new Error('synthetic offline transport')]
]) {
  test(`actual getDraft preserves ${name} as a failure, never empty`, async () => {
    rejectWith(error)
    await assert.rejects(client.getDraft('rb_OWNED', 'indicator'), received => received === error)
  })
}

test('actual getDraft preserves a real successful saved draft without normalization', async () => {
  const response = { code: 1, msg: 'success', data: { module: 'indicator', payload: { draft: { period: 80 } }, draftRevision: 4 } }
  transport.use(async () => response)
  assert.equal(await client.getDraft('rb_OWNED', 'indicator'), response)
})

test('real loadDrafts reads both empty modules through actual API without warning or losing local edits', async () => {
  const h = await harness(); h.messages.length = 0
  h.page.ind.draft = { period: 80 }; h.page.persistLocal('indicator')
  const key = h.page.localKey('A', 'indicator'); const before = h.entries.get(key)
  const calls = []; transport.use(async config => { calls.push(config.url); throw failure(404) })
  h.api.getDraft = client.getDraft
  assert.equal(await h.page.loadDrafts('A'), true)
  assert.deepEqual(calls, ['/api/research-lab/v2/branches/A/drafts/indicator', '/api/research-lab/v2/branches/A/drafts/strategy'])
  assert.equal(h.page.drafts.indicator, null); assert.equal(h.page.drafts.strategy, null)
  assert.equal(h.page.ind.draft.period, 80); assert.equal(h.page.indDirty, true)
  assert.equal(h.entries.get(key), before); assert.equal(h.messages.length, 0)
})

test('real loadDrafts scope and server failures preserve prior draft metadata and local editing', async () => {
  for (const error of [failure(404, { ...missingBody(), msg: '分支不存在' }), failure(500), new Error('synthetic offline')]) {
    const h = await harness(); h.messages.length = 0
    h.page.drafts.indicator = { draftRevision: 7, payload: { draft: { period: 50 } } }
    h.page.ind.draft = { period: 80 }; h.page.persistLocal('indicator')
    const beforeDraft = clone(h.page.drafts.indicator); const key = h.page.localKey('A', 'indicator'); const before = h.entries.get(key)
    rejectWith(error); h.api.getDraft = client.getDraft
    assert.equal(await h.page.loadDrafts('A'), false)
    assert.deepEqual(clone(h.page.drafts.indicator), beforeDraft)
    assert.equal(h.page.ind.draft.period, 80); assert.equal(h.entries.get(key), before)
    assert.ok(h.messages.some(row => row.kind === 'warning' && row.msg.includes('现有草稿保留')))
  }
})

test('an empty first module followed by a failed second read does not partly clear existing drafts', async () => {
  const h = await harness(); h.messages.length = 0
  h.page.drafts.indicator = { draftRevision: 2, payload: { draft: { period: 50 } } }
  h.page.drafts.strategy = { draftRevision: 3, payload: { sourceId: 11 } }
  const before = clone(h.page.drafts)
  transport.use(async config => {
    if (config.url.endsWith('/indicator')) throw failure(404)
    throw failure(503)
  })
  h.api.getDraft = client.getDraft
  assert.equal(await h.page.loadDrafts('A'), false)
  assert.deepEqual(clone(h.page.drafts), before)
  assert.ok(h.messages.some(row => row.kind === 'warning' && row.msg.includes('现有草稿保留')))
})
