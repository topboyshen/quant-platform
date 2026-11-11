<template>
  <div class="strategy-ide-shell" :class="{ 'theme-dark': isDarkTheme }">
    <decision-context-bar
      :artifact-label="strategyArtifactLabel"
      :artifact-status="strategyArtifactStatus"
      :artifact-detail="strategyArtifactDetail"
      @next="openBacktestCenter"
    />
    <a-alert
      v-if="decisionContextNotice"
      class="decision-context-notice"
      type="info"
      show-icon
      :message="decisionContextNotice"
    />
    <div class="strategy-ide-layout">
      <section class="script-panel script-panel--editor">
        <strategy-editor
          ref="scriptEditor"
          :key="editorKey"
          v-model="scriptCode"
          :is-dark="isDarkTheme"
          :visible="true"
          :user-id="userId"
          :strategy-id="currentSourceId"
          :saved-code="currentSource && !scriptCodeHidden ? (currentSource.code || '') : ''"
          :script-source-id="currentSourceId"
          :asset-type="currentAssetType"
          :initial-template-key="scriptTemplateKey || editorInitialTemplateKey"
          :initial-param-schema="currentSourceParamSchema"
          :initial-param-values="scriptTemplateParams"
          :hidden-source="scriptCodeHidden"
          :readonly="false"
          :consume-copilot-draft="false"
          :remote-templates-enabled="supportsScriptTemplates"
          :code-verification-enabled="supportsCodeVerification"
          side-mode="split"
          @verified="scriptVerified = true"
          @template-change="handleTemplateChange"
        >
          <template #toolbar>
            <div class="ide-toolbar">
              <div class="toolbar-left">
                <div class="strategy-workspace-switcher">
                  <div class="strategy-workspace-copy">
                    <strong>{{ currentWorkspaceTitle }}</strong>
                    <span>{{ currentWorkspaceDescription }}</span>
                  </div>
                  <a-radio-group
                    :value="currentAssetType"
                    button-style="solid"
                  >
                    <a-radio-button value="script" @click="handleAssetTypeChange('script')">{{ text.ctaStrategy }}</a-radio-button>
                    <a-radio-button value="portfolio_strategy" @click="handleAssetTypeChange('portfolio_strategy')">{{ text.portfolioStrategy }}</a-radio-button>
                  </a-radio-group>
                </div>
                <a-select
                  v-if="supportsScriptSources"
                  class="script-select"
                  show-search
                  allow-clear
                  option-filter-prop="children"
                  :value="selectedScriptId"
                  :loading="loadingScripts"
                  :placeholder="text.selectScriptPlaceholder"
                  @change="handleScriptSelect"
                >
                  <a-select-option
                    v-for="item in scriptOptions"
                    :key="item.id"
                    :value="item.id"
                  >
                    {{ item.optionLabel }}
                  </a-select-option>
                </a-select>

                <a-tooltip :title="currentNewScriptLabel">
                  <a-button class="ide-icon-btn" @click="requestCreateNewDraft({ openTemplate: supportsScriptTemplates })">
                    <a-icon type="plus" />
                  </a-button>
                </a-tooltip>
                <a-tooltip v-if="supportsScriptSources" :title="text.refreshScripts">
                  <a-button class="ide-icon-btn" :loading="loadingScripts" @click="refreshSources">
                    <a-icon type="reload" />
                  </a-button>
                </a-tooltip>
                <a-tooltip v-if="supportsScriptSources && supportsScriptVersions" :title="text.versionHistory">
                  <a-button
                    class="ide-icon-btn"
                    :disabled="!currentSourceId || scriptCodeHidden"
                    :loading="scriptVersionLoading"
                    @click="openVersionDrawer"
                  >
                    <a-icon type="history" />
                  </a-button>
                </a-tooltip>
                <a-tooltip v-if="supportsScriptSources && currentSourceId" :title="text.saveAsNew">
                  <a-button
                    class="ide-icon-btn"
                    :loading="savingScriptMode === 'copy'"
                    :disabled="scriptCodeHidden || savingScript || deletingScript"
                    @click="saveScript(true)"
                  >
                    <a-icon type="copy" />
                  </a-button>
                </a-tooltip>
                <a-tooltip v-if="supportsScriptSources && currentSourceId" :title="text.deleteScript">
                  <a-button
                    class="ide-icon-btn ide-icon-btn--danger"
                    :loading="deletingScript"
                    :disabled="savingScript"
                    @click="deleteCurrentSource"
                  >
                    <a-icon type="delete" />
                  </a-button>
                </a-tooltip>
                <a-button
                  v-if="supportsScriptSources"
                  class="script-save-button"
                  type="primary"
                  :loading="savingScriptMode === 'save'"
                  :disabled="scriptCodeHidden || savingScript || deletingScript || !hasUnsavedScriptChanges"
                  @click="saveScript(false)"
                >
                  {{ text.saveScript }}
                </a-button>
              </div>

              <div class="toolbar-right">
                <a-button
                  v-if="supportsUniverseLibrary && currentAssetType === 'portfolio_strategy'"
                  class="universe-library-button"
                  :class="{ 'universe-library-button--selected': currentAssetType === 'portfolio_strategy' && !!selectedUniverseId }"
                  @click="showUniverseLibrary = true"
                >
                  <a-icon type="cluster" />
                  {{ currentUniverseSummary || text.universeLibrary }}
                </a-button>
                <a-button v-if="supportsFactorLibrary" class="factor-library-button" @click="showFactorLibrary = true">
                  <a-icon type="database" />
                  {{ text.factorLibrary }}
                </a-button>
                <a-button
                  v-if="supportsScriptSources && currentAssetType === 'script'"
                  class="indicator-convert-button"
                  :loading="indicatorConvertIndicatorLoading"
                  @click="openIndicatorConvertPicker"
                >
                  <a-icon type="branches" />
                  {{ text.indicatorConvertEntry }}
                </a-button>
                <a-button
                  v-if="supportsScriptSources && supportsStrategyBacktest && currentAssetType === 'script'"
                  class="script-backtest-button"
                  @click="openBacktestCenter"
                >
                  <a-icon type="bar-chart" />
                  {{ text.backtestTitle }}
                </a-button>
              </div>
            </div>
          </template>
        </strategy-editor>
      </section>
    </div>

    <a-modal
      v-if="supportsScriptSources && showIndicatorConvertModal && isActiveStrategyRoute"
      :visible="showIndicatorConvertModal"
      :title="text.indicatorConvertTitle"
      :confirmLoading="indicatorConvertLoading"
      :ok-text="text.indicatorConvertConfirm"
      :cancel-text="text.cancel"
      :wrap-class-name="isDarkTheme ? 'indicator-convert-modal indicator-convert-modal--dark' : 'indicator-convert-modal'"
      @ok="confirmIndicatorToStrategy"
      @cancel="closeIndicatorConvertModal"
    >
      <div class="indicator-convert-box">
        <div class="indicator-convert-selector">
          <label class="field-label">{{ text.indicatorConvertSelect }}</label>
          <a-select
            v-model="selectedIndicatorConvertId"
            show-search
            option-filter-prop="children"
            style="width: 100%"
            :loading="indicatorConvertIndicatorLoading"
            :placeholder="text.indicatorConvertSelectPlaceholder"
            @change="handleIndicatorConvertSelect"
          >
            <a-select-option
              v-for="item in indicatorConvertIndicators"
              :key="String(item.indicatorId)"
              :value="String(item.indicatorId)"
              :disabled="item.codeHidden || !item.code"
            >
              {{ item.name || text.defaultIndicatorName }}
              <template v-if="item.symbol"> - {{ item.symbol }}</template>
              <template v-if="item.codeHidden || !item.code"> - {{ text.indicatorConvertNoCodeShort }}</template>
            </a-select-option>
          </a-select>
        </div>

        <div v-if="indicatorConvertContext" class="indicator-convert-current">
          <div>
            <span>{{ text.indicatorConvertSource }}</span>
            <strong>{{ indicatorConvertContext.name || text.defaultIndicatorName }}</strong>
          </div>
          <small>
            {{ marketLabel(indicatorConvertContext.market) }}
            <template v-if="indicatorConvertContext.symbol"> / {{ indicatorConvertContext.symbol }}</template>
            <template v-if="indicatorConvertContext.timeframe"> / {{ indicatorConvertContext.timeframe }}</template>
          </small>
        </div>

        <a-alert
          v-else
          type="info"
          show-icon
          :message="text.indicatorConvertSelectFirst"
        />

        <label class="field-label field-label--spaced">{{ text.indicatorConvertInstruction }}</label>
        <a-textarea
          v-model="indicatorConvertInstruction"
          :auto-size="{ minRows: 5, maxRows: 8 }"
          :placeholder="text.indicatorConvertPlaceholder"
        />
        <div class="indicator-convert-note">
          <a-icon type="info-circle" />
          <span>{{ text.indicatorConvertBoundary }}</span>
        </div>
        <a-alert
          v-if="indicatorConvertError"
          type="error"
          show-icon
          :message="indicatorConvertError"
        />
      </div>
    </a-modal>

    <factor-library-modal
      v-if="supportsFactorLibrary"
      :visible="showFactorLibrary"
      :is-dark="isDarkTheme"
      :asset-type="currentAssetType"
      @close="showFactorLibrary = false"
    />

    <universe-library-modal
      v-if="supportsUniverseLibrary"
      :visible="showUniverseLibrary"
      :is-dark="isDarkTheme"
      :asset-type="currentAssetType"
      :selected-universe-id="selectedUniverseId"
      @use="handleUniverseUse"
      @close="showUniverseLibrary = false"
    />

    <a-drawer
      v-if="supportsScriptSources && supportsScriptVersions"
      :visible="showVersionDrawer"
      :title="text.versionHistory"
      :width="640"
      :wrap-class-name="isDarkTheme ? 'script-version-drawer script-version-drawer--dark' : 'script-version-drawer'"
      @close="showVersionDrawer = false"
    >
      <a-spin :spinning="scriptVersionLoading">
        <div class="code-version-toolbar">
          <span>{{ currentSourceName || text.defaultName }}</span>
          <a-button size="small" icon="reload" @click="loadScriptVersions">{{ text.refreshScripts }}</a-button>
        </div>
        <a-empty v-if="!scriptVersions.length" :description="text.versionEmpty" />
        <div v-else class="code-version-list">
          <div v-for="item in scriptVersions" :key="item.id" class="code-version-item">
            <div class="code-version-item__main">
              <strong>{{ text.versionNo.replace('{version}', item.version_no) }}</strong>
              <span>{{ item.name || currentSourceName }}</span>
              <small>{{ formatTime(item.created_at) }}</small>
            </div>
            <div class="code-version-item__actions">
              <a-button
                size="small"
                :disabled="scriptCodeHidden || item.code_hidden || item.hidden_source"
                @click="previewScriptVersion(item)"
              >
                {{ text.versionPreview }}
              </a-button>
              <a-button
                size="small"
                type="primary"
                :loading="restoringScriptVersionId === item.id"
                :disabled="scriptCodeHidden || item.code_hidden || item.hidden_source || item.restore_disabled"
                @click="confirmRestoreScriptVersion(item)"
              >
                {{ text.versionRestore }}
              </a-button>
            </div>
          </div>
        </div>
      </a-spin>

      <div v-if="scriptVersionPreview" class="code-version-preview">
        <div class="code-version-preview__head">
          <strong>{{ text.versionPreviewTitle.replace('{version}', scriptVersionPreview.version_no) }}</strong>
          <a-button size="small" icon="close" @click="scriptVersionPreview = null" />
        </div>
        <div v-if="scriptVersionPreview.code_hidden || scriptVersionPreview.hidden_source" class="code-version-hidden">
          <a-icon type="lock" />
          <strong>{{ text.hiddenScriptTitle }}</strong>
          <span>{{ text.versionHiddenBlocked }}</span>
        </div>
        <pre v-else>{{ scriptVersionPreview.code }}</pre>
      </div>
    </a-drawer>
  </div>
</template>

