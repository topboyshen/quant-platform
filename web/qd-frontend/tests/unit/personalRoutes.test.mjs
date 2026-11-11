import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PERSONAL_HOME_PATH,
  RETIRED_PERSONAL_ROUTE_PATHS,
  isRetiredPersonalRoute,
  normalizePersonalRedirect
} from '../../src/utils/personalRoutes.js'

test('covers the retired commercial and multi-user routes', () => {
  for (const path of ['/billing', '/broker-accounts', '/indicator-community', '/portfolio', '/user-manage']) {
    assert.ok(RETIRED_PERSONAL_ROUTE_PATHS.includes(path), `${path} must stay retired`)
    assert.equal(isRetiredPersonalRoute(path), true)
    assert.equal(normalizePersonalRedirect(path), PERSONAL_HOME_PATH)
  }
})

test('normalizes retired route variants before authentication', () => {
  assert.equal(normalizePersonalRedirect('/billing?plan=pro'), PERSONAL_HOME_PATH)
  assert.equal(normalizePersonalRedirect('/billing/history'), PERSONAL_HOME_PATH)
  assert.equal(normalizePersonalRedirect('/BILLING'), PERSONAL_HOME_PATH)
  assert.equal(normalizePersonalRedirect('%2Fbilling'), PERSONAL_HOME_PATH)
})

test('preserves valid personal routes and rejects unsafe redirects', () => {
  assert.equal(normalizePersonalRedirect('/strategy-ide?strategy_id=7'), '/strategy-ide?strategy_id=7')
  assert.equal(normalizePersonalRedirect('https://example.com/billing'), PERSONAL_HOME_PATH)
  assert.equal(normalizePersonalRedirect('//example.com/billing'), PERSONAL_HOME_PATH)
  assert.equal(normalizePersonalRedirect('/%E0%A4%A'), PERSONAL_HOME_PATH)
})
