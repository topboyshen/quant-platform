import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const componentPath = fileURLToPath(
  new URL('../../src/views/indicator-ide/index.vue', import.meta.url)
)
const source = fs.readFileSync(componentPath, 'utf8')

test('personal indicator IDE hides optional surfaces without backend routes', () => {
  assert.match(source, /supportsSignalAlerts:\s*false/)
  assert.match(source, /supportsIndicatorVersions:\s*false/)
  assert.match(
    source,
    /<a-modal\s+v-if="supportsSignalAlerts"\s+:title="\$t\('indicatorIde\.signalAlert\.title'\)"/
  )
  assert.match(source, /v-if="supportsSignalAlerts"[\s\S]{0,300}?openSignalAlertModal/)
  assert.match(source, /v-if="supportsIndicatorVersions"[\s\S]*?openCodeVersionDrawer/)
})

test('indicator IDE mount and method guards cannot emit guaranteed 404 requests', () => {
  assert.match(
    source,
    /if \(this\.supportsSignalAlerts\) \{[\s\S]*?this\.loadSignalAlertNotificationDefaults\(\)[\s\S]*?this\.loadSignalAlertTasks\(\)[\s\S]*?\}/
  )
  assert.match(
    source,
    /async loadSignalAlertTasks \(\) \{\s*if \(!this\.supportsSignalAlerts\) return/
  )
  assert.match(
    source,
    /async loadCodeVersions \(\) \{\s*if \(!this\.supportsIndicatorVersions\) return/
  )
})
