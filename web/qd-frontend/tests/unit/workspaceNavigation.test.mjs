import assert from 'node:assert/strict'
import test from 'node:test'
import { buildWorkspaceNavigation, isWorkspaceItemActive, workspaceNavigationTarget } from '../../src/utils/workspaceNavigation.js'

const page = (path, more = {}) => ({ path, ...more })
const dashboardRoutes = ['/personal-home', '/personal-reports', '/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'].map(path => page(path))
const tree = children => [{ path: '/', children }]

test('unified research entries reuse authorized paths without mutating router records', () => {
  const source = tree([...dashboardRoutes, page('/research-lab'), page('/settings'), page('/agent-tokens'), page('/profile', { hidden: true }), page('/billing', { hidden: true, redirect: '/personal-home' })])
  const before = structuredClone(source)
  const groups = buildWorkspaceNavigation(source, { researchLabV2: { label: '统一研究工作区' } })
  assert.deepEqual(groups.map(group => group.label), ['工作台', '市场观察', '报告与证据', '统一研究工作区', '数据质量', '验证与复盘', '任务与运行', '独立工具', '管理'])
  assert.deepEqual(groups[1].to, { path: '/personal-home', query: { section: 'market' } })
  assert.deepEqual(groups.find(group => group.id === 'management').children.map(item => item.to), [
    { path: '/settings' },
    { path: '/agent-tokens' }
  ])
  assert.deepEqual(groups.find(group => group.id === 'tools').children.map(item => item.to.path), ['/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'])
  assert.deepEqual(source, before)
  const links = groups.flatMap(group => group.children || [group])
  assert.ok(links.every(item => !['/profile', '/billing'].includes(item.to.path)))
})

test('management links are absent when permission filtering removed them', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  assert.equal(groups.some(group => group.id === 'management'), false)
  assert.deepEqual(groups.find(group => group.id === 'operations').to, { path: '/personal-reports', query: { section: 'operations' } })
  assert.deepEqual(buildWorkspaceNavigation([]), [])
  assert.deepEqual(buildWorkspaceNavigation(tree([page('/settings', { hidden: true })])), [])
  const reportsOnly = buildWorkspaceNavigation(tree([page('/personal-reports')]))
  assert.deepEqual(reportsOnly.map(group => group.id), ['reports', 'quality', 'validation', 'operations'])
  const homeOnly = buildWorkspaceNavigation(tree([page('/personal-home')]))
  assert.deepEqual(homeOnly.map(group => group.id), ['home', 'market'])
})

test('home, market and evidence select distinct groups even on one route', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  for (const [query, expected] of [[{}, 'home'], [{ section: 'market' }, 'market'], [{ section: 'evidence' }, 'operations'], [{ section: 'unknown' }, 'home']]) {
    assert.deepEqual(groups.filter(group => isWorkspaceItemActive(group, { path: '/personal-home', query })).map(group => group.id), [expected])
  }
  assert.equal(isWorkspaceItemActive(groups.find(group => group.id === 'tools'), { path: '/strategy-ide', query: { sourceId: '5' } }), true)
  assert.equal(isWorkspaceItemActive(groups[0], { path: '/personal-reports' }), false)
})

test('reselecting an editor preserves its source query and hash', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  const strategy = groups.find(group => group.id === 'tools').children.find(item => item.id === 'strategy')
  const route = { path: '/strategy-ide', query: { sourceId: '5', tab: 'script', assetType: 'script' }, hash: '#code' }
  const target = workspaceNavigationTarget(strategy, route)
  assert.deepEqual(target, route)
  assert.notEqual(target.query, route.query)
  assert.deepEqual(workspaceNavigationTarget(strategy, { path: '/indicator-ide', query: { indicatorId: '2' } }), { path: '/strategy-ide' })
  assert.deepEqual(workspaceNavigationTarget(groups[0], { path: '/personal-home', query: { section: 'market' } }), { path: '/personal-home' })
})

test('report sections activate exactly one entry; nnG8 and unknown sections stay in evidence', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  for (const [query, expected] of [[{}, 'reports'], [{ section: 'nng8' }, 'reports'], [{ section: 'quality' }, 'quality'], [{ section: 'validation' }, 'validation'], [{ section: 'operations' }, 'operations'], [{ section: 'unknown' }, 'reports']]) {
    assert.deepEqual(groups.filter(group => isWorkspaceItemActive(group, { path: '/personal-reports', query })).map(group => group.id), [expected])
  }
})

test('shared report paths change sections rather than preserving the prior section', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  const quality = groups.find(group => group.id === 'quality')
  const validation = groups.find(group => group.id === 'validation')
  const operations = groups.find(group => group.id === 'operations')
  const reports = groups.find(group => group.id === 'reports')
  assert.deepEqual(workspaceNavigationTarget(quality, { path: '/personal-reports', query: { section: 'nng8', selected: 'astock' } }), { path: '/personal-reports', query: { section: 'quality' } })
  assert.deepEqual(workspaceNavigationTarget(validation, quality.to), { path: '/personal-reports', query: { section: 'validation' } })
  assert.deepEqual(workspaceNavigationTarget(operations, validation.to), { path: '/personal-reports', query: { section: 'operations' } })
  assert.deepEqual(workspaceNavigationTarget(reports, operations.to), { path: '/personal-reports' })
})

test('reselecting report/evidence preserves its nnG8 selection without mutating route', () => {
  const groups = buildWorkspaceNavigation(tree(dashboardRoutes))
  const route = { path: '/personal-reports', query: { section: 'nng8', selected: 'astock' }, hash: '#source' }
  const target = workspaceNavigationTarget(groups.find(group => group.id === 'reports'), route)
  assert.deepEqual(target, route)
  assert.notEqual(target.query, route.query)
})
