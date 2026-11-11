import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PERSONAL_COPILOT_DISABLED_ACTIONS,
  isCopilotActionAllowed
} from '../../src/utils/personalCopilotCapabilities.js'

test('personal mode denies unavailable report and placeholder chat actions', () => {
  for (const action of ['save_memory', 'create_monitor_task', 'ask_about_report', 'export_report_pdf']) {
    assert.equal(PERSONAL_COPILOT_DISABLED_ACTIONS.includes(action), true)
    assert.equal(isCopilotActionAllowed(action, true), false)
    assert.equal(isCopilotActionAllowed(action, false), true)
  }
  assert.equal(isCopilotActionAllowed('open_indicator_ide', true), true)
})
