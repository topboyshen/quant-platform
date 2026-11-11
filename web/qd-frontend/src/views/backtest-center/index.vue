<template>
  <div class="backtest-page" :class="{ 'theme-dark': isDarkTheme }" data-testid="backtest-center">
    <decision-context-bar
      v-if="!historyViewing"
      :artifact-label="backtestArtifactLabel"
      :artifact-status="backtestArtifactStatus"
      :artifact-detail="backtestArtifactDetail"
    />
    <a-alert
      v-if="sourceSelectionRequired && !historyViewing"
      type="info"
      show-icon
      class="source-selection-alert"
      :message="sourceSelectionReason || '本次只有研究标的，尚未选定策略来源。请手动选择已保存策略；不会按标的自动取第一份。'"
    />
    <a-button
      v-if="sourceSelectionRequired && form.sourceId && !historyViewing"
      size="small"
      data-testid="backtest-confirm-source"
      @click="selectSource(form.sourceId, { manual: true })"
    >明确选用当前显示的策略</a-button>
    <section class="hero-card">
      <div>
        <div class="eyebrow">{{ $t('strategyV2.apiBadge') }}</div>
        <h1>{{ $t('backtest-center.title') }}</h1>
        <p>{{ $t('strategyV2.manifestHint') }}</p>
      </div>
      <div class="hero-actions">
        <a-radio-group v-model="mode" button-style="solid" data-testid="research-mode-switch">
          <a-radio-button value="portfolio">{{ $t('strategyV2.backtest.mode.portfolio') }}</a-radio-button>
          <a-radio-button v-if="supportsFactorResearch" value="factor">{{ $t('strategyV2.backtest.mode.factor') }}</a-radio-button>
        </a-radio-group>
        <span class="hero-stat"><strong>{{ runnableSources.length }}</strong>{{ mode === 'factor' ? $t('strategyV2.factorResearch.eligibleSources') : $t('strategyV2.backtest.sources') }}</span>
        <span class="hero-stat"><strong>{{ history.length }}</strong>{{ mode === 'factor' ? $t('strategyV2.factorResearch.runs') : $t('strategyV2.backtest.runs') }}</span>
        <a-button icon="reload" :loading="historyLoading" @click="refreshPage">
          {{ $t('backtest-center.refreshHistory') }}
        </a-button>
        <a-button icon="history" @click="historyVisible = true">
          {{ mode === 'factor' ? $t('strategyV2.factorResearch.historyTitle') : $t('strategyV2.backtest.historyTitle') }}
        </a-button>
      </div>
    </section>

    <div class="workspace-grid">
      <section class="panel config-panel">
        <div class="config-scroll">
          <div class="panel-heading">
            <div>
              <span class="step-badge">1</span>
              <h2>{{ $t('strategyV2.sourceTitle') }}</h2>
            </div>
            <a-tag v-if="manifest" color="green">{{ $t('strategyV2.backtest.capabilityReady') }}</a-tag>
          </div>
          <a-select
            v-model="form.sourceId"
            class="full-width"
            show-search
            option-filter-prop="children"
            data-testid="backtest-source-select"
            :disabled="running"
            :loading="selectingSource"
            :placeholder="$t('backtest-center.strategyPlaceholder')"
            @change="id => selectSource(id, { manual: true })"
          >
            <a-select-option
              v-for="item in availableSources"
              :key="item.id"
              :value="item.id"
              :disabled="!isCtaBacktestSource(item)"
            >
              {{ item.name }} · {{ sourceTypeLabel(item) }}
              <span v-if="!isCtaBacktestSource(item)"> · {{ $t('strategyV2.backtest.ctaOnlyOption') }}</span>
            </a-select-option>
          </a-select>

          <a-alert
            v-if="hasUnsupportedSources"
            class="cta-phase-alert"
            type="info"
            show-icon
            :message="$t('strategyV2.backtest.ctaOnlyTitle')"
            :description="$t('strategyV2.backtest.ctaOnlyDesc')"
          />

          <a-alert
            class="execution-capability-alert"
            type="warning"
            show-icon
            banner
            :message="$t('strategyV2.backtest.executionBoundaryTitle')"
          >
            <template slot="description">
              <p>{{ $t('strategyV2.backtest.executionBoundaryDesc') }}</p>
              <div class="execution-capability-rules">
                <div>
                  <a-tag color="blue">MARKET</a-tag>
                  <span>{{ $t('strategyV2.backtest.marketOrderRule') }}</span>
                </div>
                <div>
                  <a-tag color="purple">LIMIT · NEXT_BAR</a-tag>
                  <span>{{ $t('strategyV2.backtest.limitOrderRule') }}</span>
                </div>
                <div>
                  <a-tag color="green">LEDGER</a-tag>
                  <span>{{ $t('strategyV2.backtest.authoritativeLedgerRule') }}</span>
                </div>
                <div>
                  <a-tag>HISTORY</a-tag>
                  <span>{{ $t('strategyV2.backtest.legacyHistoryRule') }}</span>
                </div>
              </div>
            </template>
          </a-alert>

          <a-alert
            v-if="sourceLoadError"
            class="source-load-alert"
            type="warning"
            show-icon
            :message="$t('strategyV2.sourceContractRequired')"
            :description="sourceLoadError"
          />

          <div v-if="manifest" class="manifest-card">
            <div class="manifest-title">
              <a-icon type="safety-certificate" />
              {{ $t('strategyV2.manifestTitle') }}
            </div>
            <div class="manifest-grid">
              <div><span>{{ $t('strategyV2.strategyType') }}</span><strong>{{ strategyTypeLabel }}</strong></div>
              <div><span>{{ $t('strategyV2.frequency') }}</span><strong>{{ manifestFrequency }}</strong></div>
              <div><span>{{ $t('strategyV2.markets') }}</span><strong>{{ (manifest.markets || []).join(', ') || '-' }}</strong></div>
              <div><span>{{ $t('strategyV2.universe') }}</span><strong>{{ universeLabel }}</strong></div>
            </div>
          </div>

          <div class="panel-heading runtime-heading">
            <div>
              <span class="step-badge">2</span>
              <h2>{{ mode === 'factor' ? $t('strategyV2.factorResearch.runtimeTitle') : $t('strategyV2.runtimeTitle') }}</h2>
            </div>
          </div>
          <p class="section-hint">{{ mode === 'factor' ? $t('strategyV2.factorResearch.runtimeHint') : $t('strategyV2.runtimeHint') }}</p>
          <a-form layout="vertical">
            <div class="form-grid">
              <a-form-item :label="$t('backtest-center.startDate')">
                <a-date-picker v-model="form.startDate" :disabled-date="disabledStartDate" class="full-width" />
              </a-form-item>
              <a-form-item :label="$t('backtest-center.endDate')">
                <a-date-picker v-model="form.endDate" :disabled-date="disabledEndDate" class="full-width" />
              </a-form-item>
              <a-form-item v-if="mode === 'portfolio'" :label="$t('backtest-center.initialCapital')">
                <a-input-number v-model="form.initialCapital" :min="10" class="full-width" />
              </a-form-item>
              <a-form-item :label="$t('backtest-center.commission')">
                <a-input-number v-model="form.commission" :min="0" :max="1" :step="0.0001" class="full-width" />
              </a-form-item>
              <a-form-item :label="$t('backtest-center.slippage')">
                <a-input-number v-model="form.slippage" :min="0" :max="1" :step="0.0001" class="full-width" />
              </a-form-item>
              <a-form-item v-if="mode === 'portfolio'" :label="$t('strategyV2.leverageEnabled')">
                <div class="switch-row">
                  <a-switch v-model="form.leverageEnabled" :disabled="!leverageAllowed" />
                  <span>{{ leverageAllowed ? $t('strategyV2.backtest.optional') : $t('strategyV2.codeOwned') }}</span>
                </div>
              </a-form-item>
              <a-form-item v-if="mode === 'portfolio' && form.leverageEnabled" :label="$t('strategyV2.leverageMultiplier')">
                <a-input-number v-model="form.leverage" :min="1" :max="maxLeverage" :step="0.5" class="full-width" />
              </a-form-item>
            </div>

            <a-alert
              v-if="backtestRangePolicy"
              class="range-limit-alert"
              :type="backtestRangeExceeded ? 'error' : 'info'"
              show-icon
              :message="backtestRangeAlertTitle"
              :description="backtestRangeAlertDescription"
            />

            <div v-if="mode === 'portfolio' && paramDefinitions.length" class="params-section">
              <div class="subheading">
                <h3>{{ $t('backtest-center.codeParams') }}</h3>
                <span>{{ $t('strategyV2.backtest.paramCount', { count: paramDefinitions.length }) }}</span>
              </div>
              <div class="form-grid">
                <a-form-item v-for="item in paramDefinitions" :key="item.name" :label="parameterLabel(item)" :extra="paramSourceHint(item)">
                  <a-switch
                    v-if="item.type === 'boolean'"
                    :checked="Boolean(params[item.name])"
                    @change="value => setParam(item.name, value)"
                  />
                  <a-input-number
                    v-else-if="item.type !== 'text' && item.type !== 'string'"
                    :value="params[item.name]"
                    :min="item.min"
                    :max="item.max"
                    :step="item.step || 1"
                    class="full-width"
                    @change="value => setParam(item.name, value)"
                  />
                  <a-input
                    v-else
                    :value="params[item.name]"
                    class="full-width"
                    @input="event => setParam(item.name, event.target.value)"
                  />
                </a-form-item>
              </div>
            </div>

            <div v-if="mode === 'factor'" class="params-section factor-settings">
              <div class="subheading">
                <h3>{{ $t('strategyV2.factorResearch.settings') }}</h3>
                <span>{{ $t('strategyV2.factorResearch.independentMode') }}</span>
              </div>
              <a-alert
                class="factor-contract-alert"
                type="info"
                show-icon
                :message="$t('strategyV2.factorResearch.universeContractTitle')"
                :description="$t('strategyV2.factorResearch.universeContractDesc')" />
              <div class="form-grid">
                <a-form-item :label="$t('strategyV2.factorResearch.factor')">
                  <a-select v-model="factorForm.factorId" class="full-width">
                    <a-select-option value="momentum_20">{{ $t('strategyV2.factorResearch.factors.momentum20') }}</a-select-option>
                    <a-select-option value="volatility_20">{{ $t('strategyV2.factorResearch.factors.volatility20') }}</a-select-option>
                    <a-select-option value="reversal_5">{{ $t('strategyV2.factorResearch.factors.reversal5') }}</a-select-option>
                    <a-select-option value="value">{{ $t('strategyV2.factorResearch.factors.value') }}</a-select-option>
                    <a-select-option value="quality">{{ $t('strategyV2.factorResearch.factors.quality') }}</a-select-option>
                    <a-select-option value="size">{{ $t('strategyV2.factorResearch.factors.size') }}</a-select-option>
                  </a-select>
                </a-form-item>
                <a-form-item :label="$t('strategyV2.factorResearch.groups')">
                  <a-input-number v-model="factorForm.groups" :min="2" :max="10" class="full-width" />
                </a-form-item>
                <a-form-item :label="$t('strategyV2.factorResearch.holdingPeriod')">
                  <a-input-number v-model="factorForm.holdingPeriod" :min="1" :max="60" class="full-width" />
                </a-form-item>
                <a-form-item :label="$t('strategyV2.factorResearch.industryNeutralization')">
                  <div class="switch-row"><a-switch v-model="factorForm.neutralizeIndustry" /><span>{{ $t('strategyV2.factorResearch.neutralizationHint') }}</span></div>
                </a-form-item>
              </div>
            </div>

          </a-form>
        </div>
        <div class="run-action-bar">
          <a-button
            type="primary"
            block
            size="large"
            icon="thunderbolt"
            data-testid="run-backtest"
            :disabled="runDisabled"
            :loading="running"
            @click="runActive"
          >
            {{ mode === 'portfolio' ? $t('backtest-center.runBacktest') : $t('strategyV2.factorResearch.run') }}
          </a-button>
        </div>
      </section>

      <section class="panel result-panel">
        <div class="panel-heading">
          <div>
            <span class="step-badge">3</span>
            <h2>{{ mode === 'factor' ? $t('strategyV2.factorResearch.workspace') : $t('backtest-center.resultOverview') }}</h2>
          </div>
          <span v-if="selectedRun" class="run-id">{{ mode === 'factor' ? 'FR-' : '#' }}{{ selectedRun.id || selectedRun.runId }}</span>
          <a-button
            v-if="historyViewing && selectedRun && mode !== 'factor'"
            size="small"
            type="link"
            data-testid="backtest-use-run-config"
            @click="useRunAsNewBacktest(selectedRun)"
          >以此配置新回测</a-button>
          <a-button
            v-if="historyViewing && mode !== 'factor'"
            size="small"
            type="link"
            data-testid="backtest-exit-history"
            @click="closeHistoryView"
          >返回当前回测</a-button>
        </div>

        <!-- 历史身份：统一取该次运行的记录，缺失显示“未知”；不借当前表单，不默认成零 -->
        <div v-if="historyViewing && mode !== 'factor'" class="history-identity" data-testid="backtest-history-identity">
          <span class="history-identity__item" data-testid="history-identity-symbol"><span class="history-identity__label">{{ $t('backtest-center.history.symbol') }}：</span><span class="history-identity__value">{{ displayHistoryValue(historyIdentity.symbol) }}</span></span>
          <span class="history-identity__item" data-testid="history-identity-range"><span class="history-identity__label">{{ $t('backtest-center.history.range') }}：</span><span class="history-identity__value">{{ displayHistoryValue(historyIdentity.range) }}</span></span>
          <span class="history-identity__item" data-testid="history-identity-version"><span class="history-identity__label">{{ $t('backtest-center.history.version') }}：</span><span class="history-identity__value">{{ displayHistoryValue(historyIdentity.version) }}</span></span>
          <span class="history-identity__item" data-testid="history-identity-capital"><span class="history-identity__label">{{ $t('backtest-center.history.initialCapital') }}：</span><span class="history-identity__value">{{ displayHistoryValue(historyIdentity.initialCapital) }}</span></span>
        </div>
        <div v-if="historyViewing && selectedRun && !historyDetailLoading && !historyDetailError" class="history-source-link">
          <a-button size="small" type="link" data-testid="backtest-source-snapshot-toggle" @click="historySourceVisible = !historySourceVisible">
            {{ historySourceVisible ? '收起当次来源' : '查看当次来源（只读）' }}
          </a-button>
          <div v-if="historySourceVisible" class="history-source-snapshot" data-testid="backtest-source-snapshot">
            <strong>运行 #{{ selectedRun.id || selectedRun.runId }} 保存的来源线索</strong>
            <span>策略来源 ID：{{ historySourceIdentity.sourceId }}</span>
            <span>源码指纹：{{ historySourceIdentity.codeHash }}</span>
            <span>契约指纹：{{ historySourceIdentity.contractHash }}</span>
            <span>上游指标：{{ historySourceIdentity.indicatorId }}</span>
            <small>记录存在，但现有接口不能读取不可变的当次源码版本；以上仅为该次运行保存的线索，不会打开当前最新源码冒充历史版本。</small>
          </div>
        </div>
        <div v-if="historyDetailLoading" class="history-detail-state" data-testid="backtest-history-detail-loading" aria-live="polite">正在读取该次运行的详情…</div>

        <div v-if="running" class="result-running" data-testid="backtest-running" aria-live="polite">
          <div class="running-icon"><a-icon type="loading" /></div>
          <h3>{{ mode === 'factor' ? $t('strategyV2.factorResearch.runningTitle') : $t('strategyV2.backtest.runningTitle') }}</h3>
          <p>{{ mode === 'factor' ? $t('strategyV2.factorResearch.runningDesc') : $t('strategyV2.backtest.runningDesc') }}</p>
          <strong>{{ $t('strategyV2.backtest.elapsed', { seconds: runElapsedSeconds }) }}</strong>
          <div class="running-contract">
            <span><a-icon type="check-circle" />{{ $t('strategyV2.backtest.sourceCheck') }}</span>
            <span><a-icon type="database" />{{ mode === 'factor' ? $t('strategyV2.factorResearch.pointInTimeExecution') : $t('strategyV2.backtest.serverExecution') }}</span>
            <span><a-icon type="safety-certificate" />{{ mode === 'factor' ? $t('strategyV2.factorResearch.metricsBeforeReturn') : $t('strategyV2.backtest.auditBeforeReturn') }}</span>
          </div>
        </div>

        <div v-else-if="runError || historyDetailError" class="result-error" data-testid="backtest-error">
          <a-alert
            type="error"
            show-icon
            data-testid="backtest-history-detail-error"
            :message="historyDetailError ? $t('backtest-center.history.detailFailed') : $t('backtest-center.result.failed')"
            :description="runError || historyDetailError"
          />
        </div>

        <div v-else-if="historyViewing && historyDetailLoading" class="result-empty" data-testid="backtest-history-loading">
          <h3>正在读取该次运行的详情…</h3>
        </div>

        <div v-else-if="historyViewing && !activeResult" class="result-empty" data-testid="backtest-history-empty">
          <h3>该次运行没有可展示的结果</h3>
        </div>

        <div v-else-if="!activeResult" class="result-empty" data-testid="backtest-empty">
          <div class="empty-orbit"><a-icon type="line-chart" /></div>
          <h3>{{ emptyResultTitle }}</h3>
          <p>{{ emptyResultDescription }}</p>
          <div class="empty-checks">
            <span :class="{ done: manifest }"><a-icon :type="manifest ? 'check-circle' : 'clock-circle'" />{{ $t('strategyV2.backtest.sourceCheck') }}</span>
            <span><a-icon type="safety-certificate" />{{ mode === 'factor' ? $t('strategyV2.factorResearch.crossSectionCheck') : $t('strategyV2.backtest.safeFill') }}</span>
            <span><a-icon type="database" />{{ $t('strategyV2.backtest.persisted') }}</span>
          </div>
          <div v-if="manifest" class="empty-preview-grid">
            <div class="empty-preview-card">
              <a-icon type="code" />
              <span>{{ $t('strategyV2.sourceTitle') }}</span>
              <strong>{{ source && source.name }}</strong>
            </div>
            <div class="empty-preview-card">
              <a-icon type="global" />
              <span>{{ $t('strategyV2.universe') }}</span>
              <strong>{{ universeLabel }}</strong>
            </div>
            <div class="empty-preview-card">
              <a-icon type="clock-circle" />
              <span>{{ $t('strategyV2.frequency') }}</span>
              <strong>{{ manifestFrequency }}</strong>
            </div>
          </div>
        </div>

        <portfolio-result
          v-else-if="mode === 'portfolio'"
          :result="result"
          :initial-capital-input="activeResultInitialCapital"
          :is-dark="isDarkTheme" />
        <factor-research-result v-else :result="factorResult" :is-dark="isDarkTheme" />
      </section>
    </div>

    <a-drawer
      :visible="historyVisible"
      :title="mode === 'factor' ? $t('strategyV2.factorResearch.historyTitle') : $t('strategyV2.backtest.historyTitle')"
      placement="right"
      width="430"
      :wrap-class-name="isDarkTheme ? 'backtest-history-drawer theme-dark' : 'backtest-history-drawer'"
      :destroy-on-close="false"
      @close="historyVisible = false"
    >
      <div class="drawer-history-header" data-testid="backtest-history">
        <p>{{ mode === 'factor' ? $t('strategyV2.factorResearch.historyDesc') : $t('strategyV2.backtest.historyDesc') }}</p>
        <a-button icon="reload" size="small" :loading="historyLoading" @click="loadHistory">
          {{ $t('backtest-center.refreshHistory') }}
        </a-button>
      </div>
      <a-alert
        v-if="historyError"
        type="warning"
        show-icon
        class="drawer-history-error"
        data-testid="backtest-history-error"
        :message="$t('backtest-center.history.listFailed')"
        :description="historyError"
      />
      <p v-if="historyError && history.length" class="history-stale-note" data-testid="backtest-history-stale">{{ $t('backtest-center.history.staleList') }}</p>
      <a-empty v-if="historyState === 'empty'" :description="mode === 'factor' ? $t('strategyV2.factorResearch.noHistory') : $t('strategyV2.backtest.noHistory')" />
      <div v-else class="drawer-run-list">
        <button
          v-for="item in history"
          :key="item.id"
          type="button"
          class="run-card"
          :class="{ active: selectedRun && Number(selectedRun.id || selectedRun.runId) === Number(item.id) }"
          @click="openRun(item)"
        >
          <span class="run-card__top"><strong>{{ mode === 'factor' ? (item.source_name || item.factor_id) : (item.strategy_name || item.symbol) }}</strong><em>{{ mode === 'factor' ? 'FR-' : '#' }}{{ item.id }}</em></span>
          <span class="run-card__meta">{{ item.market }} · {{ item.timeframe }} · {{ formatDate(item.created_at) }}</span>
          <template v-if="mode === 'factor'">
            <span class="run-card__status status-complete">{{ factorLabel(item.factor_id) }} · {{ item.groups_count }}Q · {{ item.holding_period }}{{ $t('strategyV2.factorResearch.bars') }}</span>
            <span class="run-card__metrics factor-history-metrics">
              <b :class="factorTone(item.rank_ic)">IC {{ formatSignedNumber(item.rank_ic, 4) }}</b>
              <small>{{ $t('strategyV2.factorResearch.icir') }} {{ formatNumber(item.icir, 3) }} · {{ $t('strategyV2.factorResearch.netLongShort') }} {{ formatRate(item.net_long_short_return) }}</small>
            </span>
          </template>
          <template v-else>
            <span class="run-card__status" :class="`status-${historyStatusKey(item)}`">{{ historyStatusLabel(item) }}</span>
            <span class="run-card__metrics">
              <b :class="historyReturnTone(item)">{{ formatPercent(item.total_return) }}</b>
              <small>{{ $t('strategyV2.backtest.executions') }} {{ item.total_executions || 0 }} · {{ $t('strategyV2.backtest.closedTrades') }} {{ item.total_trades || 0 }}</small>
            </span>
          </template>
        </button>
      </div>
    </a-drawer>
  </div>
