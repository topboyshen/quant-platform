// 回测中心 · 历史列表的展示状态与乱序判定（纯函数，便于离线单测）
//
// 评审要求：
// - 失败不得冒充空历史（“读取中 / 确实为空 / 读取失败”必须可区分）；
// - 迟到的响应不得覆盖最后一次选择。

/**
 * 历史列表应呈现的状态。**优先级**：loading → failed → empty → ready。
 * 关键：error 优先于 empty —— 读取失败时绝不显示“没有历史”。
 *
 * @param {{loading?: boolean, error?: unknown, count?: number}} input
 * @returns {'loading'|'failed'|'empty'|'ready'}
 */
export function historyViewState (input) {
  const { loading, error, count } = input || {}
  if (loading) return 'loading'
  if (error) return 'failed'
  if (!count) return 'empty'
  return 'ready'
}

/**
 * 迟到的响应是否可以落到界面上。
 * 只有当响应属于**最后一次**发起的请求时才接受。
 */
export function shouldAcceptResponse (responseSeq, currentSeq) {
  return Number(responseSeq) === Number(currentSeq)
}

/**
 * 选择相邻连点时的最终有效选择：最后一次点击胜出。
 * 传入所有已发出请求的序号与最后一次的序号，返回应生效的序号；不匹配则 null。
 */
export function effectiveSelection (requestSeq, latestSeq) {
  return shouldAcceptResponse(requestSeq, latestSeq) ? Number(requestSeq) : null
}
