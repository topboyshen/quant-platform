<template>
  <div class="personal-reports" :class="{ 'reports--dark': reportsDark }" data-testid="report-center">
    <a-card v-if="section === 'reports'" :bordered="false">
      <div class="reports-head">
        <div>
          <div class="reports-title">报告与证据</div>
          <div class="reports-sub">AStock、Profit、ETF 和运行报告在同一目录。选择来源后可继续研究。</div>
        </div>
        <div class="reports-head-right">
          <a-tag :color="antColor('grey')" data-testid="report-observed">最近读取 {{ observedAt }}</a-tag>
          <a-tag :color="antColor(stateCopy.tone)" data-testid="report-state">{{ stateCopy.label }}</a-tag>
          <a-button size="small" :loading="loading" data-testid="report-refresh" @click="reload">刷新</a-button>
          <a-button size="small" class="reports-back" data-testid="report-back-list" @click="backToList">返回列表</a-button>
          <a-button size="small" type="link" data-testid="report-back-workbench" @click="backToWorkbench">返回工作台</a-button>
        </div>
      </div>

      <div class="reports-banner" :class="'banner-' + stateCopy.tone" data-testid="report-banner">{{ bannerText }}</div>

      <div class="reports-filters">
        <button
          v-for="option in groupFilters"
          :key="option.value"
          class="chip"
          :data-testid="'report-group-' + option.value"
          :aria-pressed="String(group === option.value)"
          @click="selectGroup(option.value)"
        >{{ option.label }}</button>
        <a-input-search
          v-model="query"
          class="reports-search"
          data-testid="report-search"
          placeholder="搜索标题、正文或阻断原因"
          :allow-clear="true"
          @change="onQueryChange"
        />
        <label class="reports-attn">
          <input type="checkbox" :checked="attentionOnly" @change="onAttentionChange" /> 只看需关注
        </label>
      </div>

      <p v-if="externalError" class="reader-reason" role="alert">AStock 来源：{{ externalError }}；其他来源仍可独立查看。</p>
      <p v-if="localError" class="reader-reason" role="alert">Quant-Server 来源：{{ localError }}；其他来源仍可独立查看。</p>

      <div class="reports-grid" :class="{ 'is-detail': mobileDetail }">
        <div class="reports-panel reports-catalogue-panel">
          <div class="reports-catalogue" data-testid="report-rows">
            <div v-if="!visibleFamilies.length" class="reports-empty">
              {{ rows.length ? '没有匹配的报告（可清空筛选）' : '没有报告' }}
            </div>
            <button
              v-for="fam in visibleFamilies"
              :key="fam.familyKey"
              class="item"
              :aria-current="String(isActive(fam))"
              @click="selectFamily(fam)"
            >
              <div class="t">{{ fam.title }}</div>
              <div v-if="primaryOf(fam).readable" class="c">
                <span v-if="primaryOf(fam).conclusion.kind === 'excerpt'" class="excerpt">原文摘要 · </span>
                <span>{{ primaryOf(fam).conclusion.text }}</span>
              </div>
              <div v-else class="c">不可读取：{{ primaryOf(fam).detail || '后端未提供原因' }}</div>
              <div v-if="primaryOf(fam).keyReason" class="c">{{ primaryOf(fam).keyReason }}</div>
              <div class="m">
                <a-tag :color="antColor(primaryOf(fam).business.tone)" data-testid="report-status">
                  {{ primaryOf(fam).business.label }}
                </a-tag>
                <span>生成 {{ fmt(primaryOf(fam).generatedAt) }}</span>
                <span>行情截至 {{ fmt(primaryOf(fam).marketCutoff) }}</span>
                <span v-if="fam.variants.length > 1">{{ fam.variants.length }} 个格式</span>
              </div>
            </button>
          </div>
        </div>

        <div class="reports-panel reports-reader-panel" data-testid="report-reader">
          <div v-if="selectionNote" class="reports-banner banner-orange" role="status" aria-live="polite" data-testid="report-selection-note">{{ selectionNote }}</div>
          <div v-if="!selected && !selectionNote" class="reports-empty">请选择左侧报告。</div>
          <template v-if="selected">
            <h2 class="reader-title">{{ selected.title }}</h2>
            <div v-if="readerVariants.length > 1" class="reader-variants">
              <button
                v-for="(variant, variantIndex) in readerVariants"
                :key="variant.keyAmbiguous ? 'ambiguous-option-' + variantIndex : variant.key"
                class="chip"
                :disabled="variant.keyAmbiguous || !variant.key"
                :aria-pressed="String(variant.key === selected.key)"
                @click="selectVariant(variant)"
              >{{ variant.variantLabel }} · {{ variant.keyAmbiguous ? '标识重复，不可选择' : variant.readability.label }}</button>
            </div>
            <div class="reader-times">
              <div><span>生成时间</span><b :class="{ missing: selected.generatedAtMissing }">{{ fmt(selected.generatedAt) }}</b></div>
              <div><span>行情截至</span><b :class="{ missing: !selected.marketCutoff }">{{ fmt(selected.marketCutoff) }}</b></div>
              <div><span>最近读取</span><b>{{ fmt(selected.observedAt) }}</b></div>
              <div><span>可读取性</span><b>{{ selected.readability.label }}</b></div>
            </div>
            <div class="reader-meta">{{ selected.groupLabel }} · {{ selected.variantLabel }}</div>
            <div class="reports-banner" :class="'banner-' + selected.business.tone" data-testid="report-business-state">
              <strong>{{ selected.business.label }}</strong><span v-if="selected.business.reason"> · {{ selected.business.reason }}</span>
            </div>
            <nng8-research-link
              v-if="selected.report"
              :key="selected.key"
              :report="selected.report"
              :captured-at="selected.capturedAt === undefined ? null : selected.capturedAt"
              :account-id="accountId"
              :source-ready="!!selected.identity && !loading && !unauthorized" />
            <p v-if="!selected.identity" class="reader-limit">{{ selected.archiveReason }}</p>

            <template v-if="selected.readable">
              <p class="reader-lead" :class="{ missing: selected.conclusion.kind === 'missing' }">
                <a-tag v-if="selected.conclusion.kind === 'excerpt'">原文摘要</a-tag>
                <span>{{ selected.conclusion.text }}</span>
              </p>
              <div class="reader-limit">
                可读取 ≠ 业务成功或盈利。时间口径：生成时间取报告自身字段；行情截至取该份报告覆盖的截止日；
                两者缺失一律显示「未知」，不使用文件时间或同步时间顶替。
                <template v-if="selected.detail"><br />后端说明：{{ selected.detail }}</template>
              </div>
              <div v-if="selected.blocks.length" class="reader-original" data-testid="report-full-content">
                <section v-for="(block, index) in selected.blocks" :key="index"><h3 v-if="block.heading">{{ block.heading }}</h3><p>{{ block.text }}</p></section>
              </div>
              <p v-else class="reader-body-note">来源当前只提供摘要；完整正文未取得。</p>
            </template>

            <template v-else>
              <div class="reader-reason" data-testid="report-reason">
                <div>这份报告当前不可读取。</div>
                <div>后端原因：{{ selected.detail || '未提供原因' }}</div>
              </div>
              <div class="reader-limit">不可读取不等于没有内容，也不代表业务失败；此处只陈述走不通的原因，不推断结论。</div>
            </template>

            <details class="reader-tech">
              <summary>技术详情</summary>
              <div class="kv">
                <b>来源</b><span>{{ selected.sourcePath || '（v1 未返回路径）' }}</span>
                <b>格式</b><span>{{ selected.format || '未知' }}</span>
                <b>schema 版本</b><span>{{ selected.schemaVersion || '未知' }}（格式版本，不是报告版本）</span>
                <b>文件时间</b><span>{{ fmt(selected.fileMtime) }}</span>
                <b>读取时间</b><span>{{ fmt(selected.observedAt) }}</span>
                <b>状态码</b><span>{{ selected.status }}</span>
                <b>报告键</b><span>{{ selected.key || '（无法识别）' }}</span>
                <b>行情截止来源</b><span>{{ selected.marketCutoffFrom || '（无）' }}</span>
                <b>摘要字段</b><span>{{ selected.summaryKeys.join(', ') || '（无）' }}</span>
                <b>错误原因</b><span>{{ selected.detail || '—' }}</span>
                <template v-if="selected.report"><b>来源内容摘要</b><span>{{ selected.report.content_sha256 || '未知' }}</span><b>原报告摘要</b><span>{{ selected.report.source_report_sha256 || '来源未提供' }}</span><b>数据模式 / 发布门禁</b><span>{{ selected.report.data_mode || '未知' }} / {{ selected.report.publication_gate || '未知' }}</span></template>
              </div>
            </details>
          </template>
        </div>
      </div>

      <div class="reports-note">报告生成、行情截止与读取时间分别显示。检查完成和策略有效性分别核对；缺失证据保持未知。</div>
    </a-card>
    <unified-research-evidence v-if="section !== 'reports'" :key="section" :section="section" />
  </div>