</template>

<script>
import * as echarts from 'echarts'
import moment from 'moment'
import { mapState } from 'vuex'
import {
  normalizeStrategyBacktestResult,
  strategyRunFailed,
  strategyRunParams,
  strategyRunStatus
} from '@/utils/strategyResearchContracts.mjs'
import {
  CTA_ORDER_V2_ENGINE,
  isCtaOrderV2Capability
} from '@/utils/strategyOrderV2Capability.mjs'
import {
  buildSavedSourceDecisionContext,
  clearDecisionContext,
  readDecisionContext,
  writeDecisionContext
} from '@/utils/decisionContext'
import {
  compileScriptSource,
  getScriptSourceDetail,
  getScriptSourceList,
  getStrategyFactorResearchHistory,
  getStrategyFactorResearchRun,
  getStrategyBacktestHistory,
  getStrategyBacktestRun,
  runStrategyFactorResearch,
  runStrategyBacktest
} from '@/api/strategy'
import PortfolioResult from './PortfolioResult.vue'
import { historyViewState, shouldAcceptResponse } from './historyState.js'
import FactorResearchResult from './FactorResearchResult.vue'
import DecisionContextBar from '@/components/DecisionContextBar/index.vue'
import { readTemporaryAIReference } from '@/utils/researchLinkAI.js'
import {
  readSourceHandoff,
  sourceHandoffMatches
} from '@/utils/researchLinkRun.js'
import { diffParamOverrides, mergeDeclaredWithSaved } from '@/utils/researchParamHandoff'

