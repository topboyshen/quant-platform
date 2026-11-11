<template>
  <div class="personal-home" :class="{ dark: isDark }">
    <div class="hero">
      <div>
        <div class="eyebrow">PERSONAL QUANT WORKSPACE</div>
        <h1>Radar 趋势波段决策中心</h1>
        <p>系统 ETF 信号池与您的 A 股观察名单彼此独立；先选标的，再把同一上下文带入完整研究链路。</p>
        <div class="hero-meta">
          <span>数据生成 {{ formatTime(context.generated_at) }}</span>
          <span>最近请求 {{ formatTime(lastAttemptAt) }}</span>
          <a-tag :color="radarFreshness.stale ? 'orange' : 'green'">{{ radarFreshness.label }}</a-tag>
        </div>
      </div>
      <div class="hero-actions">
        <a-tag :color="statusColor">{{ radarStatusLabel }}</a-tag>
        <a-button icon="reload" :loading="loading || watchlistLoading" @click="loadAll">刷新全部</a-button>
      </div>
    </div>

    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="error"
      :description="`本次请求：${formatTime(lastAttemptAt)}。已清空页面中的旧 Radar 数据和旧 Radar 选中上下文；研究与回测功能仍可独立使用。`"
      class="section-gap"
    />

    <a-alert
      v-if="degradedReasons.length"
      type="warning"
      show-icon
      message="当前数据处于降级状态"
      class="section-gap"
    >
      <div slot="description" class="reason-list">
        <a-tag v-for="reason in degradedReasons" :key="reason" color="orange">{{ reasonLabel(reason) }}</a-tag>
      </div>
    </a-alert>

    <a-row :gutter="16" class="metric-grid">
      <a-col :xs="24" :sm="12" :xl="6">
        <a-card :bordered="false" class="metric-card">
          <div class="metric-label">大盘环境</div>
          <div class="metric-value"><a-badge :status="market.available ? 'success' : 'error'" />{{ availabilityLabel(market.available) }}</div>
          <div class="metric-meta">数据日 {{ formatTime(market.as_of) }} · {{ radarSource }}</div>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :xl="6">
        <a-card :bordered="false" class="metric-card">
          <div class="metric-label">宏观风险</div>
          <div class="metric-value"><a-tag :color="riskLevelColor">{{ riskLevelLabel }}</a-tag></div>
          <div class="metric-meta">VIX {{ riskMetric('vix') }} · 美债10Y {{ riskMetric('us_10y') }}</div>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :xl="6">
        <a-card :bordered="false" class="metric-card">
          <div class="metric-label">最近收盘审计</div>
          <div class="metric-value"><a-tag :color="auditColor">{{ auditStatusLabel }}</a-tag></div>
          <div class="metric-meta">{{ audit.decision_date || '--' }} · {{ decisionOutcome }}</div>
        </a-card>
      </a-col>
      <a-col :xs="24" :sm="12" :xl="6">
        <a-card :bordered="false" class="metric-card">
          <div class="metric-label">策略有效性</div>
          <div class="metric-value"><a-tag :color="effectivenessColor">{{ effectivenessLabel }}</a-tag></div>
          <div class="metric-meta">样本 {{ effectiveness.sample_size || 0 }} / {{ effectiveness.minimum_samples || '--' }}</div>
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="16" class="selection-grid">
      <a-col :xs="24" :xl="18">
        <a-card :bordered="false" class="content-card source-card">
          <template slot="title">
            <span>Radar ETF 信号池</span>
            <a-tag color="blue">系统研究 {{ radarPoolCount }} 只</a-tag>
          </template>
          <p class="source-note">这里捕捉“新上穿且放量”或“完整周线新金叉”，不是每天推荐股票。持续上涨也可能不触发；以下为收盘记录，不是实时买卖建议。</p>
          <div class="pool-meta">应验收 {{ pool.expectedDate || '--' }} · 最近批次 {{ pool.decisionDate || '--' }} · 刷新仅重读证据，不补跑策略</div>
          <a-alert v-if="!pool.available && !loading" type="warning" show-icon message="研究池清单不可用；不从历史信号推测池规模" class="inline-alert" />
          <a-tabs v-model="poolTab">
            <a-tab-pane key="universe" :tab="`完整研究池（${radarPoolCount}）`">
              <div v-if="pool.available" class="pool-summary" data-testid="etf-pool-summary">
                <a-tag>未触发 {{ pool.counts.no_trigger }}</a-tag>
                <a-tag color="blue">含放行 {{ pool.counts.allowed }} 只</a-tag>
                <a-tag color="orange">全拦截 {{ pool.counts.blocked }} 只</a-tag>
                <a-tag :color="pool.pendingCount ? 'orange' : ''">待核实 {{ pool.pendingCount }}</a-tag>
              </div>
              <div class="pool-filters">
                <a-input-search v-model="poolQuery" placeholder="搜索代码、名称、类别" aria-label="搜索ETF" allow-clear />
                <a-select v-model="poolStateFilter" aria-label="筛选评估状态" style="min-width: 150px">
                  <a-select-option value="all">全部状态</a-select-option>
                  <a-select-option v-for="(state, key) in poolStates" :key="key" :value="key">{{ state.label }}</a-select-option>
                </a-select>
              </div>
              <a-table
                data-testid="etf-universe"
                :columns="poolColumns"
                :data-source="filteredPool"
                :loading="loading"
                :pagination="{ pageSize: 10, hideOnSinglePage: true }"
                row-key="code"
                :locale="{ emptyText: pool.available ? '没有匹配的 ETF' : '研究池清单不可用' }"
                size="small"
                :scroll="{ x: 1010 }">
                <template slot="poolAsset" slot-scope="value, row">
                  <div class="trigger-reasons">
                    <strong>{{ value }}</strong>
                    <small>收盘 {{ row.reference_close == null ? '--' : row.reference_close.toFixed(3) }} 元</small>
                    <small>{{ row.data_date || '行情日期未知' }}</small>
                  </div>
                </template>
                <template slot="poolState" slot-scope="value, row"><a-tag :color="row.color">{{ row.label }}</a-tag></template>
                <template slot="poolPrice" slot-scope="value">{{ value == null ? '--' : value.toFixed(3) }}</template>
                <template slot="poolReason" slot-scope="value, row">
                  <div class="trigger-reasons">
                    <span>{{ row.conditionUncertain ? '分项条件未完成，不能据此判定无信号' : reasonLabel(value) }}</span>
                    <span>日线：{{ row.diagnostics.daily }}</span>
                    <span v-if="row.diagnostics.weekly">周线：{{ row.diagnostics.weekly }}</span>
                  </div>
                </template>
                <template slot="poolHistory" slot-scope="value, row">
                  <div class="trigger-reasons">
                    <span>{{ row.triggerHistory.label }}</span>
                    <small>{{ row.triggerHistory.detail }}</small>
                  </div>
                </template>
                <template slot="expandedRowRender" slot-scope="row">
                  <div class="trigger-detail">
                    <strong>本批次判定依据 · {{ row.decision_date || '日期未知' }}</strong>
                    <p>{{ row.diagnostics.label }}。历史数值缺失时不事后重算；技术触发与风控放行分开判断。</p>
                    <div v-for="line in row.diagnostics.details" :key="line">{{ line }}</div>
                  </div>
                </template>
                <template slot="poolDate" slot-scope="value">{{ value || '--' }}</template>
                <template slot="poolAction" slot-scope="value, row">
                  <a-button size="small" :type="isSelected('radar', row) ? 'primary' : 'default'" @click="selectPool(row)">
                    {{ isSelected('radar', row) ? '已选中' : '选中研究' }}
                  </a-button>
                </template>
              </a-table>
            </a-tab-pane>
            <a-tab-pane key="history" :tab="`历史信号（${recentSignals.length}）`">
              <a-alert type="info" show-icon message="历史触发，仅供回看；不代表当前仍有信号或仍获风控放行。" class="inline-alert" />
              <a-table
                :columns="signalColumns"
                :data-source="recentSignals"
                :loading="loading"
                :pagination="{ pageSize: 6, hideOnSinglePage: true }"
                :row-key="(row, index) => `${row.code || 'signal'}-${row.decision_date || index}`"
                :locale="{ emptyText: error ? 'Radar 数据不可用' : '当前没有近期信号' }"
                size="small"
                :scroll="{ x: 660 }"
              >
                <template slot="signal" slot-scope="value"><a-tag :color="signalColor(value)">{{ signalLabel(value) }}</a-tag></template>
                <template slot="confidence" slot-scope="value">{{ value == null ? '--' : number(value) }}</template>
                <template slot="radarAction" slot-scope="value, row">
                  <a-button size="small" @click="selectRadar(row)">研究历史</a-button>
                </template>
              </a-table>
            </a-tab-pane>
          </a-tabs>
        </a-card>
      </a-col>
      <a-col :xs="24" :xl="6">
        <a-card :bordered="false" class="content-card source-card">
          <template slot="title">
            <span>个人 A 股观察名单</span>
            <a-tag color="magenta">您的自选 {{ aShareWatchlist.length }} 只</a-tag>
          </template>
          <p class="source-note">这里只显示当前登录账户的 A 股自选，与 Radar 的 {{ radarPoolCount }} 只 ETF 系统信号池分开。</p>
          <a-alert v-if="watchlistError" type="warning" show-icon :message="watchlistError" class="inline-alert" />
          <a-table
            :columns="watchlistColumns"
            :data-source="aShareWatchlist"
            :loading="watchlistLoading"
            :pagination="false"
            :row-key="row => `${row.market || 'CNStock'}-${row.symbol}`"
            :locale="{ emptyText: watchlistError ? '观察名单加载失败' : '个人 A 股观察名单为空' }"
            size="small"
          >
            <template slot="watchMarket">A股</template>
            <template slot="watchAction" slot-scope="value, row">
              <a-button size="small" :type="isSelected('watchlist', row) ? 'primary' : 'default'" @click="selectWatchlist(row)">
                {{ isSelected('watchlist', row) ? '已选中' : '选中研究' }}
              </a-button>
            </template>
          </a-table>
          <div v-if="watchlist.length !== aShareWatchlist.length" class="watchlist-footnote">
            观察名单共 {{ watchlist.length }} 只；本区仅统计其中的 A 股。
          </div>
        </a-card>
      </a-col>
    </a-row>

    <a-alert
      v-if="!selectedContext || !selectedContext.symbol"
      type="info"
      show-icon
      message="请选择一只 Radar ETF 研究标的或个人 A 股"
      description="选中后，代码、名称、市场、Radar 信号与风险、数据时间和来源会以版本化上下文带入后续模块。"
      class="section-gap"
    />
    <a-card v-else :bordered="false" class="content-card decision-card section-gap">
      <div class="decision-summary">
        <div>
          <a-tag :color="selectedContext.origin === 'radar' ? 'blue' : 'magenta'">{{ selectedOriginLabel }}</a-tag>
          <strong>{{ selectedContext.name || selectedContext.symbol }}</strong>
          <span>{{ selectedContext.market }}:{{ selectedContext.symbol }}</span>
          <a-tag v-if="selectedSignalType" :color="signalColor(selectedSignalType)">{{ signalLabel(selectedSignalType) }}</a-tag>
        </div>
        <a-tag :color="selectedContextStale ? 'orange' : 'green'">{{ selectedContextStale ? '上下文已过期' : '上下文可继续' }}</a-tag>
      </div>
      <div class="decision-meta">
        数据截至 {{ formatTime(selectedContext.asOf) }} · 来源 {{ selectedContext.source || '--' }} · 选中时间 {{ formatTime(selectedContext.selectedAt || selectedContext.updatedAt) }}
      </div>
      <div class="quick-links">
        <button v-for="item in quickLinks" :key="item.path" type="button" @click="continueNext(item.path)">
          <a-icon :type="item.icon" />
          <span><strong>{{ item.title }}</strong><small>{{ item.desc }}</small></span>
          <a-icon type="right" />
        </button>
      </div>
    </a-card>

    <a-row :gutter="16" class="section-gap">
      <a-col :xs="24" :xl="14">
        <a-card title="可信因子历史" :bordered="false" class="content-card">
          <a-table
            :columns="factorColumns"
            :data-source="factors"
            :pagination="false"
            :row-key="row => row.factor"
            size="small"
            :scroll="{ x: 720 }"
          >
            <template slot="value" slot-scope="value, row">
              {{ number(value) }}{{ row.unit || '' }}
            </template>
            <template slot="percentile" slot-scope="value">
              {{ value == null ? '--' : `${number(value)}%` }}
            </template>
            <template slot="history" slot-scope="value, row">
              <a-tag :color="value ? 'green' : 'orange'">{{ row.sample_size || 0 }} 日</a-tag>
            </template>
            <template slot="fresh" slot-scope="value">
              <a-tag :color="value ? 'red' : 'green'">{{ value ? '陈旧' : '正常' }}</a-tag>
            </template>
          </a-table>
        </a-card>
      </a-col>
      <a-col :xs="24" :xl="10">
        <a-card title="每日决策审计" :bordered="false" class="content-card audit-card">
          <div class="audit-row"><span>运行状态</span><strong>{{ auditStatusLabel }}</strong></div>
          <div class="audit-row"><span>ETF 目标 / 已评估</span><strong>{{ coverage.target_count || 0 }} / {{ coverage.evaluated_count || 0 }}</strong></div>
          <div class="audit-row"><span>信号 / 放行 / 拦截</span><strong>{{ decision.signal_count || 0 }} / {{ decision.allowed_signal_count || 0 }} / {{ decision.blocked_signal_count || 0 }}</strong></div>
          <div class="audit-row"><span>未触发 / 数据失败</span><strong>{{ targetDecisions.no_trigger || 0 }} / {{ targetDecisions.data_failed || 0 }}</strong></div>
          <div class="audit-row"><span>决策资格</span><strong>{{ audit.run && audit.run.decision_eligible ? '具备' : '不具备' }}</strong></div>
          <div v-if="reasonCodes.length" class="reason-list audit-reasons">
            <a-tag v-for="reason in reasonCodes" :key="reason" color="orange">{{ reasonLabel(reason) }}</a-tag>
          </div>
        </a-card>
      </a-col>
    </a-row>

    <div class="attribution">Personal deployment · Powered by QuantDinger · 数据与决策仅供研究，不构成投资建议</div>
  </div>
