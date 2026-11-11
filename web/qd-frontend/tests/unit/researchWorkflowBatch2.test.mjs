import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { clone, deferred, harness, httpError } from './researchWorkflowBatch1Harness.mjs'
import { buildParameterMapping, defaultMappings, emptyStrategyDraft, identityCompatible, normalizeStrategyDraft, parameterType, parseDeclaredValue, validateStrategySchema } from '../../src/views/research-lab-v2/paramMappingLogic.js'

const specs = [
  { name: 'period', type: 'integer', default: 20, min: 0, max: 100, step: 10, unit: 'bars' },
  { name: 'threshold', type: 'number', default: 1.1, min: 0, max: 5, step: 0.1, unit: 'multiple' },
  { name: 'enabled', type: 'boolean', default: true },
  { name: 'label', type: 'text', default: 'old' },
  { name: 'mode', type: 'enum', default: 'fast', choices: ['fast', 'slow'] }
]
const values = { period: 40, threshold: 1.2, enabled: false, label: '', mode: 'slow' }
function schema (sourceId = 11, changes = {}) {
  return { sourceId, codeSha256: 'source-code-' + sourceId, contractHash: 'source-contract-' + sourceId, paramSchema: clone(specs), params: { declared: Object.fromEntries(specs.map(s => [s.name, s.default])), effective: Object.fromEntries(specs.map(s => [s.name, s.default])) }, ...changes }
}
function indicator (id = 'I_A', effective = values) {
  return { id, payloadSha256: 'indicator-sha-' + id, params: { effective: clone(effective) }, payload: { indicator: { legacyId: 1 }, params: { specs: clone(specs) } } }
}
function mapped (sc = schema(), ind = indicator()) {
  return { ...emptyStrategyDraft(), sourceId: sc.sourceId, handoffArtifactId: ind.id, indicatorPayloadSha256: ind.payloadSha256,
    mappings: Object.fromEntries(specs.map(s => [s.name, { mode: 'identity', sourceParam: s.name, value: sc.params.effective[s.name], reason: '' }])) }
}
async function component () {
  const h = await harness()
  h.artifacts.I_A = indicator(); h.page.artifactCache.I_A = clone(h.artifacts.I_A)
  h.page.strat = mapped()
  h.api.strategyParamSchema = async (bid, sourceId) => ({ data: schema(sourceId) })
  await h.page.loadStrategySchema()
  return h
}

