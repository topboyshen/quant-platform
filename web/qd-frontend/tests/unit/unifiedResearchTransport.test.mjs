// Synthetic sessions and a network-incapable adapter. The actual shared Axios
// interceptors and unified API helpers execute without production credentials.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import axiosLibrary from 'axios'
import { isUnifiedResearchRead } from '../../src/utils/unifiedResearchRead.js'

let transportNumber = 0
async function unifiedActualTransport () {
  const registry = '__unified_real_transport_' + (++transportNumber)
  const events = { removed: [], notifications: [], navigated: [] }
  const values = new Map([['Access-Token', 'synthetic-user-a'], ['User-Info', { id: 101 }], ['User-Roles', ['synthetic']], ['PHPSESSID', 'synthetic-session-a']])
  const storage = { get: key => values.get(key), set: (key, value) => values.set(key, value), remove: key => { events.removed.push(key); values.delete(key) } }
  const browser = { location: { hostname: 'example.invalid', hash: '#/personal-reports?section=validation', assign: url => events.navigated.push(url) } }
  globalThis[registry] = { axios: axiosLibrary, storage, notification: { error: item => events.notifications.push(item) }, VueAxios: {}, i18n: { t: key => key }, window: browser, document: { cookie: '' } }
  let code = readFileSync(new URL('../../src/utils/request.js', import.meta.url), 'utf8')
    .replace("import axios from 'axios'", `const { axios } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import storage from 'store'", `const { storage } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import notification from 'ant-design-vue/es/notification'", `const { notification } = globalThis[${JSON.stringify(registry)}]`)
    .replace("import { VueAxios } from './axios'", `const { VueAxios } = globalThis[${JSON.stringify(registry)}]`)
    .replace("from '@/store/mutation-types'", 'from ' + JSON.stringify(new URL('../../src/store/mutation-types.js', import.meta.url).href))
    .replace("import i18n from '@/locales'", `const { i18n } = globalThis[${JSON.stringify(registry)}]`)
    .replace("from './unifiedResearchRead'", 'from ' + JSON.stringify(new URL('../../src/utils/unifiedResearchRead.js', import.meta.url).href))
  code = `const { window, document } = globalThis[${JSON.stringify(registry)}]\n` + code
  const requestUrl = 'data:text/javascript;charset=utf-8,' + encodeURIComponent(code)
  let request
  try { request = (await import(requestUrl)).default } finally { delete globalThis[registry] }
  const apiCode = readFileSync(new URL('../../src/api/unifiedResearch.js', import.meta.url), 'utf8')
    .replace("from '@/utils/request'", 'from ' + JSON.stringify(requestUrl))
  const api = await import('data:text/javascript;charset=utf-8,' + encodeURIComponent(apiCode))
  const pending = []
  request.defaults.adapter = config => new Promise((resolve, reject) => {
    pending.push({
      config,
      succeed: body => resolve({ data: body, status: 200, statusText: 'OK', headers: {}, config }),
      fail: status => reject(new axiosLibrary.AxiosError('synthetic transport rejection', 'ERR_BAD_REQUEST', config, null, { status, data: { msg: 'synthetic upstream rejection' }, headers: {}, config }))
    })
  })
  return { api, request, pending, values, events, drain: () => new Promise(resolve => setImmediate(resolve)) }
}

for (const method of ['getWorkbenchOverview', 'getRadarMarketContext', 'getNng8Readonly', 'listProjects', 'getExternalOperations']) {
  test(`real Axios ${method}: automatic _t does not turn account-bound 401 into global logout`, async () => {
    const h = await unifiedActualTransport()
    const failed = h.api[method]()
    const rejected = assert.rejects(failed, error => error.response.status === 401)
    await h.drain()
    assert.equal(h.pending.length, 1)
    const config = h.pending[0].config
    assert.equal(config.method, 'get')
    assert.equal(config.unifiedResearchCallerHandlesAuth, true)
    assert.deepEqual(Object.keys(config.params), ['_t'])
    assert.ok(Number.isSafeInteger(config.params._t) && config.params._t > 0)
    assert.equal(isUnifiedResearchRead(config), true)
    h.pending[0].fail(401); await rejected
    assert.deepEqual(h.events.removed, [])
    assert.deepEqual(h.events.navigated, [])
    assert.deepEqual(h.events.notifications, [])
    assert.equal(h.values.get('Access-Token'), 'synthetic-user-a')
  })
}

test('real Axios: late registered external action 401 cannot erase replacement account session', async () => {
  const h = await unifiedActualTransport()
  const response = h.api.operateExternalTask('astock-cninfo', 'pause', { confirmed: true, observedRevision: 'rev-a', clientRequestId: 'request-1' })
  const rejected = assert.rejects(response, error => error.response.status === 401)
  await h.drain(); assert.equal(h.pending[0].config.externalOperationsCallerHandlesAuth, true)
  h.values.set('Access-Token', 'synthetic-user-b'); h.values.set('User-Info', { id: 202 })
  h.pending[0].fail(401); await rejected
  assert.deepEqual(h.events.removed, []); assert.deepEqual(h.events.navigated, [])
  assert.equal(h.values.get('Access-Token'), 'synthetic-user-b')
})

test('real Axios: external original request lookup is a GET and never dispatches an action', async () => {
  const h = await unifiedActualTransport(); const response = h.api.getExternalOperationRequest('request-1'); await h.drain()
  assert.equal(h.pending.length, 1); assert.equal(h.pending[0].config.method, 'get'); assert.equal(h.pending[0].config.url, '/api/research-lab/v2/external-operations/requests/request-1')
  const body = { code: 1, data: { operation: { clientRequestId: 'request-1', status: 'unknown' } } }; h.pending[0].succeed(body); assert.deepEqual(await response, body)
})

test('real Axios: user A late 401 cannot erase user B session or replace its successful evidence', async () => {
  const h = await unifiedActualTransport()
  const old = h.api.getWorkbenchOverview()
  const rejected = assert.rejects(old, error => error.response.status === 401)
  await h.drain()
  h.values.set('Access-Token', 'synthetic-user-b')
  h.values.set('User-Info', { id: 202 })
  h.values.set('PHPSESSID', 'synthetic-session-b')
  const fresh = h.api.getWorkbenchOverview()
  await h.drain()
  const body = { code: 1, data: { contract_version: 'workbench.overview.v1', items: [{ id: 'synthetic-current-b', status: 'ok' }] } }
  h.pending[1].succeed(body)
  assert.deepEqual(await fresh, body)
  h.pending[0].fail(401); await rejected
  assert.deepEqual(h.events.removed, [])
  assert.deepEqual(h.events.navigated, [])
  assert.equal(h.values.get('Access-Token'), 'synthetic-user-b')
  assert.deepEqual(h.values.get('User-Info'), { id: 202 })
  assert.equal(h.values.get('PHPSESSID'), 'synthetic-session-b')
})

test('real Axios: unified 403 remains a rejected request, never a synthetic success', async () => {
  const h = await unifiedActualTransport()
  const failed = h.api.getNng8Readonly()
  const rejected = assert.rejects(failed, error => error.response.status === 403)
  await h.drain(); h.pending[0].fail(403); await rejected
  assert.deepEqual(h.events.removed, [])
  assert.deepEqual(h.events.navigated, [])
  assert.equal(h.events.notifications.length, 1)
})

const nonExceptions = [
  ['POST even on an allowed URL', { url: '/api/radar/workbench/overview', method: 'post' }],
  ['non-whitelisted read URL', { url: '/api/radar/workbench/reports/raw', method: 'get' }],
  ['external baseURL', { url: '/api/radar/workbench/overview', method: 'get', baseURL: 'https://example.invalid' }],
  ['external absolute URL', { url: 'https://example.invalid/api/radar/workbench/overview', method: 'get' }],
  ['non-_t query parameter', { url: '/api/radar/workbench/overview', method: 'get', params: { replay: 1 } }],
  ['query embedded in URL', { url: '/api/radar/workbench/overview?replay=1', method: 'get' }]
]
for (const [label, config] of nonExceptions) {
  test(`real Axios: ${label} retains global 401 cleanup and redirect`, async () => {
    const h = await unifiedActualTransport()
    const failed = h.request({ ...config, unifiedResearchCallerHandlesAuth: true })
    const rejected = assert.rejects(failed, error => error.response.status === 401)
    await h.drain()
    assert.equal(isUnifiedResearchRead(h.pending[0].config), false)
    h.pending[0].fail(401); await rejected
    assert.deepEqual(h.events.removed, ['Access-Token', 'User-Info', 'User-Roles', 'PHPSESSID'])
    assert.equal(h.events.navigated.length, 1)
    assert.match(h.events.navigated[0], /^\/#\/user\/login\?redirect=/)
  })
}

test('cache-buster exception accepts only its safe positive integer value, not arbitrary query data', () => {
  const base = { url: '/api/radar/workbench/overview', method: 'get', unifiedResearchCallerHandlesAuth: true }
  assert.equal(isUnifiedResearchRead({ ...base, params: { _t: 1700000000000 } }), true)
  for (const value of [null, undefined, false, true, 0, -1, 1.5, '1700000000000', NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(isUnifiedResearchRead({ ...base, params: { _t: value } }), false, String(value))
  }
  assert.equal(isUnifiedResearchRead({ ...base, params: { _t: 1700000000000, replay: 1 } }), false)
})