</template>

<script>
import { mapState } from 'vuex'
import { getRadarMarketContext } from '@/api/radar'
import { getWatchlist } from '@/api/market'
import { ETF_STATES, etfPoolView, filterEtfPool, etfResearchContext, historicalEtfContext, singleFlight } from '@/utils/etfPool'
import {
  clearDecisionContext,
  continueDecisionContext,
  DECISION_CONTEXT_CHANGED_EVENT,
  hydrateWatchlistRadarContext,
  isDecisionContextStale,
  normalizeDecisionMarket,
  readDecisionContext,
  writeDecisionContext
} from '@/utils/decisionContext'
import {
  formatRadarNumber,
  formatRadarTime,
  getRadarRiskPresentation,
  getRadarFreshness,
  localizeAuditStatus,
  localizeAvailability,
  localizeEffectivenessState,
  localizeOutcome,
  localizeRadarStatus,
  localizeReason,
  localizeSignalType
} from '@/utils/radarPresentation'

export default {
  name: 'PersonalHome',
  data () {
    return {
      loading: false,
      watchlistLoading: false,
      error: '',
      watchlistError: '',
      lastAttemptAt: '',
      context: {},
      watchlist: [],
      selectedContext: null,
      clockNow: Date.now(),
      poolTab: 'universe',
      poolQuery: '',
      poolStateFilter: 'all',
      poolStates: ETF_STATES,
      poolColumns: [
        { title: '代码', dataIndex: 'code', width: 85 },
        { title: '标的 / 收盘参考', dataIndex: 'name', scopedSlots: { customRender: 'poolAsset' }, width: 150 },
        { title: '评估状态', dataIndex: 'state', scopedSlots: { customRender: 'poolState' }, width: 120 },
        { title: '本批次原因（展开看数值）', dataIndex: 'reason_code', scopedSlots: { customRender: 'poolReason' }, width: 260 },
        { title: '最近合格触发（历史）', key: 'triggerHistory', scopedSlots: { customRender: 'poolHistory' }, width: 240 },
        { title: '操作', key: 'action', scopedSlots: { customRender: 'poolAction' }, width: 100 }
      ],
      factorColumns: [
        { title: '因子', dataIndex: 'label', width: 150 },
        { title: '观测值', dataIndex: 'value', scopedSlots: { customRender: 'value' }, width: 120 },
        { title: '分位', dataIndex: 'percentile', scopedSlots: { customRender: 'percentile' }, width: 90 },
        { title: '历史样本', dataIndex: 'history_ready', scopedSlots: { customRender: 'history' }, width: 100 },
        { title: '观测日', dataIndex: 'value_date', width: 115 },
        { title: '新鲜度', dataIndex: 'is_stale', scopedSlots: { customRender: 'fresh' }, width: 90 }
      ],
      signalColumns: [
        { title: '决策日', dataIndex: 'decision_date', width: 115 },
        { title: '代码', dataIndex: 'code', width: 90 },
        { title: '名称', dataIndex: 'name', width: 110 },
        { title: '信号', dataIndex: 'signal_type', scopedSlots: { customRender: 'signal' }, width: 105 },
        { title: '置信度', dataIndex: 'confidence', scopedSlots: { customRender: 'confidence' }, width: 85 },
        { title: '操作', key: 'action', scopedSlots: { customRender: 'radarAction' }, width: 95 }
      ],
      watchlistColumns: [
        { title: '代码', dataIndex: 'symbol', width: 90 },
        { title: '名称', dataIndex: 'name' },
        { title: '市场', dataIndex: 'market', scopedSlots: { customRender: 'watchMarket' }, width: 60 },
        { title: '操作', key: 'action', scopedSlots: { customRender: 'watchAction' }, width: 95 }
      ],
      quickLinks: [
        { path: '/ai-asset-analysis', icon: 'thunderbolt', title: '继续到 AI 资产分析', desc: '快速诊断当前标的' },
        { path: '/indicator-ide', icon: 'line-chart', title: '继续到指标研究', desc: '验证技术指标' },
        { path: '/strategy-ide', icon: 'code', title: '继续到策略研究', desc: '生成并完善策略' },
        { path: '/backtest-center', icon: 'bar-chart', title: '继续到回测验证', desc: '执行历史验证' }
      ]
    }
  },
  computed: {
    ...mapState({ navTheme: state => state.app.theme }),
    isDark () { return ['dark', 'realdark'].includes(this.navTheme) },
    market () { return this.context.market || {} },
    risk () { return this.context.risk || {} },
    signals () { return this.context.signals || {} },
    audit () { return this.signals.daily_audit || {} },
    coverage () { return this.audit.coverage || {} },
    decision () { return this.audit.decision || {} },
    targetDecisions () { return this.audit.target_decisions || {} },
    effectiveness () { return this.signals.effectiveness || {} },
    factors () { return Array.isArray(this.market.factors) ? this.market.factors : [] },
    recentSignals () { return Array.isArray(this.signals.recent) ? this.signals.recent : [] },
    aShareWatchlist () {
      return this.watchlist.filter(item => this.resolveMarket(item) === 'CNStock')
    },
    pool () { return etfPoolView(this.context.research_pool, { generatedAt: this.context.generated_at, now: this.clockNow }) },
    filteredPool () { return filterEtfPool(this.pool.items, this.poolQuery, this.poolStateFilter) },
    radarPoolCount () { return this.pool.configuredCount == null ? '--' : this.pool.configuredCount },
    degradedReasons () {
      const reasons = this.context.quality && this.context.quality.degraded_reasons
      return Array.isArray(reasons) ? reasons.map(item => typeof item === 'string' ? item : 'unknown') : []
    },
    reasonCodes () {
      const codes = this.audit.run && this.audit.run.reason_codes
      return Array.isArray(codes) ? codes : []
    },
    radarFreshness () {
      return getRadarFreshness({
        generatedAt: this.context.generated_at,
        asOf: this.market.as_of,
        expectedAsOf: this.market.expected_as_of
      }, { now: this.clockNow })
    },
    radarSource () { return this.context.source || this.market.source || 'Trading Radar 私有服务' },
    radarStatusLabel () { return this.error ? '不可用' : localizeRadarStatus(this.context.status) },
    riskPresentation () { return getRadarRiskPresentation(this.risk, { unavailable: Boolean(this.error) }) },
    riskLevelLabel () { return this.riskPresentation.label },
    riskLevelColor () {
      if (this.riskPresentation.state === 'unavailable') return 'red'
      if (this.riskPresentation.state === 'incomplete') return 'orange'
      return this.levelColor(this.riskPresentation.level)
    },
    auditStatusLabel () { return localizeAuditStatus(this.error ? 'unavailable' : (this.audit.run && this.audit.run.status ? this.audit.run.status : 'missing')) },
    decisionOutcome () { return localizeOutcome(this.error ? 'unavailable' : (this.audit.decision && this.audit.decision.outcome)) },
    statusColor () { return ({ ok: 'green', healthy: 'green', degraded: 'orange', error: 'red' })[this.error ? 'error' : this.context.status] || 'blue' },
    auditColor () {
      const status = this.audit.run && this.audit.run.status
      return ({ completed: 'green', success: 'green', running: 'blue', partial: 'orange', failed: 'red' })[status] || 'orange'
    },
    effectivenessLabel () { return localizeEffectivenessState(this.error ? 'unavailable' : (this.effectiveness.state || 'insufficient')) },
    effectivenessColor () {
      return ({ insufficient: 'orange', insufficient_samples: 'orange', sample_insufficient: 'orange', uncertain: 'gold', preliminary: 'blue', effective: 'green', ineffective: 'red' })[this.effectiveness.state] || 'orange'
    },
    selectedOriginLabel () { return this.selectedContext && this.selectedContext.origin === 'radar' ? 'Radar ETF 研究' : '个人 A 股观察名单' },
    selectedSignalType () {
      const signal = this.selectedContext && this.selectedContext.radar && this.selectedContext.radar.signal
      return signal && signal.type ? signal.type : ''
    },
    selectedContextStale () { return this.selectedContext ? isDecisionContextStale(this.selectedContext, undefined, this.clockNow) : true }
  },
  created () {
    this.refreshSelectedContext()
    this.loadAll()
  },
  mounted () {
    this._decisionContextListener = () => this.refreshSelectedContext()
    window.addEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
    this._clockTimer = window.setInterval(() => { this.clockNow = Date.now() }, 60000)
  },
  activated () {
    this.refreshSelectedContext()
    this.loadWatchlist()
    this.loadContext()
  },
  beforeDestroy () {
    this._disposed = true
    window.clearInterval(this._clockTimer)
    if (this._decisionContextListener) {
      window.removeEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
      this._decisionContextListener = null
    }
  },
  methods: {
    refreshSelectedContext () {
      this.selectedContext = readDecisionContext({ allowStale: true })
      return this.selectedContext
    },
    async loadAll () {
      await Promise.all([this.loadContext(), this.loadWatchlist()])
    },
    async loadContext () {
      return singleFlight(this, '_radarLoadPromise', () => this.fetchContext())
    },
    async fetchContext () {
      this.loading = true
      this.error = ''
      this.lastAttemptAt = new Date().toISOString()
      try {
        const response = await getRadarMarketContext()
        if (this._disposed) return
        if (!response || response.code !== 1 || !response.data) throw new Error('invalid-radar-response')
        this.context = response.data
        this.hydrateSelectedWatchlistRadarContext()
        this.reconcileRadarSelection()
        this.clockNow = Date.now()
      } catch (error) {
        if (this._disposed) return
        const code = this.radarErrorCode(error)
        this.context = {}
        this.error = code ? localizeReason(code) : 'Radar 数据加载失败，请稍后重试。'
        const stored = readDecisionContext({ allowStale: true })
        if (stored && stored.origin === 'radar') {
          clearDecisionContext()
          this.selectedContext = null
        } else if (stored && stored.origin === 'watchlist') {
          // Keep the user's asset selection, but strip the Radar-derived
          // signal/risk/market snapshot that just failed validation. Preserve
          // the original timestamp so an error never makes old data fresh.
          this.selectedContext = writeDecisionContext({
            origin: 'watchlist',
            market: stored.market,
            symbol: stored.symbol,
            name: stored.name,
            radar: { signal: {}, risk: {}, market: {} },
            asOf: stored.asOf,
            source: stored.source || 'QuantDinger 个人观察名单',
            selectedAt: stored.selectedAt,
            evidenceUpdatedAt: stored.evidenceUpdatedAt,
            updatedAt: stored.updatedAt
          })
        }
      } finally {
        this.loading = false
      }
    },
    async loadWatchlist () {
      return singleFlight(this, '_watchlistLoadPromise', () => this.fetchWatchlist())
    },
    async fetchWatchlist () {
      this.watchlistLoading = true
      this.watchlistError = ''
      try {
        const response = await getWatchlist()
        if (this._disposed) return
        const list = response && Array.isArray(response.data)
          ? response.data
          : (response && response.data && Array.isArray(response.data.watchlist) ? response.data.watchlist : null)
        if (!response || response.code !== 1 || !list) throw new Error('invalid-watchlist-response')
        this.watchlist = list.filter(item => item && item.symbol)
      } catch (_) {
        this.watchlist = []
        this.watchlistError = '个人观察名单加载失败，请重试。'
      } finally {
        this.watchlistLoading = false
      }
    },
    radarErrorCode (error) {
      const payload = error && error.response && error.response.data
      const detail = payload && payload.error
      return detail && typeof detail === 'object' ? detail.type : ''
    },
    hydrateSelectedWatchlistRadarContext () {
      const stored = readDecisionContext({ allowStale: true })
      const hydrated = hydrateWatchlistRadarContext(stored || {}, this.context)
      if (!hydrated) return stored
      this.selectedContext = writeDecisionContext(hydrated)
      return this.selectedContext
    },
    resolveMarket (item) {
      const explicit = normalizeDecisionMarket(item && (item.market || item.market_category))
      if (explicit) return explicit
      const symbol = String(item && (item.symbol || item.code) || '').trim()
      return /^\d{6}(?:\.(?:SH|SZ|BJ))?$/i.test(symbol) ? 'CNStock' : 'USStock'
    },
    selectRadar (row) {
      const input = historicalEtfContext(row, this.context)
      if (input) this.applySelectedContext(writeDecisionContext(input))
    },
    selectPool (row) {
      const input = etfResearchContext(row, this.context)
      if (input) this.applySelectedContext(writeDecisionContext(input))
    },
    reconcileRadarSelection () {
      const stored = readDecisionContext({ allowStale: true })
      if (!stored || stored.origin !== 'radar') return
      const signalDate = stored.radar && stored.radar.signal && stored.radar.signal.decisionDate
      if (stored.evidenceState === 'historical' || (signalDate && signalDate !== this.pool.decisionDate)) {
        this.selectedContext = writeDecisionContext({ ...stored, evidenceState: 'historical' })
        return
      }
      const input = etfResearchContext({ code: stored.symbol }, this.context)
      if (input) this.selectedContext = writeDecisionContext({ ...input, selectedAt: stored.selectedAt })
      else { clearDecisionContext(); this.selectedContext = null }
    },
    selectWatchlist (row) {
      const selected = writeDecisionContext({
        origin: 'watchlist',
        market: this.resolveMarket(row),
        symbol: row.symbol || row.code,
        name: row.name,
        risk: this.risk,
        marketContext: this.market,
        asOf: this.market.as_of || this.context.generated_at,
        source: 'QuantDinger 个人观察名单',
        evidenceUpdatedAt: this.context.generated_at
      })
      this.applySelectedContext(selected)
    },
    applySelectedContext (selected) {
      if (!selected) {
        this.$message.error('无法保存研究上下文，请检查浏览器存储权限。')
        return
      }
      this.selectedContext = selected
      this.$message.success(`已选中 ${selected.market}:${selected.symbol}`)
    },
    continueNext (path) {
      const selected = continueDecisionContext({}, { allowStale: true })
      if (!selected || !selected.symbol) {
        this.$message.warning('请先选择研究标的。')
        return
      }
      this.selectedContext = selected
      this.$router.push(path).catch(() => {})
    },
    isSelected (origin, row) {
      if (!this.selectedContext || this.selectedContext.origin !== origin) return false
      if (origin === 'radar' && this.selectedContext.evidenceState === 'historical') return false
      const symbol = String(row && (row.symbol || row.code) || '').toUpperCase()
      return this.selectedContext.symbol === symbol && this.selectedContext.market === this.resolveMarket(row)
    },
    formatTime (value) { return formatRadarTime(value) },
    availabilityLabel (value) { return localizeAvailability(this.error ? false : value) },
    signalLabel (value) { return localizeSignalType(value) },
    reasonLabel (value) { return localizeReason(value) },
    number (value) {
      return formatRadarNumber(value)
    },
    riskMetric (field) {
      if (!this.riskPresentation.available || this.riskPresentation.missingFields.includes(field)) return '--'
      return formatRadarNumber(this.risk[field])
    },
    levelColor (level) {
      return ({
        low: 'green',
        normal: 'green',
        stable: 'green',
        risk_on: 'green',
        liquidity_supportive: 'green',
        warning: 'orange',
        medium: 'orange',
        moderate: 'orange',
        elevated: 'orange',
        growth_blocked: 'orange',
        inflation_pressure: 'orange',
        liquidity_tightening: 'orange',
        high: 'red',
        critical: 'red',
        extreme: 'red',
        risk_off: 'red',
        recession_risk: 'red'
      })[String(level || '').toLowerCase()] || 'blue'
    },
    signalColor (signal) {
      const value = String(signal || '').toLowerCase()
      if (value.includes('buy') || value.includes('long') || value.includes('bull')) return 'green'
      if (value.includes('sell') || value.includes('short') || value.includes('bear')) return 'red'
      return 'blue'
    }
  }
}
</script>