test('five declared target parameters map every explicit source, not the first parameter', () => {
  const result = buildParameterMapping(mapped(), schema(), indicator())
  assert.equal(result.ok, true)
  assert.deepEqual(result.effective, values)
  assert.deepEqual(result.applied, values)
  assert.equal(result.paramMapping.length, 5)
  assert.deepEqual(result.paramMapping.map(p => p.from), specs.map(s => 'indicator.' + s.name))
})
test('default-only targets keep saved schema values and produce no fictitious mapping', () => {
  const sc = schema(); sc.params.effective.period = 60
  const draft = { ...emptyStrategyDraft(), sourceId: 11, mappings: defaultMappings(sc) }
  const result = buildParameterMapping(draft, sc, null)
  assert.equal(result.ok, true); assert.equal(result.effective.period, 60)
  assert.deepEqual(result.applied, {}); assert.deepEqual(result.paramMapping, [])
})
for (const [name, raw, expected] of [['period', '0', 0], ['enabled', false, false], ['label', '', ''], ['threshold', '1.1', 1.1]]) {
  test(`declared ${name} retains ${JSON.stringify(raw)} as a real value`, () => {
    const parsed = parseDeclaredValue(specs.find(s => s.name === name), raw)
    assert.deepEqual(parsed, { ok: true, value: expected })
  })
}
for (const [name, raw] of [['period', ''], ['period', true], ['period', 1.5], ['period', 30.1], ['period', 110], ['threshold', Infinity], ['enabled', 'maybe'], ['label', 0], ['mode', 'unlisted']]) {
  test(`invalid declaration value ${name}=${String(raw)} is explicitly refused`, () => assert.equal(parseDeclaredValue(specs.find(s => s.name === name), raw).ok, false))
}
test('legacy missing type uses declaration default only, never the edited input', () => {
  assert.equal(parameterType({ name: 'ratio', default: 20 }), 'integer')
  assert.equal(parameterType({ name: 'period', default: true }), 'boolean')
  assert.equal(parseDeclaredValue({ name: 'period', default: true }, 1).ok, false)
  assert.equal(parseDeclaredValue({ name: 'ratio', type: 'mystery', default: 1 }, 1).ok, false)
})
test('optional parameter without default can stay absent; required absent is refused', () => {
  const sc = schema(); sc.paramSchema.push({ name: 'optional', type: 'string' })
  assert.equal(validateStrategySchema(sc, 11).ok, true)
  sc.paramSchema.at(-1).required = true
  assert.equal(validateStrategySchema(sc, 11).ok, false)
})
test('unit mismatch refuses identity, missing units remain unknown instead of guessed', () => {
  assert.equal(identityCompatible({ type: 'integer', unit: 'days' }, { type: 'integer', unit: 'bars' }), false)
  assert.equal(identityCompatible({ type: 'integer', unit: '' }, { type: 'number', unit: 'bars' }), true)
  const src = indicator(); src.payload.params.specs[0].unit = 'days'
  assert.equal(buildParameterMapping(mapped(schema(), src), schema(), src).ok, false)
})
test('numeric identity checks target integrality rather than silently truncating a float', () => {
  const src = indicator(); src.payload.params.specs[0].type = 'number'; src.params.effective.period = 40.5
  assert.equal(buildParameterMapping(mapped(schema(), src), schema(), src).ok, false)
})
for (const change of ['target', 'source', 'hash', 'branch', 'transform']) {
  test(`mapping rejects changed ${change} rather than selecting another value`, () => {
    const ind = indicator(); const draft = mapped()
    if (change === 'target') draft.mappings.other = { mode: 'identity', sourceParam: 'period' }
    if (change === 'source') draft.mappings.period.sourceParam = 'doesNotExist'
    if (change === 'hash') draft.indicatorPayloadSha256 = 'changed'
    if (change === 'branch') draft.handoffArtifactId = 'I_B'
    if (change === 'transform') draft.mappings.period.mode = 'multiply'
    assert.equal(buildParameterMapping(draft, schema(), ind).ok, false)
  })
}
test('manual override records an explicit reason and cannot masquerade as identity', () => {
  const draft = mapped(); draft.mappings.period = { mode: 'manual_override', sourceParam: 'period', value: 60, reason: '' }
  assert.equal(buildParameterMapping(draft, schema(), indicator()).ok, false)
  draft.mappings.period.reason = '测试独立 60 周期假设'
  const result = buildParameterMapping(draft, schema(), indicator())
  assert.equal(result.ok, true); assert.equal(result.effective.period, 60)
  assert.deepEqual(result.paramMapping[0], { from: 'indicator.period', to: 'period', value: 60, transform: 'manual_override', reason: '测试独立 60 周期假设' })
})
test('legacy scalar is retained only as unverified draft, never promoted to new mapping', () => {
  const draft = normalizeStrategyDraft({ mapped: 40, sourceId: 11, mappingConfirmed: true })
  assert.equal(draft.legacyMapping, true); assert.equal(draft.mappingConfirmed, false)
  assert.deepEqual(draft.mappings, {}); assert.equal(draft.mapped, 40)
})
test('component captures all five values with immutable source and contract fingerprints', async () => {
  const h = await component()
  h.page.confirmMapping(true)
  assert.equal(h.page.mappingReady, true)
  let captured
  h.api.createArtifact = async (bid, body) => {
    captured = clone(body)
    const artifact = { id: 'new-strategy', params: { effective: clone(body.from.applied) } }
    h.artifacts[artifact.id] = artifact
    return { data: { artifact, branch: clone(h.page.branch) } }
  }
  await h.page.confirmStrategy()
  assert.equal(captured.paramMappingVersion, 1)
  assert.deepEqual(captured.from.applied, values)
  assert.equal(captured.from.indicatorArtifactId, 'I_A')
  assert.equal(captured.from.expectedIndicatorPayloadSha256, 'indicator-sha-I_A')
  assert.equal(captured.from.expectedCodeHash, 'source-code-11')
  assert.equal(captured.from.expectedContractHash, 'source-contract-11')
})
test('component editing mapping invalidates approval and requires a new confirmation', async () => {
  const h = await component(); h.page.confirmMapping(true)
  h.page.setMapping('period', 'mode', 'manual_override')
  h.page.setMapping('period', 'value', '60')
  assert.equal(h.page.mappingReady, false)
  h.page.confirmMapping(true)
  assert.equal(h.page.strat.mappingConfirmed, false)
  h.page.setMapping('period', 'reason', 'new hypothesis')
  h.page.confirmMapping(true)
  assert.equal(h.page.mappingReady, true); assert.equal(h.page.mappingValidation.effective.period, 60)
})
test('schema failure cannot enable guessed mappings or a POST', async () => {
  const h = await component(); h.api.strategyParamSchema = async () => { throw httpError(403) }
  await h.page.loadStrategySchema()
  assert.equal(h.page.strategySchemaStatus, 'failed'); assert.equal(h.page.mappingReady, false)
  assert.match(h.page.strategySchemaError, /未退回猜测参数/)
  let count = 0; h.api.createArtifact = async () => { count++ }
  await h.page.confirmStrategy(); assert.equal(count, 0)
})
test('source version changes require renewed approval, old hash never confirms new schema', async () => {
  const h = await component(); h.page.confirmMapping(true)
  h.api.strategyParamSchema = async () => ({ data: schema(11, { codeSha256: 'new-code' }) })
  await h.page.loadStrategySchema()
  assert.equal(h.page.strat.mappingConfirmed, false); assert.equal(h.page.mappingReady, false)
  assert.equal(h.page.strategySchema.codeSha256, 'new-code')
})
test('late schema response after branch switch cannot install A schema in B', async () => {
  const h = await component(); const wait = deferred()
  h.api.strategyParamSchema = (bid, id) => bid === 'A' ? wait.promise : Promise.resolve({ data: schema(id) })
  const pending = h.page.loadStrategySchema()
  await h.page.loadBranch('B')
  const state = clone(h.page.strat); const before = clone(h.page.strategySchema)
  wait.resolve({ data: schema(11, { codeSha256: 'late-A' }) }); await pending
  assert.deepEqual(clone(h.page.strat), state); assert.deepEqual(clone(h.page.strategySchema), before)
})
test('late schema response after draft edit cannot install or confirm its captured draft', async () => {
  const h = await component(); const wait = deferred()
  h.api.strategyParamSchema = () => wait.promise
  const pending = h.page.loadStrategySchema(); h.page.setStrat('code', 'changed-draft')
  wait.resolve({ data: schema(11) }); await pending
  assert.equal(h.page.strategySchema, null); assert.equal(h.page.mappingReady, false)
  assert.equal(h.page.strat.code, 'changed-draft'); assert.equal(h.page.strategySchemaStatus, 'idle')
})
test('late schema failure after deactivation cannot show an error in a new generation', async () => {
  const h = await component(); const wait = deferred(); h.api.strategyParamSchema = () => wait.promise
  const pending = h.page.loadStrategySchema(); h.component.deactivated.call(h.page)
  wait.reject(httpError(403)); await pending
  assert.equal(h.page.strategySchema, null); assert.equal(h.page.strategySchemaError, '')
})
test('backtest typed controls preserve false, zero and empty string, with separate clear action', async () => {
  const h = await component()
  h.page.artifactCache.S_A.payload.paramSchema = clone(specs)
  h.page.artifactCache.S_A.params.effective = clone(values)
  h.page.setOverride('period', '0'); h.page.setOverride('enabled', false); h.page.setOverride('label', '')
  assert.deepEqual(clone(h.page.runBody().config.paramsOverride), { period: 0, enabled: false, label: '' })
  h.page.clearOverride('label')
  assert.deepEqual(clone(h.page.runBody().config.paramsOverride), { period: 0, enabled: false })
  assert.equal(h.page.hasOverride('label'), false)
})
test('invalid override refuses edit; corrupted persisted value refuses entire run payload', async () => {
  const h = await component(); h.page.artifactCache.S_A.payload.paramSchema = clone(specs)
  h.page.setOverride('period', '40.5'); assert.equal(h.page.hasOverride('period'), false)
  h.page.bt.override = { period: 40, enabled: 'false-looking-word' }
  assert.throws(() => h.page.runBody(), /布尔值/)
})
test('undeclared string override is refused rather than inferred as a text parameter', async () => {
  const h = await component()
  h.page.setOverride('notDeclared', 'value')
  assert.equal(h.page.hasOverride('notDeclared'), false)
  h.page.bt.override = { notDeclared: 'value' }
  assert.throws(() => h.page.runBody(), /类型未声明或不支持/)
})
test('legacy raw schema minimum/maximum aliases constrain actual component overrides', async () => {
  const h = await component()
  h.page.artifactCache.S_A.payload.paramSchema = [{ name: 'period', type: 'integer', default: 20, minimum: 1, maximum: 60 }]
  h.page.setOverride('period', 0); assert.equal(h.page.hasOverride('period'), false)
  h.page.setOverride('period', 70); assert.equal(h.page.hasOverride('period'), false)
  h.page.setOverride('period', 40); assert.equal(h.page.bt.override.period, 40)
  h.page.bt.override.period = 70
  assert.throws(() => h.page.runBody(), /超出声明范围/)
})
test('explicit null legacy min/max suppress aliases exactly as source compiler', async () => {
  const h = await component()
  h.page.artifactCache.S_A.payload.paramSchema = [{ name: 'period', type: 'integer', default: 20, min: null, max: null, minimum: 1, maximum: 60 }]
  h.page.setOverride('period', 0); assert.equal(h.page.bt.override.period, 0)
  h.page.setOverride('period', 70); assert.equal(h.page.bt.override.period, 70)
})
test('decimal step tolerance matches server: exact 0.3 allowed, 0.3000000005 refused', () => {
  const spec = { name: 'ratio', type: 'number', default: 0.1, min: 0.1, max: 0.9, step: 0.2 }
  assert.deepEqual(parseDeclaredValue(spec, 0.3), { ok: true, value: 0.3 })
  assert.equal(parseDeclaredValue(spec, 0.3000000005).ok, false)
})
test('new mapping table stays inside shrinkable mobile grid and scrolls instead of hiding data', () => {
  const source = readFileSync(new URL('../../src/views/research-lab-v2/index.vue', import.meta.url), 'utf8')
  const css = source.split('<style')[1]
  assert.match(css, /@media\s*\(max-width:\s*900px\)\s*\{\s*\.rl2-grid\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
  assert.match(css, /\.rl2-main,\s*\.rl2-side\s*\{\s*min-width:\s*0/)
  const scroll = css.match(/\.rl2-param-scroll\s*\{([^}]+)\}/)[1]
  assert.match(scroll, /min-width:\s*0/)
  assert.match(scroll, /width:\s*100%/)
  assert.match(scroll, /overflow-x:\s*auto/)
  assert.doesNotMatch(scroll, /hidden/)
})
