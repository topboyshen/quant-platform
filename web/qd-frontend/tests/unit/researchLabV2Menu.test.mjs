// Item b: V2 menu entry = flag (route registered) + permission (route survives permission filter) + capability.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { buildWorkspaceNavigation } from '../../src/utils/workspaceNavigation.js'

const page = path => ({ path })
const legacy = ['/personal-home', '/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'].map(page)
const tree = children => [{ path: '/', children }]
const tools = groups => groups.find(g => g.id === 'tools').children.map(i => i.to.path)
const workspace = groups => groups.find(g => g.id === 'lab-v2')

test('flag on + route permitted + capability enabled -> unified workspace is a primary entry', () => {
  const groups = buildWorkspaceNavigation(tree([...legacy, page('/research-lab')]), { researchLabV2: { label: '统一研究工作区（V2）' } })
  assert.equal(workspace(groups).to.path, '/research-lab')
  assert.equal(workspace(groups).children, undefined)
  assert.ok(groups.indexOf(workspace(groups)) < groups.findIndex(g => g.id === 'tools'))
  assert.deepEqual(tools(groups), ['/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'])
})

test('capability disabled (not whitelisted / switch) -> no entry, legacy four unchanged', () => {
  const groups = buildWorkspaceNavigation(tree([...legacy, page('/research-lab')]), { researchLabV2: false })
  assert.equal(workspace(groups), undefined)
  assert.deepEqual(tools(groups), ['/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'])
})

test('flag off (route never registered) or permission filtered -> no entry even if capability says enabled', () => {
  for (const groups of [buildWorkspaceNavigation(tree(legacy), { researchLabV2: { label: '统一研究工作区（V2）' } }), buildWorkspaceNavigation(tree(legacy))]) {
    assert.equal(workspace(groups), undefined)
    assert.deepEqual(tools(groups), ['/ai-asset-analysis', '/indicator-ide', '/strategy-ide', '/backtest-center'])
  }
})

test('layout only asks for capabilities in flag builds', () => {
  const src = readFileSync(new URL('../../src/layouts/BasicLayout.vue', import.meta.url), 'utf8')
  assert.match(src, /if \(process\.env\.VUE_APP_RESEARCH_LAB_V2 === '1'\) \{\s*import\('@\/api\/researchLabV2'\)/)
})
