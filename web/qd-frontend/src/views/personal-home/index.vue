<template>
  <div class="personal-home" :class="{ dark: isDark }">
    <header class="hero">
      <div>
        <div class="eyebrow">QUANTDINGER · 个人研究</div>
        <h1>研究工作台</h1>
        <p>先看结论与证据，再选择标的；模拟跟踪不等于账户收益。</p>
        <div class="hero-meta">
          <span>Radar 行情截至 {{ formatTime(market.as_of) }}</span>
          <span>最近请求 {{ formatTime(lastAttemptAt) }}（CST）</span>
          <a-tag :color="radarFreshness.stale ? 'orange' : 'green'">{{ radarFreshness.label }}</a-tag>
        </div>
      </div>
      <div class="hero-actions">
        <a-button icon="reload" :loading="loading || watchlistLoading || workbenchLoading" data-testid="home-refresh-all" @click="loadAll">刷新全部</a-button>
      </div>
    </header>
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

    <workbench-overview ref="workbenchOverview" :dark="isDark" data-testid="personal-home-workbench" @loading-change="workbenchLoading = $event">
      <template #research>
        <a-card :bordered="false" class="metric-card effectiveness-card" data-testid="effectiveness-card">
          <div class="effectiveness-heading"><span>ETF信号验证</span><a-tag :color="effectivenessView.color">{{ effectivenessView.label }}</a-tag></div>
          <div class="effectiveness-maturity" data-testid="effectiveness-maturity">
            <strong>{{ effectivenessView.sampleSize }} <small>/ {{ effectivenessView.minimumSamples }}</small></strong>
            <span>{{ effectivenessView.primaryHorizon ? `${effectivenessView.primaryHorizon}个交易日成熟样本 / 判定门槛` : '成熟样本 / 判定门槛' }}</span>
          </div>
          <p v-if="effectivenessDisplay.recentText" class="effectiveness-note" data-testid="effectiveness-recent">{{ effectivenessDisplay.recentText }}</p>
          <p v-if="effectivenessDisplay.replayText" class="effectiveness-note effectiveness-replay" data-testid="effectiveness-replay">{{ effectivenessDisplay.replayText }}</p>
          <template v-if="effectivenessView.detailsAvailable">
            <div class="effectiveness-counts">
              <div><span>观察记录</span><strong>{{ effectivenessView.progress.eligible }}</strong></div>
              <div><span>待完成{{ effectivenessView.primaryHorizon }}日结果</span><strong>{{ effectivenessView.progress.awaiting }}</strong></div>
              <div><span>未计入</span><strong>{{ effectivenessView.progress.excluded }}</strong></div>
            </div>
            <p v-if="effectivenessView.progress.shortHorizons.length" class="effectiveness-short">
              短周期已完成：<span v-for="horizon in effectivenessView.progress.shortHorizons" :key="horizon.days">{{ horizon.days }}日 {{ horizon.count }}</span>
            </p>
          </template>
          <p v-else class="effectiveness-note" data-testid="effectiveness-detail-unavailable">{{ effectivenessView.detailMessage }}</p>
          <details class="effectiveness-detail" data-testid="effectiveness-detail">
            <summary>查看计数口径与进度说明</summary>
            <template v-if="effectivenessView.detailsAvailable">
              <p>历史候选记录 {{ effectivenessView.progress.candidate }} = 观察记录 {{ effectivenessView.progress.eligible }} + 未计入 {{ effectivenessView.progress.excluded }}。</p>
              <dl>
                <template v-for="reason in effectivenessView.progress.excludedReasons">
                  <dt :key="`${reason.key}-label`">{{ reason.label }}</dt><dd :key="`${reason.key}-count`">{{ reason.count }}</dd>
                </template>
              </dl>
              <p class="effectiveness-subheading">待完成{{ effectivenessView.primaryHorizon }}日结果</p>
              <dl>
                <template v-for="reason in effectivenessView.progress.awaitingReasons">
                  <dt :key="`${reason.key}-label`">{{ reason.label }}</dt><dd :key="`${reason.key}-count`">{{ reason.count }}</dd>
                </template>
              </dl>
              <p class="effectiveness-subheading">其他未计入成熟的观察记录</p>
              <dl>
                <dt>未模拟成交（不计成熟）</dt><dd>{{ effectivenessView.progress.unfilled }}</dd>
                <dt>结果证据无效（不计成熟）</dt><dd>{{ effectivenessView.progress.invalidOutcome }}</dd>
              </dl>
              <p>最早预计成熟：{{ effectivenessView.progress.nextMaturity || '暂无可用日期' }}<br>仅交易日历估计，不保证届时计入样本；需等待自然结果记录。</p>
              <p>已保存结果截至：{{ effectivenessView.progress.latestEvaluation || '--' }}</p>
            </template>
            <p>所有周期均为交易日。按决策记录计数，不是去重后的独立信号或独立交易。观察记录包含已成熟、待结果、未成交与结果无效记录；各持有期样本可重叠，不能相加。</p>
            <p>仅验证 ETF 信号，不包含 Profit Radar 股票选股和账户权益；达到样本门槛不代表已经盈利。</p>
          </details>
        </a-card>
      </template>
      <template #market-summary>
        <a-row :gutter="16" class="metric-grid">
          <a-col :xs="24" :sm="8" :xl="8">
            <a-card :bordered="false" class="metric-card">
              <div class="metric-label">大盘环境</div>
              <div class="metric-value"><a-badge :status="market.available ? 'success' : 'error'" />{{ availabilityLabel(market.available) }}</div>
              <div class="metric-meta">数据日 {{ formatTime(market.as_of) }} · {{ radarSource }}</div>
            </a-card>
          </a-col>
          <a-col :xs="24" :sm="8" :xl="8">
            <a-card :bordered="false" class="metric-card">
              <div class="metric-label">宏观风险</div>
              <div class="metric-value"><a-tag :color="riskLevelColor">{{ riskLevelLabel }}</a-tag></div>
              <div class="metric-meta">VIX {{ riskMetric('vix') }} · 美债10Y {{ riskMetric('us_10y') }}</div>
            </a-card>
          </a-col>
          <a-col :xs="24" :sm="8" :xl="8">
            <a-card :bordered="false" class="metric-card">
              <div class="metric-label">最近收盘审计</div>
              <div class="metric-value"><a-tag :color="auditColor">{{ auditStatusLabel }}</a-tag></div>
              <div class="metric-meta">{{ audit.decision_date || '--' }} · {{ decisionOutcome }}</div>
            </a-card>
          </a-col>
        </a-row>
      </template>
      <template #research-entry>
        <div class="home-entry-grid" aria-label="研究与报告入口">
          <button type="button" class="home-entry" @click="focusHomeSection('market')">
            <span><strong>市场观察</strong><small>ETF 系统研究 {{ radarPoolCount }} 只 · 个人 A 股 {{ watchlistError ? '未知' : aShareWatchlist.length }} 只</small></span><a-icon type="arrow-right" />
          </button>
          <router-link class="home-entry" to="/personal-reports">
            <span><strong>报告中心</strong><small>收盘日报、市场摘要与逐份 Profit 报告</small></span><a-icon type="arrow-right" />
          </router-link>
          <router-link v-if="nng8Enabled" class="home-entry" :to="nng8SectionRoute" data-testid="home-nng8-entry">
            <span><strong>nnG8 报告与数据</strong><small>外部晨报、数据质量与冻结校准</small></span><a-icon type="arrow-right" />
          </router-link>
        </div>
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

      </template>
      <template #market-pools>
        <details ref="marketSection" class="home-market-section" data-testid="home-market-section">
          <summary>市场观察 <small>ETF 系统研究池与个人自选分开 · 展开选择研究标的</small></summary>
          <div class="selection-grid" data-testid="market-selection-grid">
            <section class="market-pool-panel" aria-label="Radar ETF 系统研究池">
              <a-card :bordered="false" class="content-card source-card etf-source-card">
                <template slot="title">
                  <span>Radar ETF 信号池</span>
                  <a-tag color="blue">系统研究 {{ radarPoolCount }} 只</a-tag>
                </template>
                <p class="market-intro">收盘研究记录，非实时买卖建议。未触发信号的标的仍可选中研究。</p>
                <div class="pool-meta"><span>应验收 {{ pool.expectedDate || '--' }}</span><span>最近批次 {{ pool.decisionDate || '--' }}</span><span>刷新只读证据，不补跑策略</span></div>
                <details class="pool-method"><summary>信号规则与数据口径</summary><p>这里捕捉“新上穿且放量”或“完整周线新金叉”，不是每天推荐股票。持续上涨也可能不触发；技术触发与风控放行分开判断。历史信号只供回看，不代表当前仍然有效。</p></details>
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
                      :expanded-row-keys="expandedPoolRows"
                      @expand="onPoolExpand"
                      row-key="code"
                      :locale="{ emptyText: pool.available ? '没有匹配的 ETF' : '研究池清单不可用' }"
                      size="small"
                      :scroll="{ x: 800 }">
                      <template slot="poolAsset" slot-scope="value, row">
                        <div class="trigger-reasons pool-asset">
                          <strong>{{ value }}</strong><span class="asset-code">{{ row.code }}</span>
                          <small>收盘 {{ row.reference_close == null ? '--' : row.reference_close.toFixed(3) }} 元</small>
                          <small>{{ row.data_date || '行情日期未知' }}</small>
                        </div>
                      </template>
                      <template slot="poolState" slot-scope="value, row"><a-tag :color="row.color">{{ row.label }}</a-tag></template>
                      <template slot="poolPrice" slot-scope="value">{{ value == null ? '--' : value.toFixed(3) }}</template>
                      <template slot="poolReason" slot-scope="value, row">
                        <div class="trigger-reasons">
                          <span>{{ row.conditionUncertain ? '分项条件未完成，不能据此判定无信号' : reasonLabel(value) }}</span>
                          <button class="evidence-toggle" type="button" :aria-expanded="expandedPoolRows.includes(row.code)" :aria-label="`${expandedPoolRows.includes(row.code) ? '收起' : '查看'} ${row.name} 判定依据`" @click="onPoolExpand(!expandedPoolRows.includes(row.code), row)">{{ expandedPoolRows.includes(row.code) ? '收起依据' : '查看依据' }} <a-icon :type="expandedPoolRows.includes(row.code) ? 'up' : 'down'" /></button>
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
                          <div>日线：{{ row.diagnostics.daily }}</div>
                          <div v-if="row.diagnostics.weekly">周线：{{ row.diagnostics.weekly }}</div>
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
                    <p class="table-scroll-hint">窄屏可左右滑动表格；“查看依据”保留完整条件与数值。</p>
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
            </section>
            <aside class="market-watchlist-panel" aria-label="个人 A 股观察名单">
              <a-card :bordered="false" class="content-card source-card watchlist-source-card">
                <template slot="title">
                  <span>个人 A 股观察名单</span>
                  <a-tag>您的自选 {{ watchlistError ? '未知' : aShareWatchlist.length }} 只</a-tag>
                </template>
                <p class="market-intro">仅当前账户的 A 股自选，与 ETF 系统研究池分开；不代表系统推荐。</p>
                <a-alert v-if="watchlistError" type="warning" show-icon :message="watchlistError" class="inline-alert" />
                <a-spin :spinning="watchlistLoading">
                  <ul v-if="aShareWatchlist.length" class="watchlist-items" data-testid="personal-watchlist">
                    <li v-for="row in aShareWatchlist" :key="`${row.market || 'CNStock'}-${row.symbol}`" class="watchlist-item" :class="{ 'is-selected': isSelected('watchlist', row) }">
                      <div class="watchlist-asset"><strong>{{ row.name || row.symbol }}</strong><span class="asset-code">{{ row.symbol }} <small>· A股</small></span></div>
                      <a-button size="small" :type="isSelected('watchlist', row) ? 'primary' : 'default'" :aria-label="`研究 ${row.name || row.symbol} ${row.symbol}`" :aria-pressed="isSelected('watchlist', row)" @click="selectWatchlist(row)">{{ isSelected('watchlist', row) ? '已选中' : '选中研究' }}</a-button>
                    </li>
                  </ul>
                  <a-empty v-else-if="!watchlistLoading" :description="watchlistError ? '观察名单加载失败，请重试' : '个人 A 股观察名单为空'" />
                  <div v-else class="watchlist-loading">正在读取个人观察名单…</div>
                </a-spin>
                <div v-if="watchlist.length !== aShareWatchlist.length" class="watchlist-footnote">
                  观察名单共 {{ watchlist.length }} 只；本区仅统计其中的 A 股。
                </div>
              </a-card>
            </aside>
          </div>

        </details>
      </template>
      <template #audit>
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

      </template>
    </workbench-overview>
    <div class="attribution">Personal deployment · Powered by QuantDinger · 数据与决策仅供研究，不构成投资建议</div>
  </div>

