import assert from 'node:assert/strict'
import test from 'node:test'

import {
  informativeCopilotTargetName,
  resolveCopilotTargetName,
  sameCopilotTarget
} from '../../src/utils/copilotTargetNames.js'

const changbaishan = { market: 'CNStock', symbol: '603099', name: '长白山' }

test('a route handoff with only market and symbol cannot downgrade the current asset name', () => {
  assert.equal(resolveCopilotTargetName(
    { market: 'CNStock', symbol: '603099', name: '' },
    { current: changbaishan }
  ), '长白山')
  assert.equal(resolveCopilotTargetName(
    { market: 'CNStock', symbol: '603099', name: '603099' },
    { current: changbaishan }
  ), '长白山')
})

test('switching A to B never carries the A name into B', () => {
  const target = { market: 'CNStock', symbol: '300088', name: '' }
  assert.equal(resolveCopilotTargetName(target, {
    current: changbaishan,
    watchlist: [{ market: 'CNStock', symbol: '300088', name: '长信科技' }]
  }), '长信科技')
  assert.equal(resolveCopilotTargetName(target, { current: changbaishan }), '300088')
})

test('watchlist and canonical names repair a symbol-only local context', () => {
  assert.equal(resolveCopilotTargetName(
    { market: 'CNStock', symbol: '603099' },
    {
      current: { market: 'CNStock', symbol: '603099', name: '603099' },
      watchlist: [changbaishan]
    }
  ), '长白山')
  assert.equal(resolveCopilotTargetName(
    { market: 'CNStock', symbol: '603099' },
    { canonical: changbaishan }
  ), '长白山')
})

test('name helpers reject symbol placeholders and require an exact instrument match', () => {
  assert.equal(informativeCopilotTargetName('603099', '603099'), '')
  assert.equal(informativeCopilotTargetName('长白山', '603099'), '长白山')
  assert.equal(sameCopilotTarget(changbaishan, { market: 'CNStock', symbol: '603099' }), true)
  assert.equal(sameCopilotTarget(changbaishan, { market: 'USStock', symbol: '603099' }), false)
})
