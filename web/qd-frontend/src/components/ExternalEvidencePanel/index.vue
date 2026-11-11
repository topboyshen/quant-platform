<template>
  <section class="evidence-panel" :class="{ 'evidence-panel--dark': dark }" data-testid="external-evidence-panel">
    <h4>{{ title }}</h4>
    <p v-if="!entries.length" class="evidence-muted">此{{ references === null ? '分支' : '记录' }}没有已保存的外部证据引用。</p>
    <article v-for="entry in entries" :key="entry.id">
      <p><strong>{{ entry.payload ? entry.payload.title : '已保存的外部研究证据' }}</strong></p>
      <p class="evidence-muted">{{ entry.payload ? entry.payload.originLabel : '引用已保存，原文暂未载入' }}</p>
      <dl v-if="entry.payload">
        <div><dt>研究标的（用户确认）</dt><dd>{{ entry.payload.subject && entry.payload.subject.symbol }} · A 股日线</dd></div>
        <div><dt>报告日期</dt><dd>{{ value(entry.payload.reportDate) }}</dd></div>
        <div><dt>生成时间</dt><dd>{{ value(entry.payload.generatedAt) }}</dd></div>
        <div><dt>源时间</dt><dd>{{ value(entry.payload.sourceTime) }}</dd></div>
        <div><dt>行情截止</dt><dd>{{ value(entry.payload.marketCutoff) }}</dd></div>
        <div><dt>{{ entry.payload.source === 'nng8' ? '包采集时间' : '来源捕获身份时间' }}</dt><dd>{{ value(entry.payload.capturedAt) }}</dd></div>
      </dl>
      <p class="evidence-muted">原文是研究依据；标的和假设由用户选择，报告没有自动成为策略规则。未知业务时间保持未知。</p>
      <details v-if="entry.payload" data-testid="external-evidence-original">
        <summary>查看保存的原文与风险</summary>
        <p v-for="(line, i) in entry.payload.conclusion || []" :key="'c' + i">{{ line }}</p>
        <p v-for="(line, i) in entry.payload.risks || []" :key="'r' + i" class="evidence-risk">{{ line }}</p>
        <div v-for="(block, i) in entry.payload.blocks || []" :key="'b' + i"><h5 v-if="block.heading">{{ block.heading }}</h5><p>{{ block.text }}</p></div>
      </details>
      <details class="evidence-muted"><summary>证据身份</summary><p>产物 {{ entry.id }}</p><p>来源报告 {{ identity(entry).reportId }}</p><p>来源内容 SHA256 {{ identity(entry).contentSha256 }}</p><p>归档快照 SHA256 {{ value(entry.payloadSha256) }}</p><p>来源捕获身份时间 {{ value(identity(entry).capturedAt) }}（未知时不借用同步或归档时间）</p></details>
    </article>
  </section>
</template>

<script>
import { evidenceEntries } from '@/utils/nng8ResearchLink'
export default {
  name: 'ExternalEvidencePanel',
  props: { artifact: { type: Object, default: null }, references: { type: Array, default: null }, artifacts: { type: Object, default: () => ({}) }, title: { type: String, default: '外部研究依据' } },
  computed: {
    entries () { return evidenceEntries(this.artifact, this.references, this.artifacts) },
    dark () { return Boolean(this.$store && this.$store.state && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme)) }
  },
  methods: { value (v) { return v === null || v === undefined || v === '' ? '未知' : v }, identity (entry) { return entry.payload || entry.reference || {} } }
}
</script>

<style scoped>
.evidence-panel { color: #293b32; background: #f5f8f6; border: 1px solid #d7e2da; border-radius: 8px; padding: 14px; margin-bottom: 12px; min-width: 0; overflow-wrap: anywhere; }
.evidence-panel h4 { color: inherit; margin: 0 0 10px; }.evidence-panel h5 { color: inherit; }.evidence-panel p { white-space: pre-wrap; margin: 7px 0; }.evidence-panel dl { margin: 10px 0; }.evidence-panel dl div { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 8px; }.evidence-panel dd { margin: 0; }.evidence-panel details { margin-top: 10px; }.evidence-panel summary { cursor: pointer; }.evidence-muted { color: #566d60; font-size: 12px; }.evidence-risk { border-left: 2px solid #a66c28; padding-left: 8px; }
.evidence-panel--dark { color: #e1ece5; background: #24322a; border-color: #425b4b; }.evidence-panel--dark .evidence-muted { color: #b4c8bb; }
</style>
