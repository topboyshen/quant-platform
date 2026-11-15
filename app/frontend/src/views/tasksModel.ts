/** Snapshot age and closing-window note for the task center (pure, so it is testable). */
const MINUTE = 60_000

export function snapshotAge(snapshotAt: string | null, now: Date = new Date()): string {
  if (!snapshotAt) return ''
  const min = Math.max(0, Math.round((now.getTime() - new Date(snapshotAt).getTime()) / MINUTE))
  return min < 1 ? '刚刚' : `${min} 分钟前`
}

export function snapshotNote(snapshotAt: string | null, now: Date = new Date()): string {
  if (!snapshotAt) return ''
  const min = Math.round((now.getTime() - new Date(snapshotAt).getTime()) / MINUTE)
  if (min < 15) return ''
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(now)
  const hour = Number(parts.find((p) => p.type === 'hour')?.value ?? 0)
  const minute = Number(parts.find((p) => p.type === 'minute')?.value ?? 0)
  const at = hour * 60 + minute
  return at >= 15 * 60 && at <= 16 * 60 + 25
    ? '收盘窗口 15:00–16:25 暂停采集，16:30 恢复'
    : '采集可能延迟，请检查 qp-collect.timer'
}