</template>

<script>
import { getWorkbenchReports } from '@/api/workbench'
import { getNng8Readonly } from '@/api/unifiedResearch'
import Nng8ResearchLink from '@/components/Nng8ResearchLink'
import UnifiedResearchEvidence from '@/components/UnifiedResearchEvidence'
import { NNG8_READONLY_ENABLED, normalizeNng8, nng8AccountId } from '@/utils/nng8Readonly'
import { researchSection } from '@/utils/unifiedResearch'
import { unifiedReportRows, filterUnifiedReports, REPORT_SOURCE_FILTERS } from '@/utils/unifiedReportCatalog'
import {
  PAGE_STATE, PAGE_STATE_COPY, pageState, resolveSelection,
  findRow, groupByFamily, createLoader
} from '@/utils/reportCatalog'

const UNKNOWN = '未知'

export default {
  name: 'PersonalReports',
  components: { Nng8ResearchLink, UnifiedResearchEvidence },
  data () {
    return {
      nng8Enabled: NNG8_READONLY_ENABLED,
      loading: false,
      error: '',
      unauthorized: false,
      contractOk: false,
      payload: null,
      externalError: '',
      localError: '',
      unsubscribe: null,
      rows: [],
      group: REPORT_SOURCE_FILTERS.some(option => option.value === (this.$route && this.$route.query.group)) ? this.$route.query.group : 'all',
      query: '',
      attentionOnly: false,
      selectedKey: null,
      selectionNote: '',
      mobileDetail: false,
      loader: createLoader()
    }
  },
  computed: {
    accountId () { return nng8AccountId(this.$store && this.$store.getters.userInfo) },
    reportsDark () {
      return Boolean(this.$store && this.$store.state && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme))
    },
    section () { return researchSection(this.$route && this.$route.query.section) },
    state () {
      if (!this.loading && this.contractOk && !this.unauthorized && !this.error && (this.localError || this.externalError)) return PAGE_STATE.PARTIAL
      return pageState({
        loading: this.loading,
error: this.error,
unauthorized: this.unauthorized,
        contractOk: this.contractOk,
reports: this.rows
      })
    },
    stateCopy () {
      if (this.state === PAGE_STATE.AVAILABLE) return { label: this.rows.some(row => row.business.attention) ? '目录已读取 · 有待处理项' : '目录已读取', tone: 'blue' }
      return PAGE_STATE_COPY[this.state] || PAGE_STATE_COPY.unavailable
    },
    observedTagColor () {
      return 'default'
    },
    observedAt () {
      return this.fmt(this.rows.length ? this.rows[0].observedAt : null)
    },
    bannerText () {
      return {
        [PAGE_STATE.AVAILABLE]: '报告目录已读取。每份报告的执行、数据和信号状态见对应条目。',
        [PAGE_STATE.PARTIAL]: '部分报告不可读取：下表已逐项标注，结论请只依据可读取的来源。',
        [PAGE_STATE.EMPTY]: '当前目录里没有报告产物。没有内容不等于读取失败。',
        [PAGE_STATE.LOADING]: '正在读取报告目录…',
        [PAGE_STATE.UNAVAILABLE]: this.error || '报告中心读取失败，请稍后重试。',
        [PAGE_STATE.UNAUTHORIZED]: '登录状态已失效，请重新登录后查看报告。'
      }[this.state]
    },
    groupFilters () {
      return REPORT_SOURCE_FILTERS
    },
    filteredRows () {
      return filterUnifiedReports(this.rows, { group: this.group, query: this.query, attentionOnly: this.attentionOnly })
    },
    visibleFamilies () {
      return groupByFamily(this.filteredRows)
    },
    selected () {
      return findRow(this.filteredRows, this.selectedKey)
    },
    readerVariants () {
      if (!this.selected) return []
      const fam = this.visibleFamilies.find(f => f.variants.some(v => v.key === this.selected.key))
      return fam ? fam.variants : []
    }
  },
  created () {
    if (this.$store && this.$store.subscribe) this.unsubscribe = this.$store.subscribe(m => { if (/(^|\/)SET_(TOKEN|INFO)$/.test(m.type)) { this.loader.next(); this.rows = []; this.loading = false; if (this.section === 'reports') this.$nextTick(() => this.fetchReports()) } })
    if (this.section === 'reports') this.fetchReports()
  },
  watch: {
    '$route.query.group' (value) { if (REPORT_SOURCE_FILTERS.some(option => option.value === value)) this.selectGroup(value) },
    section () {
      this.loader.next()
      this.loading = false
      if (this.section === 'reports') this.fetchReports()
    }
  },
  activated () {
    // keep-alive 返回本页时重新读取，避免展示旧列表。
    // 但首次挂载时 created 已发起请求且仍在途中，此处再发一次会造成重复取数（实测 2 次）——
    // 故加入 in-flight 守卫：已有请求在途中就不再重复发起。
    if (this.loading || this.section !== 'reports') return
    this.fetchReports()
  },
  deactivated () { this.loader.next(); this.loading = false },
  beforeDestroy () { this.loader.next(); if (this.unsubscribe) this.unsubscribe() },
  methods: {
    /**
     * Ant Design Vue 的 a-tag color 只接受预设色名。
     * 传入非预设值（如 'default' / 'grey'）会被当成“自定义颜色”，
     * 而自定义颜色分支会强制文字为 #fff —— 浅色主题下变成白底白字、不可读。
     * 因此非预设色一律不传 color，交给默认标签样式。
     */
    antColor (tone) {
      return ['green', 'red', 'orange', 'blue', 'cyan', 'gold', 'purple', 'lime', 'magenta', 'volcano', 'geekblue'].includes(tone)
        ? tone
        : undefined
    },
    fmt (value) {
      return value === null || value === undefined || value === '' ? UNKNOWN : value
    },
    primaryOf (fam) {
      return fam.variants.find(v => v.readable && findRow(this.rows, v.key)) ||
        fam.variants.find(v => findRow(this.rows, v.key)) || fam.variants[0]
    },
    isActive (fam) {
      return Boolean(this.selected) && fam.variants.some(v => v.key === this.selectedKey)
    },
    async fetchReports () {
      const token = this.loader.next()
      const account = this.accountId
      this.loading = true
      this.externalError = ''; this.localError = ''
      const sources = await Promise.allSettled([getWorkbenchReports(), ...(this.nng8Enabled ? [getNng8Readonly()] : [])])
      if (!this.loader.isCurrent(token) || account !== this.accountId) return
      const denied = sources.some(source => source.status === 'rejected' && source.reason && source.reason.response && source.reason.response.status === 401)
      this.loading = false
      this.unauthorized = denied
      if (denied) { this.rows = []; this.payload = null; this.contractOk = false; this.error = '登录状态已失效'; this.syncSelection(); return }
      const response = sources[0].status === 'fulfilled' && sources[0].value
      const data = response && response.code === 1 && response.data && Array.isArray(response.data.reports) ? response.data : null
      const externalResponse = sources[1] && sources[1].status === 'fulfilled' && sources[1].value
      const external = externalResponse && externalResponse.code === 1 ? normalizeNng8(externalResponse.data) : null
      if (!data) this.localError = '本次读取失败，未取得当前 Profit、ETF 和运行摘要。'
      if (sources[1] && !external) this.externalError = '本次读取失败，未取得当前 AStock 证据。'
      this.contractOk = Boolean(data || external)
      this.payload = data
      this.rows = this.contractOk ? unifiedReportRows(data, external) : []
      this.error = this.contractOk ? '' : '报告中心读取失败，请稍后重试。'
      this.syncSelection()
    },
    syncSelection () {
      if (!this.contractOk || this.unauthorized || this.error) {
        this.selectionNote = '报告目录读取未成功，暂不判定选中项是否失效。'
        return
      }
      const selection = resolveSelection(this.filteredRows, this.selectedKey)
      this.selectedKey = selection.selectedKey
      this.selectionNote = selection.note
    },
    reload () {
      this.selectionNote = ''
      this.fetchReports()
    },
    selectGroup (value) {
      this.group = value
      this.selectedKey = null
      this.syncSelection()
    },
    onQueryChange () {
      this.selectedKey = null
      this.syncSelection()
    },
    onAttentionChange (event) {
      this.attentionOnly = event.target.checked
      this.selectedKey = null
      this.syncSelection()
    },
    selectFamily (fam) {
      const primary = this.primaryOf(fam)
      this.selectVariant(primary)
      this.mobileDetail = true
    },
    selectVariant (variant) {
      const selected = findRow(this.rows, variant.key)
      this.selectedKey = selected ? selected.key : null
      this.selectionNote = selected
        ? (selected.keyReason || '')
        : (variant.keyReason || '该报告缺少唯一标识，无法安全选择，请重新选择。')
    },
    backToList () {
      this.mobileDetail = false
    },
    backToWorkbench () {
      this.$router.push('/personal-home').catch(() => {})
    }
  }
}
</script>

