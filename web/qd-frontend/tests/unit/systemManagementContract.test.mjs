import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'

import { assertAgentApiSuccess, assertApiSuccess } from '../../src/utils/apiResponse.js'

const root = path.resolve(import.meta.dirname, '../..')
const read = relativePath => fs.readFileSync(path.join(root, relativePath), 'utf8')

const settings = read('src/views/settings/index.vue')
const routerConfig = read('src/config/router.config.js')
const basicLayout = read('src/layouts/BasicLayout.vue')
const request = read('src/utils/request.js')
const agentTokens = read('src/views/agent-tokens/index.vue')
const aiSkills = read('src/views/ai-skills/index.vue')

test('settings uses native password inputs and clears returned secret values', () => {
  const passwordBranches = settings.match(/v-else-if="entry\.item\.type === 'password'"[\s\S]*?<\/template>/g) || []
  assert.equal(passwordBranches.length, 3)
  for (const branch of passwordBranches) {
    assert.match(branch, /:type="passwordVisible\[entry\.item\.key\] \? 'text' : 'password'"/)
    assert.doesNotMatch(branch, /\s+type="text"/)
  }
  assert.match(settings, /this\.values = this\.sanitizeSettingsValues\(this\.schema, valuesRes\.data \|\| \{\}\)/)
  assert.match(settings, /item\.type === 'password'[\s\S]*?sanitized\[groupKey\]\[item\.key\] = ''/)
})

test('AI skills is absent from the route map and system-management shortcuts', () => {
  assert.doesNotMatch(routerConfig, /path:\s*'\/ai-skills'/)
  assert.doesNotMatch(basicLayout, /['"]\/ai-skills['"]/)
})

test('unimplemented universe manager cannot be mounted by a direct route', () => {
  const routeStart = routerConfig.indexOf("        path: '/universe-manager'")
  const routeBlock = routerConfig.slice(routeStart, routerConfig.indexOf('      }', routeStart) + 7)
  assert.ok(routeStart >= 0, 'the retired deep link must have an explicit safe redirect')
  assert.match(routeBlock, /redirect: PERSONAL_HOME_PATH/)
  assert.doesNotMatch(routeBlock, /component:|views\/universe-manager/)
  assert.doesNotMatch(routerConfig, /import\(['"]@\/views\/universe-manager(?:\/index\.vue)?['"]\)/)
})

test('settings startup does not call unavailable catalog or universe admin APIs', () => {
  const mounted = settings.match(/mounted \(\) \{([\s\S]*?)\n {2}\},\n {2}beforeDestroy/)?.[1] || ''
  assert.match(mounted, /this\.loadSettings\(\)/)
  assert.doesNotMatch(mounted, /refreshCatalogOverview|refreshUniverseOverview/)
  assert.match(settings, /filter\(\(\[groupKey\]\) => groupKey !== 'market_catalog'\)/)
})

test('request interceptor never logs raw authentication tokens', () => {
  assert.doesNotMatch(request, /Raw token from storage|Token value|console\.warn\([^\n]*rawToken/)
})

test('human and Agent APIs enforce their distinct success envelopes', () => {
  assert.throws(
    () => assertApiSuccess({ code: 0, msg: 'business failure' }),
    /business failure/
  )
  assert.deepEqual(assertApiSuccess({ code: 1, data: [] }), { code: 1, data: [] })
  assert.deepEqual(assertAgentApiSuccess({ code: 0, data: [] }), { code: 0, data: [] })
  assert.throws(
    () => assertAgentApiSuccess({ code: 400, message: 'agent failure' }),
    /agent failure/
  )
  assert.match(agentTokens, /assertAgentApiSuccess\(await revokeAgentToken/)
  assert.match(aiSkills, /assertApiSuccess\(await updateAiSkill/)
  assert.match(aiSkills, /assertApiSuccess\(await deleteAiSkill/)
  assert.match(aiSkills, /assertApiSuccess\(await installAiSkill/)
})