</template>

<script>
import { mapState } from 'vuex'
import { getRadarMarketContext } from '@/api/radar'
import WorkbenchOverview from '@/components/WorkbenchOverview'
import { getWatchlist } from '@/api/market'
import { ETF_STATES, etfPoolView, filterEtfPool, etfResearchContext, historicalEtfContext, singleFlight } from '@/utils/etfPool'
import { effectivenessProgressView, effectivenessDisplayView } from '@/utils/effectivenessProgress'
import { homeTime } from '@/utils/homePresentation'
import { NNG8_READONLY_ENABLED, NNG8_SECTION_ROUTE } from '@/utils/nng8Readonly'
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
  getRadarRiskPresentation,
  getRadarFreshness,
  localizeAuditStatus,
  localizeAvailability,
  localizeOutcome,
  localizeRadarStatus,
  localizeReason,
  localizeSignalType
} from '@/utils/radarPresentation'

export default {
  name: 'PersonalHome',
  components: { WorkbenchOverview },
  data () {
    return {
      nng8Enabled: NNG8_READONLY_ENABLED,
      nng8SectionRoute: NNG8_SECTION_ROUTE,
      loading: false,
      watchlistLoading: false,
      workbenchLoading: false,
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
      expandedPoolRows: [],
      poolStates: ETF_STATES,
      poolColumns: [
        { title: '标的 / 收盘参考', dataIndex: 'name', scopedSlots: { customRender: 'poolAsset' }, width: 175 },
        { title: '评估状态', dataIndex: 'state', scopedSlots: { customRender: 'poolState' }, width: 115 },
        { title: '本批次判定', dataIndex: 'reason_code', scopedSlots: { customRender: 'poolReason' }, width: 180 },
        { title: '最近合格触发（历史）', key: 'triggerHistory', scopedSlots: { customRender: 'poolHistory' }, width: 190 },
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
    effectivenessView () { return effectivenessProgressView(this.signals.effectiveness, { unavailable: Boolean(this.error) }) },
    effectivenessDisplay () { return this.error ? { recentText: '', replayText: '' } : effectivenessDisplayView(this.signals.effectiveness_display, this.signals.effectiveness) },
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
    selectedOriginLabel () { return this.selectedContext && this.selectedContext.origin === 'radar' ? 'Radar ETF 研究' : '个人 A 股观察名单' },
    selectedSignalType () {
      const signal = this.selectedContext && this.selectedContext.radar && this.selectedContext.radar.signal
      return signal && signal.type ? signal.type : ''
    },
    selectedContextStale () { return this.selectedContext ? isDecisionContextStale(this.selectedContext, undefined, this.clockNow) : true }
  },
  watch: {
    '$route.query.section' () { this.focusRouteSection() }
  },
  created () {
    this.refreshSelectedContext()
    this.loadAll()
  },
  mounted () {
    this._decisionContextListener = () => this.refreshSelectedContext()
    window.addEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
    this._clockTimer = window.setInterval(() => { this.clockNow = Date.now() }, 60000)
    this._homeSectionListener = section => {
      if (this.$route.path === '/personal-home') this.focusHomeSection(section)
    }
    this.$root.$on('personal-home-section', this._homeSectionListener)
    this.focusRouteSection()
  },
  activated () {
    this.refreshSelectedContext()
    this.loadWatchlist()
    this.loadContext()
    this.refreshWorkbench()
    this.focusRouteSection()
  },
  beforeDestroy () {
    this._disposed = true
    this.$root.$off('personal-home-section', this._homeSectionListener)
    window.clearInterval(this._clockTimer)
    if (this._decisionContextListener) {
      window.removeEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
      this._decisionContextListener = null
    }
  },
  methods: {
    onPoolExpand (expanded, row) {
      this.expandedPoolRows = expanded
        ? [...new Set([...this.expandedPoolRows, row.code])]
        : this.expandedPoolRows.filter(code => code !== row.code)
    },
    focusRouteSection () {
      const section = this.$route.query && this.$route.query.section
      if (this.$route.path === '/personal-home') this.focusHomeSection(['market', 'evidence'].includes(section) ? section : 'overview')
    },
    focusHomeSection (section) {
      this.$nextTick(() => {
        if (section === 'overview') {
          this.$el.scrollIntoView({ block: 'start' })
        } else if (section === 'evidence') {
          const workbench = this.$refs.workbenchOverview
          if (workbench) workbench.openEvidence()
        } else if (section === 'market') {
          const target = this.$refs.marketSection
          if (target) {
            target.open = true
            this.$nextTick(() => target.scrollIntoView({ block: 'start' }))
          }
        }
      })
    },
    refreshSelectedContext () {
      this.selectedContext = readDecisionContext({ allowStale: true })
      return this.selectedContext
    },
    async loadAll () {
      await Promise.all([this.loadContext(), this.loadWatchlist(), this.refreshWorkbench()])
    },
    refreshWorkbench () {
      const wb = this.$refs && this.$refs.workbenchOverview
      if (wb && typeof wb.load === 'function') return wb.load()
      return Promise.resolve()
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
    formatTime (value) { return homeTime(value, '--') },
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
.effectiveness-card /deep/ .ant-card-body { padding: 18px; }
.effectiveness-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px; font-size: 13px; }
.effectiveness-heading .ant-tag { margin: 0; }
.effectiveness-maturity { display: flex; flex-direction: column; gap: 2px; margin: 10px 0 12px; }
.effectiveness-maturity strong { font-size: 28px; font-variant-numeric: tabular-nums; line-height: 1.2; }
.effectiveness-maturity small { font-size: 18px; font-weight: 500; }
.effectiveness-replay { color: #8a94a6; font-style: italic; }
.effectiveness-maturity span, .effectiveness-note, .effectiveness-short { margin: 0; color: #596579; font-size: 12px; line-height: 1.7; }
.effectiveness-counts { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 8px; }
.effectiveness-counts > div { display: flex; flex-direction: column; gap: 3px; }
.effectiveness-counts span { color: #596579; font-size: 11px; }
.effectiveness-counts strong { font-size: 17px; font-variant-numeric: tabular-nums; }
.effectiveness-short span { display: inline-block; margin-right: 7px; }
.effectiveness-detail { margin-top: 10px; border-top: 1px solid #e5eaf0; padding-top: 9px; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.effectiveness-detail summary { cursor: pointer; color: #35628f; }
.effectiveness-detail summary:focus-visible { outline: 2px solid #1890ff; outline-offset: 3px; }
.effectiveness-detail p { margin: 9px 0 0; }
.effectiveness-detail dl { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 3px 10px; margin: 8px 0; }
.effectiveness-detail dt { font-weight: normal; }
.effectiveness-detail dd { margin: 0; font-variant-numeric: tabular-nums; }
.effectiveness-subheading { font-weight: 600; }
.selection-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; margin-top: 0; }
.market-pool-panel, .market-watchlist-panel { min-width: 0; }
.selection-grid ::v-deep .ant-table { min-width: 0; }
.selection-grid .source-card { margin-bottom: 0; }
.selection-grid .source-card /deep/ .ant-card-head { padding: 0 20px; }
.selection-grid .source-card /deep/ .ant-card-body { padding: 16px 20px 20px; }
.selection-grid .source-card /deep/ .ant-card-head-title { white-space: normal; }
.market-intro { margin: 0 0 10px; color: #647269; font-size: 13px; line-height: 1.7; }
.pool-method { margin-bottom: 12px; font-size: 12px; color: #647269; }
.pool-method summary { width: fit-content; cursor: pointer; color: #356452; }
.pool-method p { max-width: 780px; margin: 8px 0 0; line-height: 1.8; }
.asset-code { font-variant-numeric: tabular-nums; letter-spacing: .025em; white-space: nowrap; }
.pool-asset { gap: 3px; }
.pool-asset strong { font-size: 13px; word-break: normal; }
.pool-asset .asset-code { color: #62756b; }
.evidence-toggle { align-self: flex-start; padding: 2px 0; border: 0; background: none; color: #176b4b; font: inherit; cursor: pointer; }
.evidence-toggle .anticon { margin-left: 3px; font-size: 10px; }
.evidence-toggle:focus-visible, .pool-method summary:focus-visible { outline: 2px solid #176b4b; outline-offset: 3px; }
.market-pool-panel /deep/ .ant-table-thead > tr > th { white-space: nowrap; }
.market-pool-panel /deep/ .ant-table-body { overscroll-behavior-x: contain; }
.market-pool-panel /deep/ .ant-btn { white-space: nowrap; }
.table-scroll-hint { display: none; margin: 10px 0 0; font-size: 12px; color: #6a776f; }
.watchlist-items { display: grid; grid-template-columns: minmax(0, 1fr); gap: 0; padding: 0; margin: 0; list-style: none; }
.watchlist-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; min-height: 74px; padding: 12px 0; border-top: 1px solid #e7ece9; }
.watchlist-asset { display: flex; flex-direction: column; min-width: 0; gap: 5px; }
.watchlist-asset strong { color: #243f32; font-size: 14px; line-height: 1.5; word-break: normal; overflow-wrap: anywhere; }
.watchlist-asset .asset-code { color: #6a776f; font-size: 12px; }
.watchlist-asset small { font-size: inherit; }
.watchlist-item .ant-btn { min-width: 88px; height: 30px; white-space: nowrap; }
.watchlist-item.is-selected { background: #f0f7f2; box-shadow: inset 3px 0 #176b4b; padding-left: 10px; padding-right: 6px; }
.watchlist-loading { min-height: 100px; padding: 30px 0; text-align: center; color: #6a776f; }
.dark .market-intro, .dark .pool-method, .dark .pool-asset .asset-code, .dark .table-scroll-hint, .dark .watchlist-asset .asset-code, .dark .watchlist-loading { color: #afc1b7; }
.dark .watchlist-asset strong { color: #e3ece7; }
.dark .pool-method summary, .dark .evidence-toggle { color: #8fd3b2; }
.dark .watchlist-item { border-color: #3b493f; }
.dark .watchlist-item.is-selected { background: #263d30; box-shadow: inset 3px 0 #8fd3b2; }
.dark .market-pool-panel /deep/ .ant-table-row-expand-icon { color: #d8e5dc; background: #26342c; border-color: #5c6d62; }
.dark .pool-filters /deep/ .ant-input::placeholder { color: #a4b4aa !important; }
.dark .pool-filters /deep/ .ant-input-suffix { color: #a4b4aa; }
.content-card { border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,.05); }
.source-card { min-height: 370px; margin-bottom: 16px; }
.source-card /deep/ .ant-card-head-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.source-note { min-height: 38px; margin: 0 0 12px; color: #7b8494; font-size: 12px; }
.inline-alert { margin-bottom: 10px; }
.pool-meta { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-bottom: 8px; color: #7b8494; font-size: 12px; }
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
.personal-home.dark .effectiveness-card { color: #e4e9f2; background: #202329; }
.personal-home.dark .effectiveness-card /deep/ .ant-card-body { color: #e4e9f2; background: #202329; border-radius: 12px; }
.personal-home.dark .effectiveness-heading > span:not(.ant-tag), .personal-home.dark .effectiveness-maturity strong, .personal-home.dark .effectiveness-maturity small, .personal-home.dark .effectiveness-counts strong, .personal-home.dark .effectiveness-detail, .personal-home.dark .effectiveness-detail p, .personal-home.dark .effectiveness-detail dt, .personal-home.dark .effectiveness-detail dd { color: #e4e9f2; }
.dark .effectiveness-maturity span, .dark .effectiveness-counts span, .dark .effectiveness-note, .dark .effectiveness-short { color: #aeb9cb; }
.dark .effectiveness-detail { border-color: #3b4350; }
.dark .effectiveness-detail summary { color: #8fc5ff; }
@media (max-width: 1000px) { .quick-links { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 600px) { .hero { flex-direction: column; padding: 20px; } .hero h1 { font-size: 22px; } .decision-summary { align-items: flex-start; flex-direction: column; } .quick-links { grid-template-columns: 1fr; } }

/* Phase 1: presentation only; existing evidence and selection components remain mounted. */
.personal-home { max-width: 1540px; margin: 0 auto; padding: 12px 16px 24px; }
.hero { padding: 4px 0 20px; border-radius: 0; background: none; box-shadow: none; color: #203c31; gap: 20px; }
.hero h1 { color: inherit; font-size: 27px; font-weight: 650; margin: 6px 0; }
.hero p, .hero-meta { color: #65766d; font-size: 12px; }
.eyebrow { color: #4b7a62; letter-spacing: .08em; font-weight: 600; }
.hero-actions { flex-shrink: 0; padding-top: 16px; }
.metric-grid { margin-top: 18px; }
.metric-card { min-height: auto; border: 1px solid #dfe8e2; border-radius: 10px; box-shadow: none; }
.metric-grid .metric-card { border: 0; background: transparent; }
.metric-grid .metric-card /deep/ .ant-card-body { padding: 12px 8px; }
.metric-value { font-size: 18px; margin: 8px 0 5px; min-height: 28px; }
.effectiveness-card { height: 100%; margin-bottom: 0; }
.effectiveness-card /deep/ .ant-card-body { padding: 19px; }
.effectiveness-heading { font-size: 16px; font-weight: 600; }
.effectiveness-maturity strong { font-size: 38px; }
.effectiveness-counts { gap: 12px 20px; }
.effectiveness-counts span { font-size: 12px; }
.content-card { box-shadow: none; border: 1px solid #dfe8e2; border-radius: 10px; }
.home-entry-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 16px; }
.home-entry { display: flex; align-items: center; justify-content: space-between; text-align: left; gap: 14px; padding: 17px 18px; border: 1px solid #dfe8e2; border-radius: 9px; background: #fff; color: #203c31; cursor: pointer; }
.home-entry:hover { border-color: var(--primary-color, #176b4b); }
.home-entry strong, .home-entry small { display: block; }
.home-entry strong { font-size: 15px; font-weight: 600; }
.home-entry small { margin-top: 5px; font-size: 12px; color: #65766d; }
.home-market-section { scroll-margin-top: 88px; margin-top: 18px; border: 1px solid #dfe8e2; border-radius: 10px; background: #fff; padding: 16px 18px; }
.home-market-section { container-type: inline-size; container-name: market; }
.home-market-section > summary { cursor: pointer; font-size: 15px; font-weight: 600; }
.home-market-section > summary small { display: block; margin: 5px 0 0 17px; color: #65766d; font-size: 12px; font-weight: 400; }
.home-market-section > .selection-grid { margin-top: 18px; }
.home-market-section .source-card { min-height: auto; }
@container market (min-width: 640px) {
  .watchlist-items { grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 24px; }
}
@container market (min-width: 1200px) {
  .selection-grid { grid-template-columns: minmax(0, 1fr) 320px; }
  .watchlist-items { grid-template-columns: minmax(0, 1fr); }
}
@container market (max-width: 860px) {
  .table-scroll-hint { display: block; }
}
.quick-links { gap: 8px; }
.quick-links button { padding: 11px; border-radius: 7px; }
.quick-links small { font-size: 12px; }
.audit-card { min-height: auto; }
.dark .hero { color: #e3ece7; }
.dark .hero p, .dark .hero-meta, .dark .home-entry small, .dark .home-market-section > summary small { color: #abbcb2; }
.dark .home-entry, .dark .home-market-section { color: #e3ece7; background: #202923; border-color: #3b493f; }
.dark .content-card, .dark .metric-card { border-color: #3b493f; }
.dark .effectiveness-card, .dark .effectiveness-card /deep/ .ant-card-body { background: #202923 !important; }
.dark .effectiveness-card /deep/ .ant-card-body { height: 100%; }
@media (max-width: 900px) { .quick-links { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 600px) {
  .personal-home { padding: 8px 14px 16px; }
  .hero { padding: 0 0 15px; gap: 12px; flex-direction: column; }
  .hero-actions { padding-top: 0; }
  .hero h1 { font-size: 24px; }
  .home-entry-grid { grid-template-columns: minmax(0,1fr); gap: 10px; }
  .quick-links { grid-template-columns: minmax(0,1fr); }
  .home-market-section { padding: 14px 10px; }
  .selection-grid .source-card /deep/ .ant-card-head { padding: 0 12px; }
  .selection-grid .source-card /deep/ .ant-card-body { padding: 12px; }
  .pool-meta { gap: 4px 10px; }
}
</style>
