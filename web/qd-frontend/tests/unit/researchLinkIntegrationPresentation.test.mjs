import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

import {
  RESEARCH_STAGES,
  researchSourceState,
  researchStageIndex
} from '../../src/utils/researchChainPresentation.js'

const bar = fs.readFileSync(
  fileURLToPath(new URL('../../src/components/DecisionContextBar/index.vue', import.meta.url)),
  'utf8'
)
const require = createRequire(import.meta.url)
const less = require('less')
const postcss = require('postcss')

function directDeclarations (container, selector) {
  const declarations = {}
  for (const rule of container.nodes || []) {
    if (rule.type !== 'rule' || !rule.selector.split(',').map(part => part.trim()).includes(selector)) continue
    for (const declaration of rule.nodes || []) {
      if (declaration.type === 'decl') declarations[declaration.prop] = declaration.value
    }
  }
  return declarations
}

test('research chain names four distinct live module routes in workflow order', () => {
  const paths = RESEARCH_STAGES.map(stage => stage.path)
  assert.deepEqual(paths, [
    '/ai-asset-analysis',
    '/indicator-ide',
    '/strategy-ide',
    '/backtest-center'
  ])
  assert.equal(new Set(paths).size, 4)
  paths.forEach((path, index) => assert.equal(researchStageIndex(path), index))
  assert.equal(researchStageIndex('/not-a-research-page'), -1)
  assert.equal(researchStageIndex(null), -1)
})

test('source status never turns missing, stale, or unknown evidence green', () => {
  assert.equal(researchSourceState('ready').color, 'green')
  assert.equal(researchSourceState('temporary').color, 'blue')
  assert.equal(researchSourceState('stale').color, 'orange')
  assert.equal(researchSourceState('unavailable').color, 'red')
  for (const value of ['unknown', '', null, undefined, 'garbage']) {
    const state = researchSourceState(value)
    assert.equal(state.label, '版本未知')
    assert.notEqual(state.color, 'green')
  }
})

test('chain card uses source artifact props separately from global market context', () => {
  assert.match(bar, /artifactLabel:\s*\{\s*type:\s*String/)
  assert.match(bar, /artifactStatus:\s*\{\s*type:\s*String,\s*default:\s*'unknown'/)
  assert.match(bar, /artifactDetail:\s*\{\s*type:\s*String/)
  assert.match(bar, /researchSourceState\(this\.artifactStatus\)/)
  assert.match(bar, /本环节来源/)
  assert.match(bar, /artifactLabel\s*\|\|\s*'未提供具体产物引用'/)
  assert.match(bar, /研究链/)
  assert.match(bar, /:aria-current="index === currentStageIndex \? 'step' : null"/)
  assert.doesNotMatch(bar, /v-html="artifact/)
})

test('missing decision context leaves an honest visible chain shell and disables forward navigation', () => {
  assert.match(bar, /<section class="decision-context-bar" data-testid="decision-context-bar">/)
  assert.match(bar, /v-else class="decision-context-bar__asset decision-context-bar__asset--missing"/)
  assert.match(bar, /未选择研究对象/)
  assert.match(bar, /数据截止与来源未知/)
  assert.match(bar, /:disabled="!context \|\| stale"/)
  assert.match(bar, /if \(!this\.context \|\| this\.stale\) \{[\s\S]*?return/)
})

test('compiled Less assigns every chain-card section one real column at 390px without clipping', async () => {
  const style = bar.match(/<style lang="less">([\s\S]*?)<\/style>/)
  assert.ok(style, 'DecisionContextBar Less style exists')
  const compiled = postcss.parse((await less.render(style[1])).css)
  const media1100 = compiled.nodes.find(node => node.type === 'atrule' && node.name === 'media' && node.params === '(max-width: 1100px)')
  const media680 = compiled.nodes.find(node => node.type === 'atrule' && node.name === 'media' && node.params === '(max-width: 680px)')
  assert.ok(media1100)
  assert.ok(media680)

  const chain = directDeclarations(compiled, '.decision-context-bar__chain')
  const artifact = directDeclarations(compiled, '.decision-context-bar__artifact')
  assert.equal(chain['grid-column-start'], '1')
  assert.equal(chain['grid-column-end'], '-1')
  assert.equal(artifact['grid-column-start'], '1')
  assert.equal(artifact['grid-column-end'], '3')

  const mediumPlacement = {
    '.decision-context-bar__chain': ['1', '3', '1', '2'],
    '.decision-context-bar__asset': ['1', '2', '2', '3'],
    '.decision-context-bar__actions': ['2', '3', '2', '3'],
    '.decision-context-bar__meta': ['1', '3', '3', '4'],
    '.decision-context-bar__artifact': ['1', '3', '4', '5']
  }
  for (const [selector, values] of Object.entries(mediumPlacement)) {
    const declarations = directDeclarations(media1100, selector)
    assert.deepEqual(
      [declarations['grid-column-start'], declarations['grid-column-end'], declarations['grid-row-start'], declarations['grid-row-end']],
      values,
      `${selector} has non-conflicting 768px placement`
    )
  }

  assert.equal(directDeclarations(media680, '.decision-context-bar')['grid-template-columns'], 'minmax(0, 1fr)')
  for (const selector of Object.keys(mediumPlacement)) {
    const declarations = directDeclarations(media680, selector)
    assert.deepEqual(
      [declarations['grid-column-start'], declarations['grid-column-end'], declarations['grid-row-start'], declarations['grid-row-end'], declarations['min-width']],
      ['1', '2', 'auto', 'auto', '0'],
      `${selector} stays in the sole explicit 390px column`
    )
  }

  compiled.walkDecls(declaration => {
    assert.notEqual(declaration.prop, 'grid-column', 'Less must not evaluate a grid-column division')
    if (/^overflow(?:-[xy])?$/.test(declaration.prop)) {
      assert.notEqual(declaration.value, 'hidden', 'no evidence may be hidden to mask overflow')
    }
  })
  assert.equal(directDeclarations(compiled, '.decision-context-bar__asset strong')['overflow-wrap'], 'anywhere')
  assert.equal(directDeclarations(compiled, '.decision-context-bar__artifact small')['overflow-wrap'], 'anywhere')
})
