const HOME_PATH = '/personal-home'
const REPORTS_PATH = '/personal-reports'
const REPORT_SECTIONS = new Set(['quality', 'validation', 'operations'])

function navigationSection (path, query = {}) {
  const section = query.section
  if (path === HOME_PATH) return section === 'market' || section === 'evidence' ? section : undefined
  // nnG8 remains part of report/evidence browsing. Unknown sections also fall
  // back to the catalogue instead of activating all entries on this route.
  if (path === REPORTS_PATH) return REPORT_SECTIONS.has(section) ? section : undefined
  return undefined
}

// The caller supplies the permission-filtered router tree. This presentation
// model never creates a route or infers permission from a user's display name.
// options.researchLabV2: { label } only when the V2 capability check passed (feature-flag builds only; the label
// string itself lives in the flag-gated layout code so flag-off bundles carry none of it). The entry
// additionally requires the permission-filtered /research-lab route, so a flag-off build never shows it.
export function buildWorkspaceNavigation (routes = [], options = {}) {
  const root = routes.find(route => route.path === '/')
  const children = root && Array.isArray(root.children) ? root.children : []
  const available = new Map(children.filter(route => !route.hidden && !route.redirect).map(route => [route.path, route]))
  const item = (id, label, path, section) => available.has(path)
    ? { id, label, to: { path, ...(section ? { query: { section } } : {}) } }
    : null
  const group = (id, label, icon, items) => {
    const visibleItems = items.filter(Boolean)
    return visibleItems.length ? { id, label, icon, children: visibleItems } : null
  }

  return [
    available.has(HOME_PATH) && { ...item('home', '工作台', HOME_PATH), icon: 'dashboard' },
    available.has(HOME_PATH) && { ...item('market', '市场观察', HOME_PATH, 'market'), icon: 'radar-chart' },
    available.has(REPORTS_PATH) && { ...item('reports', '报告与证据', REPORTS_PATH), icon: 'file-text' },
    options.researchLabV2 && options.researchLabV2.label && available.has('/research-lab') && {
      ...item('lab-v2', options.researchLabV2.label, '/research-lab'), icon: 'experiment'
    },
    available.has(REPORTS_PATH) && { ...item('quality', '数据质量', REPORTS_PATH, 'quality'), icon: 'check-circle' },
    available.has(REPORTS_PATH) && { ...item('validation', '验证与复盘', REPORTS_PATH, 'validation'), icon: 'bar-chart' },
    available.has(REPORTS_PATH) && { ...item('operations', '任务与运行', REPORTS_PATH, 'operations'), icon: 'clock-circle' },
    group('tools', '独立工具', 'tool', [
      item('ai', 'AI 资产分析', '/ai-asset-analysis'),
      item('indicator', '指标研究', '/indicator-ide'),
      item('strategy', '策略研究', '/strategy-ide'),
      item('backtest', '回测验证', '/backtest-center')
    ]),
    group('management', '管理', 'setting', [
      item('settings', '系统设置', '/settings'),
      item('agents', '研究 Agent', '/agent-tokens')
    ])
  ].filter(Boolean)
}

export function isWorkspaceItemActive (item, route = {}) {
  if (item.children) return item.children.some(child => isWorkspaceItemActive(child, route))
  // Keep old evidence bookmarks recognisable without generating a duplicate
  // primary entry. New clicks lead to the unified read-only operations view.
  if (item.id === 'operations' && route.path === HOME_PATH && navigationSection(route.path, route.query) === 'evidence') return true
  if (!item.to || item.to.path !== route.path) return false
  if (route.path !== HOME_PATH && route.path !== REPORTS_PATH) return true
  return navigationSection(item.to.path, item.to.query) === navigationSection(route.path, route.query)
}

export function workspaceNavigationTarget (item, route = {}) {
  // Re-selecting an editor must not remove its source/version query. The
  // original router and each page still own cross-page context and guards.
  const sectionRoute = route.path === HOME_PATH || route.path === REPORTS_PATH
  const sameSection = navigationSection(item.to.path, item.to.query) === navigationSection(route.path, route.query)
  if (item.to.path === route.path && (!sectionRoute || sameSection)) {
    return { path: route.path, query: { ...route.query }, hash: route.hash || '' }
  }
  return { ...item.to, ...(item.to.query ? { query: { ...item.to.query } } : {}) }
}