<style scoped>
.personal-reports {
  --report-ink: rgba(0, 0, 0, 0.85);
  --report-secondary: rgba(0, 0, 0, 0.55);
  --report-muted: rgba(0, 0, 0, 0.45);
  --report-faint: rgba(0, 0, 0, 0.35);
  --report-border: #e3e8e5;
  --report-soft: #f0f2f1;
  --report-accent: #2f9e6b;
  --report-warn: #c9822a;
  --report-warn-bg: #fdf4e7;
  --report-bad: #c4514b;
  --report-bad-bg: #fbeceb;
  --report-good-bg: #eaf6f0;
  --report-info: #3f6fb5;
  --report-info-bg: #eef3fb;
  color: var(--report-ink);
}
.personal-reports.reports--dark {
  --report-ink: #e3ede7;
  --report-secondary: #bccbc2;
  --report-muted: #adbbb2;
  --report-faint: #a2b4a8;
  --report-border: #506157;
  --report-soft: #26332b;
  --report-accent: #7adba3;
  --report-warn: #f3cf94;
  --report-warn-bg: #403522;
  --report-bad: #ffb2ab;
  --report-bad-bg: #412a29;
  --report-good-bg: #263d30;
  --report-info: #bdd2ff;
  --report-info-bg: #2a3549;
}
.reports-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 12px; }
.reports-title { font-size: 16px; font-weight: 600; color: var(--report-ink); }
.reports-sub { color: var(--report-muted); font-size: 12px; margin: 4px 0 12px; }
.reports-head-right { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.reports-banner { border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; font-size: 13px; }
.banner-green { background: var(--report-good-bg); color: var(--report-accent); }
.banner-orange { background: var(--report-warn-bg); color: var(--report-warn); }
.banner-red { background: var(--report-bad-bg); color: var(--report-bad); }
.banner-blue { background: var(--report-info-bg); color: var(--report-info); }
.banner-grey { background: var(--report-soft); color: var(--report-secondary); }
.reports-filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.chip { font-size: 13px; padding: 3px 12px; border-radius: 999px; border: 1px solid var(--report-border); background: transparent; color: var(--report-ink); cursor: pointer; }
.chip[aria-pressed="true"] { border-color: var(--report-accent); color: var(--report-accent); }
.reports-search { max-width: 220px; }
.reports-attn { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--report-secondary); }
.reports-grid { display: grid; grid-template-columns: 300px 1fr; gap: 14px; align-items: start; }
.reports-panel { border: 1px solid var(--report-border); border-radius: 10px; padding: 12px; }
.reports-catalogue { display: flex; flex-direction: column; gap: 8px; max-height: 70vh; overflow: auto; }
.item { border: 1px solid var(--report-border); border-radius: 9px; padding: 10px; cursor: pointer; background: transparent; color: var(--report-ink); text-align: left; width: 100%; }
.item[aria-current="true"] { border-color: var(--report-accent); box-shadow: inset 3px 0 0 var(--report-accent); }
.item .t { font-weight: 600; font-size: 14px; margin-bottom: 3px; }
.item .c { font-size: 12px; color: var(--report-secondary); margin-bottom: 6px; }
.item .excerpt { color: var(--report-faint); }
.item .m { font-size: 11px; color: var(--report-muted); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.reader-title { font-size: 17px; margin: 0 0 6px; color: var(--report-ink); }
.reader-original { border-top: 1px solid var(--report-border); margin-top: 18px; padding-top: 16px; }
.reader-original h3 { color: var(--report-ink); font-size: 15px; }
.reader-original p { color: var(--report-secondary); white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.85; }
.reader-variants { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.reader-times { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--report-secondary); border-bottom: 1px solid var(--report-border); padding-bottom: 10px; margin-bottom: 10px; }
.reader-times b { color: var(--report-ink); }
.reader-times .missing { color: var(--report-warn); }
.reader-meta { font-size: 12px; color: var(--report-faint); margin-bottom: 10px; }
.reader-lead { font-size: 15px; font-weight: 600; margin: 0 0 8px; color: var(--report-ink); }
.reader-lead.missing { color: var(--report-secondary); }
.reader-limit { font-size: 13px; color: var(--report-secondary); background: var(--report-soft); border-radius: 8px; padding: 8px 10px; margin: 10px 0; }
.reader-body-note { font-size: 13px; color: var(--report-secondary); border: 1px dashed var(--report-border); border-radius: 8px; padding: 14px; text-align: center; }
.reader-reason { font-size: 13px; color: var(--report-bad); background: var(--report-bad-bg); border: 1px solid var(--report-bad); border-radius: 8px; padding: 10px 12px; margin: 10px 0; }
.reader-tech { margin-top: 12px; border-top: 1px solid var(--report-border); padding-top: 10px; }
.reader-tech summary { cursor: pointer; font-size: 13px; color: var(--report-secondary); }
.kv { display: grid; grid-template-columns: 120px 1fr; gap: 4px 10px; font-size: 12px; margin-top: 8px; color: var(--report-secondary); word-break: break-all; }
.kv b { color: var(--report-faint); font-weight: 500; }
.reports-empty { padding: 24px 12px; text-align: center; color: var(--report-secondary); font-size: 13px; }
.reports-note { margin-top: 12px; color: var(--report-muted); font-size: 12px; }
.reports-back { display: none; }
.reports-source-boundary { display: flex; flex-direction: column; gap: 8px; margin: 0 0 18px; padding: 12px 16px; border: 1px solid #dce5df; border-radius: 9px; color: #58675f; font-size: 12px; line-height: 1.7; }
.reports--dark .reports-source-boundary { color: var(--report-secondary); border-color: var(--report-border); }
.reports--dark /deep/ .reports-search .ant-input { color: var(--report-ink); background: var(--report-soft); border-color: var(--report-border); }
.reports--dark /deep/ .reports-search .ant-input::placeholder,
.reports--dark /deep/ .reports-search .ant-input-search-icon,
.reports--dark /deep/ .reports-search .ant-input-clear-icon { color: var(--report-muted); }
@media (max-width: 860px) {
  .reports-grid { grid-template-columns: 1fr; }
  .reports-catalogue { max-height: none; }
  .reports-grid.is-detail .reports-catalogue-panel { display: none; }
  .reports-grid:not(.is-detail) .reports-reader-panel { display: none; }
  .reports-back { display: inline-block; }
  /* 窄屏头部改纵向堆叠，避免“最近读取”标签被按钮行压住 */
  .reports-head { flex-direction: column; align-items: stretch; }
  .reports-head-right { justify-content: flex-start; }
}
</style>