export default {
  name: 'BacktestCenter',
  components: { DecisionContextBar, PortfolioResult, FactorResearchResult },
  data () {
    return {
      supportsFactorResearch: false,
      mode: 'portfolio',
      sources: [],
      portfolioHistory: [],
      factorHistory: [],
      // 历史读取的三态：loading / failed / empty 必须可区分（失败不得冒充空历史）
      historyError: '',
      historySeq: 0,
      // 历史查看与选择的乱序保护：迟到响应不得覆盖最后一次选择
      runOpenSeq: 0,
      historyViewing: false,
      historySourceVisible: false,
      sourceSelectionRequired: false,
      sourceSelectionArrivalKey: '',
      sourceSelectionReason: '',
      sourceBaselineSnapshot: null,
      linkedSourcePin: null,
    // 历史详情请求状态：加载中 / 读取失败 分开表达（列表失败不代替详情失败）
    historyDetailLoading: false,
    historyDetailError: '',
      source: null,
      manifest: null,
      sourceRevision: null,
      sourceContractHash: '',
      sourceCapability: null,
      backtestRangePolicy: null,
      params: {},
      paramBaseline: {},
      paramSourceOf: {},
      selectingSource: false,
      sourceSelectionSeq: 0,
      sourceLoadError: '',
      sourceListLoadError: '',
      decisionContextReady: false,
      incomingResearchEntryKey: '',
      incomingResearchEntrySeq: 0,
      result: null,
      factorResult: null,
      selectedRun: null,
      runError: '',
      running: false,
      runElapsedSeconds: 0,
      runTimer: null,
      historyLoading: false,
      historyVisible: false,
      equityChart: null,
      chartResizeObserver: null,
      form: {
        sourceId: null,
        startDate: moment().subtract(1, 'year'),
        endDate: moment(),
        initialCapital: 10000,
        commission: 0.0005,
        slippage: 0.0005,
        leverageEnabled: false,
        leverage: 1
      },
      factorForm: {
        factorId: 'momentum_20',
        groups: 5,
        holdingPeriod: 5,
        neutralizeIndustry: false
      }
    }
  },
  computed: {
    ...mapState({ navTheme: state => state.app.theme }),
    isDarkTheme () {
      return this.navTheme === 'dark' || this.navTheme === 'realdark'
    },
    activeResult () {
      return this.mode === 'portfolio' ? this.result : this.factorResult
    },
    availableSources () {
      if (this.mode !== 'factor') return this.sources
      return this.sources.filter(item => item.asset_type === 'portfolio_strategy')
    },
    runnableSources () {
      if (this.mode !== 'portfolio') return []
      return this.availableSources.filter(item => this.isCtaBacktestSource(item))
    },
    hasUnsupportedSources () {
      return this.availableSources.some(item => !this.isCtaBacktestSource(item))
    },
    history () {
      return this.mode === 'factor' ? this.factorHistory : this.portfolioHistory
    },

    // 三态：loading / failed / empty / ready —— 失败优先于空态，不把读取失败渲染成“没有历史”
    historyState () {
      return historyViewState({
        loading: this.historyLoading,
        error: this.historyError,
        count: this.history.length
      })
    },

    // 历史身份：只读该次运行自己的字段；缺失一律显示“未知”，不回读当前表单，不默认成零
    historyIdentity () {
      const run = this.selectedRun || {}
      const symbol = run.symbol || run.stock_code || run.code || ''
      const start = run.start_date || ''
      const end = run.end_date || ''
      const version = run.strategy_version || run.version || run.release_sha256 || ''
      const capital = run.initial_capital
      return {
        symbol: symbol ? String(symbol) : '未知',
        range: start && end ? `${start} ~ ${end}` : '未知',
        version: version ? String(version) : '未知',
        initialCapital: capital === undefined || capital === null || capital === '' ? '未知' : String(capital)
      }
    },
    historySourceIdentity () {
      const run = this.selectedRun || {}
      const result = run.result && typeof run.result === 'object' ? run.result : {}
      const source = run.sourceMeta || run.source_meta || result.sourceMeta || result.source_meta || {}
      const metadata = source.metadata && typeof source.metadata === 'object' ? source.metadata : {}
      const value = item => item == null || item === '' ? '未知' : String(item)
      return {
        sourceId: value(source.sourceId || source.source_id || run.source_id),
        codeHash: value(source.codeHash || source.code_hash || run.source_code_hash),
        contractHash: value(source.contractHash || source.contract_hash || run.source_contract_hash),
        indicatorId: value(metadata.source_indicator_id || source.source_indicator_id)
      }
    },
    backtestArtifactLabel () {
      if (this.sourceSelectionRequired) return '未选定本次交接策略'
      return this.source && this.source.id ? `策略源码 #${this.source.id}` : '未选定回测策略'
    },
    backtestArtifactStatus () {
      return this.sourceLoadError ? 'unavailable' : this.manifest && this.sourceCodeHash ? 'ready' : 'unknown'
    },
    backtestArtifactDetail () {
      const query = (this.$route && this.$route.query) || {}
      if (String(query.link || '') === 'ai') {
        const context = readDecisionContext({ allowStale: true }) || {}
        const ref = typeof sessionStorage !== 'undefined'
          ? readTemporaryAIReference(sessionStorage, {
            market: query.market || context.market,
            symbol: query.symbol || context.symbol
          })
          : null
        return ref
          ? `上游 AI 临时报告 ${ref.localRef}；当前尚须明确选定策略，报告不是已保存策略版本。`
          : '上游 AI 临时报告引用不可回溯；请明确选择策略，不会使用同标的第一份。'
      }
      if (this.sourceLoadError) return this.sourceLoadError
      if (this.source && this.sourceCodeHash) return `当前已核对源码指纹 ${this.sourceCodeHash.slice(0, 12)}…；运行须用户显式发起。`
      return '仅有标的不能确定策略版本；请选择具体已保存策略。'
    },

    // 历史视图下初始资金只取该次运行的值；缺失时不给子组件传当前表单值（避免借值）
    activeResultInitialCapital () {
      if (!this.historyViewing) return this.form.initialCapital
      const run = this.selectedRun || {}
      const capital = run.initial_capital
      if (capital === undefined || capital === null || capital === '') return undefined
      return Number(capital)
    },
    factorCompatible () {
      if (!this.manifest || this.manifest.strategyType !== 'portfolio') return false
      const universe = this.manifest.universe || {}
      return Boolean(universe.reference) || (universe.instruments || []).length >= 3
    },
    emptyResultTitle () {
      if (this.mode === 'factor') {
        if (!this.manifest) return this.$t('strategyV2.factorResearch.selectPortfolioTitle')
        if (!this.factorCompatible) return this.$t('strategyV2.factorResearch.incompatibleTitle')
        return this.$t('strategyV2.factorResearch.readyTitle')
      }
      return this.manifest ? this.$t('strategyV2.backtest.readyTitle') : this.$t('strategyV2.backtest.selectTitle')
    },
    emptyResultDescription () {
      if (this.mode === 'factor') {
        if (!this.manifest) return this.$t('strategyV2.factorResearch.selectPortfolioDesc')
        if (!this.factorCompatible) return this.$t('strategyV2.factorResearch.incompatibleDesc')
        return this.$t('strategyV2.factorResearch.readyDesc')
      }
      return this.manifest ? this.$t('strategyV2.backtest.readyDesc') : this.$t('backtest-center.emptyResult')
    },
    leverageAllowed () {
      return Boolean(this.manifest && this.manifest.leverageAllowed)
    },
    maxLeverage () {
      return Number((this.manifest && this.manifest.maxLeverage) || 1)
    },
    manifestFrequency () {
      const subscriptions = (this.manifest && this.manifest.subscriptions) || []
      return (this.manifest && this.manifest.primaryFrequency) || (subscriptions[0] && subscriptions[0].frequency) || '-'
    },
    backtestRangeLimitDays () {
      if (!this.backtestRangePolicy) return null
      const value = Number(this.mode === 'factor'
        ? this.backtestRangePolicy.factorMaxSelectedDays
        : this.backtestRangePolicy.maxSelectedDays)
      return Number.isFinite(value) ? Math.max(0, value) : null
    },
    selectedRangeDays () {
      if (!this.form.startDate || !this.form.endDate) return null
      return this.form.endDate.clone().startOf('day').diff(this.form.startDate.clone().startOf('day'), 'days')
    },
    estimatedBacktestBars () {
      if (!this.backtestRangePolicy || this.selectedRangeDays === null || this.selectedRangeDays < 0) return 0
      const seconds = Number(this.backtestRangePolicy.timeframeSeconds || 86400)
      const warmupBars = Number(this.mode === 'factor'
        ? this.backtestRangePolicy.factorWarmupBars
        : this.backtestRangePolicy.warmupBars) || 0
      return Math.ceil((this.selectedRangeDays * 86400) / Math.max(1, seconds)) + warmupBars
    },
    backtestRangeExceeded () {
      if (this.selectedRangeDays === null || this.selectedRangeDays < 0) return true
      return this.backtestRangeLimitDays !== null && this.selectedRangeDays > this.backtestRangeLimitDays
    },
    backtestRangeAlertTitle () {
      return this.$t('strategyV2.backtest.rangeLimitTitle', {
        timeframe: this.manifestFrequency,
        maxDays: this.backtestRangeLimitDays
      })
    },
    backtestRangeAlertDescription () {
      const key = this.backtestRangeExceeded
        ? 'strategyV2.backtest.rangeLimitExceeded'
        : 'strategyV2.backtest.rangeLimitEstimate'
      return this.$t(key, {
        timeframe: this.manifestFrequency,
        maxDays: this.backtestRangeLimitDays,
        bars: this.estimatedBacktestBars.toLocaleString(),
        maxBars: Number(this.backtestRangePolicy && this.backtestRangePolicy.maxBars || 0).toLocaleString()
      })
    },
    runDisabled () {
      return this.selectingSource || this.sourceSelectionRequired || !!this.sourceLoadError || !this.manifest || this.manifest.runnable === false || !this.isSupportedCtaCapability(this.sourceCapability, this.sourceContractHash) || !this.sourceCodeHash || !this.sourceContractHash || !this.isCtaBacktestSource(this.source) || !this.isCtaBacktestManifest(this.manifest) || this.backtestRangeExceeded || (this.mode === 'factor' && !this.factorCompatible)
    },
    sourceCodeHash () {
      return String((this.sourceRevision && this.sourceRevision.codeHash) || '').trim()
    },
    strategyTypeLabel () {
      const type = String((this.manifest && this.manifest.strategyType) || 'cta')
      return this.$t(type === 'portfolio' ? 'strategyV2.portfolio' : 'strategyV2.cta')
    },
    universeLabel () {
      const universe = (this.manifest && this.manifest.universe) || {}
      if (universe.reference) return universe.reference
      const instruments = Array.isArray(universe.instruments) ? universe.instruments : []
      if (this.manifest && this.manifest.strategyType === 'cta' && instruments.length) {
        return instruments.map(this.formatInstrument).join(', ')
      }
      return this.$t('strategyV2.symbolCount', { count: instruments.length })
    },
    paramDefinitions () {
      const schema = this.parseObject(this.source && this.source.param_schema)
      return Array.isArray(schema.params) ? schema.params : []
    },
    metrics () {
      if (!this.result) return []
      return [
        { key: 'return', label: this.$t('backtest-center.metrics.totalReturn'), value: this.formatPercent(this.result.totalReturn), tone: Number(this.result.totalReturn) >= 0 ? 'positive' : 'negative' },
        { key: 'benchmark', label: this.$t('strategyV2.backtest.benchmarkReturn'), value: this.result.benchmarkStatus === 'available' ? this.formatPercent(this.result.benchmarkTotalReturn) : '-', tone: Number(this.result.benchmarkTotalReturn) >= 0 ? 'positive' : 'negative' },
        { key: 'excess', label: this.$t('strategyV2.backtest.excessReturn'), value: this.result.benchmarkStatus === 'available' ? this.formatPercent(this.result.excessReturn) : '-', tone: Number(this.result.excessReturn) >= 0 ? 'positive' : 'negative' },
        { key: 'drawdown', label: this.$t('backtest-center.metrics.maxDrawdown'), value: this.formatPercent(this.result.maxDrawdown), tone: 'negative' },
        { key: 'executions', label: this.$t('strategyV2.backtest.executions'), value: Number(this.result.totalExecutions || 0), tone: '' },
        { key: 'trades', label: this.$t('strategyV2.backtest.closedTrades'), value: Number(this.result.totalTrades || 0), tone: '' },
        { key: 'win', label: this.$t('backtest-center.metrics.winRate'), value: this.formatPercent(this.result.winRate, false), tone: '' },
        { key: 'sharpe', label: this.$t('backtest-center.metrics.sharpe'), value: this.formatNumber(this.result.sharpeRatio), tone: '' }
      ]
    },
    equityPoints () {
      return (this.result && this.result.equityCurve) || []
    },
    executionRows () {
      return (this.result && (this.result.executions || this.result.rawTrades)) || []
    },
    tradeRows () {
      return (this.result && (this.result.closedTrades || this.result.trades)) || []
    },
    tradeColumns () {
      return [
        { title: this.$t('backtest-center.symbol'), dataIndex: 'symbol', key: 'symbol', width: 150 },
        { title: this.$t('backtest-center.tradeColumns.side'), dataIndex: 'side', key: 'side', width: 80 },
        { title: this.$t('backtest-center.tradeColumns.entryTime'), dataIndex: 'entry_time', key: 'entry_time', width: 170, customRender: value => this.formatDate(value) },
        { title: this.$t('backtest-center.tradeColumns.exitTime'), dataIndex: 'exit_time', key: 'exit_time', width: 170, customRender: value => this.formatDate(value) },
        { title: this.$t('backtest-center.tradeColumns.quantity'), dataIndex: 'quantity', key: 'quantity', width: 110, customRender: value => this.formatNumber(value, 4) },
        { title: this.$t('backtest-center.tradeColumns.entryPrice'), dataIndex: 'entry_price', key: 'entry_price', width: 120, customRender: value => this.formatNumber(value, 4) },
        { title: this.$t('backtest-center.tradeColumns.exitPrice'), dataIndex: 'exit_price', key: 'exit_price', width: 120, customRender: value => this.formatNumber(value, 4) },
        {
          title: this.$t('backtest-center.tradeColumns.profit'),
          dataIndex: 'profit',
          key: 'profit',
          width: 110,
          customRender: value => this.$createElement('span', { class: ['trade-profit', this.profitTone(value)] }, this.formatSignedNumber(value))
        },
        { title: this.$t('backtest-center.tradeColumns.balance'), dataIndex: 'balance', key: 'balance', width: 130, customRender: value => this.formatNumber(value) },
        { title: this.$t('backtest-center.tradeColumns.closeReason'), dataIndex: 'close_reason', key: 'close_reason', width: 160 }
      ]
    },
    executionColumns () {
      return [
        { title: this.$t('strategyV2.backtest.signalTime'), dataIndex: 'signal_time', key: 'signal_time', width: 160, customRender: value => this.formatDate(value) },
        { title: this.$t('strategyV2.backtest.fillTime'), dataIndex: 'time', key: 'time', width: 160, customRender: value => this.formatDate(value) },
        { title: this.$t('backtest-center.symbol'), dataIndex: 'symbol', key: 'symbol', width: 160 },
        { title: this.$t('strategyV2.backtest.side'), dataIndex: 'side', key: 'side', width: 70 },
        { title: this.$t('backtest-center.quantity'), dataIndex: 'quantity', key: 'quantity', customRender: value => this.formatNumber(value, 6) },
        { title: this.$t('backtest-center.price'), dataIndex: 'price', key: 'price', customRender: value => this.formatNumber(value, 4) },
        { title: this.$t('backtest-center.commission'), dataIndex: 'commission', key: 'commission', customRender: value => this.formatNumber(value, 4) },
        { title: this.$t('strategyV2.backtest.reason'), dataIndex: 'reason', key: 'reason', width: 150 }
      ]
    },
    resultTrustTone () {
      if (!this.result || (this.result.audit && !this.result.audit.passed)) return 'is-error'
      if (this.result.resultStatus === 'no_signals' || this.result.resultStatus === 'open_position_only') return 'is-warning'
      return 'is-success'
    },
    resultTrustIcon () {
      return this.resultTrustTone === 'is-success' ? 'check-circle' : this.resultTrustTone === 'is-warning' ? 'exclamation-circle' : 'close-circle'
    },
    resultStatusLabel () {
      return this.$t(`strategyV2.backtest.status.${(this.result && this.result.resultStatus) || 'unknown'}`)
    },
    resultStatusHint () {
      return this.$t(`strategyV2.backtest.status.${(this.result && this.result.resultStatus) || 'unknown'}Hint`)
    },
    auditLabel () {
      return this.$t(this.result && this.result.audit && this.result.audit.passed ? 'strategyV2.backtest.auditPassed' : 'strategyV2.backtest.auditFailed')
    },
    provenanceLabel () {
      const provenance = (this.result && this.result.dataProvenance) || {}
      return this.$t(provenance.kind === 'market' ? 'strategyV2.backtest.marketData' : 'strategyV2.backtest.fixtureData')
    },
    benchmarkCaption () {
      if (!this.result || this.result.benchmarkStatus !== 'available') return this.$t('strategyV2.backtest.benchmarkUnavailable')
      const benchmark = this.result.benchmark || {}
      return this.$t('strategyV2.backtest.comparedWith', { symbol: benchmark.symbol || '-' })
    },
    curveRange () {
      const curve = (this.result && this.result.equityCurve) || []
      if (!curve.length) return ''
      return `${this.formatNumber(curve[0].value)} → ${this.formatNumber(curve[curve.length - 1].value)}`
    }
  },
  watch: {
    result () {
      this.$nextTick(this.renderEquityChart)
    },
    isDarkTheme () {
      this.$nextTick(this.renderEquityChart)
    },
    mode (value) {
      this.handleModeChange(value)
    },
    '$route.query': {
      deep: true,
      handler () {
        if (this.decisionContextReady && !this.running) {
          this.syncIncomingResearchEntry({ force: true })
        }
      }
    }
  },
  async mounted () {
    await this.refreshPage()
    this.decisionContextReady = true
    const synced = await this.syncIncomingResearchEntry({
      initial: true,
      sourcesReady: true,
      force: true
    })
    if (!synced && !this.$route.query.sourceId && !this.$route.query.runId &&
      this.$route.query.source_selection !== 'required') {
      const context = readDecisionContext({ allowStale: true })
      if (!(context && context.market && context.symbol)) {
        const firstId = this.runnableSources[0] && Number(this.runnableSources[0].id)
        if (firstId) {
          this.form.sourceId = firstId
          await this.selectSource(firstId)
        }
      }
    }
    window.addEventListener('resize', this.resizeEquityChart)
  },
  activated () {
    if (!this.decisionContextReady || this.running) return
    this.syncIncomingResearchEntry({ force: true })
  },
  deactivated () {
    // 该路由 keepAlive: true：普通切页不触发 beforeDestroy，需在停用时失效旧请求
    this.invalidateHistoryDetail()
  },
  beforeDestroy () {
    this.invalidateHistoryDetail()
    this.sourceSelectionSeq += 1
    this.selectingSource = false
    window.removeEventListener('resize', this.resizeEquityChart)
    this.stopRunTimer()
    if (this.chartResizeObserver) this.chartResizeObserver.disconnect()
    if (this.equityChart) this.equityChart.dispose()
  },
  methods: {
    displayHistoryValue (value) {
      if (value === '未知') return this.$t('backtest-center.history.unknown')
      return value
    },
    backtestDraftSnapshot () {
      const date = value => value && typeof value.format === 'function' ? value.format('YYYY-MM-DD') : String(value || '')
      return JSON.stringify({
        sourceId: this.form.sourceId,
        startDate: date(this.form.startDate),
        endDate: date(this.form.endDate),
        initialCapital: this.form.initialCapital,
        commission: this.form.commission,
        slippage: this.form.slippage,
        leverageEnabled: this.form.leverageEnabled,
        leverage: this.form.leverage,
        params: this.params
      })
    },
    requireExplicitSourceSelection (reason, key) {
      // Keep the existing form draft intact, but never treat its old source as
      // the source selected by the newly arrived research context.
      this.invalidateHistoryDetail()
      this.sourceSelectionRequired = true
      this.sourceSelectionReason = reason
      this.sourceSelectionArrivalKey = key
      this.linkedSourcePin = null
      this.sourceLoadError = reason
      return false
    },
    async verifySelectedSourcePin (pin, sourceId) {
      try {
        const compiled = await compileScriptSource({ sourceId })
        const data = compiled && compiled.data || {}
        const matching = sourceHandoffMatches(pin, {
          sourceId,
          codeHash: data.sourceRevision && data.sourceRevision.codeHash,
          contractHash: data.contractHash
        })
        return matching
          ? { ok: true, reason: '' }
          : { ok: false, reason: '策略源码或回测契约在交接后已变化；已停止使用旧交接身份。' }
      } catch (error) {
        const status = Number(error && (error.status || error.response && error.response.status))
        return {
          ok: false,
          reason: status === 401 || status === 403
            ? '当前账号无权读取该策略源码；已停止使用旧交接身份。'
            : '策略源码身份核对失败；未使用当前最新版本替代。'
        }
      }
    },
    async syncIncomingResearchEntry (options = {}) {
      if ((!this.decisionContextReady && !options.initial) || this.running) return false
      const sequence = ++this.incomingResearchEntrySeq
      const query = (this.$route && this.$route.query) || {}
      const requestedRunId = Number(query.runId)
      if (query.runId !== undefined && query.runId !== '') {
        if (!Number.isSafeInteger(requestedRunId) || requestedRunId <= 0) {
          this.historyDetailError = '回测运行 ID 无效，未打开其他记录替代。'
          return false
        }
        const key = `run:${requestedRunId}`
        if (key === this.incomingResearchEntryKey && this.historyViewing &&
          Number(this.selectedRun && (this.selectedRun.id || this.selectedRun.runId)) === requestedRunId) return true
        const opened = await this.openRun({ id: requestedRunId })
        if (opened && sequence === this.incomingResearchEntrySeq) this.incomingResearchEntryKey = key
        return opened && sequence === this.incomingResearchEntrySeq
      }
      if (!options.sourcesReady) {
        const loaded = await this.loadSources()
        if (!loaded) {
          this.sourceLoadError = this.sourceListLoadError
          return false
        }
      } else if (this.sourceListLoadError) {
        this.sourceLoadError = this.sourceListLoadError
        return false
      }
      if (sequence !== this.incomingResearchEntrySeq) return false

      const routeSourceId = Number(query.sourceId)
      if (String(query.source_selection || '') === 'required' && !routeSourceId) {
        const key = `selection:${String(query.handoff || '')}:${String(query.market || '')}:${String(query.symbol || '')}`
        if (key !== this.sourceSelectionArrivalKey) {
          return this.requireExplicitSourceSelection(
            '本次只有研究标的，尚未指定策略；请手动选择已保存策略，不会自动使用同标的第一份。', key)
        }
        return !this.sourceSelectionRequired && !!this.manifest
      }
      if (Number.isFinite(routeSourceId) && routeSourceId > 0) {
        const linked = String(query.link || '') === 'strategy-source'
        const key = `source:${routeSourceId}:${String(query.handoff || '')}`
        const pin = linked
          ? readSourceHandoff(typeof sessionStorage !== 'undefined' ? sessionStorage : null, query.handoff, routeSourceId)
          : null
        if (linked && !pin) {
          return this.requireExplicitSourceSelection('策略交接凭据已失效或不可读取；没有改用当前最新源码。请重新从策略研究发起，或手动选定策略。', key)
        }
        const routeSource = this.sources.find(item => Number(item.id) === routeSourceId)
        if (!routeSource) {
          return this.requireExplicitSourceSelection('指定策略已删除或当前无权读取；没有改用其他同标的策略。', key)
        }
        if (
          key === this.incomingResearchEntryKey &&
          Number(this.form.sourceId) === routeSourceId &&
          this.manifest
        ) {
          const verdict = linked ? await this.verifySelectedSourcePin(pin, routeSourceId) : { ok: true }
          if (!verdict.ok) {
            return this.requireExplicitSourceSelection(verdict.reason, key)
          }
          if (sequence !== this.incomingResearchEntrySeq) return false
          if (!this.syncDecisionContextFromSource(this.source || routeSource)) return false
          this.sourceLoadError = ''
          return true
        }
        if (this.sourceBaselineSnapshot && this.backtestDraftSnapshot() !== this.sourceBaselineSnapshot) {
          return this.requireExplicitSourceSelection('当前回测参数尚有未使用的改动；新交接未覆盖草稿。请核对后明确选择策略。', key)
        }
        const selected = linked
          ? await this.selectSource(routeSourceId, { expectedIdentity: pin })
          : await this.selectSource(routeSourceId)
        if (selected === true) this.incomingResearchEntryKey = key
        return selected === true
      }

      const context = readDecisionContext({ allowStale: true })
      if (!context || !context.market || !context.symbol) return false
      const key = `context:${context.market}:${context.symbol}:${context.updatedAt || ''}`
      if (key !== this.sourceSelectionArrivalKey) {
        return this.requireExplicitSourceSelection(
          '研究对象只说明标的，不能确定具体策略。请选择一份已保存策略后再回测。', key)
      }
      return !this.sourceSelectionRequired && !!this.manifest
    },
    renderEquityChart () {
      if (!this.$refs.equityChart || !this.equityPoints.length) {
        if (this.equityChart) this.equityChart.clear()
        return
      }
      if (!this.equityChart) {
        this.equityChart = echarts.init(this.$refs.equityChart)
        if (typeof ResizeObserver !== 'undefined') {
          this.chartResizeObserver = new ResizeObserver(this.resizeEquityChart)
          this.chartResizeObserver.observe(this.$refs.equityChart)
        }
      }
      const strategyData = this.equityPoints.map(item => [moment(item.time).valueOf(), Number(item.value)])
      const benchmarkData = ((this.result && this.result.benchmarkCurve) || []).map(item => [moment(item.time).valueOf(), Number(item.value)])
      const textColor = this.isDarkTheme ? '#a3a3a3' : '#64748b'
      const gridColor = this.isDarkTheme ? '#292929' : '#e9eef4'
      const strategyName = this.$t('strategyV2.backtest.strategyEquity')
      const benchmarkName = this.$t('strategyV2.backtest.spotBenchmark')
      const valueLabel = this.$t('strategyV2.backtest.equityValue')
      const series = [{
        name: strategyName,
        type: 'line',
        data: strategyData,
        showSymbol: false,
        smooth: false,
        sampling: 'lttb',
        lineStyle: { width: 2.2 },
        areaStyle: { opacity: 0.08 },
        emphasis: { focus: 'series' }
      }]
      if (benchmarkData.length) {
        series.push({
          name: benchmarkName,
          type: 'line',
          data: benchmarkData,
          showSymbol: false,
          smooth: false,
          sampling: 'lttb',
          lineStyle: { width: 1.7, type: 'dashed' },
          emphasis: { focus: 'series' }
        })
      }
      this.equityChart.setOption({
        animationDuration: 350,
        color: ['#52c41a', '#f5a623'],
        grid: { left: 12, right: 18, top: 46, bottom: 58, containLabel: true },
        legend: { top: 4, left: 4, textStyle: { color: textColor }, data: benchmarkData.length ? [strategyName, benchmarkName] : [strategyName] },
        tooltip: {
          trigger: 'axis',
          confine: true,
          backgroundColor: this.isDarkTheme ? 'rgba(15,15,15,.97)' : 'rgba(255,255,255,.98)',
          borderColor: this.isDarkTheme ? '#383838' : '#d9e0e8',
          textStyle: { color: this.isDarkTheme ? '#f8fafc' : '#17233d' },
          axisPointer: { type: 'cross', snap: true, label: { backgroundColor: '#334155' } },
          formatter: params => {
            if (!params || !params.length) return ''
            const timestamp = params[0].value[0]
            const rows = params.map(item => `${item.marker}${item.seriesName}<b>${this.formatNumber(item.value[1])}</b>`).join('<br>')
            return `<div class="backtest-tooltip"><strong>${moment(timestamp).format('YYYY-MM-DD HH:mm')}</strong><span>${valueLabel}</span><br>${rows}</div>`
          }
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        xAxis: {
          type: 'time',
          boundaryGap: false,
          axisLine: { lineStyle: { color: gridColor } },
          axisLabel: { color: textColor, formatter: value => moment(value).format('YYYY-MM-DD') },
          splitLine: { show: false },
          axisPointer: { show: true }
        },
        yAxis: {
          type: 'value',
          scale: true,
          axisLabel: { color: textColor, formatter: value => Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 }) },
          splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
          axisPointer: { show: true, label: { formatter: params => this.formatNumber(params.value) } }
        },
        dataZoom: [
          { type: 'inside', xAxisIndex: 0, filterMode: 'none', zoomOnMouseWheel: true, moveOnMouseMove: true },
          { type: 'slider', xAxisIndex: 0, height: 22, bottom: 7, borderColor: 'transparent', backgroundColor: this.isDarkTheme ? '#0d0d0d' : '#f1f5f9', fillerColor: this.isDarkTheme ? 'rgba(82,196,26,.18)' : 'rgba(82,196,26,.12)', textStyle: { color: textColor }, showDetail: false }
        ],
        series
      }, true)
    },
    resizeEquityChart () {
      if (this.equityChart) this.equityChart.resize()
    },
    parseObject (value) {
      if (value && typeof value === 'object' && !Array.isArray(value)) return value
      if (typeof value !== 'string' || !value.trim()) return {}
      try { return JSON.parse(value) } catch (error) { return {} }
    },
    async refreshPage () {
      await Promise.all([
        this.loadSources().catch(() => false),
        this.loadHistory().catch(() => false)
      ])
    },
    async loadSources () {
      try {
        const response = await getScriptSourceList()
        const data = response && response.data
        this.sources = Array.isArray(data) ? data : ((data && data.items) || [])
        this.sourceListLoadError = ''
        return true
      } catch (error) {
        this.sources = []
        this.sourceListLoadError = '策略列表加载失败，请检查网络连接后重试。'
        return false
      }
    },
    async loadHistory () {
      // 乱序保护：只接受最后一次发起的请求结果
      const seq = ++this.historySeq
      this.historyLoading = true
      this.historyError = ''
      try {
        const portfolioResponse = await getStrategyBacktestHistory({ limit: 24 })
        if (!shouldAcceptResponse(seq, this.historySeq)) return false
        this.portfolioHistory = Array.isArray(portfolioResponse.data) ? portfolioResponse.data : []
        if (this.supportsFactorResearch) {
          const factorResponse = await getStrategyFactorResearchHistory({ limit: 24 })
          if (!shouldAcceptResponse(seq, this.historySeq)) return false
          this.factorHistory = Array.isArray(factorResponse.data) ? factorResponse.data : []
        } else {
          this.factorHistory = []
        }
        this.historyError = ''
        return true
      } catch (error) {
        if (!shouldAcceptResponse(seq, this.historySeq)) return false
        // 失败时保留已有列表，并显式标记错误——不把读取失败渲染成“没有历史”
        this.historyError = (error && (error.message || error.msg)) || this.$t('backtest-center.result.failed')
        return false
      } finally {
        if (shouldAcceptResponse(seq, this.historySeq)) this.historyLoading = false
      }
    },
    async handleModeChange () {
      this.invalidateHistoryDetail()
      this.selectedRun = null
      this.runError = ''
      this.sourceLoadError = ''
      this.historyVisible = false
      const currentId = Number(this.form.sourceId)
      const currentRunnable = this.runnableSources.some(item => Number(item.id) === currentId)
      if (!currentRunnable) {
        const nextSource = this.runnableSources[0]
        this.form.sourceId = nextSource ? Number(nextSource.id) : null
        if (nextSource) await this.selectSource(nextSource.id)
        else {
          this.sourceSelectionSeq += 1
          this.selectingSource = false
          this.clearSourceContract()
        }
      }
      this.applyBacktestRangePolicy()
      this.$nextTick(() => this.resizeEquityChart())
    },
    clearSourceContract () {
      this.source = null
      this.manifest = null
      this.sourceRevision = null
      this.sourceContractHash = ''
      this.sourceCapability = null
      this.backtestRangePolicy = null
      this.params = {}
      this.paramBaseline = {}
      this.paramSourceOf = {}
      this.form.leverageEnabled = false
      this.form.leverage = 1
    },
    paramSourceHint (item) {
      const name = item && item.name
      if (!name) return ''
      const base = this.paramBaseline || {}
      if (Object.prototype.hasOwnProperty.call(base, name) && JSON.stringify(base[name]) !== JSON.stringify((this.params || {})[name])) {
        return '本次覆盖（仅随本次回测提交）'
      }
      const src = (this.paramSourceOf || {})[name]
      if (src === 'saved') return '来源：策略已保存参数'
      if (src === 'declared') return '来源：代码默认值'
      return ''
    },
    sourceLoadFailureMessage (error) {
      const status = Number(error && (error.status || error.response && error.response.status))
      if (status === 401 || status === 403) return '当前账号无权读取该策略源码；已停止交接和回测。'
      if (status === 404) return '指定策略源码已删除或不可读取；没有改用当前最新源码。'
      return (error && (error.backendMessage || error.message)) || this.$t('strategyV2.sourceContractRequired')
    },
    isCtaBacktestSource (source = {}) {
      return String(source.asset_type || '').trim().toLowerCase() === 'script'
    },
    isCtaBacktestManifest (manifest = {}) {
      return String(manifest.strategyType || '').trim().toLowerCase() === 'cta'
    },
    isSupportedCtaCapability (capability = {}, contractHash = '') {
      return isCtaOrderV2Capability(capability, contractHash)
    },
    ctaOnlyUnsupportedMessage () {
      return this.$t('strategyV2.backtest.ctaOnlyUnsupported')
    },
    syncDecisionContextFromSource (source = {}) {
      const current = readDecisionContext({ allowStale: true })
      const next = buildSavedSourceDecisionContext(source, current || {})
      if (!next) {
        clearDecisionContext()
        this.sourceLoadError = '当前策略缺少明确的市场或标的，已清除旧研究上下文并阻止回测。'
        return null
      }
      const written = writeDecisionContext(next)
      if (!written) {
        clearDecisionContext()
        this.sourceLoadError = this.$t('strategyV2.sourceContractRequired')
        return null
      }
      return written
    },
    async selectSource (sourceId, options = {}) {
      const requestedSourceId = Number(sourceId)
      const selectionSeq = ++this.sourceSelectionSeq
      const preserveResult = Boolean(options.preserveResult)
      const runConfiguration = options.runConfiguration || null
      const preserveDraftOnFailure = Boolean(options.expectedIdentity)

      this.selectingSource = true
      this.sourceLoadError = ''
      if (!preserveDraftOnFailure) {
        this.form.sourceId = Number.isFinite(requestedSourceId) && requestedSourceId > 0 ? requestedSourceId : null
        this.clearSourceContract()
        if (!preserveResult) {
          this.invalidateHistoryDetail()
          this.result = null
          this.factorResult = null
          this.selectedRun = null
          this.runError = ''
        }
        if (runConfiguration) {
          this.applyRunConfiguration(runConfiguration, { historyView: true })
        }
      }

      if (!Number.isFinite(requestedSourceId) || requestedSourceId <= 0) {
        this.sourceLoadError = this.$t('strategyV2.sourceContractRequired')
        this.selectingSource = false
        if (!options.silentError) this.$message.error(this.sourceLoadError)
        return false
      }

      const sourceSummary = this.sources.find(item => Number(item.id) === requestedSourceId)
      if (sourceSummary && !this.isCtaBacktestSource(sourceSummary)) {
        this.sourceLoadError = this.ctaOnlyUnsupportedMessage()
        this.selectingSource = false
        if (!options.silentError) this.$message.warning(this.sourceLoadError)
        return false
      }

      try {
        const [response, compiled] = await Promise.all([
          getScriptSourceDetail(requestedSourceId),
          compileScriptSource({ sourceId: requestedSourceId })
        ])
        if (selectionSeq !== this.sourceSelectionSeq) return null

        let nextSource = response && response.data
        const compiledData = (compiled && compiled.data) || {}
        const nextManifest = compiledData.manifest || null
        const nextCapability = compiledData.capability || null
        const nextContractHash = typeof compiledData.contractHash === 'string'
          ? compiledData.contractHash.trim()
          : ''
        const rawSourceRevision = compiledData.sourceRevision || null
        const nextCodeHash = rawSourceRevision && typeof rawSourceRevision.codeHash === 'string'
          ? rawSourceRevision.codeHash.trim()
          : ''
        const nextSourceRevision = nextCodeHash
          ? { codeHash: nextCodeHash, updatedAt: rawSourceRevision.updatedAt || null }
          : null
        const nextBacktestRangePolicy = compiledData.backtestRangePolicy || null
        if (!nextSource || !nextManifest) {
          throw new Error((compiled && compiled.msg) || (response && response.msg) || this.$t('strategyV2.sourceContractRequired'))
        }
        if (!nextSourceRevision) {
          throw new Error(this.$t('strategyV2.backtest.sourceRevisionRequired'))
        }
        if (!this.isCtaBacktestSource(nextSource) || !this.isCtaBacktestManifest(nextManifest)) {
          throw new Error(this.ctaOnlyUnsupportedMessage())
        }
        if (
          nextManifest.runnable === false ||
          !this.isSupportedCtaCapability(nextCapability, nextContractHash) ||
          !this.isSupportedCtaCapability(nextManifest.capability, nextContractHash)
        ) {
          throw new Error(this.$t('strategyV2.backtest.capabilityRequired'))
        }

        const resolvedSourceId = Number(nextSource.id)
        if (!Number.isFinite(resolvedSourceId) || resolvedSourceId !== requestedSourceId) {
          throw new Error(this.$t('strategyV2.sourceContractRequired'))
        }
        if (options.expectedIdentity && !sourceHandoffMatches(options.expectedIdentity, {
          sourceId: resolvedSourceId,
          codeHash: nextCodeHash,
          contractHash: nextContractHash
        })) {
          throw new Error('策略源码或回测契约已变化；未使用当前最新版本代替交接时的源码。')
        }
        if (options.expectedIdentity && (
          !String(nextSource.updated_at || '').trim() ||
          !String(nextSourceRevision.updatedAt || '').trim() ||
          String(nextSource.updated_at).trim() !== String(nextSourceRevision.updatedAt).trim()
        )) {
          throw new Error('策略详情与编译结果并非同一来源修订；已停止交接，请重新从策略研究进入。')
        }

        const sourceSchema = this.parseObject(nextSource.param_schema)
        if (
          (!Array.isArray(sourceSchema.params) || !sourceSchema.params.length) &&
          compiledData.param_schema
        ) {
          nextSource = { ...nextSource, param_schema: compiledData.param_schema }
        }
        const paramSchema = this.parseObject(nextSource.param_schema)
        const nextParamDefinitions = Array.isArray(paramSchema.params) ? paramSchema.params : []
        // gap B (B1/B5): start from declared ⊕ saved template_params, not the declared defaults
        const savedParams = this.parseObject(nextSource.template_params)
        const merged = mergeDeclaredWithSaved(nextParamDefinitions, savedParams)
        const nextParams = merged.params

        if (selectionSeq !== this.sourceSelectionSeq) return null
        // Reconcile shared research context before committing source/params to
        // the form. A failed context write must not leave a half-applied source.
        if (!this.syncDecisionContextFromSource(nextSource)) {
          throw new Error(this.sourceLoadError || this.$t('strategyV2.sourceContractRequired'))
        }
        if (preserveDraftOnFailure && !preserveResult) {
          this.invalidateHistoryDetail()
          this.result = null
          this.factorResult = null
          this.selectedRun = null
          this.runError = ''
        }
        this.source = nextSource
        this.manifest = nextManifest
        this.sourceRevision = nextSourceRevision
        this.sourceContractHash = nextContractHash
        this.sourceCapability = nextCapability
        this.backtestRangePolicy = nextBacktestRangePolicy
        this.params = nextParams
        this.form.sourceId = resolvedSourceId
        this.paramBaseline = { ...nextParams }
        this.paramSourceOf = merged.sourceOf
        this.sourceLoadError = ''
        if (runConfiguration) {
          this.applyRunConfiguration(runConfiguration, { historyView: true })
        } else {
          this.applyBacktestRangePolicy()
        }
        this.linkedSourcePin = options.expectedIdentity || null
        this.sourceBaselineSnapshot = this.backtestDraftSnapshot()
        this.sourceSelectionRequired = false
        this.sourceSelectionReason = ''
        if (options.manual) {
          this.linkedSourcePin = null // an explicit new choice is not the previous handoff
        }
        return true
      } catch (error) {
        if (selectionSeq !== this.sourceSelectionSeq) return null
        if (!preserveDraftOnFailure) {
          this.clearSourceContract()
          this.form.sourceId = requestedSourceId
          if (runConfiguration) {
            this.applyRunConfiguration(runConfiguration, { historyView: true })
          }
        }
        this.sourceLoadError = this.sourceLoadFailureMessage(error)
        if (preserveDraftOnFailure) {
          this.sourceSelectionRequired = true
          this.sourceSelectionReason = this.sourceLoadError
          this.linkedSourcePin = null
        }
        if (!options.silentError) this.$message.error(this.sourceLoadError)
        return false
      } finally {
        if (selectionSeq === this.sourceSelectionSeq) {
          this.selectingSource = false
        }
      }
    },
    sourceTypeLabel (item) {
      if (String(item.template_key || '').startsWith('robot_v2_')) return this.$t('strategyV2.robot')
      if (item.asset_type === 'script') return this.$t('strategyV2.cta')
      if (item.asset_type === 'portfolio_strategy') return this.$t('strategyV2.portfolio')
      return item.asset_type || this.$t('strategyV2.backtest.unknownSourceType')
    },
    formatInstrument (item) {
      const marketType = String(item.market_type || item.marketType || '').toLowerCase()
      const marketTypeKey = marketType ? `marketContext.${marketType}` : ''
      const marketTypeLabel = marketTypeKey && this.$te(marketTypeKey) ? this.$t(marketTypeKey) : marketType
      return [item.symbol, marketTypeLabel].filter(Boolean).join(' · ')
    },
    parameterLabel (item) {
      if (!item.labelKey) return item.name
      const label = this.$t(item.labelKey)
      return label === item.labelKey ? item.name : label
    },
    setParam (name, value) {
      this.params = { ...this.params, [name]: value }
    },
    disabledStartDate (current) {
      if (!current || !this.form.endDate) return false
      const endDate = this.form.endDate.clone().startOf('day')
      if (current.isAfter(endDate, 'day')) return true
      if (this.backtestRangeLimitDays === null) return false
      return current.isBefore(endDate.clone().subtract(this.backtestRangeLimitDays, 'days'), 'day')
    },
    disabledEndDate (current) {
      if (!current || !this.form.startDate) return false
      const startDate = this.form.startDate.clone().startOf('day')
      if (current.isBefore(startDate, 'day')) return true
      if (this.backtestRangeLimitDays === null) return false
      return current.isAfter(startDate.clone().add(this.backtestRangeLimitDays, 'days'), 'day')
    },
    applyBacktestRangePolicy () {
      if (this.backtestRangeLimitDays === null || !this.form.startDate || !this.form.endDate) return
      if (this.selectedRangeDays <= this.backtestRangeLimitDays && this.selectedRangeDays >= 0) return
      this.form.startDate = this.form.endDate.clone().startOf('day').subtract(this.backtestRangeLimitDays, 'days')
      this.$message.info(this.$t('strategyV2.backtest.rangeLimitAdjusted', {
        timeframe: this.manifestFrequency,
        maxDays: this.backtestRangeLimitDays
      }))
    },
    ensureBacktestRangeAllowed () {
      if (!this.backtestRangeExceeded) return true
      this.$message.error(this.backtestRangeAlertDescription)
      return false
    },
    startRunTimer () {
      this.stopRunTimer()
      this.runElapsedSeconds = 0
      const startedAt = Date.now()
      this.runTimer = window.setInterval(() => {
        this.runElapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
      }, 1000)
    },
    stopRunTimer () {
      if (this.runTimer) window.clearInterval(this.runTimer)
      this.runTimer = null
    },
    async run () {
      this.invalidateHistoryDetail()
      if (!this.form.sourceId) {
        this.$message.warning(this.$t('strategyV2.sourceContractRequired'))
        return
      }
      if (this.selectingSource || this.sourceSelectionRequired || this.sourceLoadError || !this.manifest) {
        this.$message.warning(this.sourceLoadError || this.$t('strategyV2.sourceContractRequired'))
        return
      }
      if (!this.isCtaBacktestSource(this.source) || !this.isCtaBacktestManifest(this.manifest)) {
        this.$message.warning(this.ctaOnlyUnsupportedMessage())
        return
      }
      if (this.manifest.runnable === false || !this.isSupportedCtaCapability(this.sourceCapability, this.sourceContractHash)) {
        this.$message.warning(this.$t('strategyV2.backtest.capabilityRequired'))
        return
      }
      if (!this.sourceCodeHash) {
        this.$message.warning(this.$t('strategyV2.backtest.sourceRevisionRequired'))
        return
      }
      const pinVerdict = this.linkedSourcePin
        ? await this.verifySelectedSourcePin(this.linkedSourcePin, this.form.sourceId)
        : { ok: true }
      if (!pinVerdict.ok) {
        this.sourceLoadError = pinVerdict.reason
        this.$message.warning(this.sourceLoadError)
        return
      }
      if (!this.ensureBacktestRangeAllowed()) return
      this.running = true
      this.result = null
      this.selectedRun = null
      this.runError = ''
      this.startRunTimer()
      try {
        const response = await runStrategyBacktest({
          sourceId: this.form.sourceId,
          engine: CTA_ORDER_V2_ENGINE,
          expectedContractHash: this.sourceContractHash,
          startDate: this.form.startDate.format('YYYY-MM-DD'),
          endDate: this.form.endDate.format('YYYY-MM-DD'),
          initialCapital: this.form.initialCapital,
          commission: this.form.commission,
          slippage: this.form.slippage,
          leverageEnabled: this.form.leverageEnabled,
          leverage: this.form.leverageEnabled ? this.form.leverage : 1,
          // gap B (B2/B3): only values changed on this page are sent as overrides
          params: diffParamOverrides(this.params, this.paramBaseline || {})
        })
        this.result = normalizeStrategyBacktestResult(response.data)
        this.selectedRun = { id: response.data && response.data.runId }
        await this.loadHistory()
      } catch (error) {
        this.runError = (error && error.backendMessage) || this.$t('strategyV2.backtest.runFailed')
        this.$message.error((error && error.backendMessage) || this.$t('strategyV2.backtest.runFailed'))
        await this.loadHistory()
      } finally {
        this.stopRunTimer()
        this.running = false
      }
    },
    async runFactorResearch () {
      this.invalidateHistoryDetail()
      if (this.sourceSelectionRequired || this.sourceLoadError) {
        this.$message.warning(this.sourceLoadError || this.sourceSelectionReason || this.$t('strategyV2.sourceContractRequired'))
        return
      }
      if (!this.form.sourceId) {
        this.$message.warning(this.$t('strategyV2.sourceContractRequired'))
        return
      }
      if (!this.ensureBacktestRangeAllowed()) return
      this.running = true
      this.factorResult = null
      this.startRunTimer()
      try {
        const response = await runStrategyFactorResearch({
          sourceId: this.form.sourceId,
          startDate: this.form.startDate.format('YYYY-MM-DD'),
          endDate: this.form.endDate.format('YYYY-MM-DD'),
          commission: this.form.commission,
          slippage: this.form.slippage,
          factorId: this.factorForm.factorId,
          groups: this.factorForm.groups,
          holdingPeriod: this.factorForm.holdingPeriod,
          neutralizeIndustry: this.factorForm.neutralizeIndustry,
          timeout: 180000
        })
        this.factorResult = response.data
        this.selectedRun = { id: response.data && response.data.runId }
        await this.loadHistory()
      } catch (error) {
        this.$message.error((error && error.backendMessage) || this.$t('strategyV2.factorResearch.runFailed'))
      } finally {
        this.stopRunTimer()
        this.running = false
      }
    },
    runActive () {
      return this.mode === 'portfolio' || !this.supportsFactorResearch ? this.run() : this.runFactorResearch()
    },
    async openRun (item) {
      this.historyVisible = false
      if (this.mode === 'factor') return this.openFactorRun(item)
      const requestedRunId = Number(item && item.id)
      if (!Number.isSafeInteger(requestedRunId) || requestedRunId <= 0) {
        this.historyDetailError = '回测运行 ID 无效，未打开其他记录替代。'
        return false
      }
      // 请求身份：只有最后一次选择可以落到界面上（成功、失败、结束加载都核对）
      const seq = ++this.runOpenSeq
      this.historyViewing = true
      this.historySourceVisible = false
      this.historyDetailLoading = true
      this.historyDetailError = ''
      // 不让上一条记录的结果冒充本次所选记录
      this.result = null
      this.runError = ''
      this.selectedRun = item || null
      try {
        const response = await getStrategyBacktestRun(requestedRunId)
        if (!shouldAcceptResponse(seq, this.runOpenSeq)) return false
        const run = response.data || {}
        if (Number(run.id || run.runId) !== requestedRunId) {
          throw new Error('回测详情的运行 ID 与所选记录不一致；未打开其他记录替代。')
        }
        this.selectedRun = run
        if (strategyRunFailed(run)) {
          this.result = null
          this.runError = run.error_message || this.$t('backtest-center.result.failed')
        } else {
          const result = run.result && Object.keys(run.result).length ? run.result : null
          this.result = normalizeStrategyBacktestResult(result)
          // 读取成功但未保存结果属于历史空态；只有真实失败记录进入错误分支。
          this.runError = ''
        }
        return true
      } catch (error) {
        if (!shouldAcceptResponse(seq, this.runOpenSeq)) return false
        // 详情读取失败：明确提示，不静默关闭抽屉、不保留旧结果冒充本次记录
        this.historyDetailError = (error && (error.message || error.msg)) || this.$t('backtest-center.result.failed')
        this.result = null
        return false
      } finally {
        if (shouldAcceptResponse(seq, this.runOpenSeq)) this.historyDetailLoading = false
      }
      // 查看历史：只展示**该次运行**的快照（结果与身份）。
      // 不覆盖当前参数草稿、不切换当前源码、不编译当前源码——
      // 否则会把“旧结果”和“当前策略上下文”混在一起，并误报当前源码编译失败。
      // 需要按该配置发起新回测时，由用户显式选择「以此配置新回测」。
    },

    // 退出历史 / 切换对象 / 进入新回测：让在途详情请求失效，旧记录不得复活
    invalidateHistoryDetail () {
      // 历史模式结束时连同快照一起清除，避免缓存页返回后旧结果挂到当前身份。
      // 非历史模式的当前回测结果保留；此处不改当前参数草稿。
      if (this.historyViewing) {
        this.result = null
        this.selectedRun = null
        this.runError = ''
      }
      this.runOpenSeq += 1
      this.historyDetailLoading = false
      this.historyDetailError = ''
      this.historySourceVisible = false
      // 退出历史视图：切换对象/切换源码/进入新回测后，历史身份不再展示
      this.historyViewing = false
    },

    closeHistoryView () {
      this.invalidateHistoryDetail()
      this.historyViewing = false
      this.selectedRun = null
      this.result = null
      this.runError = ''
    },

    async useRunAsNewBacktest (run) {
      // 用户明确选择后，才把历史配置当作新回测草稿，并校验当前源码。
      // 真实编译失败仍会阻止运行（沿用既有 selectSource / 校验路径）。
      const target = run || this.selectedRun
      if (!target) return
      this.invalidateHistoryDetail()
      this.historyViewing = false
      this.applyRunConfiguration(target, { historyView: false })
      const sourceId = Number(target.source_id)
      if (!Number.isFinite(sourceId) || sourceId <= 0) {
        this.sourceSelectionSeq += 1
        this.selectingSource = false
        this.sourceLoadError = this.$t('strategyV2.sourceContractRequired')
        this.$message.warning(this.sourceLoadError)
        return
      }
      const loaded = await this.selectSource(sourceId, {
        preserveResult: true,
        runConfiguration: target,
        silentError: true
      })
      if (loaded === false && this.sourceLoadError) {
        this.$message.warning(this.sourceLoadError)
      }
    },
    applyRunConfiguration (run, options = {}) {
      const defaults = this.paramDefinitions.reduce((output, item) => {
        output[item.name] = item.default
        return output
      }, {})
      this.params = { ...defaults, ...strategyRunParams(run) }
      if (run.start_date) this.form.startDate = moment(run.start_date)
      if (run.end_date) this.form.endDate = moment(run.end_date)
      if (run.initial_capital !== undefined && run.initial_capital !== null) {
        this.form.initialCapital = Number(run.initial_capital)
      }
      if (run.commission !== undefined && run.commission !== null) this.form.commission = Number(run.commission)
      if (run.slippage !== undefined && run.slippage !== null) this.form.slippage = Number(run.slippage)
      const leverage = Math.max(1, Number(run.leverage || 1))
      this.form.leverage = leverage
      this.form.leverageEnabled = (options.historyView || this.leverageAllowed) && leverage > 1
      if (!options.historyView) this.applyBacktestRangePolicy()
    },
    async openFactorRun (item) {
      const response = await getStrategyFactorResearchRun(item.id)
      const run = response.data || {}
      this.selectedRun = run
      this.factorResult = run.result || null
      this.factorForm = {
        factorId: run.factor_id || 'momentum_20',
        groups: Number(run.groups_count || 5),
        holdingPeriod: Number(run.holding_period || 5),
        neutralizeIndustry: Boolean(run.neutralize_industry)
      }
      this.form.commission = Number(run.commission || 0)
      this.form.slippage = Number(run.slippage || 0)
      if (run.start_date) this.form.startDate = moment(run.start_date)
      if (run.end_date) this.form.endDate = moment(run.end_date)
      if (run.source_id && Number(this.form.sourceId) !== Number(run.source_id)) {
        this.form.sourceId = Number(run.source_id)
        const detail = await getScriptSourceDetail(run.source_id)
        this.source = detail.data
        const compiled = await compileScriptSource({ sourceId: run.source_id })
        this.manifest = compiled.data && compiled.data.manifest
        this.backtestRangePolicy = compiled.data && compiled.data.backtestRangePolicy
      }
    },
    factorLabel (factorId) {
      return this.$t(`strategyV2.factorResearch.factors.${{
        momentum_20: 'momentum20',
        volatility_20: 'volatility20',
        reversal_5: 'reversal5',
        value: 'value',
        quality: 'quality',
        size: 'size'
      }[factorId] || 'momentum20'}`)
    },
    factorTone (value) {
      const number = Number(value || 0)
      return number > 0 ? 'positive' : number < 0 ? 'negative' : ''
    },
    formatPercent (value, signed = true) {
      const number = Number(value || 0)
      return `${signed && number > 0 ? '+' : ''}${number.toFixed(2)}%`
    },
    formatRate (value) {
      return `${(Number(value || 0) * 100).toFixed(3)}%`
    },
    formatNumber (value, digits = 2) {
      const number = Number(value || 0)
      if (!Number.isFinite(number)) return '∞'
      return number.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
    },
    formatSignedNumber (value, digits = 2) {
      const number = Number(value || 0)
      if (!Number.isFinite(number)) return '-'
      return `${number > 0 ? '+' : ''}${this.formatNumber(number, digits)}`
    },
    profitTone (value) {
      const number = Number(value || 0)
      if (number > 0) return 'positive'
      if (number < 0) return 'negative'
      return 'neutral'
    },
    formatDate (value) {
      return value ? moment(value).format('YYYY-MM-DD HH:mm') : '-'
    },
    historyStatusLabel (item) {
      return this.$t(`strategyV2.backtest.status.${this.historyStatusKey(item)}`)
    },
    historyStatusKey (item) {
      return strategyRunStatus(item)
    },
    historyReturnTone (item) {
      if (strategyRunStatus(item) === 'no_signals' || strategyRunStatus(item) === 'failed') return 'neutral'
      return Number(item.total_return) >= 0 ? 'positive' : 'negative'
    }
  }
}
</script>

