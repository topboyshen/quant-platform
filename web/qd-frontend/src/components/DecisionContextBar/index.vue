<template>
  <section class="decision-context-bar" data-testid="decision-context-bar">
    <div class="decision-context-bar__chain" aria-label="研究链">
      <span class="decision-context-bar__chain-title">研究链</span>
      <span
        v-for="(stage, index) in researchStages"
        :key="stage.path"
        class="decision-context-bar__chain-stage"
        :class="{ 'decision-context-bar__chain-stage--current': index === currentStageIndex }"
        :aria-current="index === currentStageIndex ? 'step' : null"
      >{{ index + 1 }}. {{ stage.label }}</span>
    </div>
    <div v-if="context && context.symbol" class="decision-context-bar__asset">
      <a-tag :color="context.origin === 'radar' ? 'blue' : 'magenta'">{{ originLabel }}</a-tag>
      <strong>{{ context.name || context.symbol }}</strong>
      <code>{{ context.market }}:{{ context.symbol }}</code>
      <a-tag v-if="signalType" color="gold">{{ signalLabel }}</a-tag>
      <a-tag v-if="riskLevel" color="orange">风险 {{ riskLabel }}</a-tag>
    </div>
    <div v-else class="decision-context-bar__asset decision-context-bar__asset--missing">
      <strong>未选择研究对象</strong>
      <span>当前页面没有可核验的标的上下文；请从 Radar 重新选择。</span>
    </div>
    <div v-if="context && context.symbol" class="decision-context-bar__meta">
      <span>数据截至 {{ formatTime(context.asOf) }}</span>
      <span>来源 {{ context.source || 'QuantDinger' }}</span>
      <a-tag :color="stale ? 'orange' : 'green'">{{ stale ? '上下文已过期' : '上下文可继续' }}</a-tag>
    </div>
    <div v-else class="decision-context-bar__meta">数据截止与来源未知</div>
    <div class="decision-context-bar__artifact" data-testid="research-chain-source">
      <span class="decision-context-bar__artifact-title">本环节来源</span>
      <strong>{{ artifactLabel || '未提供具体产物引用' }}</strong>
      <a-tag v-if="sourceState.color" :color="sourceState.color">{{ sourceState.label }}</a-tag>
      <a-tag v-else>{{ sourceState.label }}</a-tag>
      <small v-if="artifactDetail">{{ artifactDetail }}</small>
    </div>
    <div class="decision-context-bar__actions">
      <a-button size="small" @click="navigate('/personal-home')">返回 Radar</a-button>
      <a-button size="small" type="primary" :disabled="!context || stale" @click="handleNext">{{ nextStep.label }}</a-button>
    </div>
  </section>
</template>

<script>
import {
  DECISION_CONTEXT_CHANGED_EVENT,
  continueDecisionContext,
  isDecisionContextStale,
  readDecisionContext
} from '@/utils/decisionContext'
import { formatRadarTime, localizeRiskLevel, localizeSignalType } from '@/utils/radarPresentation'
import { RESEARCH_STAGES, researchSourceState, researchStageIndex } from '@/utils/researchChainPresentation'

export default {
  name: 'DecisionContextBar',
  props: {
    artifactLabel: { type: String, default: '' },
    artifactStatus: { type: String, default: 'unknown' },
    artifactDetail: { type: String, default: '' }
  },
  data () {
    return { context: null }
  },
  computed: {
    researchStages () { return RESEARCH_STAGES },
    currentStageIndex () { return researchStageIndex(this.$route && this.$route.path) },
    sourceState () {
      return researchSourceState(this.artifactStatus)
    },
    originLabel () {
      return this.context && this.context.origin === 'radar'
        ? 'Radar ETF 研究（系统池）'
        : '个人 A 股观察名单'
    },
    signalType () {
      return this.context && this.context.radar && this.context.radar.signal
        ? this.context.radar.signal.type
        : ''
    },
    signalLabel () { return localizeSignalType(this.signalType) },
    riskLevel () {
      return this.context && this.context.radar && this.context.radar.risk
        ? this.context.radar.risk.level
        : ''
    },
    riskLabel () { return localizeRiskLevel(this.riskLevel) },
    stale () { return this.context ? isDecisionContextStale(this.context) : true },
    nextStep () {
      const current = String(this.$route && this.$route.path || '')
      const steps = {
        '/ai-asset-analysis': { path: '/indicator-ide', label: '继续指标研究' },
        '/indicator-ide': { path: '/strategy-ide', label: '继续策略研究' },
        '/strategy-ide': { path: '/backtest-center', label: '继续回测验证' },
        '/backtest-center': { path: '/ai-asset-analysis', label: '返回 AI 复盘' }
      }
      return steps[current] || { path: '/ai-asset-analysis', label: '继续 AI 分析' }
    }
  },
  watch: {
    '$route.path': 'refresh'
  },
  created () { this.refresh() },
  mounted () {
    this._decisionContextListener = () => this.refresh()
    window.addEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
  },
  activated () { this.refresh() },
  beforeDestroy () {
    if (this._decisionContextListener) {
      window.removeEventListener(DECISION_CONTEXT_CHANGED_EVENT, this._decisionContextListener)
      this._decisionContextListener = null
    }
  },
  methods: {
    refresh () { this.context = readDecisionContext({ allowStale: true }) },
    formatTime (value) { return formatRadarTime(value) },
    handleNext () {
      if (!this.context || this.stale) {
        this.$message.warning('研究上下文缺失或已过期，请返回 Radar 重新选择标的。')
        return
      }
      if (this.$listeners && this.$listeners.next) {
        this.$emit('next', this.nextStep)
        return
      }
      this.navigate(this.nextStep.path)
    },
    navigate (path) {
      if (!path) return
      if (path !== '/personal-home') {
        if (!this.context || this.stale) {
          this.$message.warning('研究上下文缺失或已过期，请返回 Radar 重新选择标的。')
          return
        }
        const continued = continueDecisionContext()
        if (!continued || !continued.symbol) {
          this.$message.warning('研究上下文不可用，请返回 Radar 重新选择标的。')
          return
        }
      }
      this.$router.push(path).catch(() => {})
    }
  }
}
</script>

