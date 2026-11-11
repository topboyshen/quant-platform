export function backtestRatioToPercent (value) {
  if (value === undefined || value === null || value === '') return value
  const number = Number(value)
  return Number.isFinite(number) ? number * 100 : value
}

export function strategyRunParams (run = {}) {
  if (run.params && typeof run.params === 'object' && !Array.isArray(run.params)) {
    return { ...run.params }
  }
  const snapshot = run.config_snapshot && typeof run.config_snapshot === 'object'
    ? run.config_snapshot
    : {}
  const signalConfig = snapshot.signalConfig && typeof snapshot.signalConfig === 'object'
    ? snapshot.signalConfig
    : {}
  const params = signalConfig.indicatorParams
  return params && typeof params === 'object' && !Array.isArray(params) ? { ...params } : {}
}

export function strategyRunStatus (run = {}) {
  if (String(run.status || '').toLowerCase() === 'failed') return 'failed'
  const status = String(run.result_status || (run.result && run.result.resultStatus) || '').trim()
  return status || 'unknown'
}

export function strategyRunFailed (run = {}) {
  return strategyRunStatus(run) === 'failed'
}

export function strategyResultHasLedgerAudit (result = {}) {
  return Boolean(
    result &&
    result.audit &&
    typeof result.audit === 'object' &&
    typeof result.audit.passed === 'boolean'
  )
}

function legacyExecutionSide (trade = {}) {
  if (trade.side) return trade.side
  const type = String(trade.type || '').trim().toLowerCase()
  if (/(buy|long_entry|open_long|cover|short_exit|close_short|reduce_short)/.test(type)) return 'buy'
  if (/(sell|long_exit|close_long|reduce_long|short_entry|open_short)/.test(type)) return 'sell'
  return type || '-'
}

function normalizeLegacyExecution (trade = {}, index = 0) {
  return {
    ...trade,
    order_id: trade.order_id || trade.orderId || `legacy-${index + 1}`,
    signal_time: trade.signal_time || trade.signal_bar_time || null,
    time: trade.time || trade.bar_time || trade.entry_time || trade.exit_time || null,
    side: legacyExecutionSide(trade),
    quantity: trade.quantity ?? trade.amount ?? trade.size ?? null,
    commission: trade.commission ?? trade.fee ?? null,
    status: trade.status || 'filled',
    reason: trade.reason || trade.close_reason || trade.type || ''
  }
}

export function normalizeStrategyBacktestResult (result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return result

  const hasExecutionLedger = Array.isArray(result.executions) || Array.isArray(result.rawTrades)
  const totalExecutions = Number(result.totalExecutions)
  const canAdaptLegacyTrades = (
    !hasExecutionLedger &&
    Array.isArray(result.trades) &&
    Number.isFinite(totalExecutions) &&
    totalExecutions === result.trades.length
  )
  const legacyTrades = canAdaptLegacyTrades
    ? result.trades.map(normalizeLegacyExecution)
    : null
  const summaryOnly = !strategyResultHasLedgerAudit(result)

  if (!legacyTrades && !summaryOnly) return result

  return {
    ...result,
    ...(legacyTrades ? { executions: legacyTrades } : {}),
    compatibility: {
      ...(result.compatibility && typeof result.compatibility === 'object'
        ? result.compatibility
        : {}),
      ...(summaryOnly ? { summaryOnly: true } : {}),
      ...(legacyTrades ? { legacyExecutions: true } : {})
    }
  }
}