<style lang="less" scoped>
.backtest-page { min-height: 100%; padding: 18px 22px 28px; background: #f4f6f8; color: #182230; }
.backtest-page > .decision-context-bar { margin-bottom: 14px; border-radius: 12px; }
.source-selection-alert { margin-bottom: 10px; }
.history-source-link { display: grid; justify-items: start; margin: 8px 0 12px; }
.history-source-snapshot { display: grid; width: 100%; min-width: 0; gap: 5px; padding: 12px; border: 1px solid #dbe4ec; border-radius: 9px; background: #f8fafc; font-size: 12px; overflow-wrap: anywhere; }
.history-source-snapshot small { color: #64748b; line-height: 1.55; }
.theme-dark .history-source-snapshot { border-color: rgba(255, 255, 255, 0.14); background: #171717; }
.theme-dark .history-source-snapshot small { color: rgba(255, 255, 255, 0.56); }
.hero-card, .panel { border: 1px solid #e4e9ef; border-radius: 12px; background: #fff; box-shadow: 0 10px 30px rgba(15, 35, 60, 0.055); }
.hero-card { display: flex; justify-content: space-between; align-items: center; gap: 22px; padding: 18px 22px; margin-bottom: 14px; }
.hero-card h1 { margin: 2px 0 4px; font-size: 24px; color: #17233d; }
.hero-card p, .section-hint, .history-title-row p { margin: 0; color: #718096; line-height: 1.55; }
.eyebrow { color: #52c41a; font-weight: 800; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; }
.hero-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.hero-stat { display: inline-flex; align-items: baseline; gap: 5px; padding: 7px 10px; border-radius: 8px; color: #718096; background: #f7f9fb; font-size: 12px; }
.hero-stat strong { color: #25364f; font-size: 16px; }
.workspace-grid { display: grid; grid-template-columns: minmax(340px, 420px) minmax(620px, 1fr); gap: 14px; align-items: start; }
.panel { padding: 18px; }
.config-panel { position: sticky; top: 74px; display: flex; overflow: hidden; max-height: calc(100vh - 230px); flex-direction: column; padding: 0; }
.config-scroll { min-height: 0; overflow-y: auto; padding: 18px 18px 10px; scrollbar-gutter: stable; }
.run-action-bar { position: relative; z-index: 2; flex: none; padding: 12px 18px 16px; border-top: 1px solid #e8edf2; background: #fff; box-shadow: 0 -10px 24px rgba(15, 35, 60, 0.06); }
.result-panel { min-width: 0; min-height: calc(100vh - 190px); }
.history-identity { display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; margin: 0 0 12px; }
.history-identity__item { display: inline-flex; flex: 1 1 148px; align-items: baseline; min-width: 0; gap: 0; padding: 6px 10px; border: 1px solid #edf0f4; border-radius: 8px; background: #f8fafc; }
.history-identity__label { flex: none; color: #7c8ca1; font-size: 12px; }
.history-identity__value { min-width: 0; color: #20324a; font-size: 13px; overflow-wrap: anywhere; }
.history-stale-note { margin: 0 0 10px; color: #8a5a00; font-size: 12px; line-height: 1.45; }
.panel-heading, .panel-heading > div, .subheading, .history-title-row, .history-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.panel-heading > div { justify-content: flex-start; }
.panel-heading h2, .history-title-row h2 { margin: 0; color: #17233d; font-size: 17px; }
.step-badge { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 7px; color: #397d16; background: #f0f9e8; font-size: 11px; font-weight: 800; }
.cta-phase-alert, .execution-capability-alert, .source-load-alert { margin-top: 12px; }
.execution-capability-alert p { margin: 0 0 9px; line-height: 1.55; }
.execution-capability-rules { display: grid; gap: 7px; }
.execution-capability-rules > div { display: grid; grid-template-columns: 118px minmax(0, 1fr); align-items: start; gap: 6px; line-height: 1.45; }
.execution-capability-rules .ant-tag { width: fit-content; margin-top: 1px; font-weight: 700; }
.runtime-heading { margin-top: 22px; }
.full-width { width: 100%; }
.manifest-card { margin-top: 12px; padding: 13px; border: 1px solid #dfe8f1; border-radius: 9px; background: #f8fbff; }
.manifest-title { display: flex; align-items: center; gap: 7px; margin-bottom: 10px; color: #315c85; font-weight: 700; }
.manifest-grid, .metrics-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.manifest-grid div, .metric-card { display: flex; flex-direction: column; gap: 3px; }
.manifest-grid span, .metric-card span, .assumption-strip span { color: #7c8ca1; font-size: 11px; }
.manifest-grid strong { color: #23344d; overflow-wrap: anywhere; }
.section-hint { margin: 7px 0 10px; font-size: 12px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; }
.range-limit-alert { margin: 0 0 14px; }
.switch-row { display: flex; align-items: center; gap: 8px; min-height: 32px; color: #7c8ca1; font-size: 11px; }
.params-section { margin: 2px 0 14px; padding-top: 12px; border-top: 1px solid #eef2f6; }
.factor-contract-alert { margin: 10px 0 14px; }
.subheading h3 { margin: 0; color: #26364c; font-size: 14px; }
.subheading span { color: #7c8ca1; font-size: 11px; }
.metrics-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.metric-card { padding: 13px; border: 1px solid #edf0f4; border-radius: 9px; background: #f7f9fc; }
.metric-card strong { font-size: 20px; color: #20324a; }
.positive { color: #16a34a !important; }
.negative { color: #dc2626 !important; }
.trade-profit { font-weight: 700; font-variant-numeric: tabular-nums; }
.neutral { color: #94a3b8 !important; }
.run-id { color: #8a97aa; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.result-empty { min-height: calc(100vh - 265px); display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 38px 30px; text-align: center; }
.result-running { min-height: 390px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 30px; text-align: center; color: #718096; }
.result-running h3 { margin: 4px 0 0; color: #17233d; font-size: 18px; }
.result-running p { max-width: 620px; margin: 0; line-height: 1.6; }
.result-running > strong { color: #3f7f1f; font-variant-numeric: tabular-nums; }
.running-icon { display: inline-flex; width: 64px; height: 64px; align-items: center; justify-content: center; border: 1px solid #d9f7be; border-radius: 22px; color: #52c41a; background: #f6ffed; font-size: 27px; }
.running-contract { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 7px; }
.running-contract span { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid #e4e9ef; border-radius: 999px; color: #64748b; font-size: 11px; }
.empty-orbit { display: inline-flex; width: 68px; height: 68px; align-items: center; justify-content: center; border: 1px solid #dce8d5; border-radius: 22px; color: #52c41a; background: #f6ffed; font-size: 28px; transform: rotate(-4deg); }
.result-empty h3 { margin: 17px 0 5px; color: #23344d; font-size: 17px; }
.result-empty p { max-width: 480px; margin: 0; color: #7c8ca1; }
.empty-checks { display: flex; justify-content: center; gap: 9px; flex-wrap: wrap; margin-top: 20px; }
.empty-checks span { display: inline-flex; align-items: center; gap: 5px; padding: 6px 9px; border: 1px solid #e7ebf0; border-radius: 999px; color: #7c8ca1; font-size: 11px; }
.empty-checks span.done { color: #3f7f1f; border-color: #d9f7be; background: #f6ffed; }
.empty-preview-grid { display: grid; width: 100%; max-width: 820px; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 26px; }
.empty-preview-card { display: grid; min-width: 0; grid-template-columns: 28px 1fr; grid-template-rows: auto auto; column-gap: 9px; padding: 13px 14px; border: 1px solid #e7ebf0; border-radius: 10px; background: #fafbfc; text-align: left; }
.empty-preview-card > .anticon { display: inline-flex; grid-row: 1 / 3; align-items: center; justify-content: center; color: #52c41a; font-size: 18px; }
.empty-preview-card span { grid-column: 2; overflow: hidden; color: #7c8ca1; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.empty-preview-card strong { display: -webkit-box; min-width: 0; min-height: 32px; grid-column: 2; overflow: hidden; color: #27364a; font-size: 12px; line-height: 1.35; overflow-wrap: anywhere; word-break: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.result-trustbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; padding: 10px 12px; border: 1px solid; border-radius: 9px; }
.result-trustbar > div:first-child { display: flex; min-width: 0; align-items: center; gap: 8px; }
.result-trustbar strong { white-space: nowrap; }
.result-trustbar span { overflow: hidden; color: #64748b; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.result-trustbar.is-success { border-color: #b7eb8f; background: #f6ffed; color: #3f8600; }
.result-trustbar.is-warning { border-color: #ffe58f; background: #fffbe6; color: #ad6800; }
.result-trustbar.is-error { border-color: #ffccc7; background: #fff2f0; color: #cf1322; }
.trust-badges { display: flex; flex: none; }
.chart-card { margin-top: 14px; padding: 14px; border: 1px solid #edf0f4; border-radius: 9px; }
.subheading > div { min-width: 0; }
.benchmark-caption { display: block; margin-top: 3px; }
.equity-chart { display: block; width: 100%; height: 360px; margin-top: 8px; }
.assumption-strip { display: grid; grid-template-columns: 1fr 1.8fr 0.8fr 0.8fr; gap: 8px; margin-top: 12px; }
.assumption-strip div { min-width: 0; padding: 9px 10px; border-radius: 8px; background: #f8fafc; }
.assumption-strip strong { display: block; overflow: hidden; margin-top: 2px; color: #334155; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.result-tabs { margin-top: 14px; }
.history-header { margin: 16px 0 9px; }
.history-header h3 { margin: 0; color: #26364c; }
.history-title-row { align-items: flex-start; }
.history-title-row p { margin-top: 4px; font-size: 12px; }
.drawer-history-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.drawer-history-header p { margin: 0; color: #718096; font-size: 12px; line-height: 1.55; }
.drawer-run-list { display: flex; flex-direction: column; gap: 9px; }
.run-card { display: flex; min-width: 0; flex-direction: column; gap: 6px; padding: 11px; border: 1px solid #e6eaf0; border-radius: 9px; background: #fafbfc; color: inherit; text-align: left; cursor: pointer; transition: 0.16s ease; }
.run-card:hover, .run-card.active { border-color: #85ce62; background: #fbfff8; transform: translateY(-1px); }
.run-card__top, .run-card__metrics { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.run-card__top strong { min-width: 0; overflow: hidden; color: #2a3b52; text-overflow: ellipsis; white-space: nowrap; }
.run-card__top em { color: #9aa5b5; font-style: normal; font-size: 11px; }
.run-card__meta { overflow: hidden; color: #8491a4; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.run-card__status { width: fit-content; padding: 2px 6px; border-radius: 999px; color: #4d7c0f; background: #ecfccb; font-size: 9px; }
.run-card__status.status-no_signals, .run-card__status.status-open_position_only { color: #a16207; background: #fef3c7; }
.run-card__status.status-failed { color: #b42318; background: #fee4e2; }
.result-error { padding: 32px; }
.run-card__status.status-unknown { color: #64748b; background: #e2e8f0; }
.run-card__metrics b { font-size: 15px; }
.factor-history-metrics b { font-variant-numeric: tabular-nums; }
.run-card__metrics small { color: #8491a4; }
.theme-dark.backtest-page { background: #080808; color: rgba(255, 255, 255, 0.88); }
.theme-dark .hero-card, .theme-dark .panel { border-color: rgba(255, 255, 255, 0.1); background: #111; box-shadow: 0 12px 34px rgba(0, 0, 0, 0.28); }
.theme-dark .run-action-bar { border-color: rgba(255, 255, 255, 0.1); background: #111; box-shadow: 0 -12px 28px rgba(0, 0, 0, 0.34); }
.theme-dark .hero-card h1, .theme-dark .panel-heading h2, .theme-dark .history-title-row h2, .theme-dark .subheading h3, .theme-dark .history-header h3, .theme-dark .result-empty h3, .theme-dark .result-running h3 { color: #f3f4f6; }
.theme-dark .hero-stat, .theme-dark .metric-card, .theme-dark .assumption-strip div, .theme-dark .run-card { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; }
.theme-dark .hero-stat strong, .theme-dark .manifest-grid strong, .theme-dark .metric-card strong, .theme-dark .assumption-strip strong, .theme-dark .run-card__top strong { color: #e5e7eb; }
.theme-dark .manifest-card { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; }
.theme-dark .manifest-title { color: #73d13d; }
.theme-dark .chart-card { border-color: rgba(255, 255, 255, 0.1); }
.theme-dark .result-trustbar.is-success { border-color: #315d22; background: #13200f; color: #73d13d; }
.theme-dark .result-trustbar.is-warning { border-color: #664d03; background: #211b08; color: #ffc53d; }
.theme-dark .result-trustbar.is-error { border-color: #6b2525; background: #251111; color: #ff7875; }
.theme-dark .result-trustbar span { color: rgba(255, 255, 255, 0.52); }
.theme-dark .run-card:hover, .theme-dark .run-card.active { border-color: #52c41a; background: #15230f; }
.theme-dark .empty-preview-card { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; }
.theme-dark .empty-preview-card strong { color: #e5e7eb; }
.theme-dark .history-identity__item { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; }
.theme-dark .history-identity__label { color: rgba(255, 255, 255, 0.52); }
.theme-dark .history-identity__value { color: #e5e7eb; }
.theme-dark .history-stale-note { color: #ffc53d; }
.theme-dark .result-running > strong { color: #73d13d; }
.theme-dark .running-icon { border-color: rgba(82, 196, 26, 0.38); background: #10190c; }
.theme-dark .running-contract span { border-color: rgba(255, 255, 255, 0.1); color: rgba(255, 255, 255, 0.58); background: #0d0d0d; }
.theme-dark /deep/ .ant-form-item-label > label, .theme-dark /deep/ .ant-table { color: rgba(255, 255, 255, 0.72); }
.theme-dark /deep/ .ant-table-thead > tr > th { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; color: rgba(255, 255, 255, 0.68); }
.theme-dark /deep/ .ant-table-tbody > tr > td { border-color: rgba(255, 255, 255, 0.08); background: #111; color: rgba(255, 255, 255, 0.72); }
@media (max-width: 1100px) { .workspace-grid { grid-template-columns: minmax(0, 1fr); } .config-panel, .result-panel { min-width: 0; } .config-panel { position: static; overflow: visible; max-height: none; } .config-scroll { overflow: visible; } .result-panel { min-height: 560px; } .result-empty { min-height: 480px; } }
@media (max-width: 720px) { .backtest-page { padding: 12px; } .hero-card { align-items: flex-start; flex-direction: column; } .hero-actions { justify-content: flex-start; } .form-grid, .manifest-grid, .metrics-grid, .assumption-strip, .empty-preview-grid { grid-template-columns: 1fr; } }
</style>

<style lang="less">
.backtest-history-drawer .ant-drawer-header { border-bottom: 1px solid #e8edf2; }
.backtest-history-drawer .ant-drawer-body { padding: 16px; }
.backtest-history-drawer.theme-dark .ant-drawer-content,
.backtest-history-drawer.theme-dark .ant-drawer-header { background: #111; }
.backtest-history-drawer.theme-dark .ant-drawer-header { border-color: rgba(255, 255, 255, 0.1); }
.backtest-history-drawer.theme-dark .ant-drawer-title,
.backtest-history-drawer.theme-dark .ant-drawer-close { color: rgba(255, 255, 255, 0.88); }
.backtest-history-drawer.theme-dark .drawer-history-header p { color: rgba(255, 255, 255, 0.5); }
.backtest-history-drawer.theme-dark .run-card { border-color: rgba(255, 255, 255, 0.1); background: #0d0d0d; color: rgba(255, 255, 255, 0.72); }
.backtest-history-drawer.theme-dark .run-card:hover,
.backtest-history-drawer.theme-dark .run-card.active { border-color: #52c41a; background: #15230f; }
.backtest-history-drawer.theme-dark .run-card__top strong { color: #e5e7eb; }
</style>
