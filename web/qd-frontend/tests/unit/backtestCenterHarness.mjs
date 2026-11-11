// 真实的 backtest-center 组件调用链脚手架：
// 读 .vue 的 <script> 段 → 把 import 换成可注入桩 → 生成临时 ESM 模块并 import
// → 用真实 data/methods/computed 构建实例 → 驱动真实方法链。
// 不重写被测逻辑，只提供宿主环境；不使用动态求值。
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
let stubRegistry = null
let moduleCounter = 0

/** 由生成的临时模块调用：把 import 换成注入桩。 */
export function __resolveStub (spec) {
  if (!stubRegistry) throw new Error('桩未初始化')
  return stubRegistry(spec)
}

function extractScript (sfcPath) {
  const raw = fs.readFileSync(sfcPath, 'utf8')
  const m = /<script>([\s\S]*?)<\/script>/.exec(raw)
  if (!m) throw new Error('未找到 <script> 段')
  return m[1]
}

/** 把 ESM import 改写成从 __stub(...) 取值，保留真实解构语义。 */
function rewriteImports (code) {
  const importRe = /^import\s+([\s\S]*?)\s+from\s+'([^']+)'\s*$/gm
  return code
    .replace(importRe, (all, clause, spec) => {
      const trimmed = clause.trim()
      const quoted = JSON.stringify(spec)
      const star = /^\*\s+as\s+([A-Za-z0-9_$]+)$/.exec(trimmed)
      if (star) return `const ${star[1]} = __stub(${quoted})`
      if (trimmed.startsWith('{')) return `const ${trimmed} = __stub(${quoted})`
      return `const ${trimmed} = __stub(${quoted}).default`
    })
    .replace(/^import\s+'[^']+'\s*$/gm, '')
}

function chainableStub () {
  const target = function () { return proxy }
  const proxy = new Proxy(target, {
    get (_t, prop) {
      if (prop === 'default') return proxy
      if (prop === 'then') return undefined
      if (prop === 'valueOf') return () => 0
      if (prop === 'toString') return () => ''
      return proxy
    },
    apply () { return proxy }
  })
  return proxy
}

export async function instantiateBacktestCenter ({ sfcPath, api = {}, real = {}, overrides = {} }) {
  // 不能展开 api：展开会丢掉调用方传入的动态桩（Proxy），需原样读取
  const apiModule = new Proxy(api, {
    get (t, prop) {
      const value = t[prop]
      return value === undefined ? async () => ({ data: [] }) : value
    }
  })
  stubRegistry = (spec) => {
    if (spec === './historyState.js') return real.historyState
    if (spec === '@/api/strategy') return apiModule
    if (spec === '@/utils/strategyResearchContracts.mjs') return real.contracts
    if (spec === '@/utils/researchLinkRun.js') return real.researchLinkRun || chainableStub()
    if (spec === '@/utils/researchLinkAI.js') return real.researchLinkAI || chainableStub()
    if (spec === 'vuex') return { mapState: () => ({}), mapGetters: () => ({}), mapActions: () => ({}), mapMutations: () => ({}) }
    return chainableStub()
  }

  const code = rewriteImports(extractScript(sfcPath))
  const harnessUrl = pathToFileURL(path.join(here, 'backtestCenterHarness.mjs')).href
  const file = path.join(os.tmpdir(), `bc-harness-${process.pid}-${moduleCounter++}.mjs`)
  fs.writeFileSync(file, `import { __resolveStub as __stub } from ${JSON.stringify(harnessUrl)}\n${code}\n`)
  let options
  try {
    const mod = await import(`${pathToFileURL(file).href}?v=${moduleCounter}`)
    options = mod.default
  } finally {
    try { fs.unlinkSync(file) } catch { /* 清理失败不影响测试 */ }
    stubRegistry = null
  }
  if (!options) throw new Error('未能取得组件 options')

  const messages = { error: [], warning: [], success: [], info: [] }
  const vm = {}
  vm.$t = (key) => key
  vm.$message = Object.keys(messages).reduce((acc, kind) => {
    acc[kind] = (text) => { messages[kind].push(text) }
    return acc
  }, {})
  vm.$nextTick = (fn) => (typeof fn === 'function' ? Promise.resolve().then(fn) : Promise.resolve())
  vm.$store = { state: {}, getters: {}, dispatch: async () => ({}), commit: () => {}, watch: () => () => {} }
  vm.$route = { query: {}, params: {} }
  vm.$router = { push: () => {}, replace: () => {} }
  vm.$refs = {}
  vm.$watch = () => () => {}
  vm.$set = (obj, key, value) => { obj[key] = value }
  vm.$forceUpdate = () => {}

  Object.assign(vm, typeof options.data === 'function' ? options.data.call(vm) : {})
  for (const [name, fn] of Object.entries(options.methods || {})) vm[name] = fn.bind(vm)
  // 生命周期钩子（activated/deactivated/beforeDestroy 等）是顶层选项，不住在 methods 里
  for (const [name, value] of Object.entries(options)) {
    if (typeof value !== 'function' || ['data', 'setup', 'render', 'beforeCreate'].includes(name)) continue
    if (vm[name] === undefined) vm[name] = value.bind(vm)
  }
  for (const [name, def] of Object.entries(options.computed || {})) {
    const getter = typeof def === 'function' ? def : def.get
    if (!getter) continue
    Object.defineProperty(vm, name, {
      get: getter.bind(vm),
      set: def && def.set ? def.set.bind(vm) : undefined,
      configurable: true
    })
  }
  // 覆盖项一律落成可写数据属性：即使同名是 computed（无 setter）也要能遮蔽
  for (const [key, value] of Object.entries(overrides)) {
    Object.defineProperty(vm, key, { value, writable: true, configurable: true, enumerable: true })
  }
  vm.__messages = messages
  return vm
}

export function deferred () {
  let resolveFn
  let rejectFn
  const promise = new Promise((resolve, reject) => { resolveFn = resolve; rejectFn = reject })
  return { promise, resolve: resolveFn, reject: rejectFn }
}
