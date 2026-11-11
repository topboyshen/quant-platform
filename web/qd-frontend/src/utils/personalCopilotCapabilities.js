export const PERSONAL_COPILOT_DISABLED_ACTIONS = Object.freeze([
  'save_memory',
  'create_monitor_task',
  'ask_about_report',
  'export_report_pdf'
])

export function isCopilotActionAllowed (actionType, personalMode = false) {
  if (!personalMode) return true
  return !PERSONAL_COPILOT_DISABLED_ACTIONS.includes(String(actionType || ''))
}

export default isCopilotActionAllowed
