export const fmt = (s?: string | null) =>
  s ? new Date(s).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '-'
export const tagType = (s?: string | null) =>
  s === 'succeeded' || s === 'success' || s === 'active' || s === 'healthy' || s === 'running' ? 'success'
    : s === 'failed' || s === 'critical' || s === 'unhealthy' || s === 'exit-code' ? 'error'
      : s === 'warning' ? 'warning' : 'default'