<style lang="less">
.decision-context-bar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(260px, auto) auto;
  align-items: center;
  gap: 10px 16px;
  flex: 0 0 auto;
  margin: 0;
  padding: 9px 12px;
  border: 1px solid rgba(24, 144, 255, 0.2);
  background: linear-gradient(135deg, rgba(24, 144, 255, 0.07), rgba(82, 196, 26, 0.035)), #fff;
  color: #1d2a3d;
}

.decision-context-bar__chain {
  grid-column-start: 1;
  grid-column-end: -1;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 12px;
  min-width: 0;
  color: #66748a;
  font-size: 12px;
}

.decision-context-bar__chain-title,
.decision-context-bar__artifact-title {
  font-weight: 600;
  color: #405067;
}

.decision-context-bar__chain-stage--current {
  color: #096dd9;
  font-weight: 600;
}

.decision-context-bar__artifact {
  grid-column-start: 1;
  grid-column-end: 3;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 8px;
  min-width: 0;
  font-size: 12px;
}

.decision-context-bar__artifact strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.decision-context-bar__artifact small {
  min-width: 0;
  overflow-wrap: anywhere;
  color: #768398;
}

.decision-context-bar__asset,
.decision-context-bar__meta,
.decision-context-bar__actions {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex-wrap: wrap;
}

.decision-context-bar__asset strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.decision-context-bar__asset code {
  overflow-wrap: anywhere;
  color: #66748a;
  font-size: 12px;
}

.decision-context-bar__asset--missing {
  color: #9a6700;
  font-size: 12px;
}

.decision-context-bar__meta {
  color: #768398;
  font-size: 12px;
}

.decision-context-bar__actions {
  justify-content: flex-end;
}

body.dark .decision-context-bar,
body.realdark .decision-context-bar,
.theme-dark .decision-context-bar {
  border-color: rgba(255, 255, 255, 0.13);
  background: linear-gradient(135deg, rgba(24, 144, 255, 0.13), rgba(82, 196, 26, 0.05)), #17191d;
  color: rgba(255, 255, 255, 0.88);
}

body.dark .decision-context-bar__asset code,
body.dark .decision-context-bar__meta,
body.dark .decision-context-bar__chain,
body.dark .decision-context-bar__artifact small,
body.realdark .decision-context-bar__asset code,
body.realdark .decision-context-bar__meta,
body.realdark .decision-context-bar__chain,
body.realdark .decision-context-bar__artifact small,
.theme-dark .decision-context-bar__asset code,
.theme-dark .decision-context-bar__meta,
.theme-dark .decision-context-bar__chain,
.theme-dark .decision-context-bar__artifact small {
  color: rgba(255, 255, 255, 0.58);
}

body.dark .decision-context-bar__chain-title,
body.dark .decision-context-bar__artifact-title,
body.realdark .decision-context-bar__chain-title,
body.realdark .decision-context-bar__artifact-title,
.theme-dark .decision-context-bar__chain-title,
.theme-dark .decision-context-bar__artifact-title {
  color: rgba(255, 255, 255, 0.82);
}

@media (max-width: 1100px) {
  .decision-context-bar {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .decision-context-bar__chain {
    grid-column-start: 1;
    grid-column-end: 3;
    grid-row-start: 1;
    grid-row-end: 2;
  }

  .decision-context-bar__asset {
    grid-column-start: 1;
    grid-column-end: 2;
    grid-row-start: 2;
    grid-row-end: 3;
  }

  .decision-context-bar__actions {
    grid-column-start: 2;
    grid-column-end: 3;
    grid-row-start: 2;
    grid-row-end: 3;
  }

  .decision-context-bar__meta {
    grid-column-start: 1;
    grid-column-end: 3;
    grid-row-start: 3;
    grid-row-end: 4;
  }

  .decision-context-bar__artifact {
    grid-column-start: 1;
    grid-column-end: 3;
    grid-row-start: 4;
    grid-row-end: 5;
  }
}

@media (max-width: 680px) {
  .decision-context-bar {
    grid-template-columns: minmax(0, 1fr);
  }

  .decision-context-bar__chain,
  .decision-context-bar__asset,
  .decision-context-bar__meta,
  .decision-context-bar__artifact,
  .decision-context-bar__actions {
    grid-column-start: 1;
    grid-column-end: 2;
    grid-row-start: auto;
    grid-row-end: auto;
    min-width: 0;
  }

  .decision-context-bar__actions {
    justify-content: flex-start;
  }
}
</style>