<script>
import { Modal } from 'ant-design-vue'
import { mapState } from 'vuex'
import StrategyEditor from './components/StrategyEditor.vue'
import FactorLibraryModal from './FactorLibraryModal.vue'
import UniverseLibraryModal from './UniverseLibraryModal.vue'
import DecisionContextBar from '@/components/DecisionContextBar/index.vue'
import { temporaryCodeDigest, INDICATOR_HANDOFF_CONTRACT, readTemporaryAIReference } from '@/utils/researchLinkAI.js'
import {
  newSourceHandoffNonce,
  sourceHandoffIdentity,
  writeSourceHandoff
} from '@/utils/researchLinkRun.js'
import {
  buildResearchArtifactDecisionContext,
  buildSavedSourceDecisionContext,
  clearDecisionContext,
  findDecisionContextSource,
  readDecisionContext,
  writeDecisionContext
} from '@/utils/decisionContext'
import {
  aiGenerateStrategy,
  compileScriptSource,
  createScriptSource,
  deleteScriptSource,
  getIndicatorListForStrategy,
  getScriptSourceDetail,
  getScriptSourceList,
  getScriptSourceVersion,
  getScriptSourceVersions,
  restoreScriptSourceVersion,
  updateScriptSource,
  verifyStrategyCode
} from '@/api/strategy'

const DEFAULT_SCRIPT_CODE = `"""
My Custom Strategy

Describe the strategy logic, supported markets, entry/exit rules, and risk controls here.
"""

def on_init(ctx):
    ctx.period = ctx.param("period", 50)
    ctx.position_pct = ctx.param("position_pct", 0.9)
    ctx.lot_size = ctx.param("lot_size", 100)

def on_bar(ctx, bar):
    bars = ctx.bars(ctx.period + 1)
    price = float(bar.close or 0)
    if len(bars) <= ctx.period or price <= 0:
        return
    closes = [float(item.close) for item in bars]
    previous_average = sum(closes[-ctx.period - 1:-1]) / ctx.period
    current_average = sum(closes[-ctx.period:]) / ctx.period
    crossed_up = closes[-2] <= previous_average and closes[-1] > current_average
    crossed_down = closes[-2] >= previous_average and closes[-1] < current_average
    if crossed_up and not ctx.position:
        ctx.open_long(order_type="market", allocation_pct=ctx.position_pct, lot_size=ctx.lot_size, reason="single_ma_cross_up")
    elif crossed_down and ctx.position > 0:
        ctx.close_long(order_type="market", reason="single_ma_cross_down")
`

const DEFAULT_PORTFOLIO_CODE = `"""
My Portfolio Strategy

Run one momentum sleeve per configured symbol. Combine sleeve results outside this single-symbol runtime.
"""

def on_init(ctx):
    ctx.lookback = ctx.param("lookback", 126)
    ctx.rebalance_bars = ctx.param("rebalance_bars", 5)
    ctx.target_weight = ctx.param("target_weight", 0.5)
    ctx.last_rebalance = -1

def on_bar(ctx, bar):
    if ctx.current_index - ctx.last_rebalance < ctx.rebalance_bars:
        return
    bars = ctx.bars(ctx.lookback + 1)
    price = float(bar.close or 0)
    if len(bars) <= ctx.lookback or price <= 0:
        return
    start = float(bars[0].close or 0)
    ctx.last_rebalance = ctx.current_index
    if start > 0 and price > start and not ctx.position:
        ctx.open_long(order_type="market", allocation_pct=ctx.target_weight, reason="momentum_sleeve_entry")
    elif (start <= 0 or price <= start) and ctx.position > 0:
        ctx.close_long(order_type="market", reason="momentum_sleeve_exit")
`

