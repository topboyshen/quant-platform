// 窄屏图表参数。只决定画布内边距和图例，不隐藏序列，也不改数据。
// 主因不在这里：390px 实测是结果区最小内容宽度把网格列撑到约 595px，
// 再被外层 overflow-x:hidden 裁切。容器先允许收缩，这里再避免收缩后的画布内裁切。

export function portfolioChartLayout (width) {
  const numeric = Number(width)
  const known = Number.isFinite(numeric) && numeric > 0
  const bucket = !known ? 'desktop' : numeric < 480 ? 'narrow' : numeric < 900 ? 'tablet' : 'desktop'
  const narrow = bucket === 'narrow'
  const side = narrow ? 40 : 58
  const right = narrow ? 12 : 56
  const top = narrow ? 34 : 42
  const equityHeight = narrow ? 210 : 245
  const lowerHeight = narrow ? 78 : 92
  const gap = narrow ? 26 : 38
  const secondTop = top + equityHeight + gap
  const thirdTop = secondTop + lowerHeight + gap
  return {
    bucket,
    showAxisName: !narrow,
    legend: narrow
      ? { type: 'scroll', top: 0, left: 0, right: 0, itemWidth: 12, itemHeight: 8, pageIconSize: 10 }
      : { top: 2, left: 8 },
    grid: [
      { left: side, right, top, height: equityHeight, containLabel: narrow },
      { left: side, right, top: secondTop, height: lowerHeight, containLabel: narrow },
      { left: side, right, top: thirdTop, height: lowerHeight, containLabel: narrow }
    ]
  }
}