<style lang="less" scoped>
.personal-home { min-height: calc(100vh - 88px); padding: 8px; color: #172033; }
.hero { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding: 24px 26px; border-radius: 14px; background: linear-gradient(135deg, #0b1f3a, #123f62); color: #fff; box-shadow: 0 12px 30px rgba(15, 56, 87, .18); }
.hero h1 { margin: 4px 0 8px; color: #fff; font-size: 28px; }
.hero p { margin: 0; color: rgba(255,255,255,.72); }
.eyebrow { color: #58d3c5; font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.hero-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; margin-top: 13px; color: rgba(255,255,255,.68); font-size: 12px; }
.hero-actions { display: flex; align-items: center; gap: 10px; }
.section-gap { margin-top: 16px; }
.metric-grid { margin-top: 16px; }
.metric-card { min-height: 122px; margin-bottom: 16px; border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,.06); }
.metric-label { color: #7b8494; font-size: 12px; }
.metric-value { min-height: 34px; margin: 10px 0 7px; font-size: 22px; font-weight: 750; }
.metric-meta { color: #8a94a6; font-size: 12px; }
.selection-grid { margin-top: 0; }
.content-card { border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,.05); }
.source-card { min-height: 370px; margin-bottom: 16px; }
.source-card /deep/ .ant-card-head-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.source-note { min-height: 38px; margin: 0 0 12px; color: #7b8494; font-size: 12px; }
.inline-alert { margin-bottom: 10px; }
.pool-meta { margin-bottom: 10px; color: #7b8494; font-size: 12px; }
.pool-summary { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
.pool-filters { display: flex; gap: 10px; margin-bottom: 12px; }
.trigger-reasons { display: flex; flex-direction: column; gap: 5px; line-height: 1.5; font-size: 12px; }
.trigger-reasons small, .trigger-detail p { color: #7b8494; }
.trigger-detail { max-width: 920px; padding: 8px; line-height: 1.8; overflow-wrap: anywhere; }
@media (max-width: 600px) { .pool-filters { flex-direction: column; } }
.watchlist-footnote { margin-top: 10px; color: #8a94a6; font-size: 12px; }
.decision-card { border: 1px solid rgba(24,144,255,.22); background: linear-gradient(135deg, rgba(24,144,255,.06), rgba(82,196,26,.035)); }
.decision-summary { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.decision-summary > div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.decision-summary strong { font-size: 17px; }
.decision-summary span { color: #6f7888; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.decision-meta { margin-top: 10px; color: #7b8494; font-size: 12px; }
.audit-card { min-height: 390px; }
.audit-row { display: flex; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid #edf0f4; }
.audit-row span { color: #7b8494; }
.reason-list { display: flex; flex-wrap: wrap; gap: 6px; }
.audit-reasons { margin-top: 18px; }
.quick-links { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 12px; margin-top: 16px; }
.quick-links button { display: flex; align-items: center; gap: 12px; padding: 14px; border: 1px solid #e5eaf0; border-radius: 12px; color: inherit; text-align: left; background: #fff; cursor: pointer; }
.quick-links button:hover { border-color: var(--primary-color,#1890ff); box-shadow: 0 7px 18px rgba(24,144,255,.11); }
.quick-links button > .anticon:first-child { color: var(--primary-color,#1890ff); font-size: 22px; }
.quick-links button > .anticon:last-child { margin-left: auto; color: #a8b0bc; }
.quick-links span { display: flex; flex-direction: column; gap: 3px; }
.quick-links small { color: #8a94a6; }
.attribution { padding: 22px 0 8px; color: #9aa3af; font-size: 11px; text-align: center; }
.dark { color: rgba(255,255,255,.86); }
.dark .metric-card, .dark .content-card, .dark .quick-links button { border-color: #30343b; background: #202329; }
.dark .decision-card { background: linear-gradient(135deg, rgba(24,144,255,.11), rgba(82,196,26,.06)); }
.dark .audit-row { border-bottom-color: #343840; }
@media (max-width: 1000px) { .quick-links { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 600px) { .hero { flex-direction: column; padding: 20px; } .hero h1 { font-size: 22px; } .decision-summary { align-items: flex-start; flex-direction: column; } .quick-links { grid-template-columns: 1fr; } }
</style>
