export function assertApiSuccess (response, fallbackMessage = 'Request failed', successCodes = [1]) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'code') && !successCodes.map(Number).includes(Number(response.code))) {
    const error = new Error(response.msg || response.message || fallbackMessage)
    error.apiResponse = response
    throw error
  }
  return response
}

// Agent Gateway v1 intentionally uses a different envelope from the legacy
// human API: code 0 means success (see docs/agent/agent-openapi.json).
export function assertAgentApiSuccess (response, fallbackMessage = 'Agent request failed') {
  return assertApiSuccess(response, fallbackMessage, [0])
}