export default {
  name: 'StrategyIde',
  components: {
    DecisionContextBar,
    StrategyEditor,
    FactorLibraryModal,
    UniverseLibraryModal
  },
  data () {
    return {
      // Personal-safe source persistence, verification, and single-script
      // backtests have complete backend contracts. The remaining upstream
      // catalogs stay fail-closed until their routes and ownership model ship.
      supportsScriptSources: true,
      supportsScriptTemplates: false,
      supportsScriptVersions: false,
      supportsFactorLibrary: false,
      supportsUniverseLibrary: false,
      supportsStrategyBacktest: true,
      supportsCodeVerification: true,
      pageInitializing: true,
      incomingResearchEntryKey: '',
      incomingResearchEntrySeq: 0,
      scriptSources: [],
      loadingScripts: false,
      selectedScriptId: undefined,
      currentSourceId: null,
      currentSource: null,
      currentAssetType: 'script',
      scriptCode: DEFAULT_SCRIPT_CODE,
      scriptCodeHidden: false,
      scriptTemplateKey: '',
      scriptTemplateParams: {},
      scriptParamSchema: {},
      editorInitialTemplateKey: '',
      editorKeySeed: 0,
      scriptVerified: false,
      savingScript: false,
      savingScriptMode: '',
      deletingScript: false,
      backtestHandoffLoading: false,
      showVersionDrawer: false,
      scriptVersionLoading: false,
      scriptVersions: [],
      scriptVersionPreview: null,
      restoringScriptVersionId: null,
      showFactorLibrary: false,
      showUniverseLibrary: false,
      showIndicatorConvertModal: false,
      indicatorConvertLoading: false,
      indicatorConvertIndicatorLoading: false,
      indicatorConvertIndicators: [],
      selectedIndicatorConvertId: undefined,
      indicatorConvertContext: null,
      indicatorConvertInstruction: '',
      indicatorConvertError: '',
      indicatorConvertLoadStatus: null,
      runConfig: {
        market_category: 'Crypto',
        exchange_id: 'binance',
        symbol: 'BTC/USDT',
        timeframe: '1m',
        market_type: 'swap',
        trade_direction: 'long',
        initial_capital: 10000,
        leverage: 5
      },
      lastSavedSnapshot: '',
      decisionContextNotice: ''
    }
  },
  computed: {
    ...mapState({
      navTheme: state => state.app.theme
    }),
    isActiveStrategyRoute () {
      // Vue Router always supplies path in the browser. A missing path is
      // tolerated only for older unit harnesses that instantiate this SFC.
      const path = this.$route && this.$route.path
      return !path || path === '/strategy-ide'
    },
    isDarkTheme () {
      const body = typeof document !== 'undefined' ? document.body : null
      return this.navTheme === 'dark' ||
        this.navTheme === 'realdark' ||
        !!(body && (body.classList.contains('dark') || body.classList.contains('realdark')))
    },
    isZh () {
      return String((this.$i18n && this.$i18n.locale) || '').toLowerCase().startsWith('zh')
    },
    userId () {
      const userInfo = this.$store && this.$store.getters && this.$store.getters.userInfo
      return (userInfo && userInfo.id) || 1
    },
    editorKey () {
      return `script-editor-${this.currentSourceId || 'draft'}-${this.editorKeySeed}`
    },
    currentSourceName () {
      return (this.currentSource && (this.currentSource.name || this.currentSource.strategy_name)) ||
        this.extractScriptMetadataFromCode(this.scriptCode).name ||
        ''
    },
    strategyArtifactLabel () {
      if (this.isIndicatorConvertRoute()) return '指标源码临时交接'
      return this.currentSourceId
        ? `策略源码 #${this.currentSourceId}`
        : '策略草稿（未保存）'
    },
    upstreamAIReference () {
      const query = (this.$route && this.$route.query) || {}
      if (String(query.link || '') !== 'ai' || typeof sessionStorage === 'undefined') return null
      const context = readDecisionContext({ allowStale: true }) || {}
      return readTemporaryAIReference(sessionStorage, {
        market: query.market || context.market,
        symbol: query.symbol || context.symbol
      })
    },
    strategyArtifactStatus () {
      if (this.isIndicatorConvertRoute()) {
        return this.indicatorConvertContext ? 'temporary' : 'unavailable'
      }
      if (String((this.$route && this.$route.query && this.$route.query.link) || '') === 'ai') {
        return this.upstreamAIReference ? 'temporary' : 'unavailable'
      }
      return this.currentSourceId && !this.hasUnsavedScriptChanges ? 'ready' : 'temporary'
    },
    strategyArtifactDetail () {
      if (this.isIndicatorConvertRoute()) {
        return this.indicatorConvertContext
          ? '本次会话的指标代码已核对并待手动转换；不是已保存策略版本。'
          : '指标临时交接缺失、变化或不可读取；不会借用当前已保存源码。'
      }
      if (String((this.$route && this.$route.query && this.$route.query.link) || '') === 'ai') {
        const ref = this.upstreamAIReference
        return ref
          ? `上游 AI 临时报告 ${ref.localRef}，生成于 ${ref.generatedAt || '未知'}；不是可回溯的持久消息版本。`
          : '上游 AI 临时报告引用已失效或不可读取；不能宣称可回溯到历史消息。'
      }
      if (this.currentSourceId && !this.hasUnsavedScriptChanges) {
        return '当前已保存源码；进入回测前仍会核对当次源码与契约身份。'
      }
      return '当前编辑内容尚未作为可回测版本保存；导航不会自动运行回测。'
    },
    currentSourceParamSchema () {
      if (this.scriptParamSchema && Array.isArray(this.scriptParamSchema.params) && this.scriptParamSchema.params.length) {
        return this.scriptParamSchema
      }
      const source = this.currentSource || {}
      const direct = this.parseObject(source.param_schema)
      if (Array.isArray(direct.params) && direct.params.length) return direct
      if (Array.isArray(source.params) && source.params.length) return { params: source.params }
      const metadata = this.parseObject(source.metadata)
      const metaSchema = this.parseObject(metadata.param_schema)
      if (Array.isArray(metaSchema.params) && metaSchema.params.length) return metaSchema
      return direct
    },
    allScriptOptions () {
      return (this.scriptSources || []).map(item => {
        const id = this.getScriptSourceId(item)
        return {
          ...item,
          id,
          optionLabel: item.name || item.strategy_name || `Script #${id}`
        }
      }).filter(item => item.id)
    },
    scriptOptions () {
      return this.allScriptOptions.filter(item => item.asset_type === this.currentAssetType)
    },
    selectedUniverseId () {
      return Number(this.runConfig && (this.runConfig.universe_id || this.runConfig.universeId)) || undefined
    },
    currentUniverseSummary () {
      if (this.currentAssetType !== 'portfolio_strategy' || !this.selectedUniverseId) return ''
      const label = String((this.runConfig && (this.runConfig.universe_name || this.runConfig.universeName || this.runConfig.universe_code || this.runConfig.universeCode)) || this.selectedUniverseId)
      return this.text.universeSelected.replace('{name}', label)
    },
    currentWorkspaceTitle () {
      return this.currentAssetType === 'portfolio_strategy' ? this.text.portfolioWorkspaceTitle : this.text.ctaWorkspaceTitle
    },
    currentWorkspaceDescription () {
      return this.currentAssetType === 'portfolio_strategy' ? this.text.portfolioWorkspaceDescription : this.text.ctaWorkspaceDescription
    },
    currentNewScriptLabel () {
      return this.currentAssetType === 'portfolio_strategy' ? this.text.newPortfolioStrategy : this.text.newCtaStrategy
    },
    hasUnsavedScriptChanges () {
      if (!this.currentSourceId) return !this.scriptCodeHidden && !!String(this.scriptCode || '').trim()
      return this.lastSavedSnapshot !== this.scriptSnapshot()
    },
    text () {
      return [
        'selectScriptLabel',
        'selectScriptPlaceholder',
        'ctaStrategy',
        'portfolioStrategy',
        'newScript',
        'refreshScripts',
        'saveScript',
        'saveAsNew',
        'publishScript',
        'deleteScript',
        'createLive',
        'backtestTitle',
        'cancel',
        'defaultName',
        'autoNameSuffix',
        'codeRequired',
        'hiddenScriptTitle',
        'hiddenScriptDesc',
        'loadScriptsFailed',
        'loadScriptFailed',
        'saveSuccess',
        'saveFailed',
        'deleteSuccess',
        'deleteFailed',
        'deleteConfirmTitle',
        'deleteConfirmDesc',
        'runningEditBlocked',
        'verifyPassed',
        'verifyBlocked',
        'verifyFailed',
        'publishSuccess',
        'publishFailed',
        'publishBacktestRequired',
        'publishModalTitle',
        'publishConfirm',
        'marketTag',
        'publishHint',
        'publishName',
        'publishNamePlaceholder',
        'publishPricingType',
        'publishFree',
        'publishPaid',
        'publishPrice',
        'publishDescription',
        'publishDescriptionPlaceholder',
        'priceRequired',
        'publishVipFree',
        'publishVipFreeHint',
        'publishHideCode',
        'publishHideCodeHint',
        'versionHistory',
        'versionEmpty',
        'versionNo',
        'versionPreview',
        'versionRestore',
        'versionRestoreTitle',
        'versionRestoreContent',
        'versionPreviewTitle',
        'versionRestored',
        'versionHiddenBlocked',
        'versionLoadFailed',
        'versionRestoreFailed',
        'indicatorConvertEntry',
        'indicatorConvertTitle',
        'indicatorConvertConfirm',
        'indicatorConvertSelect',
        'indicatorConvertSelectPlaceholder',
        'indicatorConvertSelectFirst',
        'indicatorConvertSource',
        'indicatorConvertInstruction',
        'indicatorConvertPlaceholder',
        'indicatorConvertBoundary',
        'indicatorConvertNoCodeShort',
        'indicatorConvertNoCode',
        'indicatorConvertHiddenBlocked',
        'indicatorConvertFailed',
        'indicatorConvertSuccess',
        'defaultIndicatorName',
        'noChangesToSave',
        'factorLibrary',
        'universeLibrary',
        'universeSelected',
        'workspaceLabel',
        'ctaWorkspaceTitle',
        'ctaWorkspaceDescription',
        'portfolioWorkspaceTitle',
        'portfolioWorkspaceDescription',
        'newCtaStrategy',
        'newPortfolioStrategy',
        'switchWorkspaceTitle',
        'switchWorkspaceContent',
        'universeApplied',
        'robotTemplates',
        'robotTemplatesDesc',
        'robotGenerated'
      ].reduce((acc, key) => {
        acc[key] = this.$t(`strategyIde.${key}`)
        return acc
      }, {})
    }
  },
  watch: {
    scriptCode (value) {
      if (
        this.currentSource &&
        !this.scriptCodeHidden &&
        String(value || '') === String(this.currentSource.code || '')
      ) return
      this.scriptVerified = false
    },
    '$route.query': {
      deep: true,
      handler () {
        if (!this.pageInitializing) this.syncIncomingResearchEntry()
      }
    }
  },
  async mounted () {
    this._beforeUnload = (event) => this.handleBeforeUnload(event)
    this.bindSaveShortcut()
    window.addEventListener('beforeunload', this._beforeUnload)
    try {
      await this.initPage()
    } finally {
      this.pageInitializing = false
    }
  },
  activated () {
    this.bindSaveShortcut()
    if (!this.pageInitializing) this.syncIncomingResearchEntry({ refreshSources: true })
  },
  deactivated () {
    this.unbindSaveShortcut()
    this.invalidateIndicatorConvertView()
  },
  async beforeRouteLeave (to, from, next) {
    const leaveAfterPortalUnmount = async () => {
      this.invalidateIndicatorConvertView()
      await this.$nextTick()
      next()
    }
    if (!this.hasPendingStrategyWork()) {
      await leaveAfterPortalUnmount()
      return
    }
    Modal.confirm({
      title: this.text.switchWorkspaceTitle,
      content: this.text.switchWorkspaceContent,
      okText: this.text.switchWorkspaceTitle,
      cancelText: this.text.cancel,
      onOk: () => leaveAfterPortalUnmount(),
      onCancel: () => next(false)
    })
  },
  beforeDestroy () {
    this.unbindSaveShortcut()
    this.invalidateIndicatorConvertView()
    if (this._beforeUnload) {
      window.removeEventListener('beforeunload', this._beforeUnload)
      this._beforeUnload = null
    }
  },
  methods: {
    invalidateIndicatorConvertView () {
      // The conversion modal is rendered in a portal, outside the cached
      // workspace DOM. Leaving the workspace must close it and invalidate
      // any pending source read before it can reopen the old handoff.
      this.showIndicatorConvertModal = false
      this.incomingResearchEntrySeq += 1
      this.incomingResearchEntryKey = ''
    },
    bindSaveShortcut () {
      if (this._saveShortcut) return
      this._saveShortcut = (event) => this.handleSaveShortcut(event)
      window.addEventListener('keydown', this._saveShortcut, true)
    },
    unbindSaveShortcut () {
      if (!this._saveShortcut) return
      window.removeEventListener('keydown', this._saveShortcut, true)
      this._saveShortcut = null
    },
    applyDecisionContext (contextValue = null) {
      const context = contextValue || readDecisionContext({ allowStale: true })
      if (!context || !context.market || !context.symbol) return
      this.runConfig = {
        ...this.runConfig,
        market_category: context.market,
        exchange_id: context.market === 'Crypto' ? (this.runConfig.exchange_id || 'binance') : '',
        symbol: context.symbol,
        market_type: context.market === 'Crypto' ? (this.runConfig.market_type || 'spot') : 'spot',
        trade_direction: 'long',
        leverage: context.market === 'Crypto' ? Number(this.runConfig.leverage || 1) : 1
      }
      return true
    },
    getIncomingResearchEntryKey (contextValue = null) {
      const query = (this.$route && this.$route.query) || {}
      const context = contextValue || readDecisionContext({ allowStale: true }) || {}
      let storedCode = ''
      let storedMeta = ''
      if (typeof sessionStorage !== 'undefined') {
        try {
          storedCode = String(sessionStorage.getItem('qd_strategy_source') || '')
          storedMeta = String(sessionStorage.getItem('qd_copilot_script_strategy_meta') || '')
        } catch (_) {}
      }
      return JSON.stringify({
        route: {
          handoff: String(query.handoff || ''),
          sourceId: String(query.sourceId || query.strategy_id || ''),
          draft: String(query.draft || ''),
          aiDraft: String(query.aiDraft || ''),
          convert: String(query.convert || ''),
          convertKey: String(query.convert_key || ''),
          indicatorId: String(query.source_indicator_id || query.indicator_id || query.indicatorId || ''),
          market: String(query.market || ''),
          symbol: String(query.symbol || '')
        },
        storedCode,
        storedMeta,
        context: {
          market: String(context.market || ''),
          symbol: String(context.symbol || ''),
          updatedAt: String(context.updatedAt || '')
        }
      })
    },
    incomingStrategyOverwriteBlocked (context = null, options = {}) {
      if (options.initial || !this.hasPendingStrategyWork()) return false
      const target = context && context.market && context.symbol
        ? `${context.market}:${context.symbol}`
        : (this.isZh ? '新的研究入口' : 'the incoming research entry')
      this.decisionContextNotice = this.isZh
        ? `当前编辑内容尚未保存，未自动切换到 ${target}；请先保存或放弃当前改动。`
        : `Unsaved edits were kept. Save or discard them before switching to ${target}.`
      return true
    },
    async syncIncomingResearchEntry (options = {}) {
      const sequence = ++this.incomingResearchEntrySeq
      if (!this.isActiveStrategyRoute) return false
      if (this.supportsScriptSources && !options.sourcesReady) {
        await this.loadSources()
      }
      if (sequence !== this.incomingResearchEntrySeq || !this.isActiveStrategyRoute) return false

      const context = readDecisionContext({ allowStale: true })
      const key = this.getIncomingResearchEntryKey(context)

      // A conversion marker is authoritative over any incidental sourceId in
      // the URL: a missing temporary payload must never open a saved source.
      if (this.isIndicatorConvertRoute()) {
        const applied = await this.applyIndicatorConvertRouteOnce({ sequence })
        if (sequence !== this.incomingResearchEntrySeq || !this.isActiveStrategyRoute) return false
        if (applied) {
          this.applyDecisionContext(this.indicatorConvertContext)
          this.incomingResearchEntryKey = key
        }
        return applied
      }

      if (!options.force && !options.refreshSources && key === this.incomingResearchEntryKey) return true

      const routeId = this.getInitialRouteSourceId()
      if (routeId) {
        const source = this.allScriptOptions.find(item => String(item.id) === String(routeId))
        if (!source) {
          if (this.incomingStrategyOverwriteBlocked(context, options)) return false
          return this.failClosedRouteSource(routeId, key)
        }
        if (String(this.currentSourceId || '') !== String(routeId)) {
          if (this.incomingStrategyOverwriteBlocked(context, options)) return false
          const opened = await this.openSource(routeId, { updateRoute: false })
          if (!this.isActiveStrategyRoute) return false
          if (!opened) return this.failClosedRouteSource(routeId, key)
        }
        const synchronizedContext = this.syncDecisionContextFromSource(this.currentSource || source)
        if (!synchronizedContext) {
          this.incomingResearchEntryKey = key
          return true
        }
        this.decisionContextNotice = ''
        this.incomingResearchEntryKey = this.getIncomingResearchEntryKey(
          readDecisionContext({ allowStale: true })
        )
        return true
      }

      if (this.isDraftRoute() && this.hasCopilotScriptDraft()) {
        const artifactContext = this.copilotScriptDraftDecisionContext()
        if (!artifactContext) {
          this.decisionContextNotice = this.isZh
            ? 'AI 策略产物缺少有效市场或标的，已停止打开。'
            : 'The AI strategy artifact has no valid market or symbol and was not opened.'
          return false
        }
        if (this.incomingStrategyOverwriteBlocked(artifactContext, options)) return false
        this.createNewDraft({ openTemplate: false, updateRoute: false, assetType: 'script' })
        const applied = this.applyCopilotScriptDraft()
        if (applied) {
          const appliedContext = readDecisionContext({ allowStale: true })
          this.applyDecisionContext(appliedContext)
          this.decisionContextNotice = ''
          this.incomingResearchEntryKey = this.getIncomingResearchEntryKey(appliedContext)
        }
        return applied
      }

      if (this.isDraftRoute()) {
        if (this.incomingStrategyOverwriteBlocked(context, options)) return false
        this.createNewDraft({
          openTemplate: this.shouldOpenTemplateFromRoute(),
          updateRoute: false,
          assetType: this.getRouteAssetType()
        })
        this.applyDecisionContext(context)
        this.decisionContextNotice = ''
        this.incomingResearchEntryKey = key
        return true
      }

      const synced = await this.syncDecisionContextSource(context, options)
      if (!this.isActiveStrategyRoute) return false
      if (synced) this.incomingResearchEntryKey = key
      return synced
    },
    async syncDecisionContextSource (providedContext = null, options = {}) {
      const context = providedContext || readDecisionContext({ allowStale: true })
      if (!context || !context.market || !context.symbol) return false

      const currentMatch = findDecisionContextSource(this.currentSource ? [this.currentSource] : [], context)
      if (currentMatch) {
        this.applyDecisionContext(context)
        this.decisionContextNotice = ''
        return true
      }

      if (!options.initial && this.hasPendingStrategyWork()) {
        this.decisionContextNotice = this.isZh
          ? `当前编辑内容尚未保存，未自动切换到 ${context.market}:${context.symbol}；请先保存或放弃当前改动。`
          : `Unsaved edits were kept. Save or discard them before switching to ${context.market}:${context.symbol}.`
        return false
      }

      const source = findDecisionContextSource(this.scriptOptions, context)
      if (source) {
        const opened = await this.openSource(source.id, { updateRoute: false })
        if (!opened) return false
        this.applyDecisionContext(context)
        this.decisionContextNotice = ''
        return true
      }

      this.createNewDraft({ openTemplate: false, updateRoute: false, assetType: 'script' })
      this.applyDecisionContext(context)
      this.decisionContextNotice = this.isZh
        ? `当前研究标的 ${context.market}:${context.symbol} 暂无匹配的已保存策略，已建立新策略草稿；保存后再进入回测。`
        : `No saved strategy matches ${context.market}:${context.symbol}. A new draft is ready; save it before backtesting.`
      return true
    },
    failClosedRouteSource (routeId, entryKey = '') {
      this.createNewDraft({
        openTemplate: false,
        updateRoute: false,
        assetType: this.getRouteAssetType()
      })
      this.decisionContextNotice = this.isZh
        ? `未能加载入口指定的策略源码 #${routeId}，已停止自动选择。请刷新列表后重试，或手动选择策略。`
        : `The requested strategy source #${routeId} could not be loaded. Automatic selection was stopped; refresh or choose a strategy manually.`
      this.incomingResearchEntryKey = entryKey
      return true
    },
    hasMeaningfulUnsavedScriptChanges () {
      if (this.currentSourceId) return this.hasUnsavedScriptChanges
      if (this.scriptCodeHidden) return false
      const defaultCode = this.currentAssetType === 'portfolio_strategy' ? DEFAULT_PORTFOLIO_CODE : DEFAULT_SCRIPT_CODE
      return String(this.scriptCode || '').trim() !== String(defaultCode).trim()
    },
    hasPendingStrategyWork () {
      const editor = this.$refs.scriptEditor
      const editorHasPendingChanges = !!(
        editor &&
        typeof editor.hasPendingChanges === 'function' &&
        editor.hasPendingChanges()
      )
      return this.hasMeaningfulUnsavedScriptChanges() || editorHasPendingChanges
    },
    runAfterDiscardConfirmation (action) {
      if (typeof action !== 'function') return
      if (!this.hasPendingStrategyWork()) {
        action()
        return
      }
      this.$confirm({
        title: this.text.switchWorkspaceTitle,
        content: this.text.switchWorkspaceContent,
        okText: this.text.switchWorkspaceTitle,
        cancelText: this.text.cancel,
        onOk: action
      })
    },
    handleBeforeUnload (event) {
      if (!event || !this.hasPendingStrategyWork()) return undefined
      event.preventDefault()
      event.returnValue = ''
      return ''
    },
    requestCreateNewDraft (options = {}) {
      this.runAfterDiscardConfirmation(() => this.createNewDraft(options))
    },
    async initPage () {
      if (this.supportsScriptSources) await this.loadSources()
      if (!this.supportsScriptSources && this.isIndicatorConvertRoute()) {
        this.clearIndicatorConvertRoute()
        return
      }
      if (await this.syncIncomingResearchEntry({ initial: true, sourcesReady: true, force: true })) return
      if (this.isIndicatorConvertRoute()) return
      const firstId = this.scriptOptions.length ? this.scriptOptions[0].id : ''
      if (firstId) {
        await this.openSource(firstId, { updateRoute: true })
      } else {
        this.createNewDraft({ openTemplate: false })
      }
    },
    async loadSources () {
      if (!this.supportsScriptSources) {
        this.scriptSources = []
        this.loadingScripts = false
        return
      }
      this.loadingScripts = true
      try {
        const res = await getScriptSourceList()
        this.scriptSources = this.extractSources(res)
      } catch (e) {
        this.$message.warning(this.text.loadScriptsFailed)
      } finally {
        this.loadingScripts = false
      }
    },
    refreshSources () {
      if (!this.supportsScriptSources) return
      this.runAfterDiscardConfirmation(() => this.commitRefreshSources())
    },
    async commitRefreshSources () {
      await this.loadSources()
      if (this.currentSourceId) {
        await this.openSource(this.currentSourceId, { updateRoute: false })
      } else if (this.hasRouteSourceId()) {
        await this.syncIncomingResearchEntry({ sourcesReady: true, force: true })
      }
    },
    extractSources (res) {
      const data = res && res.data
      if (Array.isArray(data)) return data
      if (data && Array.isArray(data.items)) return data.items
      return []
    },
    getInitialRouteSourceId () {
      const query = this.$route.query || {}
      const value = query.sourceId || query.strategy_id
      return value ? String(value).trim() : ''
    },
    getRouteAssetType () {
      const query = this.$route.query || {}
      return String(query.assetType || '').trim() === 'portfolio_strategy' ? 'portfolio_strategy' : 'script'
    },
    isDraftRoute () {
      const query = this.$route.query || {}
      return String(query.draft || '') === '1' || this.isLegacyAiDraftRoute()
    },
    isLegacyAiDraftRoute () {
      const query = this.$route.query || {}
      return String(query.aiDraft || '') === '1'
    },
    isIndicatorConvertRoute () {
      const query = this.$route.query || {}
      return this.isStrictIndicatorConvertRoute() ||
        String(query.convert || '').toLowerCase() === 'indicator' ||
        !!query.convert_key ||
        !!query.source_indicator_id ||
        !!query.indicator_id
    },
    isStrictIndicatorConvertRoute () {
      const query = (this.$route && this.$route.query) || {}
      return String(query.convert_required || '') === '1' || String(query.link || '') === 'indicator'
    },
    hasRouteSourceId () {
      return !!this.getInitialRouteSourceId()
    },
    hasCopilotScriptDraft () {
      if (typeof sessionStorage === 'undefined') return false
      try {
        return !!String(sessionStorage.getItem('qd_strategy_source') || '').trim()
      } catch (_) {
        return false
      }
    },
    copilotScriptDraftDecisionContext () {
      if (typeof sessionStorage === 'undefined') return null
      const query = (this.$route && this.$route.query) || {}
      let meta = null
      try {
        const rawMeta = String(sessionStorage.getItem('qd_copilot_script_strategy_meta') || '')
        meta = rawMeta ? JSON.parse(rawMeta) : null
      } catch (_) {
        meta = null
      }
      const current = readDecisionContext({ allowStale: true }) || {}
      const candidates = [
        meta,
        { market: query.market || '', symbol: query.symbol || '', name: query.name || '' }
      ]
      for (const candidate of candidates) {
        const next = buildResearchArtifactDecisionContext(candidate || {}, current, {
          source: 'QuantDinger AI 策略产物'
        })
        if (next) return next
      }
      return null
    },
    applyCopilotScriptDraft () {
      if (typeof sessionStorage === 'undefined') return false
      let code = ''
      try {
        code = String(sessionStorage.getItem('qd_strategy_source') || '').trim()
      } catch (_) {
        code = ''
      }
      if (!code) return false
      const artifactContext = this.copilotScriptDraftDecisionContext()
      const writtenContext = artifactContext ? writeDecisionContext(artifactContext) : null
      if (!writtenContext) return false
      try {
        sessionStorage.removeItem('qd_strategy_source')
        sessionStorage.removeItem('qd_copilot_script_strategy_meta')
      } catch (_) {}
      this.currentSource = null
      this.currentSourceId = null
      this.selectedScriptId = undefined
      this.scriptCodeHidden = false
      this.scriptCode = code
      this.scriptTemplateKey = ''
      this.scriptTemplateParams = {}
      this.scriptParamSchema = {}
      this.editorInitialTemplateKey = ''
      this.scriptVerified = false
      this.lastSavedSnapshot = ''
      this.runConfig = {
        ...this.runConfig,
        market_category: writtenContext.market,
        symbol: writtenContext.symbol
      }
      return true
    },
    shouldOpenTemplateFromRoute () {
      if (!this.supportsScriptTemplates) return false
      const query = this.$route.query || {}
      return String(query.template_picker || '') === '1'
    },
    getScriptSourceId (item) {
      if (!item) return ''
      return String(item.id || '').trim()
    },
    handleScriptSelect (id) {
      if (!this.supportsScriptSources) return
      const sourceId = String(id || '').trim()
      if (sourceId && sourceId === String(this.currentSourceId || '')) return
      this.runAfterDiscardConfirmation(() => this.commitScriptSelect(sourceId))
    },
    async commitScriptSelect (id) {
      if (!id) {
        this.createNewDraft({ openTemplate: false })
        return
      }
      await this.openSource(id, { updateRoute: true })
    },
    async openSource (id, options = {}) {
      if (!this.supportsScriptSources) return false
      const sourceId = String(id || '').trim()
      if (!sourceId) return false
      try {
        const res = await getScriptSourceDetail(sourceId)
        const source = (res && res.data) || res
        if (!source || !this.getScriptSourceId(source)) {
          throw new Error(this.text.loadScriptFailed)
        }
        this.applySource(source)
        this.syncDecisionContextFromSource(source)
        // Loading a saved source may normalize an incomplete legacy run
        // config (for example, a CNStock source whose metadata omitted the
        // market). Treat that normalized state as the loaded baseline so a
        // read-only context handoff cannot enable Save or rewrite the source
        // when the user continues to backtesting.
        this.lastSavedSnapshot = this.scriptSnapshot()
        if (options.updateRoute !== false) {
          this.writeRouteSource(this.currentSourceId)
        }
        return true
      } catch (e) {
        this.$message.error(e.backendMessage || e.message || this.text.loadScriptFailed)
        return false
      }
    },
    syncDecisionContextFromSource (source = {}) {
      const current = readDecisionContext({ allowStale: true })
      const next = buildSavedSourceDecisionContext(source, current || {})
      if (!next) {
        clearDecisionContext()
        this.decisionContextNotice = this.isZh
          ? '当前策略缺少明确的市场或标的，已清除旧研究上下文并阻止带着错误标的进入回测。'
          : 'This strategy has no explicit market or symbol. The previous research context was cleared.'
        return null
      }
      const written = writeDecisionContext(next)
      if (!written) {
        clearDecisionContext()
        return null
      }
      this.applyDecisionContext(written)
      this.decisionContextNotice = ''
      return written
    },
    applySource (source) {
      const metadata = this.parseObject(source.metadata)
      const runConfig = this.parseObject(metadata.last_run_config)
      this.currentSource = source
      this.currentAssetType = source.asset_type === 'portfolio_strategy' ? 'portfolio_strategy' : 'script'
      this.currentSourceId = this.getScriptSourceId(source)
      this.selectedScriptId = this.currentSourceId ? String(this.currentSourceId) : undefined
      this.scriptCodeHidden = !!(source.code_hidden || metadata.code_hidden)
      this.scriptCode = this.scriptCodeHidden
        ? ''
        : String(source.code || (this.currentAssetType === 'portfolio_strategy' ? DEFAULT_PORTFOLIO_CODE : DEFAULT_SCRIPT_CODE))
      this.scriptTemplateKey = source.template_key || runConfig.script_template_key || ''
      this.scriptTemplateParams = {
        ...this.parseObject(metadata.script_template_params),
        ...this.parseObject(runConfig.script_template_params)
      }
      this.scriptParamSchema = this.parseObject(source.param_schema)
      this.runConfig = {
        ...this.runConfig,
        ...runConfig
      }
      const universeReference = this.extractUniverseReferenceFromCode(this.scriptCode)
      if (universeReference.id && !this.runConfig.universe_id) {
        this.runConfig = {
          ...this.runConfig,
          universe_id: universeReference.id,
          universe_code: universeReference.code
        }
      }
      this.editorInitialTemplateKey = ''
      this.editorKeySeed += 1
      this.scriptVerified = !!(metadata.lifecycle_verified || metadata.script_verified)
      this.lastSavedSnapshot = this.scriptSnapshot()
    },
    createNewDraft ({ openTemplate = false, updateRoute = true, assetType = this.currentAssetType } = {}) {
      const shouldOpenTemplate = Boolean(openTemplate && this.supportsScriptTemplates)
      this.currentSource = null
      this.currentSourceId = null
      this.selectedScriptId = undefined
      this.currentAssetType = assetType === 'portfolio_strategy' ? 'portfolio_strategy' : 'script'
      this.scriptCode = this.currentAssetType === 'portfolio_strategy' ? DEFAULT_PORTFOLIO_CODE : DEFAULT_SCRIPT_CODE
      this.scriptCodeHidden = false
      this.scriptTemplateKey = ''
      this.scriptTemplateParams = {}
      this.scriptParamSchema = {}
      this.editorInitialTemplateKey = ''
      this.scriptVerified = false
      this.runConfig = {
        ...this.runConfig,
        universe_id: undefined,
        universe_code: '',
        universe_name: ''
      }
      this.lastSavedSnapshot = ''
      this.editorKeySeed += 1
      if (updateRoute) this.writeDraftRoute({ openTemplate: shouldOpenTemplate })
      if (shouldOpenTemplate) {
        this.$nextTick(() => {
          const editor = this.$refs.scriptEditor
          if (editor && typeof editor.openTemplatePicker === 'function') {
            editor.openTemplatePicker()
          }
        })
      }
    },
    handleAssetTypeChange (assetType) {
      const target = assetType === 'portfolio_strategy' ? 'portfolio_strategy' : 'script'
      if (target === this.currentAssetType) return
      const switchWorkspace = async () => {
        this.currentAssetType = target
        const first = this.supportsScriptSources
          ? this.allScriptOptions.find(item => item.asset_type === target)
          : null
        if (first) await this.openSource(first.id, { updateRoute: true })
        else this.createNewDraft({ openTemplate: false, updateRoute: true, assetType: target })
      }
      this.runAfterDiscardConfirmation(switchWorkspace)
    },
    extractUniverseReferenceFromCode (code) {
      const source = String(code || '')
      const poolMatch = source.match(/context\.set_universe\(pool=["']([^"']+)["']\)/i)
      if (poolMatch) return { id: undefined, code: String(poolMatch[1] || '').trim() }
      const indexMatch = source.match(/context\.set_universe\(index=["']INDEX:([^"']+)["']\)/i)
      return indexMatch ? { id: undefined, code: String(indexMatch[1] || '').trim() } : { id: undefined, code: '' }
    },
    applyUniverseReferenceToCode (item) {
      if (this.scriptCodeHidden) return
      const pool = String(item.code || '').trim().toLowerCase()
      const line = `context.set_universe(pool="${pool}")`
      const pattern = /context\.set_universe\((?:pool=["'][^"']+["']|index=["']INDEX:[^"']+["'])\)/i
      const code = String(this.scriptCode || '')
      if (pattern.test(code)) {
        this.scriptCode = code.replace(pattern, line)
        return
      }
      const initPattern = /^(def\s+initialize\s*\([^)]*\):\s*)$/m
      this.scriptCode = initPattern.test(code) ? code.replace(initPattern, `$1\n    ${line}`) : `${line}\n${code}`
    },
    handleUniverseUse (item) {
      if (!this.supportsUniverseLibrary) return
      if (!item || this.currentAssetType !== 'portfolio_strategy') return
      this.runConfig = {
        ...this.runConfig,
        universe_id: Number(item.id),
        universe_code: item.code || '',
        universe_name: item.name || item.code || ''
      }
      this.applyUniverseReferenceToCode(item)
      this.showUniverseLibrary = false
      this.$message.success(this.text.universeApplied.replace('{name}', item.name || item.code || ''))
    },
    writeRouteSource (sourceId) {
      if (!this.supportsScriptSources) return
      if (!sourceId) return
      const query = { tab: 'script', sourceId: String(sourceId), assetType: this.currentAssetType }
      this.replaceRouteQuery(query)
    },
    writeDraftRoute ({ openTemplate = false } = {}) {
      const query = { tab: 'script', draft: '1', assetType: this.currentAssetType }
      if (openTemplate && this.supportsScriptTemplates) query.template_picker = '1'
      else delete query.template_picker
      this.replaceRouteQuery(query)
    },
    replaceRouteQuery (query) {
      const clean = {}
      Object.keys(query || {}).forEach(key => {
        const value = query[key]
        if (value !== undefined && value !== null && String(value) !== '') {
          clean[key] = value
        }
      })
      const current = JSON.stringify(this.$route.query || {})
      const next = JSON.stringify(clean)
      if (current !== next) {
        this.$router.replace({ path: '/strategy-ide', query: clean }).catch(() => {})
      }
    },
    parseObject (value) {
      if (!value) return {}
      if (typeof value === 'object' && !Array.isArray(value)) return { ...value }
      if (typeof value === 'string') {
        try {
          const parsed = JSON.parse(value)
          return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
        } catch (_) {
          return {}
        }
      }
      return {}
    },
    handleTemplateChange (payload) {
      this.scriptTemplateKey = (payload && payload.key) || ''
      this.scriptTemplateParams = payload && payload.params && typeof payload.params === 'object'
        ? { ...payload.params }
        : {}
      this.scriptParamSchema = payload && payload.param_schema && typeof payload.param_schema === 'object'
        ? { ...payload.param_schema }
        : {}
      if (this.scriptCodeHidden) {
        this.runConfig = {
          ...this.runConfig,
          script_template_key: this.scriptTemplateKey,
          script_template_params: { ...this.scriptTemplateParams }
        }
      }
      this.scriptVerified = false
    },
    handleSaveShortcut (event) {
      if (!event || !(event.ctrlKey || event.metaKey) || String(event.key || '').toLowerCase() !== 's') return
      if (!this.$route || this.$route.path !== '/strategy-ide') return
      event.preventDefault()
      if (!this.supportsScriptSources) return
      this.saveScript(false)
    },
    extractScriptMetadataFromCode (code) {
      const source = String(code || '')
      const doc = source.match(/^\s*(?:[rubfRUBF]*)("""|''')([\s\S]*?)\1/)
      if (doc) {
        const lines = String(doc[2] || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean)
        if (lines.length) {
          return {
            name: lines[0],
            description: lines.slice(1).join('\n')
          }
        }
      }
      const nameMatch = source.match(/^\s*(?:my_strategy_name|strategy_name)\s*=\s*(['"])(.*?)\1\s*$/m)
      const descMatch = source.match(/^\s*(?:my_strategy_description|strategy_description)\s*=\s*(['"])(.*?)\1\s*$/m)
      return {
        name: nameMatch ? nameMatch[2] : '',
        description: descMatch ? descMatch[2] : ''
      }
    },
    deriveScriptName () {
      const meta = this.extractScriptMetadataFromCode(this.scriptCode)
      if (meta.name) return meta.name
      if (this.currentSource && this.currentSource.name) return this.currentSource.name
      return this.text.defaultName
    },
    getCurrentScriptCode () {
      const editor = this.$refs.scriptEditor
      if (editor && typeof editor.getCode === 'function') {
        return String(editor.getCode() || '')
      }
      return String(this.scriptCode || '')
    },
    extractScriptTimeframeFromCode (code) {
      const source = String(code || '')
      const match = source.match(/(?:^|\n)\s*#\s*timeframe\s*:\s*([A-Za-z0-9_-]+)/i)
      if (!match) return ''
      const raw = String(match[1] || '').trim()
      const aliases = {
        '1m': '1m',
        '5m': '5m',
        '15m': '15m',
        '30m': '30m',
        '1h': '1H',
        '4h': '4H',
        '1d': '1D',
        '1w': '1W'
      }
      return aliases[raw.toLowerCase()] || ''
    },
    buildTradingConfig (codeValue) {
      const cfg = this.runConfig || {}
      const marketCategory = cfg.market_category || cfg.marketCategory || 'Crypto'
      const marketType = marketCategory === 'Crypto' && cfg.market_type === 'swap' ? 'swap' : 'spot'
      const tradeDirection = marketType === 'spot' ? 'long' : (cfg.trade_direction || 'long')
      const investmentAmount = Number(cfg.initial_capital || cfg.investment_amount || 10000)
      const code = codeValue === undefined ? this.getCurrentScriptCode() : String(codeValue || '')
      const codeTimeframe = this.extractScriptTimeframeFromCode(code)
      const out = {
        market_category: marketCategory,
        exchange_id: marketCategory === 'Crypto' ? String(cfg.exchange_id || 'binance').toLowerCase() : '',
        symbol: String(cfg.symbol || 'BTC/USDT').trim(),
        timeframe: codeTimeframe || cfg.timeframe || '1m',
        tick_interval_sec: 10,
        market_type: marketType,
        trade_direction: tradeDirection,
        initial_capital: investmentAmount,
        investment_amount: investmentAmount,
        leverage: marketType === 'spot' ? 1 : Number(cfg.leverage || 5)
      }
      ;['strategy_family', 'executor_type', 'executor_config', 'executor_preview', 'bot_type', 'bot_params'].forEach(key => {
        if (cfg[key] !== undefined && cfg[key] !== null) out[key] = cfg[key]
      })
      if (cfg.tick_interval_sec) out.tick_interval_sec = Number(cfg.tick_interval_sec)
      if (this.scriptTemplateKey) out.script_template_key = this.scriptTemplateKey
      if (this.scriptTemplateParams && Object.keys(this.scriptTemplateParams).length) {
        out.script_template_params = { ...this.scriptTemplateParams }
      }
      if (this.currentAssetType === 'portfolio_strategy') {
        const universeReference = this.extractUniverseReferenceFromCode(code)
        const universeId = Number(universeReference.id || cfg.universe_id || cfg.universeId) || undefined
        if (universeId) {
          out.universe_id = universeId
          out.universe_code = universeReference.code || cfg.universe_code || cfg.universeCode || ''
          out.universe_name = cfg.universe_name || cfg.universeName || ''
        }
      }
      return out
    },
    buildPayload () {
      const currentCode = this.getCurrentScriptCode()
      const meta = this.extractScriptMetadataFromCode(currentCode)
      const description = meta.description || (this.currentSource && this.currentSource.description) || ''
      return {
        name: meta.name || this.deriveScriptName(),
        description,
        code: currentCode,
        asset_type: this.currentAssetType,
        template_key: this.scriptTemplateKey,
        param_schema: this.currentSourceParamSchema,
        template_params: { ...this.scriptTemplateParams },
        metadata: {
          description,
          last_run_config: this.buildTradingConfig(),
          script_template_params: { ...this.scriptTemplateParams },
          lifecycle_verified: this.scriptVerified,
          script_verified: this.scriptVerified
        }
      }
    },
    buildHiddenParamPayload () {
      const existingMeta = this.parseObject(this.currentSource && this.currentSource.metadata)
      const description = (this.currentSource && this.currentSource.description) || ''
      const lastRunConfig = this.buildTradingConfig()
      return {
        name: this.currentSourceName || this.text.defaultName,
        description,
        asset_type: this.currentAssetType,
        template_key: this.scriptTemplateKey || (this.currentSource && this.currentSource.template_key) || '',
        param_schema: this.currentSourceParamSchema,
        metadata: {
          ...existingMeta,
          description,
          code_hidden: true,
          last_run_config: lastRunConfig,
          script_template_params: { ...this.scriptTemplateParams }
        }
      }
    },
    scriptSnapshot () {
      if (this.scriptCodeHidden) {
        return JSON.stringify({
          id: this.currentSourceId,
          code_hidden: true,
          template_key: this.scriptTemplateKey || '',
          template_params: { ...(this.scriptTemplateParams || {}) },
          universe_id: this.selectedUniverseId || null,
          run_config: this.buildTradingConfig(this.scriptCode)
        })
      }
      return JSON.stringify({
        code: String(this.scriptCode || ''),
        asset_type: this.currentAssetType,
        template_key: this.scriptTemplateKey || '',
        template_params: { ...(this.scriptTemplateParams || {}) },
        universe_id: this.selectedUniverseId || null,
        run_config: this.buildTradingConfig(this.scriptCode)
      })
    },
    validateScriptCode () {
      if (this.scriptCodeHidden) return true
      if (!String(this.scriptCode || '').trim()) {
        this.$message.warning(this.text.codeRequired)
        return false
      }
      return true
    },
    async verifyScriptCode (options = {}) {
      if (!this.supportsCodeVerification) return false
      this.scriptCode = this.getCurrentScriptCode()
      const code = String(this.scriptCode || '').trim()
      if (!code) {
        this.$message.error(this.text.verifyBlocked.replace('{reason}', this.text.codeRequired))
        return false
      }
      try {
        const verificationPayload = { code }
        if (
          this.currentSourceId &&
          this.currentSource &&
          String(this.currentSource.code || '') === code
        ) {
          verificationPayload.strategyId = Number(this.currentSourceId)
        }
        const res = await verifyStrategyCode(verificationPayload)
        const verification = res && res.data && typeof res.data === 'object' ? res.data : (res || {})
        const persistenceRequested = Boolean(verificationPayload.strategyId)
        const valid = (verification.success === true || verification.valid === true) &&
          (!persistenceRequested || verification.persisted !== false)
        if (!valid) {
          const reason = verification.message || verification.error || (res && res.msg) || this.text.verifyFailed
          this.$message.error(this.text.verifyBlocked.replace('{reason}', reason))
          return false
        }
        this.scriptVerified = true
        if (!options.silentSuccess) this.$message.success(this.text.verifyPassed)
        return true
      } catch (e) {
        this.$message.error(`${this.text.verifyFailed}: ${e.backendMessage || e.message || ''}`)
        return false
      }
    },
    formatVerifyHint (hint) {
      if (!hint || !hint.code) return ''
      const params = hint.params || {}
      const calls = Array.isArray(params.calls) ? params.calls : []
      const names = calls.map(item => item && item.name).filter(Boolean).join(', ')
      const key = `strategyIde.verifyHints.${hint.code}`
      const translated = this.$t(key, {
        count: params.count || 0,
        names
      })
      return translated && translated !== key ? translated : hint.code
    },
    async saveScript (forceCreate = false, options = {}) {
      if (!this.supportsScriptSources) return null
      if (this.scriptCodeHidden) {
        if (!options.silent) this.$message.info(this.text.hiddenScriptDesc)
        return null
      }
      this.scriptCode = this.getCurrentScriptCode()
      if (!this.validateScriptCode()) return null
      if (!forceCreate && this.currentSource && this.currentSource.status === 'running') {
        this.$message.warning(this.text.runningEditBlocked)
        return null
      }
      if (!forceCreate && this.currentSourceId && !this.hasUnsavedScriptChanges) {
        if (!options.silent && !options.skipUnchanged) {
          this.$message.info(this.text.noChangesToSave)
        }
        return this.currentSourceId
      }
      if (this.savingScript) return null
      this.savingScript = true
      this.savingScriptMode = options.loadingMode || (forceCreate ? 'copy' : 'save')
      try {
        const payload = this.buildPayload()
        const res = (!forceCreate && this.currentSourceId)
          ? await updateScriptSource(this.currentSourceId, payload)
          : await createScriptSource(payload)
        if (res && res.code === 1) {
          const saved = (res && res.data) || {}
          const savedId = this.getScriptSourceId(saved) || this.currentSourceId
          await this.loadSources()
          await this.openSource(savedId, { updateRoute: true })
          if (!options.silent) this.$message.success(this.text.saveSuccess)
          return this.currentSourceId
        }
        this.$message.error((res && res.msg) || this.text.saveFailed)
        return null
      } catch (e) {
        this.$message.error(e.backendMessage || e.message || this.text.saveFailed)
        return null
      } finally {
        this.savingScript = false
        this.savingScriptMode = ''
      }
    },
    async saveHiddenScriptParams (options = {}) {
      if (!this.supportsScriptSources) return null
      if (!this.scriptCodeHidden || !this.currentSourceId) return this.currentSourceId
      if (this.savingScript) return null
      this.savingScript = true
      this.savingScriptMode = options.loadingMode || 'params'
      try {
        const res = await updateScriptSource(this.currentSourceId, this.buildHiddenParamPayload())
        if (res && res.code === 1) {
          const saved = (res && res.data) || {}
          const metadata = this.parseObject(saved.metadata || (this.currentSource && this.currentSource.metadata))
          this.currentSource = {
            ...(this.currentSource || {}),
            ...saved,
            code: '',
            code_hidden: 1,
            metadata: {
              ...metadata,
              code_hidden: true
            }
          }
          this.lastSavedSnapshot = this.scriptSnapshot()
          if (!options.silent) this.$message.success(this.text.saveSuccess)
          return this.currentSourceId
        }
        if (!options.silent) this.$message.error((res && res.msg) || this.text.saveFailed)
        return null
      } catch (e) {
        if (!options.silent) this.$message.error(e.backendMessage || e.message || this.text.saveFailed)
        return null
      } finally {
        this.savingScript = false
        this.savingScriptMode = ''
      }
    },
    deleteCurrentSource () {
      if (!this.supportsScriptSources) return
      if (!this.currentSourceId) return
      this.$confirm({
        title: this.text.deleteConfirmTitle,
        content: this.text.deleteConfirmDesc,
        okType: 'danger',
        onOk: async () => {
          this.deletingScript = true
          try {
            const res = await deleteScriptSource(this.currentSourceId)
            if (res && res.code === 1) {
              this.$message.success(this.text.deleteSuccess)
              await this.loadSources()
              const firstId = this.scriptOptions.length ? this.scriptOptions[0].id : ''
              if (firstId) {
                await this.openSource(firstId, { updateRoute: true })
              } else {
                this.createNewDraft({ openTemplate: false })
              }
            } else {
              this.$message.error((res && res.msg) || this.text.deleteFailed)
            }
          } catch (e) {
            this.$message.error(e.backendMessage || e.message || this.text.deleteFailed)
          } finally {
            this.deletingScript = false
          }
        }
      })
    },
    openBacktestCenter () {
      if (!this.supportsScriptSources || !this.supportsStrategyBacktest) return
      if (this.backtestHandoffLoading) return
      const context = this.currentSourceId
        ? this.syncDecisionContextFromSource(this.currentSource || {})
        : readDecisionContext({ allowStale: true })
      if (
        this.currentSourceId &&
        (!context || !findDecisionContextSource(this.currentSource ? [this.currentSource] : [], context))
      ) {
        this.$message.warning(this.isZh
          ? '当前策略缺少一致的市场/标的研究上下文，已阻止进入回测。'
          : 'Backtest blocked because the strategy does not have a matching market/symbol context.')
        return
      }
      const go = async () => {
        try {
          if (this.supportsCodeVerification) {
            const verified = await this.verifyScriptCode({ silentSuccess: true })
            if (!verified) return
          }
          const sourceId = this.scriptCodeHidden && this.currentSourceId
            ? await this.saveHiddenScriptParams({ silent: true, loadingMode: 'backtest' })
            : await this.saveScript(false, { skipUnchanged: true, silent: true, loadingMode: 'backtest' })
          if (!sourceId) return
          const sourceSnapshot = this.scriptSnapshot()
          let pin = null
          try {
            // Compile the just-saved source and pin the backend's actual revision,
            // not a client-side hash of the editor or a mutable sourceId alone.
            const compiled = await compileScriptSource({ sourceId })
            const data = compiled && compiled.data || {}
            pin = sourceHandoffIdentity(
              sourceId,
              data.sourceRevision && data.sourceRevision.codeHash,
              data.contractHash
            )
          } catch (error) {
            const status = Number(error && (error.status || error.response && error.response.status))
            this.$message.warning(status === 401 || status === 403
              ? '当前账号无权读取刚保存的策略源码；已停止交接。'
              : '策略源码编译身份核对失败；已停止交接，请稍后重试。')
            return
          }
          if (!pin || String(this.currentSourceId || '') !== String(sourceId) ||
            sourceSnapshot !== this.scriptSnapshot()) {
            this.$message.warning('策略源码或契约身份未能核验，已停止交接；请确认当前保存内容后重试。')
            return
          }
          const handoff = newSourceHandoffNonce()
          if (typeof sessionStorage === 'undefined' || !writeSourceHandoff(sessionStorage, handoff, pin)) {
            this.$message.warning('当前会话未能保存策略交接身份，已停止跳转；请检查浏览器存储后重试。')
            return
          }
          await this.$router.push({
            path: '/backtest-center',
            query: {
              assetType: this.currentAssetType,
              sourceId: String(sourceId),
              handoff,
              link: 'strategy-source'
            }
          }).catch(() => {})
        } finally {
          this.backtestHandoffLoading = false
        }
      }
      this.backtestHandoffLoading = true
      go()
    },
    openVersionDrawer () {
      if (!this.supportsScriptSources || !this.supportsScriptVersions) return
      if (!this.currentSourceId) return
      if (this.scriptCodeHidden) {
        this.$message.info(this.text.versionHiddenBlocked)
        return
      }
      this.showVersionDrawer = true
      this.scriptVersionPreview = null
      this.loadScriptVersions()
    },
    async loadScriptVersions () {
      if (!this.supportsScriptSources || !this.supportsScriptVersions) return
      if (!this.currentSourceId) return
      this.scriptVersionLoading = true
      try {
        const res = await getScriptSourceVersions(this.currentSourceId)
        if (res && res.code === 1) {
          this.scriptVersions = Array.isArray(res.data) ? res.data : []
        } else {
          this.$message.error((res && res.msg) || this.text.versionLoadFailed)
        }
      } catch (e) {
        this.$message.error(e.backendMessage || e.message || this.text.versionLoadFailed)
      } finally {
        this.scriptVersionLoading = false
      }
    },
    async previewScriptVersion (item) {
      if (!this.supportsScriptSources || !this.supportsScriptVersions) return
      if (!item || !item.id) return
      if (this.scriptCodeHidden || item.code_hidden || item.hidden_source) {
        this.$message.info(this.text.versionHiddenBlocked)
        return
      }
      try {
        const res = await getScriptSourceVersion(item.id)
        if (res && res.code === 1) {
          this.scriptVersionPreview = res.data || null
        } else {
          this.$message.error((res && res.msg) || this.text.versionLoadFailed)
        }
      } catch (e) {
        this.$message.error(e.backendMessage || e.message || this.text.versionLoadFailed)
      }
    },
    confirmRestoreScriptVersion (item) {
      if (!this.supportsScriptSources || !this.supportsScriptVersions) return
      if (!item || !item.id) return
      if (this.scriptCodeHidden || item.code_hidden || item.hidden_source || item.restore_disabled) {
        this.$message.info(this.text.versionHiddenBlocked)
        return
      }
      this.$confirm({
        title: this.text.versionRestoreTitle,
        content: this.text.versionRestoreContent.replace('{version}', item.version_no),
        okText: this.text.versionRestore,
        cancelText: this.text.cancel,
        onOk: () => this.restoreScriptVersion(item)
      })
    },
    async restoreScriptVersion (item) {
      if (!this.supportsScriptSources || !this.supportsScriptVersions) return
      if (!item || !item.id) return
      if (this.scriptCodeHidden || item.code_hidden || item.hidden_source || item.restore_disabled) {
        this.$message.info(this.text.versionHiddenBlocked)
        return
      }
      this.restoringScriptVersionId = item.id
      try {
        const res = await restoreScriptSourceVersion(item.id)
        if (res && res.code === 1 && res.data) {
          this.applySource(res.data)
          await this.loadSources()
          await this.loadScriptVersions()
          this.scriptVersionPreview = null
          this.$message.success(this.text.versionRestored)
        } else {
          this.$message.error((res && res.msg) || this.text.versionRestoreFailed)
        }
      } catch (e) {
        this.$message.error(e.backendMessage || e.message || this.text.versionRestoreFailed)
      } finally {
        this.restoringScriptVersionId = null
      }
    },
    async openIndicatorConvertPicker () {
      if (!this.supportsScriptSources) return
      this.indicatorConvertContext = null
      this.selectedIndicatorConvertId = undefined
      this.indicatorConvertInstruction = ''
      this.indicatorConvertError = ''
      this.showIndicatorConvertModal = true
      await this.loadIndicatorOptions()
    },
    async loadIndicatorOptions () {
      if (!this.supportsScriptSources) return false
      this.indicatorConvertIndicatorLoading = true
      this.indicatorConvertError = ''
      this.indicatorConvertLoadStatus = null
      try {
        const res = await getIndicatorListForStrategy()
        const list = this.extractIndicatorList(res)
          .map(item => this.normalizeIndicator(item))
          .filter(Boolean)
        this.indicatorConvertIndicators = list
        return true
      } catch (e) {
        this.indicatorConvertIndicators = []
        this.indicatorConvertError = e.backendMessage || e.message || this.text.indicatorConvertFailed
        this.indicatorConvertLoadStatus = Number(e.response && e.response.status) || Number(e.status) || null
        return false
      } finally {
        this.indicatorConvertIndicatorLoading = false
      }
    },
    extractIndicatorList (res) {
      const data = res && res.data
      if (Array.isArray(data)) return data
      return []
    },
    normalizeIndicator (raw) {
      if (!raw || typeof raw !== 'object') return null
      const codeHidden = this.toBool(raw.code_hidden ?? raw.codeHidden)
      return {
        indicatorId: String(raw.indicatorId || raw.id || '').trim(),
        name: String(raw.name || this.defaultIndicatorNameFromCode(raw.code || '') || '').trim(),
        description: String(raw.description || '').trim(),
        code: codeHidden ? '' : String(raw.code || '').trim(),
        params: raw.params || {},
        paramsDeclared: raw.params_declared || raw.paramsDeclared || null,
        paramsSourceOf: raw.params_source_of || raw.paramsSourceOf || null,
        handoffCodeDigest: String(raw.code_digest || raw.handoffCodeDigest || ''),
        market: String(raw.market || '').trim(),
        symbol: String(raw.symbol || '').trim(),
        exchange_id: String(raw.exchange_id || '').trim(),
        market_type: String(raw.market_type || '').trim(),
        instrument_id: String(raw.instrument_id || '').trim(),
        timeframe: String(raw.timeframe || '').trim(),
        // Only a same-session consistency check; this is not a backend version.
        savedCodeDigest: temporaryCodeDigest(raw.code || ''),
        codeHidden
      }
    },
    toBool (value) {
      if (value === true || value === 1) return true
      return ['1', 'true', 'yes', 'y'].includes(String(value || '').trim().toLowerCase())
    },
    defaultIndicatorNameFromCode (code) {
      const match = String(code || '').match(/^\s*my_indicator_name\s*=\s*(['"])(.*?)\1\s*$/m)
      return match ? match[2] : ''
    },
    handleIndicatorConvertSelect (id) {
      const target = (this.indicatorConvertIndicators || []).find(item => String(item.indicatorId) === String(id))
      this.indicatorConvertContext = target || null
      this.indicatorConvertError = ''
    },
    buildIndicatorConversionPrompt () {
      const ctx = this.indicatorConvertContext || {}
      const params = ctx.params && Object.keys(ctx.params).length ? JSON.stringify(ctx.params, null, 2) : '{}'
      const instruction = String(this.indicatorConvertInstruction || '').trim() ||
        'Convert the visible indicator signals into a conservative, event-based strategy. Confirm signals on closed bars and execute on the next bar to avoid look-ahead bias.'
      return [
        'Convert this QuantDinger chart-only indicator into executable QuantDinger personal script-runtime Python code.',
        '',
        'Required runtime contract:',
        '- Return one Python script only. Start with a metadata docstring, then define both on_init(ctx) and on_bar(ctx, bar).',
        '- In on_init(ctx), read configurable values with ctx.param(name, default) and assign the returned values to ctx attributes.',
        '- In on_bar(ctx, bar), use the closed current bar and ctx.bars(n). Bars expose open, high, low, close, volume, and timestamp.',
        '- Use ctx.position, ctx.balance, ctx.equity, and ctx.current_index for state. Persist custom state as ctx attributes.',
        '- This research contract is CNStock daily, single-symbol, cash, and long-only. Use only ctx.open_long, ctx.add_long, and ctx.close_long.',
        '- Every order call must use keyword arguments and an explicit order_type="market" or order_type="limit". Never emit legacy price=, positional order arguments, buy/sell, or close_position.',
        '- Every open or add must specify exactly one of qty, amount, or allocation_pct. amount is a base-quantity alias; allocation_pct is calculated from available equity at fill time and may include lot_size for floor rounding.',
        '- ctx.close_long with no qty or amount closes the full long position. A partial close must specify exactly one of qty or amount.',
        '- Market intents must not include limit_price and fill at the next bar open. Limit intents must include limit_price, are active for NEXT_BAR only, use next-bar OHLC touch with price improvement, and expire if untouched.',
        '- Do not implement pending-next-open flags or delay an intent in script state; the engine activates every accepted intent on the next bar.',
        '- The backtest panel supplies the one market, symbol, timeframe, capital, date range, and leverage used for a run; do not embed a universe or subscription in code.',
        '- Do not use initialize, handle_data, set_universe, subscribe, get_history, indicator, factor, get_factors, get_fundamentals, order, or order_target_percent.',
        '- Express stops, take-profit, trailing exits, and time exits as explicit closed-bar conditions followed by ctx.close_long(order_type="market", ...). Do not translate a legacy desired fill price into a limit order.',
        '',
        'Signal conversion boundaries:',
        '- Preserve the indicator signal logic first. Map visual buy/entry markers to long entries, sell/exit markers to long exits, and warning markers to wait/risk states.',
        '- Keep the generated strategy long-only. Map bearish markers to long exits, warning/wait, or visual-only; never invent a short position.',
        '- First classify every marker as long entry, long exit, warning/wait, or visual-only. Marker color and type="sell" alone do not prove executable exit intent.',
        '- Preserve composite event algebra exactly. For edge(A | B), compare the complete previous composite A_prev | B_prev; do not emit a duplicate event on the next bar.',
        '- Confirm all conditions on completed bars, use historical bars only, prevent duplicate intents on the same bar, and never use future data.',
        '- Remove display-only parameters such as colors, visibility toggles, marker offsets, line extension, and plot layout.',
        '- Do not generate grid, DCA, or martingale logic unless the user explicitly requests it.',
        '',
        `Indicator name: ${ctx.name || this.text.defaultIndicatorName}`,
        ctx.description ? `Indicator description: ${ctx.description}` : '',
        `Indicator params JSON:\n${params}`,
        '',
        `User conversion request:\n${instruction}`,
        '',
        `Indicator source code:\n\`\`\`python\n${ctx.code || ''}\n\`\`\``
      ].filter(Boolean).join('\n')
    },
    async confirmIndicatorToStrategy () {
      if (!this.$route || this.$route.path !== '/strategy-ide' || !this.showIndicatorConvertModal) return
      if (!this.supportsScriptSources) return
      const ctx = this.indicatorConvertContext || {}
      if (!ctx.indicatorId) {
        this.indicatorConvertError = this.text.indicatorConvertSelectFirst
        return
      }
      if (ctx.codeHidden || !ctx.code) {
        this.indicatorConvertError = ctx.codeHidden ? this.text.indicatorConvertHiddenBlocked : this.text.indicatorConvertNoCode
        return
      }
      this.indicatorConvertLoading = true
      this.indicatorConvertError = ''
      try {
        const res = await aiGenerateStrategy({
          prompt: this.buildIndicatorConversionPrompt()
        })
        const code = this.extractAiGeneratedCode(res)
        if (!code) throw new Error((res && res.msg) || this.text.indicatorConvertFailed)
        const created = await createScriptSource({
          name: this.extractScriptMetadataFromCode(code).name || `${ctx.name || this.text.defaultIndicatorName} Strategy`,
          description: this.extractScriptMetadataFromCode(code).description || `AI converted from indicator: ${ctx.name || this.text.defaultIndicatorName}`,
          code,
          metadata: this.buildGeneratedMetadata(ctx)
        })
        if (!(created && created.code === 1)) throw new Error((created && created.msg) || this.text.indicatorConvertFailed)
        await this.loadSources()
        const sourceId = this.getScriptSourceId(created.data)
        await this.openSource(sourceId, { updateRoute: false })
        this.finishIndicatorConversion(sourceId)
        this.$message.success(this.text.indicatorConvertSuccess)
      } catch (e) {
        this.indicatorConvertError = e.backendMessage || e.message || this.text.indicatorConvertFailed
      } finally {
        this.indicatorConvertLoading = false
      }
    },
    extractAiGeneratedCode (res) {
      if (!res || typeof res !== 'object') return ''
      const data = res.data || {}
      const candidates = [res.code, data.code]
      const code = candidates.find(item => typeof item === 'string' && item.trim())
      return code ? String(code).trim() : ''
    },
    buildGeneratedMetadata (ctx = {}) {
      const market = ctx.market || this.runConfig.market_category || 'Crypto'
      const lastRunConfig = {
        ...this.buildTradingConfig(),
        market_category: market,
        exchange_id: market === 'Crypto' ? String(this.runConfig.exchange_id || 'binance').toLowerCase() : '',
        symbol: ctx.symbol || this.runConfig.symbol || 'BTC/USDT',
        timeframe: ctx.timeframe || this.runConfig.timeframe || '1m',
        market_type: market === 'Crypto' && this.runConfig.market_type === 'swap' ? 'swap' : 'spot',
        trade_direction: 'long',
        leverage: market === 'Crypto' && this.runConfig.market_type === 'swap' ? Number(this.runConfig.leverage || 1) : 1
      }
      return {
        generated_by: 'ai_indicator_to_strategy',
        source_indicator_id: ctx.indicatorId || '',
        source_indicator_name: ctx.name || '',
        source_indicator_market: ctx.market || '',
        source_indicator_symbol: ctx.symbol || '',
        source_indicator_timeframe: ctx.timeframe || '',
        // gap A (A2): record the parameters that were actually handed off, and the code digest
        source_indicator_params_effective: ctx.params && typeof ctx.params === 'object' ? { ...ctx.params } : {},
        source_indicator_params_source_of: ctx.paramsSourceOf || null,
        source_indicator_code_digest: ctx.handoffCodeDigest || temporaryCodeDigest(ctx.code || ''),
        source_indicator_is_draft: ctx.isDraft === true,
        lifecycle_verified: false,
        script_verified: false,
        last_run_config: lastRunConfig,
        script_template_params: {}
      }
    },
    closeIndicatorConvertModal () {
      if (this.indicatorConvertLoading) return
      this.showIndicatorConvertModal = false
      this.indicatorConvertError = ''
      this.clearIndicatorConvertSession()
      this.clearIndicatorConvertRoute()
    },
    finishIndicatorConversion (sourceId) {
      this.showIndicatorConvertModal = false
      this.indicatorConvertError = ''
      this.clearIndicatorConvertSession()
      this.writeRouteSource(sourceId)
    },
    clearIndicatorConvertSession () {
      const key = String((this.$route.query || {}).convert_key || '').trim()
      if (!key || typeof sessionStorage === 'undefined') return
      try {
        sessionStorage.removeItem(key)
      } catch (_) {}
    },
    async applyIndicatorConvertRouteOnce (options = {}) {
      if (!this.supportsScriptSources) return false
      if (!this.isActiveStrategyRoute) return false
      const query = this.$route.query || {}
      if (!this.isIndicatorConvertRoute()) return false
      const key = String(query.convert_key || '').trim()
      const rejectHandoff = (reason) => {
        this.indicatorConvertContext = null
        this.selectedIndicatorConvertId = undefined
        this.showIndicatorConvertModal = false
        this.decisionContextNotice = reason
        return false
      }
      if (!/^qd_indicator_to_strategy_[a-zA-Z0-9_-]+$/.test(key) || typeof sessionStorage === 'undefined') {
        return rejectHandoff('指标交接载荷不可读取；未使用当前已保存指标替代，也未改写策略草稿。请从指标研究重新发起。')
      }
      let context = null
      try {
        context = JSON.parse(sessionStorage.getItem(key) || 'null')
      } catch (_) {
        context = null
      }
      const indicatorId = String(context && context.indicatorId || '').trim()
      if (!context || context.handoff_contract !== INDICATOR_HANDOFF_CONTRACT ||
        !indicatorId || indicatorId !== String(query.source_indicator_id || '') ||
        !String(context.code || '').trim() || context.codeHidden ||
        context.code_digest !== temporaryCodeDigest(context.code) ||
        !context.saved_code_digest) {
        return rejectHandoff('指标交接载荷缺失或已变化；未自动转换、未改写策略草稿。请从指标研究重新发起。')
      }
      // A cached list is not evidence that the saved source still exists or is readable.
      // Refresh before accepting this one-time draft handoff.
      const loaded = await this.loadIndicatorOptions()
      if (!this.isActiveStrategyRoute ||
        (options.sequence != null && options.sequence !== this.incomingResearchEntrySeq)) return false
      if (String((this.$route.query || {}).convert_key || '') !== key) return false
      if (!loaded) {
        const denied = [401, 403].includes(this.indicatorConvertLoadStatus) ||
          /403|forbidden|无权|权限/i.test(this.indicatorConvertError)
        return rejectHandoff(denied
          ? '当前无权读取指标原始源码；未自动转换、未改写策略草稿。请确认权限后从指标研究重新发起。'
          : '指标原始源码当前无法读取；未自动转换、未改写策略草稿。请稍后从指标研究重新发起。')
      }
      const sourceIndex = (this.indicatorConvertIndicators || []).findIndex(item => String(item.indicatorId) === indicatorId)
      const saved = sourceIndex >= 0 ? this.indicatorConvertIndicators[sourceIndex] : null
      if (!saved || saved.codeHidden || !saved.code || saved.savedCodeDigest !== context.saved_code_digest) {
        return rejectHandoff('指标原始源码已变化、删除或当前无权读取；本次临时交接已失效，未使用最新版本替代。')
      }
      const storedContext = this.normalizeIndicator(context)
      this.indicatorConvertContext = {
        ...saved,
        ...storedContext,
        code: String(context.code), // preserve the exact submitted draft, including trailing whitespace
        isDraft: context.is_draft === true
      }
      this.selectedIndicatorConvertId = indicatorId
      this.decisionContextNotice = context.is_draft === true
        ? '正在引用本次会话传来的指标未保存草稿；仅为临时交接，非已保存版本。请核对内容后再显式转换。'
        : '正在引用本次会话传来的指标源码；已与当前已保存源码做一致性核对，但不代表不可变历史版本。'
      this.showIndicatorConvertModal = true
      return true
    },
    clearIndicatorConvertRoute () {
      const query = { ...(this.$route.query || {}) }
      delete query.convert
      delete query.convert_key
      delete query.source_indicator_id
      delete query.indicator_id
      delete query.indicatorId
      delete query.market
      delete query.symbol
      delete query.timeframe
      this.replaceRouteQuery(query)
    },
    marketLabel (market) {
      const text = String(market || '').trim()
      if (!text) return '-'
      const key = `dashboard.indicator.market.${text}`
      const translated = this.$t(key)
      return translated && translated !== key ? translated : text
    },
    formatTime (value) {
      if (!value) return ''
      const date = new Date(value)
      if (Number.isNaN(date.getTime())) return String(value)
      return date.toLocaleString()
    }
  }
}
</script>

<style lang="less" scoped>
.strategy-ide-shell {
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  height: calc(100vh - 64px);
  padding: 12px;
  background: #f5f7fb;
  overflow: hidden;
}

.strategy-ide-shell > .decision-context-bar {
  margin-bottom: 8px;
  border-radius: 8px;
}

.strategy-ide-layout {
  flex: 1 1 auto;
  height: auto;
  min-height: 0;
}

.script-panel {
  height: 100%;
  min-height: 0;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}

.script-panel ::v-deep .strategy-editor {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.script-panel ::v-deep .editor-top-toolbar {
  flex: 0 0 auto;
}

.script-panel ::v-deep .editor-layout {
  flex: 1 1 auto;
  height: calc(100% - 57px);
  min-height: 0;
}

.script-panel ::v-deep .code-col,
.script-panel ::v-deep .side-col {
  height: 100%;
  min-height: 0;
}

.script-panel ::v-deep .code-section,
.script-panel ::v-deep .side-tabs {
  height: 100%;
  min-height: 0;
}

.script-panel ::v-deep .code-editor-container {
  min-height: 0;
}

.script-panel ::v-deep .CodeMirror {
  height: 100% !important;
}

.ide-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid #e5e7eb;
  background: #fff;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.toolbar-left {
  flex: 1 1 auto;
}

.toolbar-right {
  flex: 0 0 auto;
}

.strategy-workspace-switcher {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
  border-right: 1px solid #e5e7eb;
}

.strategy-workspace-copy {
  display: flex;
  width: 150px;
  min-width: 0;
  flex-direction: column;
  line-height: 1.25;
}

.strategy-workspace-copy strong {
  color: #202735;
  font-size: 13px;
}

.strategy-workspace-copy span {
  margin-top: 3px;
  overflow: hidden;
  color: #7b8494;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.strategy-workspace-switcher .ant-radio-button-wrapper {
  height: 36px;
  padding: 0 13px;
  line-height: 34px;
  font-weight: 700;
}

.script-select-label {
  flex: 0 0 auto;
  color: #6b7280;
  font-size: 13px;
  font-weight: 600;
}

.script-select {
  width: 280px;
}

.ide-icon-btn {
  width: 36px;
  height: 36px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ide-icon-btn--danger {
  color: #ff4d4f;
  border-color: rgba(255, 77, 79, 0.45);
}

.script-save-button,
.script-live-button,
.script-backtest-button,
.indicator-convert-button,
.factor-library-button,
.universe-library-button {
  height: 36px;
  font-weight: 700;
}

.script-save-button,
.script-live-button {
  min-width: 76px;
}

.script-backtest-button,
.indicator-convert-button {
  min-width: 120px;
}

.universe-library-button--selected {
  border-color: var(--primary-color, #52c41a);
  color: var(--primary-color, #52c41a);
  background: rgba(82, 196, 26, 0.08);
}

.theme-dark {
  background: #0f0f0f;

  .script-panel {
    border-color: rgba(255, 255, 255, 0.1);
    background: #141414;
  }

  .ide-toolbar {
    border-bottom-color: rgba(255, 255, 255, 0.08);
    background: #141414;
  }

  .script-select-label {
    color: rgba(255, 255, 255, 0.62);
  }

  .strategy-workspace-copy strong {
    color: rgba(255, 255, 255, 0.9);
  }

  .strategy-workspace-copy span {
    color: rgba(255, 255, 255, 0.46);
  }

  .strategy-workspace-switcher {
    border-right-color: rgba(255, 255, 255, 0.1);
  }

  .strategy-workspace-switcher .ant-radio-button-wrapper {
    border-color: rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.68);
    background: #202020;
  }

  .strategy-workspace-switcher .ant-radio-button-wrapper-checked {
    border-color: var(--primary-color, #52c41a);
    color: #fff;
    background: var(--primary-color, #52c41a);
  }

  ::v-deep .ant-select-selection,
  ::v-deep .ant-input,
  ::v-deep textarea.ant-input,
  ::v-deep .ant-input-number,
  ::v-deep .ant-input-number-input {
    background: #111 !important;
    border-color: rgba(255, 255, 255, 0.12) !important;
    color: rgba(255, 255, 255, 0.86) !important;
  }
}

@media (max-width: 1180px) {
  .ide-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .toolbar-left,
  .toolbar-right {
    width: 100%;
    flex-wrap: wrap;
  }

  .script-select {
    flex: 1 1 260px;
    width: auto;
  }
}
</style>

<style lang="less">
.robot-builder-drawer {
  .ant-drawer-content-wrapper {
    max-width: 96vw;
  }

  .ant-drawer-body {
    height: calc(100vh - 55px);
    display: flex;
    overflow: hidden;
    padding: 14px 18px 18px;
    background: #f4f6f8;
    flex-direction: column;
  }

  .robot-builder-intro {
    flex: 0 0 auto;
    margin-bottom: 12px;
    padding: 10px 12px;
    border: 1px solid #d9e2ec;
    border-radius: 8px;
    background: #fff;
    color: #52606d;
  }

  .executor-page {
    min-height: 0;
    flex: 1;
  }
}

.robot-builder-drawer--dark {
  .ant-drawer-header,
  .ant-drawer-content,
  .ant-drawer-body {
    border-color: #2b2b2b;
    background: #0b0b0b;
    color: rgba(255, 255, 255, 0.86);
  }

  .ant-drawer-title,
  .ant-drawer-close {
    color: rgba(255, 255, 255, 0.86);
  }

  .robot-builder-intro {
    border-color: #2b2b2b;
    background: #151515;
    color: rgba(255, 255, 255, 0.58);
  }
}

.script-publish-modal {
  .ant-modal-content {
    overflow: hidden;
    border-radius: 12px;
  }

  .ant-modal-header {
    padding: 18px 24px;
    border-bottom: 1px solid #edf0f5;
  }

  .ant-modal-body {
    padding: 18px 24px 20px;
    background: #f6f7f9;
  }

  .ant-modal-footer {
    padding: 14px 24px;
    border-top: 1px solid #edf0f5;
    background: #fff;
  }

  .publish-form {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .publish-summary-card {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 74px;
    padding: 14px;
    border: 1px solid rgba(255, 77, 79, 0.22);
    border-radius: 10px;
    background: linear-gradient(135deg, rgba(255, 77, 79, 0.1) 0%, #fff 72%);
  }

  .publish-summary-icon {
    width: 42px;
    height: 42px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    color: #fff;
    background: var(--primary-color, #ff4d4f);
    box-shadow: 0 10px 24px rgba(255, 77, 79, 0.24);
    font-size: 18px;
  }

  .publish-summary-main {
    flex: 1;
    min-width: 0;
  }

  .publish-summary-label {
    margin-bottom: 3px;
    font-size: 12px;
    font-weight: 700;
    color: #6b7280;
  }

  .publish-summary-name {
    overflow: hidden;
    color: #111827;
    font-size: 16px;
    font-weight: 800;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .publish-summary-tag {
    margin: 0;
    height: 24px;
    line-height: 22px;
    border-radius: 999px;
    font-weight: 700;
  }

  .publish-note {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    padding: 10px 12px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #fff;
    color: #4b5563;
    font-size: 13px;
    line-height: 1.55;
  }

  .publish-note .anticon {
    margin-top: 3px;
    color: var(--primary-color, #ff4d4f);
  }

  .publish-section,
  .publish-option-card {
    padding: 14px;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    background: #fff;
  }

  .publish-section-title,
  .field-label,
  .publish-option-head {
    color: #1f2937;
    font-size: 13px;
    font-weight: 800;
  }

  .publish-section-title {
    margin-bottom: 10px;
  }

  .publish-pricing-group {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    width: 100%;
  }

  .publish-pricing-group .ant-radio-button-wrapper {
    height: 40px;
    line-height: 38px;
    padding: 0 14px;
    border: 1px solid #dfe3ea;
    border-radius: 8px !important;
    color: #4b5563;
    text-align: center;
    font-weight: 700;
    background: #fafafa;
    box-shadow: none;
  }

  .publish-pricing-group .ant-radio-button-wrapper::before {
    display: none;
  }

  .publish-pricing-group .ant-radio-button-wrapper-checked {
    color: #fff;
    border-color: var(--primary-color, #ff4d4f);
    background: var(--primary-color, #ff4d4f);
    box-shadow: 0 8px 18px rgba(255, 77, 79, 0.24);
  }

  .publish-price-box {
    margin-top: 12px;
  }

  .publish-price-input {
    width: 100%;
  }

  .publish-option-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .publish-option-card.active {
    border-color: var(--primary-color, #ff4d4f);
    background: linear-gradient(135deg, rgba(255, 77, 79, 0.08) 0%, #fff 76%);
    box-shadow: inset 0 0 0 1px rgba(255, 77, 79, 0.14);
  }

  .publish-option-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
  }

  .publish-hint {
    color: #6b7280;
    font-size: 12px;
    line-height: 1.55;
  }
}

.indicator-convert-modal {
  .indicator-convert-box {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .indicator-convert-selector {
    padding: 12px;
    border: 1px solid rgba(239, 68, 68, 0.18);
    border-radius: 8px;
    background: rgba(239, 68, 68, 0.06);
  }

  .field-label {
    display: block;
    color: #334155;
    font-size: 13px;
    font-weight: 700;
  }

  .field-label--spaced {
    margin-top: 4px;
  }

  .indicator-convert-current {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 16px;
    border: 1px solid rgba(239, 68, 68, 0.18);
    border-radius: 8px;
    background: linear-gradient(135deg, rgba(239, 68, 68, 0.12), rgba(255, 255, 255, 0.96));
  }

  .indicator-convert-current span {
    display: block;
    margin-bottom: 4px;
    color: #64748b;
    font-size: 12px;
    font-weight: 700;
  }

  .indicator-convert-current strong {
    color: #111827;
    font-size: 16px;
    font-weight: 800;
  }

  .indicator-convert-current small {
    color: #64748b;
    font-size: 12px;
    white-space: nowrap;
  }

  .indicator-convert-note {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    padding: 10px 12px;
    border: 1px solid rgba(59, 130, 246, 0.16);
    border-radius: 6px;
    background: rgba(59, 130, 246, 0.06);
    color: #475569;
    font-size: 13px;
    line-height: 1.55;
  }

  .indicator-convert-note .anticon {
    margin-top: 3px;
    color: #2563eb;
  }
}

.code-version-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  color: #64748b;
  font-size: 13px;
  font-weight: 700;
}

.code-version-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.code-version-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
}

.code-version-item__main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.code-version-item__main strong {
  color: #111827;
}

.code-version-item__main span,
.code-version-item__main small {
  color: #64748b;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.code-version-item__actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.code-version-preview {
  margin-top: 16px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
}

.code-version-preview__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid #e5e7eb;
  background: #f8fafc;
}

.code-version-preview pre {
  max-height: 360px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: #0f172a;
  color: #e2e8f0;
  font-size: 12px;
  line-height: 1.6;
}

.code-version-hidden {
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px 20px;
  text-align: center;
  color: #64748b;
  background: #0f172a;
}

.code-version-hidden .anticon {
  color: #52c41a;
  font-size: 24px;
}

.code-version-hidden strong {
  color: #e2e8f0;
}

.script-publish-modal--dark,
.indicator-convert-modal--dark,
.script-version-drawer--dark {
  .ant-modal-content,
  .ant-modal-header,
  .ant-modal-footer,
  .ant-drawer-content,
  .ant-drawer-header {
    background: #181818;
    border-color: rgba(255, 255, 255, 0.08);
  }

  .ant-modal-body {
    background: #141414;
  }

  .ant-modal-title,
  .ant-modal-close,
  .ant-drawer-title,
  .ant-drawer-close,
  .publish-section-title,
  .publish-option-head,
  .field-label,
  .code-version-item__main strong {
    color: rgba(255, 255, 255, 0.88);
  }

  .publish-summary-card,
  .indicator-convert-current {
    border-color: rgba(255, 77, 79, 0.32);
    background: linear-gradient(135deg, rgba(255, 77, 79, 0.18) 0%, #1c1c1c 72%);
  }

  .publish-summary-name,
  .indicator-convert-current strong {
    color: rgba(255, 255, 255, 0.9);
  }

  .publish-summary-label,
  .publish-hint,
  .indicator-convert-current span,
  .indicator-convert-current small,
  .code-version-toolbar,
  .code-version-item__main span,
  .code-version-item__main small {
    color: rgba(255, 255, 255, 0.52);
  }

  .publish-note,
  .publish-section,
  .publish-option-card,
  .indicator-convert-selector,
  .indicator-convert-note,
  .code-version-item,
  .code-version-preview {
    border-color: rgba(255, 255, 255, 0.1);
    background: #1f1f1f;
  }

  .publish-option-card.active {
    border-color: var(--primary-color, #ff4d4f);
    background: linear-gradient(135deg, rgba(255, 77, 79, 0.16) 0%, #1f1f1f 78%);
  }

  .code-version-preview__head {
    border-color: rgba(255, 255, 255, 0.08);
    background: #141414;
  }
}
</style>
