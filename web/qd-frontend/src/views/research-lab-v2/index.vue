<template>
  <div class="rl2" :class="{ 'rl2-dark': workspaceDark }" data-testid="rl2-root">
    <!-- capability gate -->
    <div v-if="capsLoaded && !caps.enabled" class="rl2-card" data-testid="rl2-disabled">
      <h3>研究实验室 V2 未对当前账号开放</h3>
      <p>请继续使用原有四个页面：
        <router-link to="/ai-asset-analysis">AI 资产分析</router-link> ·
        <router-link to="/indicator-ide">指标研究</router-link> ·
        <router-link to="/strategy-ide">策略研究</router-link> ·
        <router-link to="/backtest-center">回测验证</router-link></p>
    </div>

    <template v-else-if="capsLoaded">
      <div v-if="!caps.write || !caps.run" class="rl2-banner" data-testid="rl2-switch-banner">
        V2 运行时开关已{{ !caps.write ? '关闭新写入与新运行' : '关闭新运行' }}：已有研究可查看；按同一幂等键查询、对账与已受理回测的结果保存不受影响。
      </div>
      <div v-if="networkIssue" class="rl2-banner rl2-banner--error" data-testid="rl2-network-banner">
        {{ networkIssue }} <a-button size="small" @click="networkIssue = ''">知道了</a-button>
      </div>

      <!-- project picker -->
      <div v-if="!project" class="rl2-card" data-testid="rl2-projects">
        <h3>研究项目</h3>
        <div v-for="p in projects" :key="p.id" class="rl2-row">
          <a :data-testid="'rl2-open-' + p.id" @click="openProject(p.id)">{{ p.name }}</a>
          <span class="rl2-muted">{{ p.subject.market }}:{{ p.subject.symbol }} · {{ p.branchCount }} 个分支</span>
        </div>
        <p v-if="!projects.length" class="rl2-muted">还没有研究项目。</p>
        <div class="rl2-form">
          <a-input v-model="newProject.name" placeholder="项目名称" data-testid="rl2-new-name" style="width: 220px" />
          <a-input v-model="newProject.symbol" placeholder="A 股代码（6 位）" data-testid="rl2-new-symbol" style="width: 160px" />
          <a-button type="primary" :disabled="busy || !caps.write" data-testid="rl2-create-project" @click="doCreateProject">新建研究项目（写入服务端）</a-button>
        </div>
      </div>

      <div v-else-if="!branch" class="rl2-card rl2-muted">正在载入分支…</div>
      <template v-else>
        <!-- research bar (top) -->
        <div class="rl2-bar" data-testid="rl2-bar">
          <div class="rl2-bar-row">
            <a class="rl2-back" @click="closeProject">‹ 项目</a>
            <strong class="rl2-title">{{ project.name }}</strong>
            <span class="rl2-tag">{{ project.subject.symbol }} · A 股日线</span>
            <span>分支</span>
            <a-select :value="branch && branch.id" style="width: 150px" data-testid="rl2-branch-select" @change="switchBranch">
              <a-select-option v-for="b in branches" :key="b.id" :value="b.id">{{ b.name }}</a-select-option>
            </a-select>
            <a-button size="small" data-testid="rl2-new-branch" :disabled="!caps.write" @click="openBranchDialog()">新建分支</a-button>
            <span :class="['rl2-state', 'rl2-state--' + barState.tone]" data-testid="rl2-save-state">{{ barState.text }}</span>
          </div>
          <nav class="rl2-evidence-links" aria-label="本研究的证据入口" data-testid="rl2-evidence-links">
            <router-link :to="researchEvidenceRoute('reports')">选择报告与研究依据</router-link>
            <router-link :to="{ path: '/personal-reports', query: { section: 'quality', projectId: project.id, branchId: branch.id, ...(currentRun ? { runId: currentRun.id } : {}) } }">核对本分支{{ currentRun ? '所选回测的' : '的' }}数据质量</router-link>
            <router-link :to="researchEvidenceRoute('validation')">验证与复盘</router-link>
            <router-link :to="researchEvidenceRoute('operations')">任务与运行</router-link>
          </nav>
          <div class="rl2-steps" data-testid="rl2-steps">
            <a v-for="(m, i) in modules" :key="m.key" :class="['rl2-step', module === m.key && 'rl2-step--on']" :data-testid="'rl2-nav-' + m.key" @click="goModule(m.key)">
              <span class="rl2-step-no">{{ i + 1 }}</span>{{ m.label }} <small>{{ stepHint(m.key) }}</small>
            </a>
          </div>
        </div>

        <div class="rl2-grid">
          <div class="rl2-main">
            <!-- ================= AI ================= -->
            <section v-if="module === 'ai'" data-testid="rl2-mod-ai">
              <div v-if="headEvidence" class="rl2-card" data-testid="rl2-user-hypothesis">
                <h3>依据分析与研究假设</h3>
                <p class="rl2-muted" data-testid="rl2-evidence-hypothesis-note">{{ evidenceSourceLabel }}原文保存在右侧。可让 AI 根据这份冻结依据提出可验证的假设，也可直接填写；选定后继续指标研究。</p>
                <a-button type="primary" :loading="hypothesisCalling" :disabled="hypothesisCalling || busy || !caps.write || !handoffBranchReady || branch.hasDownstream || !(caps.ai && caps.ai.configured && caps.ai.evidenceHypotheses)" data-testid="rl2-evidence-ai-generate" @click="generateEvidenceHypotheses">依据报告生成假设（调用 AI）</a-button>
                <p v-if="!(caps.ai && caps.ai.configured && caps.ai.evidenceHypotheses)" class="rl2-muted">当前模型尚未配置，可先手动提出假设。</p>
                <p v-if="headHypothesis">当前选用：{{ headHypothesis.payload.title }} <span class="rl2-muted">（{{ headHypothesis.originLabel }}）</span></p>
                <a-textarea
                  :value="evidenceHypothesisText"
                  :rows="3"
                  :max-length="500"
                  placeholder="例如：报告提出的某个因素，是否能解释该标的下一阶段的表现？请写下你要验证的假设。"
                  data-testid="rl2-user-hypothesis-text"
                  @change="e => setEvidenceHypothesis(e.target.value)" />
                <a-button style="margin-top: 8px" :disabled="busy || !caps.write || !handoffBranchReady || branch.hasDownstream || !evidenceHypothesisText.trim()" data-testid="rl2-user-hypothesis-confirm" @click="confirmEvidenceHypothesis">保存并选定我的假设</a-button>
                <p v-if="branch.hasDownstream" class="rl2-muted">已有策略或运行，请先新建分支并继承外部证据，再提出新假设。</p>
              </div>
              <div v-else class="rl2-card">
                <h3>补充分析与假设</h3>
                <p>可先到“报告与证据”选择完整来源，交接后由 AI 提出假设；也可以在此保存已有分析。</p>
                <a-textarea v-model="ai.conclusion" :rows="2" placeholder="报告结论" data-testid="rl2-ai-conclusion" />
                <a-textarea v-model="ai.hypotheses" :rows="3" placeholder="研究假设，每行一个" data-testid="rl2-ai-hyps" style="margin-top: 8px" />
                <a-button style="margin-top: 8px" :disabled="busy || !caps.write" data-testid="rl2-ai-archive" @click="archiveReport">归档 AI 报告（用户提交）</a-button>
              </div>
              <div v-for="rep in reports" :key="rep.id" class="rl2-card">
                <div class="rl2-muted">{{ rep.payload && rep.payload.aiCall ? (rep.payload.aiCall.isLocalStub ? '测试模型草稿' : 'AI 假设草稿 · 待你选择') : rep.originLabel }}</div>
                <p v-if="rep.payload && rep.payload.content">{{ rep.payload.content.conclusion }}</p>
                <div v-for="(h, i) in ((rep.payload && rep.payload.content && rep.payload.content.hypotheses) || [])" :key="i" class="rl2-row">
                  <span>H{{ i + 1 }}：{{ h.title || h }}</span>
                  <a-tag v-if="headHypothesis && headHypothesis.payload && headHypothesis.payload.reportArtifactId === rep.id && headHypothesis.payload.hypothesisIndex === i" color="green">当前选用</a-tag>
                  <a-button v-else size="small" :data-testid="'rl2-pick-h' + (i + 1)" :disabled="busy || !caps.write" @click="pickHypothesis(rep, i)">选定假设 H{{ i + 1 }}</a-button>
                </div>
                <details v-if="rep.payload && rep.payload.content"><summary>依据、反证与风险</summary><div v-for="(h, i) in rep.payload.content.hypotheses || []" :key="i"><p>{{ h.title || h }}</p><p v-if="h.evidence">依据：{{ evidenceCitationText(h.evidence) }}</p><p v-if="h.counterEvidence">反证：{{ h.counterEvidence }}</p><p v-if="h.risks">风险：{{ h.risks }}</p></div></details>
              </div>
              <div v-if="reviewCtxData" class="rl2-card" data-testid="rl2-review">
                <h3>AI 复盘输入（本分支：{{ branch.name }}）</h3>
                <p>回测 {{ reviewCtxData.runId }} · 执行 {{ reviewCtxData.executionId }} · 有效参数 {{ describe(reviewCtxData.effectiveParams) }}</p>
                <external-evidence-panel :references="reviewCtxData.evidenceReferences || []" :artifacts="artifactCache" title="这次复盘所用运行的原始依据" />
                <p v-if="reviewCtxData.summary">年化 {{ reviewCtxData.summary.annualReturnPct }}% · 最大回撤 {{ reviewCtxData.summary.maxDrawdownPct }}% · 夏普 {{ reviewCtxData.summary.sharpe }}</p>
                <p class="rl2-muted" data-testid="rl2-ai-model">模型：{{ caps.ai ? caps.ai.label : '未知' }}（仅在点击下方按钮时调用，打开/切换页面不会调用）</p>
                <a-button type="primary" :loading="aiCalling" :disabled="aiCalling || !(caps.ai && caps.ai.configured)" data-testid="rl2-review-call" @click="callReviewModel">发起复盘（将调用模型）</a-button>
                <div v-if="aiResult" class="rl2-card" data-testid="rl2-review-output">
                  <a-tag :color="aiResult.isLocalStub ? 'orange' : 'blue'" data-testid="rl2-review-output-label">{{ aiResult.isLocalStub ? '本地桩模型输出（非真实模型）' : '模型输出' }} · 绑定回测 {{ aiResult.runId }}</a-tag>
                  <pre style="white-space: pre-wrap" data-testid="rl2-review-output-text">{{ aiResult.reply }}</pre>
                  <a-button :disabled="busy || !caps.write || !reviewOk" data-testid="rl2-review-archive-model" @click="archiveModelReview">归档模型输出（标注为模型输出，绑定本次回测）</a-button>
                  <span v-if="!reviewOk" class="rl2-muted">复盘上下文已变化，此输出不能归档到当前回测</span>
                </div>
                <a-textarea v-model="ai.review" :rows="2" placeholder="复盘内容（用户提交）" data-testid="rl2-review-text" style="margin-top: 8px" />
                <a-button style="margin-top: 8px" :disabled="busy || !caps.write" data-testid="rl2-review-archive" @click="archiveReview">归档复盘（用户提交的报告内容）</a-button>
              </div>
              <div v-else class="rl2-card rl2-muted" data-testid="rl2-review-empty">本分支尚无复盘输入（复盘上下文按分支保存，切换分支不会带过去）。</div>
            </section>

            <!-- ================= INDICATOR ================= -->
            <section v-if="module === 'indicator'" data-testid="rl2-mod-indicator">
              <div class="rl2-card">
                <h3>指标 <span class="rl2-muted">研究假设：{{ headHypothesis ? headHypothesis.payload.title : '未选定' }}</span>
                  <a-tag v-if="staleOf('indicator')" color="orange" data-testid="rl2-stale-indicator">{{ staleOf('indicator') }}</a-tag></h3>
                <a-select v-model="ind.indicatorId" style="width: 260px" placeholder="选择指标" data-testid="rl2-ind-select" @change="onIndicatorChange">
                  <a-select-option v-for="it in indicators" :key="it.id" :value="it.id">{{ it.name }} #{{ it.id }}</a-select-option>
                </a-select>
                <table v-if="indRows.length" class="rl2-table" data-testid="rl2-ind-params">
                  <tr><th>参数</th><th>声明默认</th><th>已确认快照</th><th>已应用（本页）</th><th>草稿</th><th>范围</th></tr>
                  <tr v-for="r in indRows" :key="r.name">
                    <td>{{ r.name }}</td><td>{{ r.declared }}</td><td>{{ r.confirmed === null ? '—' : r.confirmed }}</td><td><b>{{ r.applied }}</b></td>
                    <td>
                      <a-select
                        v-if="parameterType(indSpec(r.name)) === 'boolean'"
                        :value="String(ind.draft[r.name])"
                        size="small"
                        style="width: 100px"
                        :data-testid="'rl2-ind-draft-' + r.name"
                        @change="v => setIndDraft(r.name, v)"><a-select-option value="true">true</a-select-option><a-select-option value="false">false</a-select-option></a-select>
                      <a-input
                        v-else
                        :value="ind.draft[r.name]"
                        size="small"
                        style="width: 100px"
                        :data-testid="'rl2-ind-draft-' + r.name"
                        @change="e => setIndDraft(r.name, e.target.value)" />
                    </td>
                    <td>{{ r.range }}</td>
                  </tr>
                </table>
                <p class="rl2-muted">草稿 ≠ 已应用 ≠ 已确认：只有“确认为指标快照”或“新建分支并应用”才写入研究记录。</p>
                <div class="rl2-actions">
                  <a-button data-testid="rl2-ind-apply" @click="applyIndDraft">应用参数（仅本页）</a-button>
                  <a-button v-if="!branch.hasDownstream" type="primary" :disabled="busy || !caps.write || !ind.indicatorId" data-testid="rl2-ind-confirm" @click="confirmIndicator">确认为指标快照</a-button>
                  <a-button v-else type="primary" :disabled="busy || !caps.write || !ind.indicatorId" data-testid="rl2-ind-branch-apply" @click="openBranchDialog({ indicatorApplied: ind.applied })">新建分支并应用</a-button>
                </div>
                <p class="rl2-parameter-summary" data-testid="rl2-applied-summary">已应用参数：{{ describe(ind.applied) }}</p>
                <div class="rl2-card" data-testid="rl2-chart-card">
                  <a-button :disabled="busy || !ind.indicatorId" data-testid="rl2-chart-compute" @click="computeChart">计算图表</a-button>
                  <a-tag v-if="chart && chartStale" color="orange" data-testid="rl2-chart-stale">图表参数已过期，请重新计算</a-tag>
                  <div v-if="chart" data-testid="rl2-chart">
                    <p class="rl2-muted" data-testid="rl2-chart-params">计算参数 {{ describe(chart.params.effective) }} · {{ chart.computedBy }} · 数据指纹 {{ chart.dataFingerprint.slice(0, 12) }}…</p>
                    <svg viewBox="0 0 600 200" width="100%" height="200" style="background: #fafafa">
                      <path
                        v-for="l in chartGeom.lines"
                        :key="l.name"
                        :d="l.d"
                        :stroke="l.color"
                        fill="none"
                        stroke-width="1.2"
                        :data-testid="'rl2-chart-line-' + l.name" />
                    </svg>
                    <span v-for="l in chartGeom.lines" :key="'lg' + l.name" :style="{ color: l.color, marginRight: '12px' }" :data-testid="'rl2-chart-last-' + l.name">{{ l.name }} 末值 {{ l.last === null ? '—' : l.last.toFixed(4) }}</span>
                  </div>
                </div>
                <draft-bar
                  :draft="drafts.indicator"
                  :dirty="indDirty"
                  :disabled="busy || !caps.write"
                  module="indicator"
                  @save="saveDraft('indicator')"
                  @clear="clearDraft('indicator')"
                  @restore="restoreDraft('indicator')" />
              </div>
              <div class="rl2-card">
                <h3>交给下一环</h3>
                <a-button type="primary" :disabled="!headIndicator || !handoffBranchReady" data-testid="rl2-ind-handoff" @click="handoffToStrategy">带已确认快照创建策略草稿</a-button>
                <p class="rl2-parameter-summary">交接参数：{{ headIndicator ? describe(headIndicator.params.effective) : '尚无快照' }}</p>
                <span class="rl2-muted">交接只准备，不调用 AI、不创建策略。</span>
              </div>
            </section>

            <!-- ================= STRATEGY ================= -->
            <section v-if="module === 'strategy'" data-testid="rl2-mod-strategy">
              <div v-if="pendingHandoff" class="rl2-banner" data-testid="rl2-handoff-pending">
                交接 {{ pendingHandoff.prepared.indicatorArtifactId }} 尚未带入。
                <a-button size="small" :disabled="!handoffBranchReady" @click="arriveHandoff(true)">查看差异并选择</a-button>
                <a-button size="small" :disabled="!handoffBranchReady" @click="dropHandoff">忽略这次交接</a-button>
              </div>
              <div class="rl2-card">
                <h3>策略工作区 <a-tag v-if="staleOf('strategy')" color="orange">{{ staleOf('strategy') }}</a-tag></h3>
                <p>来源指标快照：{{ strat.handoffArtifactId || '—' }}</p>
                <p v-if="strat.legacyMapping" class="rl2-banner" data-testid="rl2-str-legacy">这是旧映射记录/草稿，未按新版三方一致性规则核验。原记录不改写；新建策略需重新读取声明并确认每项映射。</p>
                <div class="rl2-form">
                  <span>已保存策略源码</span>
                  <a-select :value="strat.sourceId" style="width: 260px" placeholder="选择策略源码" data-testid="rl2-str-source" @change="v => setStrat('sourceId', v)">
                    <a-select-option v-for="s in sources" :key="s.id" :value="s.id">{{ s.name }} #{{ s.id }}</a-select-option>
                  </a-select>
                  <span class="rl2-muted">“生成策略草稿（将调用 AI）”走现有接口，本地隔离环境不调用。</span>
                </div>
                <p v-if="strategySchemaStatus === 'loading'" data-testid="rl2-schema-loading">正在读取该策略的真实参数声明…</p>
                <a-button v-if="strat.sourceId && strategySchemaStatus === 'idle'" data-testid="rl2-schema-reload" @click="loadStrategySchema">读取策略参数声明</a-button>
                <div v-if="strategySchemaError" class="rl2-banner rl2-banner--error" data-testid="rl2-schema-error">{{ strategySchemaError }} <a-button size="small" @click="loadStrategySchema">重新读取声明</a-button></div>
                <template v-if="strategySchemaStatus === 'ready' && strategySchema">
                  <p class="rl2-muted rl2-schema-identity" data-testid="rl2-schema-identity">源码 {{ strategySchema.codeSha256 }} · 契约 {{ strategySchema.contractHash }}。单位未知时不推定为百分比或倍数。</p>
                  <div class="rl2-param-scroll"><table class="rl2-table rl2-mapping-table" data-testid="rl2-str-param-mapping">
                    <tr><th>目标声明</th><th>已保存 / 默认</th><th>交接方式</th><th>来源与覆盖</th><th>有效值 / 差异</th></tr>
                    <tr v-for="r in mappingRows" :key="r.name">
                      <td>{{ r.name }}<div class="rl2-muted">{{ r.spec.type }} · 单位 {{ parameterUnit(r.spec) }}<br>{{ parameterRange(r.spec) }}</div></td>
                      <td>{{ parameterValueText(r.saved) }}</td>
                      <td><a-select :value="r.selection.mode" style="width: 140px" :data-testid="'rl2-map-mode-' + r.name" @change="v => setMapping(r.name, 'mode', v)"><a-select-option value="default">保留已保存 / 默认</a-select-option><a-select-option value="identity">同值映射</a-select-option><a-select-option value="manual_override">人工覆盖</a-select-option></a-select></td>
                      <td>
                        <template v-if="r.selection.mode !== 'default'">
                          <a-select
                            :value="r.selection.sourceParam || undefined"
                            allow-clear
                            style="width: 180px"
                            placeholder="明确选择来源参数"
                            :data-testid="'rl2-map-source-' + r.name"
                            @change="v => setMapping(r.name, 'sourceParam', v || null)">
                            <a-select-option v-for="p in mappingSources" :key="p.name" :value="p.name">{{ p.name }} = {{ parameterValueText(p.value) }} · {{ p.spec.type }} / {{ parameterUnit(p.spec) }}</a-select-option>
                          </a-select>
                          <p v-if="r.suggestion" class="rl2-muted">同名兼容建议：{{ r.suggestion }}（须主动选择）</p>
                          <template v-if="r.selection.mode === 'manual_override'">
                            <a-select v-if="parameterType(r.spec) === 'boolean'" :value="String(r.selection.value)" style="width: 100px" :data-testid="'rl2-map-value-' + r.name" @change="v => setMapping(r.name, 'value', v)"><a-select-option value="true">true</a-select-option><a-select-option value="false">false</a-select-option></a-select>
                            <a-select v-else-if="parameterChoices(r.spec).length" :value="r.selection.value" style="width: 130px" :data-testid="'rl2-map-value-' + r.name" @change="v => setMapping(r.name, 'value', v)"><a-select-option v-for="(v, n) in parameterChoices(r.spec)" :key="n" :value="v">{{ parameterValueText(v) }}</a-select-option></a-select>
                            <a-input v-else :value="r.selection.value" style="width: 120px" :data-testid="'rl2-map-value-' + r.name" @change="e => setMapping(r.name, 'value', e.target.value)" />
                            <a-input :value="r.selection.reason" placeholder="人工覆盖原因（必填）" :data-testid="'rl2-map-reason-' + r.name" @change="e => setMapping(r.name, 'reason', e.target.value)" />
                          </template>
                        </template>
                        <span v-else class="rl2-muted">不生成映射，不借用指标的第一项</span>
                      </td>
                      <td>{{ parameterValueText(r.value) }}<div v-if="r.selection.mode === 'manual_override'" class="rl2-muted">人工覆盖；来源原值 {{ parameterValueText(r.original) }} ≠ 自动同值映射</div></td>
                    </tr>
                  </table></div>
                  <p v-if="!mappingRows.length">该策略未声明参数（不生成虚构的 ma_period）。</p>
                  <p v-if="!mappingValidation.ok" class="rl2-banner" data-testid="rl2-map-error">{{ mappingValidation.reason }}</p>
                  <span data-testid="rl2-str-mapok"><a-checkbox :checked="strat.mappingConfirmed" :disabled="!mappingValidation.ok" @change="e => confirmMapping(e.target.checked)">我已核对每项来源、有效值与人工覆盖差异</a-checkbox></span>
                </template>
                <div class="rl2-actions">
                  <a-button type="primary" :disabled="busy || !caps.write || !strat.sourceId || !mappingReady" data-testid="rl2-str-confirm" @click="confirmStrategy">确认为策略版本（服务端核验来源与有效参数）</a-button>
                </div>
                <draft-bar
                  :draft="drafts.strategy"
                  :dirty="stratDirty"
                  :disabled="busy || !caps.write"
                  module="strategy"
                  @save="saveDraft('strategy')"
                  @clear="clearDraft('strategy')"
                  @restore="restoreDraft('strategy')" />
              </div>
            </section>

            <!-- ================= BACKTEST ================= -->
            <section v-if="module === 'backtest'" data-testid="rl2-mod-backtest">
              <div class="rl2-card">
                <h3>回测配置 <span class="rl2-muted">策略版本 {{ headStrategy ? headStrategy.id : '未确认' }}</span>
                  <a-tag v-if="staleOf('run')" color="orange" data-testid="rl2-stale-run">{{ staleOf('run') }}</a-tag></h3>
                <table v-if="headStrategy" class="rl2-table">
                  <tr><th>参数 / 类型与范围</th><th>声明默认</th><th>策略已确认</th><th>本次覆盖（与清除不同）</th><th>有效值</th></tr>
                  <tr v-for="(v, k) in headStrategy.params.effective" :key="k">
                    <td>{{ k }}<div class="rl2-muted">{{ backtestSpec(k).type || '类型未知' }} · {{ parameterUnit(backtestSpec(k)) }}<br>{{ parameterRange(backtestSpec(k)) }}</div></td><td>{{ parameterValueText(headStrategy.params.declared[k]) }}</td><td>{{ parameterValueText(v) }}</td>
                    <td>
                      <a-select
                        v-if="parameterType(backtestSpec(k)) === 'boolean'"
                        :value="hasOverride(k) ? String(bt.override[k]) : undefined"
                        style="width: 100px"
                        placeholder="不覆盖"
                        :data-testid="'rl2-bt-override-' + k"
                        @change="v => setOverride(k, v)"><a-select-option value="true">true</a-select-option><a-select-option value="false">false</a-select-option></a-select>
                      <a-select
                        v-else-if="parameterChoices(backtestSpec(k)).length"
                        :value="bt.override[k]"
                        style="width: 120px"
                        placeholder="不覆盖"
                        :data-testid="'rl2-bt-override-' + k"
                        @change="v => setOverride(k, v)"><a-select-option v-for="(c, n) in parameterChoices(backtestSpec(k))" :key="n" :value="c">{{ parameterValueText(c) }}</a-select-option></a-select>
                      <a-input
                        v-else
                        :value="bt.override[k]"
                        size="small"
                        style="width: 90px"
                        placeholder="不覆盖"
                        :data-testid="'rl2-bt-override-' + k"
                        @change="e => setOverride(k, e.target.value)" />
                      <a-button v-if="hasOverride(k)" size="small" :data-testid="'rl2-bt-clear-' + k" @click="clearOverride(k)">清除覆盖</a-button>
                    </td>
                    <td><b>{{ parameterValueText(hasOverride(k) ? bt.override[k] : v) }}</b><div v-if="hasOverride(k)" class="rl2-muted">本次运行覆盖；不改写已确认策略</div></td>
                  </tr>
                </table>
                <div class="rl2-form">
                  开始 <a-input :value="bt.startDate" style="width: 120px" data-testid="rl2-bt-start" @change="e => setBacktest('startDate', e.target.value)" />
                  结束 <a-input :value="bt.endDate" style="width: 120px" data-testid="rl2-bt-end" @change="e => setBacktest('endDate', e.target.value)" />
                  资金 <a-input-number :value="bt.initialCapital" data-testid="rl2-bt-capital" @change="v => setBacktest('initialCapital', v)" />
                  佣金% <a-input-number :value="bt.commissionPct" :step="0.01" data-testid="rl2-bt-commission" @change="v => setBacktest('commissionPct', v)" />
                  滑点% <a-input-number :value="bt.slippagePct" :step="0.01" data-testid="rl2-bt-slippage" @change="v => setBacktest('slippagePct', v)" />
                </div>
                <div class="rl2-actions">
                  <a-button type="primary" :loading="runInFlight" :disabled="busy || runInFlight || !!pendingRunKey || !headStrategy || !caps.run || !handoffBranchReady" data-testid="rl2-bt-run" @click="startBacktest(false)">开始回测（将发起计算）</a-button>
                  <a-button v-if="pendingRunKey && !runInFlight" data-testid="rl2-bt-query-key" @click="queryPendingKey">按同一幂等键查询（不会重复计算）</a-button>
                  <a-button v-if="pendingRunMissing && !runInFlight" :disabled="busy || !caps.run" data-testid="rl2-bt-resubmit-key" @click="resubmitPendingRun">未查到受理记录：用原键和原配置重新提交</a-button>
                  <span class="rl2-muted">每次点击生成新的幂等键；超时/断网只用同一键查询或对账。</span>
                </div>
              </div>
              <div v-if="currentRun" class="rl2-card" data-testid="rl2-run-card">
                <h3>回测请求 {{ currentRun.id }}
                  <a-tag v-for="(l, i) in runLabels" :key="i" :color="toneColor(l.tone)" :data-testid="'rl2-run-label-' + l.tone">{{ l.text }}</a-tag></h3>
                <div v-if="currentRun.summary" class="rl2-kpis">
                  <div><b>{{ currentRun.summary.annualReturnPct }}%</b><span>年化</span></div>
                  <div><b>{{ currentRun.summary.maxDrawdownPct }}%</b><span>最大回撤</span></div>
                  <div><b>{{ currentRun.summary.sharpe }}</b><span>夏普</span></div>
                  <div><b>{{ currentRun.summary.trades }}</b><span>交易</span></div>
                </div>
                <p class="rl2-muted">有效参数 {{ describe(currentRun.effectiveConfig.params) }} · 执行 {{ currentRun.execution && currentRun.execution.id }} · attempt {{ currentRun.execution && currentRun.execution.attempt }}</p>
                <external-evidence-panel :references="currentRun.evidenceReferences || []" :artifacts="artifactCache" title="这次运行固定的外部依据" />
                <div v-if="currentRun.lateResults && currentRun.lateResults.length" class="rl2-banner" data-testid="rl2-late">
                  <div v-for="lr in currentRun.lateResults" :key="lr.backtestRunId">
                    {{ lr.notice }}（旧回测记录 #{{ lr.backtestRunId }}，执行 {{ lr.executionId }}）
                    <a-button size="small" :disabled="busy || !caps.write" :data-testid="'rl2-late-import-' + lr.backtestRunId" @click="importLate(lr)">导入为本项目旧记录（不重跑）</a-button>
                  </div>
                </div>
                <div class="rl2-actions">
                  <a-button v-if="currentRun.actions.includes('adopt')" data-testid="rl2-run-adopt" :disabled="!caps.write" @click="adopt(currentRun)">设为当前结果</a-button>
                  <a-button v-if="currentRun.actions.includes('reconcile')" data-testid="rl2-run-reconcile" @click="reconcile(currentRun)">对账（不会重跑）</a-button>
                  <a-button v-if="currentRun.actions.includes('retry')" data-testid="rl2-run-retry" :disabled="!caps.run" @click="retry(currentRun)">在同一请求下重试</a-button>
                  <a-button v-if="currentRun.status === 'succeeded'" type="primary" data-testid="rl2-run-review" @click="toReview(currentRun)">将本次结果交给 AI 复盘</a-button>
                  <a-button v-if="currentRun.status === 'succeeded'" :disabled="busy || runInFlight || !!pendingRunKey || !caps.run || !handoffBranchReady" data-testid="rl2-run-again" @click="startBacktest(true)">用相同配置再运行一次（新请求）</a-button>
                </div>
              </div>
              <div class="rl2-card">
                <h3>本分支回测请求</h3>
                <table class="rl2-table" data-testid="rl2-run-table">
                  <tr><th>请求</th><th>状态</th><th>执行</th><th>参数</th><th>当前结果</th></tr>
                  <tr v-for="r in runs" :key="r.id" :class="currentRun && currentRun.id === r.id && 'rl2-sel'" @click="currentRun = r">
                    <td>{{ r.id.slice(0, 12) }}…</td><td>{{ r.status }}</td>
                    <td>{{ r.execution && r.execution.id.slice(0, 12) }}… #{{ r.execution && r.execution.attempt }}</td>
                    <td>{{ describe(r.effectiveConfig.params) }}</td><td>{{ r.adoption === 'adopted' ? '是' : (r.status === 'succeeded' ? '未设为当前结果' : '—') }}</td>
                  </tr>
                </table>
                <p class="rl2-muted">同一配置可以合法运行多次：每次都是独立请求与 execution_id，结果互不混用。</p>
              </div>
            </section>
          </div>

          <!-- ================= sidebar ================= -->
          <aside class="rl2-side" data-testid="rl2-side">
            <external-evidence-panel :artifact="headEvidence" title="本分支的外部研究依据" />
            <div class="rl2-card">
              <h4>本次输入 <a-tag>{{ branch.name }}</a-tag></h4>
              <p>研究假设：{{ headHypothesis ? headHypothesis.payload.title : '—' }}</p>
              <p data-testid="rl2-side-ind">指标快照：{{ headIndicator ? headIndicator.id.slice(0, 10) + '… ' + describe(headIndicator.params.effective) : '—' }}</p>
              <p>策略版本：{{ headStrategy ? headStrategy.id.slice(0, 10) + '… ' + describe(headStrategy.params.effective) : '—' }}</p>
              <p>当前结果：{{ branch.heads.run || '—' }}</p>
            </div>
            <div class="rl2-card">
              <h4>已有产物 <span class="rl2-muted">不可变快照</span></h4>
              <div v-for="a in branchArtifacts" :key="a.id" class="rl2-art">
                <small>{{ kindLabel(a.kind) }} · {{ a.originLabel }}</small>
                <div>{{ a.id.slice(0, 14) }}… {{ a.params ? describe(a.params.effective) : '' }}</div>
              </div>
            </div>
            <details class="rl2-card" data-testid="rl2-tech">
              <summary>技术详情（版本哈希 / 调用的接口）</summary>
              <pre>{{ techDetails }}</pre>
            </details>
            <a-button v-if="caps.isAdmin" size="small" data-testid="rl2-admin-switch" @click="openAdmission">V2 运行时开关</a-button>
          </aside>
        </div>
      </template>
    </template>

    <!-- branch dialog -->
    <a-modal
      :visible="branchDlg.visible"
      title="新建分支（分支、初始快照与指针在同一事务保存）"
      ok-text="新建分支"
      cancel-text="取消"
      :confirm-loading="busy"
      @ok="doCreateBranch"
      @cancel="branchDlg.visible = false">
      <div data-testid="rl2-branch-dialog">
        <a-input v-model="branchDlg.name" placeholder="分支名称" data-testid="rl2-branch-name" />
        <p style="margin-top: 8px">从当前分支继承（引用，不复制）：</p>
        <a-checkbox-group v-model="branchDlg.carry" :options="[{ label: '外部证据', value: 'evidence', disabled: !!branchDlg.indicatorApplied && !!headEvidence }, 'hypothesis', 'indicator', 'strategy', 'run', 'review']" />
        <p v-if="branchDlg.indicatorApplied" data-testid="rl2-branch-initial">同一事务内创建指标快照：{{ describe(branchDlg.indicatorApplied) }}（设为新分支当前指标）</p>
        <p v-if="branchDlg.hypothesis">同一事务内选定假设：H{{ branchDlg.hypothesis.index + 1 }}</p>
      </div>
    </a-modal>

    <!-- conflict dialog -->
    <a-modal :visible="conflict.visible" title="研究已在其他页面更新（未覆盖）" :footer="null" @cancel="conflict.visible = false">
      <div data-testid="rl2-conflict">
        <p>{{ conflict.msg }}</p>
        <table class="rl2-table">
          <tr><th>字段</th><th>你的修改</th><th>当前服务端</th></tr>
          <tr v-for="(d, i) in conflict.diff" :key="i"><td>{{ d.field }}</td><td>{{ fmt(d.yours) }}</td><td>{{ fmt(d.current !== undefined ? d.current : d.server) }}</td></tr>
        </table>
        <div class="rl2-actions">
          <a-button type="primary" data-testid="rl2-conflict-reload" @click="conflictReload">以当前为准（刷新）</a-button>
          <a-button v-if="conflict.branchable" data-testid="rl2-conflict-branch" @click="conflictToBranch">在新分支保存我的修改</a-button>
        </div>
      </div>
    </a-modal>

    <!-- handoff diff dialog -->
    <a-modal :visible="handoffDlg.visible" title="策略工作区已有未确认内容：先看差异" :footer="null" :closable="true" @cancel="keepCurrent">
      <div data-testid="rl2-handoff-diff">
        <table class="rl2-table">
          <tr><th>字段</th><th>当前草稿（保留）</th><th>交接内容（替换后）</th></tr>
          <tr v-for="d in handoffDlg.diff" :key="d.field"><td>{{ d.field }}</td><td>{{ fmt(d.current) }}</td><td>{{ fmt(d.incoming) }}</td></tr>
        </table>
        <div class="rl2-actions">
          <a-button data-testid="rl2-handoff-keep" @click="keepCurrent">保留当前草稿（不带入）</a-button>
          <a-button type="primary" data-testid="rl2-handoff-replace" @click="replaceWithHandoff">用交接内容替换当前草稿</a-button>
        </div>
      </div>
    </a-modal>

    <!-- admission dialog -->
    <a-modal
      :visible="adm.visible"
      title="V2 运行时开关（只收紧，不超过环境变量上限）"
      ok-text="保存（写审计）"
      cancel-text="取消"
      @ok="saveAdmission"
      @cancel="adm.visible = false">
      <div v-if="adm.data" data-testid="rl2-admission">
        <p>环境变量上限：{{ adm.data.ceiling }}；有效：写入 {{ adm.data.effective.writes ? '开' : '关' }} / 运行 {{ adm.data.effective.runs ? '开' : '关' }}</p>
        <a-checkbox v-model="adm.writes" data-testid="rl2-adm-writes">允许新写入</a-checkbox>
        <a-checkbox v-model="adm.runs" data-testid="rl2-adm-runs">允许新运行</a-checkbox>
        <a-input v-model="adm.reason" placeholder="原因（必填）" data-testid="rl2-adm-reason" style="margin-top: 8px" />
        <div v-for="(a, i) in adm.data.audit" :key="i" class="rl2-muted">{{ a.changedAt }} · 用户 {{ a.actorUserId }} · {{ fmt(a.after) }} · {{ a.reason }}</div>
      </div>
    </a-modal>
  </div>
</template>

<script>
import * as api from '@/api/researchLabV2'
import {
  MODULES, STALE_LABELS, classifyError, createLatestGuard, describeEffective, isStrategyWorkEmpty,
  newIdempotencyKey, paramRows, parseParamComments, runDisplay, saveStateLabel, workEquals, chartPaths, reviewArchivable,
  defaultBacktestForm, normalizeBacktestForm, researchStorageKey, pendingRunBodySummary, sameConfigRunBody
} from './workspaceLogic'
import DraftBar from './DraftBar.vue'
import ExternalEvidencePanel from '@/components/ExternalEvidencePanel'
import {
  emptyStrategyDraft, normalizeStrategyDraft, parameterType, parameterChoices, parameterUnit, parameterValueText, parameterRange,
  parseDeclaredValue, validateStrategySchema, indicatorParameterInfo, identityCompatible, defaultMappings, mappingIdentity, buildParameterMapping
} from './paramMappingLogic'

const KIND_LABEL = { external_evidence: '外部报告依据', ai_report: 'AI 报告 / 用户假设', hypothesis: '研究假设', indicator_snapshot: '指标快照', strategy_snapshot: '策略版本', run_result: '回测结果', ai_review: 'AI 复盘' }
const emptyStrat = emptyStrategyDraft
const emptyHandoffDialog = () => ({ visible: false, diff: [], incoming: null, context: null })

export default {
  name: 'ResearchLabV2',
  components: { DraftBar, ExternalEvidencePanel },
  data () {
    return {
      modules: MODULES,
      caps: {},
      capsLoaded: false,
      projects: [],
      project: null,
      branches: [],
      branch: null,
      module: 'ai',
      artifactCache: {},
      branchArtifacts: [],
      reports: [],
      indicators: [],
      sources: [],
      ind: { indicatorId: undefined, code: '', applied: {}, draft: {}, specs: [] },
      strat: emptyStrat(),
      strategySchema: null,
      strategySchemaStatus: 'idle',
      strategySchemaError: '',
      schemaRequestSeq: 0,
      bt: defaultBacktestForm(),
      drafts: { ai: null, indicator: null, strategy: null, backtest: null },
      indDirty: false,
      stratDirty: false,
      runs: [],
      currentRun: null,
      runInFlight: false,
      pendingRunKey: null,
      pendingRunBody: null,
      pendingRun: null,
      pendingRunMissing: false,
      runRequests: {},
      runActionSeq: 0,
      reviewRequestSeq: 0,
      aiCallSeq: 0,
      previewSeq: 0,
      busyRequestSeq: 0,
      workspaceUserId: null,
      reviewCtx: {},
      reviewCtxData: null,
      pendingHandoff: null,
      handoffDlg: emptyHandoffDialog(),
      handoffGeneration: 0,
      branchRefreshSeq: 0,
      handoffRequestSeq: 0,
      handoffTargetProjectId: null,
      handoffTargetBranchId: null,
      handoffBranchReady: false,
      handoffInactive: false,
      handoffViewActive: true,
      branchDlg: { visible: false, name: '', carry: ['hypothesis'], indicatorApplied: null, hypothesis: null },
      conflict: { visible: false, msg: '', diff: [], branchable: false, retryBranch: null },
      adm: { visible: false, data: null, writes: true, runs: true, reason: '' },
      ai: { conclusion: '', hypotheses: '', review: '' },
      evidenceHypothesisDrafts: {},
      evidenceHypothesisRequests: {},
      hypothesisCalling: false,
      hypothesisRequestSeq: 0,
      hypothesisModelRequests: {},
      draftRequestSeq: 0,
      aiCalling: false,
      aiResult: null,
      chart: null,
      chartAppliedKey: '',
      newProject: { name: '', symbol: '600875' },
      busy: false,
      networkIssue: '',
      lastCall: '',
      guard: createLatestGuard()
    }
  },
  computed: {
    workspaceDark () { return Boolean(this.$store && this.$store.state.app && ['dark', 'realdark'].includes(this.$store.state.app.theme)) },
    sessionIdentity () { return JSON.stringify([this.accountId(), this.accountToken()]) },
    headEvidence () { return this.art(this.branch && this.branch.heads.evidence) },
    evidenceSourceLabel () { const p = this.headEvidence && this.headEvidence.payload || {}; return p.source === 'quant/profit' || String(p.reportId).startsWith('quant:profit:') ? 'Profit ' : p.source === 'quant/etf' || String(p.reportId).startsWith('quant:etf:') ? 'ETF ' : 'AStock ' },
    evidenceHypothesisText () { return this.evidenceHypothesisDrafts[this.scopedKey('user-hypothesis')] || '' },
    headHypothesis () { return this.art(this.branch && this.branch.heads.hypothesis) },
    headIndicator () { return this.art(this.branch && this.branch.heads.indicator) },
    headStrategy () { return this.art(this.branch && this.branch.heads.strategy) },
    indRows () {
      const confirmed = this.headIndicator && this.headIndicator.payload.indicator.legacyId === this.ind.indicatorId ? this.headIndicator.params.effective : {}
      return paramRows(this.ind.specs, confirmed, this.ind.applied, this.ind.draft)
    },
    barState () {
      const m = this.module
      const dirty = (m === 'indicator' && this.indDirty) || (m === 'strategy' && this.stratDirty)
      const confirmedId = m === 'indicator' ? this.branch && this.branch.heads.indicator : (m === 'strategy' ? this.branch && this.branch.heads.strategy : null)
      return saveStateLabel({ dirty, draft: this.drafts[m], confirmedId })
    },
    chartGeom () { return chartPaths(this.chart) },
    chartStale () { return !!this.chart && this.chartAppliedKey !== this.previewIdentity() },
    reviewOk () { return reviewArchivable(this.aiResult, this.reviewCtxData) },
    runLabels () { return runDisplay(this.currentRun) || [] },
    mappingSources () {
      const info = indicatorParameterInfo(this.headIndicator)
      return info.specs.filter(s => Object.prototype.hasOwnProperty.call(info.effective, s.name)).map(spec => ({ name: spec.name, spec, value: info.effective[spec.name] }))
    },
    mappingRows () {
      if (!this.strategySchema) return []
      const sources = indicatorParameterInfo(this.headIndicator)
      return this.strategySchema.paramSchema.map(spec => {
        const selection = this.strat.mappings[spec.name] || { mode: 'default', sourceParam: null, value: this.strategySchema.params.effective[spec.name], reason: '' }
        const original = selection.sourceParam ? sources.effective[selection.sourceParam] : undefined
        const candidate = sources.specs.find(s => s.name === spec.name)
        return { name: spec.name, spec, selection, saved: this.strategySchema.params.effective[spec.name], original, value: selection.mode === 'identity' ? original : (selection.mode === 'manual_override' ? selection.value : this.strategySchema.params.effective[spec.name]), suggestion: candidate && identityCompatible(spec, candidate) ? spec.name : null }
      })
    },
    mappingValidation () { return buildParameterMapping(this.strat, this.strategySchema, this.headIndicator) },
    mappingReady () { return this.strategySchemaStatus === 'ready' && this.mappingValidation.ok && this.strat.mappingConfirmed && this.strat.confirmedMappingKey === mappingIdentity(this.strat, this.strategySchema) },
    techDetails () {
      return JSON.stringify({
        projectId: this.project && this.project.id,
        branch: this.branch && { id: this.branch.id, revision: this.branch.revision, heads: this.branch.heads },
        indicatorPayloadSha256: this.headIndicator && this.headIndicator.payloadSha256,
        strategyPayloadSha256: this.headStrategy && this.headStrategy.payloadSha256,
        run: this.currentRun && { id: this.currentRun.id, capturedSha256: this.currentRun.capturedSha256, effectiveConfigSha256: this.currentRun.effectiveConfigSha256 },
        lastCall: this.lastCall
      }, null, 2)
    }
  },
  watch: {
    sessionIdentity () { this.resetAccountWorkspace() },
    '$route.query' (query) {
      if (this.capsLoaded && this.caps.enabled && query.project && (!this.project || this.project.id !== query.project || (query.branch && (!this.branch || this.branch.id !== query.branch)))) this.openProject(query.project)
    }
  },
  async mounted () {
    this.workspaceUserId = this.accountId()
    const context = this.capturePageContext()
    try {
      const res = await api.capabilities()
      if (!this.isPageContextCurrent(context)) return
      this.caps = res.data || {}
    } catch (e) {
      if (!this.isPageContextCurrent(context)) return
      this.caps = { enabled: false }
    }
    if (!this.isPageContextCurrent(context)) return
    this.capsLoaded = true
    if (!this.caps.enabled) return
    await this.loadProjects()
    if (!this.isPageContextCurrent(context)) return
    // relogin / reload: server-side last position wins
    const pid = (this.$route.query && this.$route.query.project) || (this.projects[0] && this.projects[0].id)
    if (pid) await this.openProject(pid)
  },
  deactivated () {
    this.suspendHandoff()
  },
  beforeDestroy () {
    this.suspendHandoff()
  },
  async activated () {
    this.handoffViewActive = true
    if (this.workspaceUserId !== this.accountId()) { this.resetAccountWorkspace(); return }
    if (!this.handoffInactive || !this.capsLoaded || !this.caps.enabled || !this.project || !this.branch) return
    const page = this.capturePageContext()
    const projectId = this.project.id
    const branchId = this.branch.id
    const generation = this.handoffGeneration
    try {
      // A cached page may return after a different account logs in. Recheck
      // ownership before allowing it to touch branch-scoped sessionStorage.
      const response = await api.getBranch(branchId)
      const branch = response && response.data
      if (!this.isPageContextCurrent(page) || !this.handoffInactive || generation !== this.handoffGeneration ||
        !this.project || this.project.id !== projectId || !this.branch || this.branch.id !== branchId ||
        !branch || branch.id !== branchId || branch.projectId !== projectId) return
      this.handoffTargetProjectId = projectId
      this.handoffTargetBranchId = branchId
      this.pendingHandoff = this.readSession(this.scopedKey('handoff', branchId))
      this.handoffInactive = false
      this.handoffBranchReady = true
      this.restorePendingRun()
      if (this.strat.sourceId) await this.loadStrategySchema()
    } catch (e) {
      if (this.isPageContextCurrent(page)) this.$message.warning('分支状态未验证；请重新打开研究分支后再操作')
    }
  },
  methods: {
    accountId () {
      const store = this.$store || {}
      const info = (store.state && store.state.user && store.state.user.info) || (store.getters && store.getters.userInfo) || {}
      if (info.id === undefined || info.id === null) return null
      // The interceptor authenticates from localStorage; another tab can change
      // that account before this tab's Vuex cache updates.
      if (typeof window !== 'undefined' && window.localStorage) {
        try {
          const live = JSON.parse(window.localStorage.getItem('User-Info') || 'null')
          if (!live || String(live.id) !== String(info.id)) return null
        } catch (_) { return null }
      }
      return String(info.id)
    },
    accountToken () {
      const store = this.$store || {}
      if (typeof window !== 'undefined' && window.localStorage) {
        try {
          const live = JSON.parse(window.localStorage.getItem('Access-Token') || 'null')
          return typeof live === 'string' ? live : (live && (live.token || live.value)) || null
        } catch (_) { return null }
      }
      return (store.state && store.state.user && store.state.user.token) || (store.getters && store.getters.token) || null
    },
    capturePageContext () { return { userId: this.accountId(), authToken: this.accountToken(), generation: this.handoffGeneration } },
    isPageContextCurrent (c) {
      return !!(c && c.userId !== null && c.authToken && this.handoffViewActive && c.userId === this.accountId() && c.authToken === this.accountToken() && c.generation === this.handoffGeneration)
    },
    resetAccountWorkspace () {
      this.invalidateHandoff()
      this.guard.next()
      this.workspaceUserId = this.accountId()
      this.project = null; this.branch = null; this.projects = []; this.branches = []
      this.handoffTargetProjectId = null; this.handoffTargetBranchId = null; this.handoffBranchReady = false
      this.artifactCache = {}; this.branchArtifacts = []; this.reports = []; this.indicators = []; this.sources = []
      this.ind = this.blankInd(); this.strat = emptyStrat(); this.bt = defaultBacktestForm()
      this.drafts = { ai: null, indicator: null, strategy: null, backtest: null }
      this.indDirty = false; this.stratDirty = false; this.runs = []; this.currentRun = null
      this.pendingRun = null; this.pendingRunKey = null; this.pendingRunBody = null; this.pendingRunMissing = false; this.runRequests = {}
      this.pendingHandoff = null; this.reviewCtx = {}; this.reviewCtxData = null; this.aiResult = null
      this.ai = { conclusion: '', hypotheses: '', review: '' }; this.chart = null; this.chartAppliedKey = ''
      this.evidenceHypothesisDrafts = {}; this.evidenceHypothesisRequests = {}; this.hypothesisModelRequests = {}
      this.networkIssue = ''; this.lastCall = ''; this.conflict.visible = false; this.branchDlg.visible = false; this.adm.visible = false
      this.caps = {}; this.capsLoaded = false
      if (this.handoffViewActive && this.accountId() !== null) this.reloadAccountWorkspace()
    },
    async reloadAccountWorkspace () {
      const page = this.capturePageContext()
      const res = await this.guarded(() => api.capabilities())
      if (!this.isPageContextCurrent(page)) return
      this.caps = (res && res.data) || { enabled: false }; this.capsLoaded = true
      if (this.caps.enabled) await this.loadProjects()
    },
    scopedKey (kind, bid = this.branch && this.branch.id, pid = this.project && this.project.id, userId = this.accountId()) {
      return researchStorageKey(kind, userId, pid, bid)
    },
    readSession (key) {
      if (!key) return null
      try { return JSON.parse(sessionStorage.getItem(key) || 'null') } catch (_) { return null }
    },
    writeSession (key, value) {
      if (!key) return false
      try { sessionStorage.setItem(key, JSON.stringify(value)); return true } catch (_) { return false }
    },
    describe: describeEffective,
    parameterType,
    parameterChoices,
    parameterUnit,
    parameterValueText,
    parameterRange,
    kindLabel (k) { return KIND_LABEL[k] || k },
    fmt (v) { return v === null || v === undefined ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v)) },
    toneColor (t) { return { ok: 'green', warn: 'orange', error: 'red', info: 'blue' }[t] || '' },
    art (id) { return id ? this.artifactCache[id] || null : null },
    staleOf (k) { const s = this.branch && this.branch.stale && this.branch.stale[k]; return s ? STALE_LABELS[s] || s : '' },
    stepHint (k) {
      if (!this.branch) return ''
      if (k === 'ai') return this.headHypothesis ? '已选假设' : ''
      if (k === 'indicator') return this.headIndicator ? '快照 ' + describeEffective(this.headIndicator.params.effective) : ''
      if (k === 'strategy') return this.headStrategy ? '版本 ' + describeEffective(this.headStrategy.params.effective) : ''
      return this.branch.heads.run ? '当前结果' : '未运行'
    },
    // ---------------------------------------------------------------- error handling
    async guarded (fn, opts = {}) {
      const page = this.capturePageContext()
      const current = () => this.isPageContextCurrent(page) && (!opts.isCurrent || opts.isCurrent())
      if (!current()) return null
      const busySeq = ++this.busyRequestSeq
      this.busy = true
      try {
        const res = await fn()
        return current() ? res : null
      } catch (err) {
        if (!current()) return null
        const e = classifyError(err)
        this.lastCall = `${e.status} ${e.type} ${e.msg}`
        if (e.kind === 'network') {
          this.networkIssue = e.msg + (opts.network || '')
        } else if (e.kind === 'conflict' && e.data && (e.data.diff || e.data.server)) {
          this.conflict = { visible: true, msg: e.msg, diff: e.data.diff || [], branchable: !!opts.branchable, retryBranch: opts.retryBranch || null }
        } else if (e.type === 'UPSTREAM_LOCKED' && opts.onUpstreamLocked) {
          opts.onUpstreamLocked()
        } else {
          this.$message.error(`${e.msg}${e.type ? '（' + e.type + '）' : ''}`)
        }
        if (opts.rethrow) throw err
        return null
      } finally {
        if (this.isPageContextCurrent(page) && this.busyRequestSeq === busySeq) this.busy = false
      }
    },
    // ---------------------------------------------------------------- projects / branches
    async loadProjects () {
      const page = this.capturePageContext()
      const res = await this.guarded(() => api.listProjects())
      if (!this.isPageContextCurrent(page)) return
      this.projects = (res && res.data) || []
    },
    async doCreateProject () {
      const res = await this.guarded(() => api.createProject({ name: this.newProject.name, subject: { kind: 'single', market: 'CNStock', symbol: this.newProject.symbol, timeframe: '1D' }, startModule: 'ai' }, newIdempotencyKey()))
      if (res) await this.openProject(res.data.project.id)
    },
    async openProject (pid) {
      if (this.accountId() === null) return
      this.workspaceUserId = this.accountId()
      this.invalidateHandoff()
      this.guard.next()
      this.handoffTargetProjectId = pid
      this.handoffTargetBranchId = null
      this.handoffBranchReady = false
      const generation = this.handoffGeneration
      const page = this.capturePageContext()
      const current = () => this.isPageContextCurrent(page) && this.handoffTargetProjectId === pid
      const res = await this.guarded(() => api.getProject(pid), { isCurrent: current })
      if (!current()) return
      if (!res || this.handoffGeneration !== generation || this.handoffTargetProjectId !== pid) {
        if (!res && this.handoffGeneration === generation) {
          this.handoffTargetProjectId = this.project && this.project.id
          this.handoffTargetBranchId = this.branch && this.branch.id
          this.handoffBranchReady = !!this.branch && this.handoffViewActive && !this.handoffInactive
        }
        return
      }
      this.project = res.data.project
      if (!this.project || this.project.id !== pid) return
      this.branches = res.data.branches
      const lo = this.project.lastOpened || {}
      const query = (this.$route && this.$route.query) || {}
      const explicitBranch = query.project === pid && query.branch
      if (explicitBranch && !this.branches.some(branch => branch.id === explicitBranch && branch.projectId === pid)) {
        this.branch = null
        this.networkIssue = '所选研究分支不属于这个项目或已不可用，请从项目列表重新打开。'
        return
      }
      const bid = explicitBranch || lo.branchId || this.project.defaultBranchId
      this.module = explicitBranch && MODULES.some(m => m.key === query.module) ? query.module : (lo.module || 'ai')
      await this.loadBranch(bid)
      if (this.project && this.project.id === pid && this.workspaceUserId === this.accountId()) this.loadLegacyLists()
    },
    closeProject () {
      this.invalidateHandoff()
      this.guard.next()
      this.handoffTargetProjectId = null
      this.handoffTargetBranchId = null
      this.handoffBranchReady = false
      this.project = null
      this.branch = null
      this.pendingHandoff = null
      this.pendingRun = null; this.pendingRunKey = null; this.pendingRunBody = null; this.pendingRunMissing = false
      this.bt = defaultBacktestForm(); this.runs = []; this.currentRun = null; this.reviewCtxData = null; this.chart = null
      this.loadProjects()
    },
    async loadLegacyLists () {
      const context = this.captureBranchActionContext()
      if (!context) return
      try {
        const [i, s] = await Promise.all([api.listIndicators(), api.listScriptSources()])
        if (!this.isBranchActionContextCurrent(context)) return
        this.indicators = (i && i.data) || []
        const sd = s && s.data
        this.sources = Array.isArray(sd) ? sd : ((sd && (sd.items || sd.sources)) || [])
      } catch (e) { /* legacy lists are optional helpers */ }
    },
    async fetchArtifact (id, isCurrent = null) {
      if (!id || this.artifactCache[id]) return
      const page = this.capturePageContext()
      if (!this.isPageContextCurrent(page)) return
      const res = await api.getArtifact(id)
      if (!this.isPageContextCurrent(page) || (isCurrent && !isCurrent())) return
      this.$set(this.artifactCache, id, res.data)
    },
    async fetchEvidenceReferences (references, isCurrent) {
      const ids = [...new Set((Array.isArray(references) ? references : []).map(ref => ref.artifactId).filter(Boolean))]
      await Promise.all(ids.map(id => this.fetchArtifact(id, isCurrent).catch(() => {})))
    },
    async loadBranch (bid) {
      // out-of-order protection: only the latest branch load may commit to the page
      if (!this.project || this.accountId() === null || (this.workspaceUserId !== null && this.workspaceUserId !== this.accountId())) return
      this.workspaceUserId = this.accountId()
      this.invalidateHandoff()
      this.handoffTargetProjectId = this.project && this.project.id
      this.handoffTargetBranchId = bid
      this.handoffBranchReady = false
      const token = this.guard.next()
      const pid = this.project.id
      const page = this.capturePageContext()
      const current = () => this.isPageContextCurrent(page) && this.guard.isLatest(token) && this.project && this.project.id === pid &&
        this.handoffTargetProjectId === pid && this.handoffTargetBranchId === bid
      const res = await this.guarded(() => api.getBranch(bid), { isCurrent: current })
      if (!current()) return
      if (!res || !this.guard.isLatest(token)) {
        return
      }
      const b = res.data
      if (!b || b.id !== bid || b.projectId !== pid) { this.$message.error('分支响应身份不匹配，请重新打开项目'); return }
      await Promise.all(['evidence', 'hypothesis', 'indicator', 'strategy', 'review'].map(k => this.fetchArtifact(b.heads[k], current).catch(() => {})))
      if (!current()) return
      const loaded = await this.guarded(() => Promise.all([api.listArtifacts(pid, `?branchId=${bid}`), api.listRuns(bid)]), { isCurrent: current })
      if (!loaded || !current()) return
      const [arts, runs] = loaded
      await this.fetchEvidenceReferences((runs.data || []).filter(r => r.branchId === bid).flatMap(r => r.evidenceReferences || []), current)
      if (!current()) return
      this.branch = b
      this.pendingHandoff = null
      this.aiResult = null
      this.reviewCtxData = null
      this.chart = null
      this.chartAppliedKey = ''
      this.ind = this.blankInd(); this.strat = emptyStrat(); this.bt = defaultBacktestForm()
      this.drafts = { ai: null, indicator: null, strategy: null, backtest: null }
      this.networkIssue = ''
      this.branchArtifacts = arts.data || []
      this.runs = (runs.data || []).filter(r => r.branchId === bid)
      this.currentRun = this.runs.find(r => r.id === b.heads.run) || this.runs[0] || null
      const reports = []
      for (const a of this.branchArtifacts.filter(x => x.kind === 'ai_report')) {
        await this.fetchArtifact(a.id, current)
        if (!current()) return
        reports.push(this.artifactCache[a.id])
      }
      this.reports = reports
      await this.loadDrafts(b.id, token, current)
      if (!current()) return
      this.loadLocal()
      this.restorePendingRun()
      this.pendingHandoff = this.readSession(this.scopedKey('handoff', bid))
      if (this.handoffViewActive) {
        this.handoffInactive = false
        this.handoffBranchReady = true
      }
      if (this.strat.sourceId) await this.loadStrategySchema()
      if (!current()) return
      const savedReview = this.readSession(this.scopedKey('review', bid))
      this.reviewCtx = savedReview && savedReview.runId ? { [bid]: savedReview.runId } : {}
      this.reviewCtxData = null
      const rid = this.reviewCtx[b.id]
      if (rid) {
        const rc = await api.reviewContext(b.id, rid).catch(() => null)
        if (!current()) return
        this.reviewCtxData = rc && rc.data && rc.data.branchId === bid && rc.data.runId === rid ? rc.data : null
        if (this.reviewCtxData) await this.fetchEvidenceReferences(this.reviewCtxData.evidenceReferences, current)
      }
    },
    async switchBranch (bid) {
      await this.loadBranch(bid)
      this.saveLastOpened()
    },
    async refreshBranch () {
      if (!this.project || !this.branch || !this.handoffBranchReady || !this.handoffViewActive || this.handoffInactive) return
      const pid = this.project.id
      const bid = this.branch.id
      const generation = this.handoffGeneration
      const page = this.capturePageContext()
      const seq = ++this.branchRefreshSeq
      const current = () => this.isPageContextCurrent(page) && !this.handoffInactive && this.handoffBranchReady &&
        this.project && this.project.id === pid && this.branch && this.branch.id === bid &&
        this.handoffTargetProjectId === pid && this.handoffTargetBranchId === bid &&
        this.handoffGeneration === generation && this.branchRefreshSeq === seq
      const res = await this.guarded(() => api.getBranch(bid), { isCurrent: current })
      if (!res) return
      if (!current() || res.data.id !== bid || res.data.projectId !== pid) return
      const b = res.data
      await Promise.all(['evidence', 'hypothesis', 'indicator', 'strategy', 'review'].map(k => this.fetchArtifact(b.heads[k], current).catch(() => {})))
      if (!current()) return
      const arts = await this.guarded(() => api.listArtifacts(pid, `?branchId=${bid}`), { isCurrent: current })
      if (!arts || !current()) return
      this.branch = b
      this.branchArtifacts = arts.data || []
    },
    goModule (m) {
      this.module = m
      this.saveLastOpened()
      if (m === 'strategy' && this.pendingHandoff) this.arriveHandoff(false)
    },
    saveLastOpened () {
      const context = this.captureBranchActionContext()
      if (context) api.setLastOpened(context.projectId, { branchId: context.branchId, module: this.module }).catch(() => {})
    },
    openBranchDialog (opts = {}) {
      const requested = opts.carry || ['hypothesis']
      const carry = this.headEvidence && !requested.includes('evidence') ? ['evidence', ...requested] : [...requested]
      this.branchDlg = { visible: true, name: opts.name || (opts.indicatorApplied ? '参数 ' + describeEffective(opts.indicatorApplied) : ''), carry, indicatorApplied: opts.indicatorApplied || null, hypothesis: opts.hypothesis || null }
    },
    async doCreateBranch () {
      const context = this.captureBranchActionContext()
      if (!context) return
      const current = () => this.isBranchActionContextCurrent(context)
      const d = this.branchDlg
      const initial = []
      if (d.indicatorApplied && this.ind.indicatorId) {
        initial.push({ kind: 'indicator_snapshot', clientRequestId: newIdempotencyKey(), from: { indicatorId: this.ind.indicatorId, applied: d.indicatorApplied }, setHead: true })
      }
      if (d.hypothesis) initial.push({ kind: 'hypothesis', from: { reportArtifactId: d.hypothesis.reportId, hypothesisIndex: d.hypothesis.index }, setHead: true })
      const body = { name: d.name, parentBranchId: context.branchId, expectedParentRevision: this.branch.revision, carryHeads: d.carry, initialArtifacts: initial, clientRequestId: newIdempotencyKey() }
      const res = await this.guarded(() => api.createBranch(context.projectId, body), { isCurrent: current })
      if (!res || !current()) return
      const created = res.data && res.data.branch
      if (!created || created.projectId !== context.projectId || !created.id) { this.$message.error('新分支响应身份不匹配'); return }
      this.branchDlg.visible = false
      const p = await this.guarded(() => api.getProject(context.projectId), { isCurrent: current })
      if (!p || !current()) return
      this.branches = p.data.branches
      this.clearLocal(context.branchId, 'indicator')
      // Forking carries only explicitly selected server heads, never the parent's form overrides.
      const formKey = this.scopedKey('local-backtest', created.id, context.projectId, context.userId)
      if (formKey) sessionStorage.removeItem(formKey)
      await this.loadBranch(created.id)
      if (!this.branch || this.branch.id !== created.id || this.project.id !== context.projectId || this.accountId() !== context.userId) return
      this.saveLastOpened()
      this.$message.success(`已新建分支「${created.name}」`)
    },
    conflictReload () { this.conflict.visible = false; this.loadBranch(this.branch.id) },
    conflictToBranch () { this.conflict.visible = false; this.openBranchDialog({ indicatorApplied: this.module === 'indicator' ? this.ind.applied : null, carry: ['hypothesis'] }) },
    // ---------------------------------------------------------------- local (session) state and drafts
    localKey (bid, m) { return this.scopedKey('local-' + m, bid) },
    loadLocal () {
      const li = this.readSession(this.localKey(this.branch.id, 'indicator'))
      const hi = this.headIndicator
      if (li) {
        this.ind = li; this.indDirty = true
      } else if (this.drafts.indicator && !this.drafts.indicator.keptAfterConfirm) {
        this.ind = { ...this.blankInd(), ...this.drafts.indicator.payload }; this.indDirty = false
        this.$message.info('已从服务端恢复草稿')
      } else if (hi) {
        this.ind = { ...this.blankInd(), indicatorId: hi.payload.indicator.legacyId, code: hi.payload.code || '', applied: { ...hi.params.effective }, draft: { ...hi.params.effective }, specs: hi.payload.params.specs.map(s => ({ ...s, default: s.default })) }
        this.indDirty = false
      } else {
        this.ind = this.blankInd(); this.indDirty = false
      }
      const ls = this.readSession(this.localKey(this.branch.id, 'strategy'))
      if (ls) { this.strat = normalizeStrategyDraft(ls); this.stratDirty = true } else if (this.drafts.strategy && !this.drafts.strategy.keptAfterConfirm) { this.strat = normalizeStrategyDraft(this.drafts.strategy.payload); this.stratDirty = false } else { this.strat = this.stratFromConfirmed(); this.stratDirty = false }
      this.bt = normalizeBacktestForm(this.readSession(this.localKey(this.branch.id, 'backtest')))
    },
    blankInd () { return { indicatorId: undefined, code: '', applied: {}, draft: {}, specs: [] } },
    stratFromConfirmed () {
      const st = this.headStrategy
      if (!st) return emptyStrat()
      const ind = (st.inputs || []).find(i => i.role === 'indicator')
      const base = { ...emptyStrat(), handoffArtifactId: ind ? ind.artifactId : null, sourceId: st.payload.source.legacyId }
      if (st.payload.paramMappingVersion !== 1) return { ...base, legacyMapping: true }
      base.indicatorPayloadSha256 = ind && this.art(ind.artifactId) ? this.art(ind.artifactId).payloadSha256 : null
      for (const pm of st.payload.paramMapping || []) {
        base.mappings[pm.to] = { mode: pm.transform, sourceParam: pm.from && pm.from.startsWith('indicator.') ? pm.from.slice('indicator.'.length) : null, value: pm.value, reason: pm.reason || '' }
      }
      // A restored snapshot is immutable history, not a new mapping approval.
      return base
    },
    persistLocal (m) {
      if (!this.captureBranchActionContext()) return
      const val = m === 'indicator' ? this.ind : this.strat
      this.writeSession(this.localKey(this.branch.id, m), val)
      if (m === 'indicator') this.indDirty = true; else this.stratDirty = true
    },
    clearLocal (bid, m) {
      const key = this.localKey(bid, m)
      if (key) sessionStorage.removeItem(key)
      if (m === 'indicator') this.indDirty = false; else this.stratDirty = false
    },
    async loadDrafts (branchId = this.branch.id, token = null, isCurrent = null) {
      const loaded = {}
      for (const m of ['indicator', 'strategy']) {
        if (isCurrent && !isCurrent()) return
        let res
        try { res = await api.getDraft(branchId, m) } catch (error) {
          if ((!token || this.guard.isLatest(token)) && (!isCurrent || isCurrent())) this.$message.warning('服务端草稿读取失败，现有草稿保留，请重试。')
          return false
        }
        if ((token && !this.guard.isLatest(token)) || (isCurrent && !isCurrent())) return
        loaded[m] = res.data
      }
      for (const m of ['indicator', 'strategy']) this.$set(this.drafts, m, loaded[m])
      return true
    },
    async saveDraft (m) {
      const context = this.captureBranchActionContext()
      if (!context || this.busy || !this.caps.write || !['indicator', 'strategy'].includes(m)) return
      const payload = JSON.parse(JSON.stringify(m === 'indicator' ? this.ind : this.strat))
      const seq = ++this.draftRequestSeq
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.draftRequestSeq
      const rev = this.drafts[m] ? this.drafts[m].draftRevision : 0
      const res = await this.guarded(() => api.putDraft(context.branchId, m, { draftRevision: rev, payload, savedFrom: navigator.userAgent.includes('Chrome') ? 'Chrome' : 'Browser' }), { isCurrent: current })
      if (!res || !current()) return
      this.$set(this.drafts, m, res.data)
      if (JSON.stringify(m === 'indicator' ? this.ind : this.strat) === JSON.stringify(payload)) { this.clearLocal(context.branchId, m); this.$message.success('提交时的草稿已保存（尚未确认为快照）') } else {
        this.persistLocal(m)
        this.$message.info('提交时的版本已保存；之后的新编辑仍未保存')
      }
    },
    async clearDraft (m) {
      const context = this.captureBranchActionContext()
      if (!context || this.busy || !this.caps.write || !['indicator', 'strategy'].includes(m)) return
      const d = this.drafts[m]
      if (!d) return
      const seq = ++this.draftRequestSeq
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.draftRequestSeq && this.drafts[m] === d
      const res = await this.guarded(() => api.deleteDraft(context.branchId, m, d.draftRevision), { isCurrent: current })
      if (res !== null) { this.$set(this.drafts, m, null); this.$message.success('草稿已清除（已确认快照不受影响）') }
    },
    async restoreDraft (m) {
      const context = this.captureBranchActionContext()
      if (!context || this.busy || !['indicator', 'strategy'].includes(m)) return
      const seq = ++this.draftRequestSeq
      const before = JSON.parse(JSON.stringify(m === 'indicator' ? this.ind : this.strat))
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.draftRequestSeq
      const res = await this.guarded(() => api.getDraft(context.branchId, m), { isCurrent: current })
      if (!res || !current()) return
      if (JSON.stringify(m === 'indicator' ? this.ind : this.strat) !== JSON.stringify(before)) { this.$message.warning('读取期间已有新编辑，未覆盖当前草稿'); return }
      if (!res.data || !res.data.payload) { this.$message.warning('没有可恢复的服务端草稿；本地编辑已保留'); return }
      this.$set(this.drafts, m, res.data)
      if (m === 'indicator') { this.ind = { ...this.blankInd(), ...JSON.parse(JSON.stringify(res.data.payload)) }; this.indDirty = false } else { this.strat = normalizeStrategyDraft(res.data.payload); this.stratDirty = false }
      const localKey = this.localKey(context.branchId, m)
      if (localKey) sessionStorage.removeItem(localKey)
      if (m === 'strategy' && this.strat.sourceId && current()) await this.loadStrategySchema()
    },
    // ---------------------------------------------------------------- AI
    researchEvidenceRoute (section) { return { path: '/personal-reports', query: { section, projectId: this.project.id, branchId: this.branch.id, ...(this.currentRun ? { runId: this.currentRun.id } : {}) } } },
    evidenceCitationText (evidence) { return Array.isArray(evidence) ? evidence.map(item => item && typeof item.quote === 'string' ? `段落 ${Number(item.blockIndex) + 1}：${item.quote}` : '').filter(Boolean).join('；') : typeof evidence === 'string' ? evidence : '' },
    async generateEvidenceHypotheses () {
      const context = this.captureBranchActionContext()
      if (!context || this.hypothesisCalling || this.busy || !this.caps.write || !this.headEvidence || this.branch.hasDownstream || !(this.caps.ai && this.caps.ai.configured && this.caps.ai.evidenceHypotheses)) return
      const evidenceId = this.headEvidence.id
      const revision = this.branch.revision
      const seq = ++this.hypothesisRequestSeq
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.hypothesisRequestSeq && this.headEvidence && this.headEvidence.id === evidenceId && this.branch.revision === revision
      const key = this.scopedKey('model-hypotheses')
      const old = this.hypothesisModelRequests[key]
      const body = old && old.evidenceArtifactId === evidenceId && old.expectedRevision === revision ? old : { evidenceArtifactId: evidenceId, expectedRevision: revision, clientRequestId: newIdempotencyKey() }
      this.$set(this.hypothesisModelRequests, key, body)
      this.hypothesisCalling = true
      try {
        const response = await this.guarded(() => api.evidenceHypotheses(context.branchId, body), { isCurrent: current })
        if (!response || !current()) return
        const result = response.data
        const artifact = result && result.artifact
        if (!artifact || artifact.kind !== 'ai_report' || artifact.branchId !== context.branchId || !result.branch || result.branch.id !== context.branchId || result.branch.projectId !== context.projectId) { this.$message.error('模型草稿身份不匹配，未带入当前分支'); return }
        this.$set(this.artifactCache, artifact.id, artifact)
        if (!this.reports.some(row => row.id === artifact.id)) this.reports.push(artifact)
        this.$message.success('AI 草稿已保存，请选择一个假设继续研究')
      } finally { if (this.isBranchActionContextCurrent(context) && seq === this.hypothesisRequestSeq) this.hypothesisCalling = false }
    },
    captureBranchActionContext () {
      if (!this.project || !this.branch) return null
      const userId = this.accountId()
      if (userId === null || (this.workspaceUserId !== null && this.workspaceUserId !== userId)) return null
      if (this.workspaceUserId === null) this.workspaceUserId = userId
      const context = { ...this.capturePageContext(), projectId: this.project.id, branchId: this.branch.id }
      return this.isBranchActionContextCurrent(context) ? context : null
    },
    isBranchActionContextCurrent (context) {
      return !!(context && this.isPageContextCurrent(context) && context.userId !== null && this.workspaceUserId === context.userId && !this.handoffInactive && this.handoffBranchReady &&
        this.project && this.project.id === context.projectId && this.branch && this.branch.id === context.branchId &&
        this.handoffTargetProjectId === context.projectId && this.handoffTargetBranchId === context.branchId &&
        this.handoffGeneration === context.generation)
    },
    async archiveReport () {
      const context = this.captureBranchActionContext()
      if (!context) return
      const current = () => this.isBranchActionContextCurrent(context)
      const hyps = this.ai.hypotheses.split('\n').map(s => s.trim()).filter(Boolean).map(title => ({ title }))
      const body = { kind: 'ai_report', clientRequestId: newIdempotencyKey(), from: { content: { conclusion: this.ai.conclusion, hypotheses: hyps }, claimedModel: null } }
      const res = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current })
      if (!res || !current()) return
      await this.refreshBranch()
      if (!current()) return
      await this.fetchArtifact(res.data.artifact.id, current)
      if (current()) this.reports.push(this.artifactCache[res.data.artifact.id])
    },
    setEvidenceHypothesis (text) {
      if (!this.captureBranchActionContext()) return
      this.$set(this.evidenceHypothesisDrafts, this.scopedKey('user-hypothesis'), text)
    },
    async confirmEvidenceHypothesis () {
      const context = this.captureBranchActionContext()
      if (!context || !this.caps.write || this.branch.hasDownstream || !this.headEvidence || this.busy) return
      const title = this.evidenceHypothesisText.trim()
      if (!title || Array.from(title).length > 500) return
      const evidenceId = this.headEvidence.id
      const key = this.scopedKey('user-hypothesis')
      const previous = this.evidenceHypothesisRequests[key]
      const body = previous && previous.from.title === title && previous.from.evidenceArtifactId === evidenceId
        ? previous
        : { kind: 'hypothesis', from: { evidenceArtifactId: evidenceId, title }, setHead: true, expectedBranchRevision: this.branch.revision, clientRequestId: newIdempotencyKey() }
      this.$set(this.evidenceHypothesisRequests, key, body)
      const current = () => this.isBranchActionContextCurrent(context) && this.headEvidence && this.headEvidence.id === evidenceId && this.evidenceHypothesisText.trim() === title
      const result = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current })
      if (!result || !current() || result.data.branch.id !== context.branchId || result.data.branch.projectId !== context.projectId) return
      await this.fetchArtifact(result.data.artifact.id, current)
      if (!current()) return
      this.branch = result.data.branch
      this.$set(this.evidenceHypothesisDrafts, key, '')
      this.$set(this.evidenceHypothesisRequests, key, null)
      await this.refreshBranch()
    },
    async pickHypothesis (rep, i) {
      const context = this.captureBranchActionContext()
      if (!context) return
      const current = () => this.isBranchActionContextCurrent(context)
      const res = await this.guarded(() => api.createArtifact(context.branchId, { kind: 'hypothesis', from: { reportArtifactId: rep.id, hypothesisIndex: i }, setHead: true, expectedBranchRevision: this.branch.revision }),
        { isCurrent: current, onUpstreamLocked: () => { if (current()) this.openBranchDialog({ name: `假设 H${i + 1}`, carry: ['indicator', 'strategy', 'run', 'review'], hypothesis: { reportId: rep.id, index: i } }) }, branchable: true })
      if (!res || !current() || res.data.branch.id !== context.branchId || res.data.branch.projectId !== context.projectId) return
      await this.fetchArtifact(res.data.artifact.id, current)
      if (!current()) return
      this.branch = res.data.branch
      await this.refreshBranch()
    },
    async archiveReview () {
      const context = this.captureBranchActionContext()
      if (!context) return
      const ctx = this.reviewCtxData
      const rid = this.reviewCtx[context.branchId]
      if (!ctx || ctx.branchId !== context.branchId || ctx.runId !== rid) return
      const current = () => this.isBranchActionContextCurrent(context) && this.reviewCtxData === ctx
      const body = { kind: 'ai_review', from: { runId: rid, question: '复盘', answer: this.ai.review }, setHead: true, expectedBranchRevision: this.branch.revision }
      const res = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current })
      if (res && current()) { await this.refreshBranch(); if (current()) this.$message.success('复盘已归档（用户提交的报告内容）') }
    },
    async callReviewModel () {
      // explicit user click only: sends the server-built prompt of THIS run to the existing AI chat endpoint
      const ctx = this.reviewCtxData
      const context = this.captureBranchActionContext()
      if (!ctx || !context || ctx.branchId !== context.branchId) return
      const seq = ++this.aiCallSeq
      const current = () => this.isBranchActionContextCurrent(context) && this.reviewCtxData === ctx && seq === this.aiCallSeq
      const busyCurrent = () => this.isBranchActionContextCurrent(context) && seq === this.aiCallSeq
      const subject = { market: this.project.subject.market, symbol: this.project.subject.symbol }
      this.aiCalling = true
      try {
        const res = await this.guarded(() => api.aiChat(ctx.prompt, subject), { isCurrent: current, isBusyCurrent: busyCurrent })
        if (!current()) return
        const reply = res && res.data && res.data.reply
        if (!reply) { if (res) this.$message.error((res && res.msg) || '模型未返回内容'); return }
        const info = this.caps.ai || {}
        const call = { endpoint: '/api/ai/chat/message', sessionId: res.data.session_id, provider: info.provider, model: info.model, isLocalStub: !!info.isLocalStub }
        this.aiResult = { runId: ctx.runId, contextSha256: ctx.contextSha256, reply, prompt: ctx.prompt, isLocalStub: !!info.isLocalStub, call }
      } finally {
        if (busyCurrent()) this.aiCalling = false
      }
    },
    async archiveModelReview () {
      const r = this.aiResult
      const context = this.captureBranchActionContext()
      if (!this.reviewOk || !context) return
      const current = () => this.isBranchActionContextCurrent(context) && this.aiResult === r && this.reviewOk
      const body = { kind: 'ai_review', from: { runId: r.runId, question: r.prompt, answer: r.reply, contentSource: 'model_output_relayed_by_client', contextSha256: r.contextSha256, aiCall: r.call, claimedModel: r.call.model }, setHead: true, expectedBranchRevision: this.branch.revision }
      const res = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current })
      if (res && current()) { await this.refreshBranch(); if (current()) this.$message.success('已归档：' + res.data.artifact.originLabel) }
    },
    async computeChart () {
      const context = this.captureBranchActionContext()
      if (!context) return
      const identity = this.previewIdentity()
      const seq = ++this.previewSeq
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.previewSeq && identity === this.previewIdentity()
      const applied = { ...this.ind.applied }
      const indicatorId = this.ind.indicatorId
      const res = await this.guarded(() => api.indicatorPreview(context.branchId, { indicatorId, applied, bars: 250 }),
        { isCurrent: current, isBusyCurrent: () => this.isBranchActionContextCurrent(context) && seq === this.previewSeq })
      if (res && current()) { this.chart = res.data; this.chartAppliedKey = identity }
    },
    previewIdentity () { return JSON.stringify([this.accountId(), this.project && this.project.id, this.branch && this.branch.id, this.branch && this.branch.heads.indicator, this.ind.indicatorId, this.ind.code, this.ind.applied]) },
    async importLate (lr) {
      const context = this.captureBranchActionContext()
      if (!context) return
      const current = () => this.isBranchActionContextCurrent(context)
      const res = await this.guarded(() => api.legacyImport(context.projectId, { backtestRunId: lr.backtestRunId }), { isCurrent: current })
      if (res && current()) { await this.refreshBranch(); if (current()) this.$message.success(`已导入旧记录 #${lr.backtestRunId}（${res.data.artifact.originLabel}，未重跑）`) }
    },
    // ---------------------------------------------------------------- indicator
    onIndicatorChange (id) {
      const it = this.indicators.find(x => x.id === id) || {}
      const specs = parseParamComments(it.code || '')
      const declared = {}
      specs.forEach(s => { declared[s.name] = s.default })
      this.ind = { indicatorId: id, code: it.code || '', applied: { ...declared }, draft: { ...declared }, specs }
      this.chart = null
      this.persistLocal('indicator')
    },
    setIndDraft (name, raw) {
      const parsed = parseDeclaredValue(this.indSpec(name), raw)
      this.ind = { ...this.ind, draft: { ...this.ind.draft, [name]: parsed.ok ? parsed.value : raw } }
      this.persistLocal('indicator')
    },
    indSpec (name) { return this.ind.specs.find(s => s.name === name) || { name } },
    applyIndDraft () {
      const applied = {}
      for (const spec of this.ind.specs) {
        const parsed = parseDeclaredValue(spec, this.ind.draft[spec.name])
        if (!parsed.ok) { this.$message.error(parsed.reason); return }
        applied[spec.name] = parsed.value
      }
      this.ind = { ...this.ind, applied }
      this.persistLocal('indicator')
      this.$message.info('已应用到本页（尚未确认，不是研究记录）')
    },
    async confirmIndicator () {
      const context = this.captureBranchActionContext()
      if (!context) return
      const current = () => this.isBranchActionContextCurrent(context)
      const d = this.drafts.indicator
      const body = { kind: 'indicator_snapshot', clientRequestId: newIdempotencyKey(), from: { indicatorId: this.ind.indicatorId, applied: this.ind.applied }, setHead: true, expectedBranchRevision: this.branch.revision }
      if (d && !this.indDirty) body.draftRevision = d.draftRevision
      const res = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current, branchable: true })
      if (!res || !current() || res.data.branch.id !== context.branchId || res.data.branch.projectId !== context.projectId) return
      await this.fetchArtifact(res.data.artifact.id, current)
      if (!current()) return
      this.branch = res.data.branch
      this.clearLocal(context.branchId, 'indicator')
      await this.refreshBranch()
      if (!current()) return
      await this.loadDrafts(context.branchId, null, current)
      if (!current()) return
      this.$message.success(`已确认指标快照（${describeEffective(res.data.artifact.params.effective)}）`)
    },
    async handoffToStrategy () {
      if (!this.branch || !this.headIndicator) return
      const indicatorArtifactId = this.headIndicator.id
      const context = this.beginHandoffRequest(indicatorArtifactId)
      if (!context) return
      const current = () => this.isHandoffContextCurrent(context) && this.headIndicator && this.headIndicator.id === indicatorArtifactId
      const res = await this.guarded(() => api.prepareHandoff(context.branchId, { type: 'indicator_to_strategy', artifactId: indicatorArtifactId }), { isCurrent: current })
      if (!res || !this.isHandoffContextCurrent(context) || !this.headIndicator || this.headIndicator.id !== indicatorArtifactId) return
      this.pendingHandoff = res.data
      this.writeSession(this.scopedKey('handoff', context.branchId), res.data)
      this.goModule('strategy')
    },
    // ---------------------------------------------------------------- strategy + handoff arrival (§4.5.1)
    setStrat (k, v) {
      if (!this.captureBranchActionContext()) return
      this.strat = { ...this.strat, [k]: v, mappingConfirmed: false, confirmedMappingKey: null }
      if (k === 'sourceId') {
        this.strat = { ...this.strat, mappings: {}, legacyMapping: false }
        this.invalidateStrategySchema()
      } else if (this.strategySchemaStatus === 'loading') this.invalidateStrategySchema()
      this.persistLocal('strategy')
      if (k === 'sourceId' && v) this.loadStrategySchema()
    },
    invalidateStrategySchema () {
      this.schemaRequestSeq += 1
      this.strategySchema = null; this.strategySchemaStatus = 'idle'; this.strategySchemaError = ''
    },
    async loadStrategySchema () {
      const context = this.captureBranchActionContext()
      const sourceId = this.strat.sourceId
      if (!context || !sourceId) return
      const seq = ++this.schemaRequestSeq
      const draftIdentity = JSON.stringify(this.strat)
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.schemaRequestSeq && this.strat.sourceId === sourceId && JSON.stringify(this.strat) === draftIdentity
      this.strategySchemaStatus = 'loading'; this.strategySchemaError = ''; this.strategySchema = null
      try {
        const response = await api.strategyParamSchema(context.branchId, sourceId)
        if (!current()) return
        const schema = response && response.data
        const checked = validateStrategySchema(schema, sourceId)
        if (!checked.ok) throw new Error(checked.reason)
        this.strategySchema = schema
        this.strat = { ...this.strat, mappings: defaultMappings(schema, this.strat.mappings) }
        if (this.strat.confirmedMappingKey !== mappingIdentity(this.strat, schema)) this.strat = { ...this.strat, mappingConfirmed: false, confirmedMappingKey: null }
        this.strategySchemaStatus = 'ready'
      } catch (error) {
        if (!current()) return
        this.strategySchemaStatus = 'failed'
        this.strategySchemaError = (error.response ? classifyError(error).msg : error.message) + '；未退回猜测参数，确认策略已禁用。'
        this.strat = { ...this.strat, mappingConfirmed: false, confirmedMappingKey: null }
      }
    },
    setMapping (name, field, value) {
      if (!this.captureBranchActionContext() || this.strategySchemaStatus !== 'ready' || !this.strategySchema.paramSchema.some(s => s.name === name)) return
      const current = this.strat.mappings[name] || {}
      const row = { ...current, [field]: value }
      if (field === 'value') {
        const parsed = parseDeclaredValue(this.strategySchema.paramSchema.find(s => s.name === name), value)
        if (parsed.ok) row.value = parsed.value
      }
      if (field === 'mode' && value === 'manual_override' && current.mode === 'identity' && current.sourceParam) row.value = indicatorParameterInfo(this.headIndicator).effective[current.sourceParam]
      this.strat = { ...this.strat, mappings: { ...this.strat.mappings, [name]: row }, legacyMapping: false, mappingConfirmed: false, confirmedMappingKey: null }
      this.persistLocal('strategy')
    },
    confirmMapping (checked) {
      if (!this.captureBranchActionContext()) return
      const validity = this.mappingValidation
      if (checked && (this.strategySchemaStatus !== 'ready' || !validity.ok)) { this.$message.error(validity.reason || '声明尚未就绪'); return }
      this.strat = { ...this.strat, legacyMapping: false, mappingConfirmed: !!checked, confirmedMappingKey: checked ? mappingIdentity(this.strat, this.strategySchema) : null }
      this.persistLocal('strategy')
    },
    invalidateHandoff () {
      this.handoffGeneration += 1
      this.invalidateStrategySchema()
      this.handoffRequestSeq += 1
      this.previewSeq += 1; this.reviewRequestSeq += 1; this.aiCallSeq += 1; this.runActionSeq += 1; this.busyRequestSeq += 1
      this.draftRequestSeq += 1; this.hypothesisRequestSeq += 1
      this.busy = false; this.aiCalling = false; this.hypothesisCalling = false; this.runInFlight = false
      // Durable receipts remain queryable after navigation; in-memory request
      // slots belong only to this generation of the visible workspace.
      this.runRequests = {}
      this.handoffDlg = emptyHandoffDialog()
    },
    suspendHandoff () {
      this.invalidateHandoff()
      this.guard.next()
      this.handoffViewActive = false
      this.handoffInactive = true
      this.handoffBranchReady = false
    },
    beginHandoffRequest (indicatorArtifactId, pendingHandoff = this.pendingHandoff) {
      this.handoffRequestSeq += 1
      this.handoffDlg = emptyHandoffDialog()
      if (!this.captureBranchActionContext()) return null
      if (!this.handoffViewActive || this.handoffInactive || !this.project || !this.branch || !this.handoffBranchReady || this.handoffTargetProjectId !== this.project.id || this.handoffTargetBranchId !== this.branch.id) return null
      return {
        ...this.capturePageContext(),
        projectId: this.project.id,
        branchId: this.branch.id,
        indicatorArtifactId,
        pendingHandoff,
        generation: this.handoffGeneration,
        requestSeq: this.handoffRequestSeq,
        strategyWork: JSON.stringify(this.strat)
      }
    },
    isHandoffContextCurrent (context) {
      return !!(context && this.isPageContextCurrent(context) && context.userId !== null && this.workspaceUserId === context.userId && !this.handoffInactive && this.project && this.branch && this.handoffBranchReady &&
        this.project.id === context.projectId && this.branch.id === context.branchId &&
        this.handoffTargetProjectId === context.projectId && this.handoffTargetBranchId === context.branchId &&
        this.handoffGeneration === context.generation && this.handoffRequestSeq === context.requestSeq &&
        this.pendingHandoff === context.pendingHandoff &&
        (!context.pendingHandoff || context.pendingHandoff.prepared.indicatorArtifactId === context.indicatorArtifactId))
    },
    async arriveHandoff (forceDialog) {
      const h = this.pendingHandoff
      if (!h || !h.prepared) return
      const context = this.beginHandoffRequest(h.prepared.indicatorArtifactId, h)
      if (!context) return
      const cw = { ...this.strat }
      const current = () => this.isHandoffContextCurrent(context) && JSON.stringify(this.strat) === context.strategyWork
      const res = await this.guarded(() => api.prepareHandoff(context.branchId, { type: 'indicator_to_strategy', artifactId: context.indicatorArtifactId, currentWork: cw }), { isCurrent: current })
      if (!res || !this.isHandoffContextCurrent(context) || JSON.stringify(this.strat) !== context.strategyWork) return
      if (res.data.decision === 'apply' && !forceDialog) return this.applyIncoming(res.data.incomingWork, context)
      if (res.data.decision === 'apply' && isStrategyWorkEmpty(cw)) return this.applyIncoming(res.data.incomingWork, context)
      if (workEquals(cw, res.data.incomingWork)) return this.dropHandoff()
      this.handoffDlg = { visible: true, diff: res.data.diff || [], incoming: res.data.incomingWork, context }
    },
    applyIncoming (incoming, context) {
      if (!this.isHandoffContextCurrent(context) || JSON.stringify(this.strat) !== context.strategyWork) return
      this.strat = normalizeStrategyDraft({ ...this.strat, ...incoming, mappingConfirmed: false, confirmedMappingKey: null })
      this.invalidateStrategySchema()
      this.persistLocal('strategy')
      this.dropHandoff()
      if (this.strat.sourceId) this.loadStrategySchema()
      this.$message.success(`已收到指标交接（${incoming.handoffArtifactId}），未调用 AI、未创建策略`)
    },
    keepCurrent () {
      this.handoffRequestSeq += 1
      this.handoffDlg = emptyHandoffDialog()
      this.$message.info('已保留当前草稿，交接尚未带入')
    },
    replaceWithHandoff () {
      const dialog = this.handoffDlg
      if (!dialog.visible || !this.isHandoffContextCurrent(dialog.context)) {
        this.handoffDlg = emptyHandoffDialog()
        return
      }
      if (JSON.stringify(this.strat) !== dialog.context.strategyWork) {
        this.handoffDlg = emptyHandoffDialog()
        this.$message.info('策略草稿已变化，请重新查看交接差异')
        return
      }
      this.applyIncoming(dialog.incoming, dialog.context)
    },
    dropHandoff () {
      if (!this.handoffViewActive || this.handoffInactive || !this.handoffBranchReady || !this.project || !this.branch ||
        this.handoffTargetProjectId !== this.project.id || this.handoffTargetBranchId !== this.branch.id) return
      this.handoffRequestSeq += 1
      this.handoffDlg = emptyHandoffDialog()
      this.pendingHandoff = null
      const key = this.scopedKey('handoff')
      if (key) sessionStorage.removeItem(key)
    },
    async confirmStrategy () {
      const context = this.captureBranchActionContext()
      if (!context) return
      if (!this.mappingReady) { this.$message.error(this.mappingValidation.reason || '请重新读取声明并核对映射'); return }
      const mappingKey = mappingIdentity(this.strat, this.strategySchema)
      const current = () => this.isBranchActionContextCurrent(context) && this.mappingReady && mappingIdentity(this.strat, this.strategySchema) === mappingKey
      const validated = this.mappingValidation
      const body = {
        kind: 'strategy_snapshot',
        clientRequestId: newIdempotencyKey(),
        paramMappingVersion: 1,
        from: { sourceId: this.strat.sourceId, applied: validated.applied, expectedCodeHash: this.strategySchema.codeSha256, expectedContractHash: this.strategySchema.contractHash, indicatorArtifactId: this.strat.handoffArtifactId, expectedIndicatorPayloadSha256: this.strat.indicatorPayloadSha256 },
        paramMapping: validated.paramMapping,
        setHead: true,
        expectedBranchRevision: this.branch.revision
      }
      if (this.drafts.strategy && !this.stratDirty) body.draftRevision = this.drafts.strategy.draftRevision
      const res = await this.guarded(() => api.createArtifact(context.branchId, body), { isCurrent: current, branchable: true })
      if (!res || !current() || res.data.branch.id !== context.branchId || res.data.branch.projectId !== context.projectId) return
      await this.fetchArtifact(res.data.artifact.id, current)
      if (!current()) return
      this.branch = res.data.branch
      this.clearLocal(context.branchId, 'strategy')
      await this.refreshBranch()
      if (!current()) return
      this.$message.success(`已确认策略版本（${describeEffective(res.data.artifact.params.effective)}）`)
    },
    // ---------------------------------------------------------------- backtest
    persistBacktest () {
      if (!this.captureBranchActionContext()) return
      if (!this.writeSession(this.localKey(this.branch.id, 'backtest'), normalizeBacktestForm(this.bt))) this.$message.warning('回测表单无法保存到浏览器会话；重新打开分支将恢复默认表单')
    },
    setBacktest (k, value) {
      if (!this.captureBranchActionContext() || !Object.prototype.hasOwnProperty.call(defaultBacktestForm(), k) || k === 'override') return
      this.bt = { ...this.bt, [k]: value }
      this.persistBacktest()
    },
    setOverride (k, raw) {
      if (!this.captureBranchActionContext()) return
      const parsed = parseDeclaredValue(this.backtestSpec(k), raw)
      if (!parsed.ok) { this.$message.error(parsed.reason); return }
      this.bt = { ...this.bt, override: { ...this.bt.override, [k]: parsed.value } }
      this.persistBacktest()
    },
    backtestSpec (k) {
      const spec = ((this.headStrategy && this.headStrategy.payload.paramSchema) || []).find(s => s.name === k)
      if (!spec) return { name: k, type: 'unsupported' }
      // Legacy immutable schemas retain the compiler's minimum/maximum aliases.
      // An explicitly declared null min/max suppresses the alias, as the compiler does.
      return { ...spec, min: Object.prototype.hasOwnProperty.call(spec, 'min') ? spec.min : spec.minimum, max: Object.prototype.hasOwnProperty.call(spec, 'max') ? spec.max : spec.maximum }
    },
    hasOverride (k) { return Object.prototype.hasOwnProperty.call(this.bt.override, k) },
    clearOverride (k) {
      if (!this.captureBranchActionContext()) return
      const override = { ...this.bt.override }; delete override[k]
      this.bt = { ...this.bt, override }; this.persistBacktest()
    },
    runBody () {
      const po = {}
      Object.keys(this.bt.override).forEach(k => {
        const parsed = parseDeclaredValue(this.backtestSpec(k), this.bt.override[k])
        if (!parsed.ok) throw new Error(parsed.reason)
        po[k] = parsed.value
      })
      return { strategyArtifactId: this.headStrategy.id, config: { startDate: this.bt.startDate, endDate: this.bt.endDate, initialCapital: this.bt.initialCapital, commissionPct: this.bt.commissionPct, slippagePct: this.bt.slippagePct, leverage: 1, paramsOverride: po }, expectedBranchRevision: this.branch.revision }
    },
    rerunBody (run) { return sameConfigRunBody(run, this.branch.id, this.branch.revision) },
    restorePendingRun () {
      const pending = this.readSession(this.scopedKey('pendingRun'))
      const own = pending && pending.userId === this.accountId() && pending.projectId === this.project.id && pending.branchId === this.branch.id && typeof pending.key === 'string' && pending.key.length > 0
      this.pendingRun = own ? pending : null
      this.pendingRunMissing = false
      this.pendingRunKey = own ? pending.key : null
      this.pendingRunBody = own && pending.body && pending.bodySummary === pendingRunBodySummary(pending.body) ? pending.body : null
      this.runInFlight = !!this.runRequests[this.scopedKey('pendingRun')]
      if (own) this.networkIssue = this.pendingRunBody ? '有原请求等待查询：请按同一幂等键查询受理状态。' : '原请求正文不完整，请仅查询原键；无法重新提交。'
    },
    clearPendingRun (pending, context) {
      if (!this.isBranchActionContextCurrent(context)) return
      const key = this.scopedKey('pendingRun', pending.branchId, pending.projectId, pending.userId)
      const saved = this.readSession(key)
      if (saved && saved.key === pending.key && key) sessionStorage.removeItem(key)
      if (this.pendingRun && this.pendingRun.key === pending.key) {
        this.pendingRun = null; this.pendingRunKey = null; this.pendingRunBody = null; this.pendingRunMissing = false
      }
    },
    beginRunAction (runId = null) {
      const context = this.captureBranchActionContext()
      return context ? { ...context, runId, requestSeq: ++this.runActionSeq } : null
    },
    isRunActionCurrent (context) { return this.isBranchActionContextCurrent(context) && context.requestSeq === this.runActionSeq },
    async startBacktest (again) {
      if (this.runInFlight || this.pendingRunKey) return
      const context = this.beginRunAction(again && this.currentRun ? this.currentRun.id : null)
      if (!context) return
      if (again && this.runs.some(r => r.status === 'running')) {
        this.$message.warning('仍有执行中的请求：请先对账，再用相同配置运行新请求')
        return
      }
      const key = newIdempotencyKey() // generated once per explicit click
      let body
      try { body = JSON.parse(JSON.stringify(again ? this.rerunBody(this.currentRun) : this.runBody())) } catch (error) { this.$message.error(error.message); return }
      const pending = { version: 1, userId: context.userId, projectId: context.projectId, branchId: context.branchId, key, body, bodySummary: pendingRunBodySummary(body) }
      if (!this.writeSession(this.scopedKey('pendingRun'), pending)) { this.$message.error('无法保存原请求身份，未发起回测；请启用浏览器会话存储后重试'); return }
      this.persistBacktest()
      this.pendingRun = pending
      this.pendingRunKey = key
      this.pendingRunBody = body
      await this.submitPendingRun(pending, context)
    },
    async submitPendingRun (pending, context) {
      if (!pending) return
      const { key, body } = pending
      const current = () => this.isRunActionCurrent(context)
      if (!current() || pending.userId !== context.userId || pending.projectId !== context.projectId || pending.branchId !== context.branchId || !body || pending.bodySummary !== pendingRunBodySummary(body)) return
      const requestKey = this.scopedKey('pendingRun', context.branchId, context.projectId, context.userId)
      if (!this.writeSession(requestKey, pending)) { this.$message.error('无法保存原请求身份，未重新提交'); return }
      this.pendingRunMissing = false
      this.$set(this.runRequests, requestKey, key)
      this.runInFlight = true
      try {
        const res = await api.startRun(context.branchId, body, key)
        if (!current()) return
        if (!res || !res.data || !res.data.run || !res.data.run.id || res.data.run.branchId !== context.branchId) throw new Error('运行响应身份不匹配，受理状态待查询')
        this.lastCall = `POST /runs ${key}`
        this.clearPendingRun(pending, context)
        this.networkIssue = ''
        await this.afterRun(res.data.run, context)
      } catch (err) {
        if (!current()) return
        const e = classifyError(err)
        if (e.kind === 'network' || e.status >= 500) {
          this.networkIssue = '回测是否已被受理未知。请保留原键和原配置，并按同一幂等键查询确认。'
        } else {
          this.clearPendingRun(pending, context)
          if (e.kind === 'conflict' && e.type === 'REVISION_CONFLICT') {
            this.conflict = { visible: true, msg: e.msg + '（未发起回测）', diff: [{ field: 'branch.revision', yours: body.expectedBranchRevision, current: e.data && e.data.currentRevision }], branchable: false }
          } else {
            this.$message.error(`${e.msg}（${e.type}）`)
          }
        }
      } finally {
        if (current() && this.runRequests[requestKey] === key) { this.$set(this.runRequests, requestKey, null); this.runInFlight = false }
      }
    },
    async queryPendingKey () {
      if (this.runInFlight) return
      const context = this.beginRunAction()
      const pending = this.pendingRun
      if (!context || !pending || pending.userId !== context.userId || pending.branchId !== context.branchId || pending.projectId !== context.projectId) return
      const current = () => this.isRunActionCurrent(context) && this.pendingRun && this.pendingRun.key === pending.key
      const res = await this.guarded(async () => {
        try { return await api.findRunByKey(pending.key) } catch (err) {
          if (classifyError(err).status !== 404) throw err
          if (current()) {
            this.pendingRunMissing = true
            this.networkIssue = '未查到受理记录。可稍后再查，或明确点击“用原键和原配置重新提交”。'
          }
          return null
        }
      }, { isCurrent: current, network: '（仍无法连接，原请求身份已保留）' })
      if (!res || !current()) return
      const run = res.data && (res.data.run || res.data)
      if (!run || !run.id || run.branchId !== context.branchId) { this.networkIssue = '查询响应身份不匹配，原请求身份已保留；请稍后再查。'; return }
      this.clearPendingRun(pending, context)
      this.networkIssue = ''
      await this.afterRun(run, context)
    },
    async resubmitPendingRun () {
      if (this.runInFlight || !this.pendingRunMissing || !this.pendingRunBody) return
      const context = this.beginRunAction()
      if (!context) return
      await this.submitPendingRun(this.pendingRun, context)
    },
    async afterRun (run, context = this.beginRunAction(run && run.id)) {
      const current = () => this.isRunActionCurrent(context)
      if (!context || !run || run.branchId !== context.branchId || !current()) return
      this.currentRun = run
      const runs = await this.guarded(() => api.listRuns(context.branchId), { isCurrent: current })
      if (!runs || !current()) return
      this.runs = (runs.data || []).filter(r => r.branchId === context.branchId)
      this.currentRun = this.runs.find(r => r.id === run.id) || run
      await this.fetchEvidenceReferences(this.currentRun.evidenceReferences, current)
      if (!current()) return
      await this.refreshBranch()
      if (!current()) return
      if (run.replayed) this.$message.info('按幂等键返回既有请求（未重复计算）')
    },
    async adopt (run) {
      if (this.runInFlight || this.pendingRunKey) return
      const context = this.beginRunAction(run.id)
      if (!context || run.branchId !== context.branchId) return
      const res = await this.guarded(() => api.adoptRun(run.id, this.branch.revision), { isCurrent: () => this.isRunActionCurrent(context) })
      if (res) await this.afterRun(res.data.run, context)
    },
    async reconcile (run) {
      if (this.runInFlight || this.pendingRunKey) return
      const context = this.beginRunAction(run.id)
      if (!context || run.branchId !== context.branchId) return
      const current = () => this.isRunActionCurrent(context)
      const res = await this.guarded(() => api.reconcileRun(run.id), { isCurrent: current })
      if (!res) return
      await this.afterRun(res.data.run, context)
      if (!current() || !res.data.run || res.data.run.branchId !== context.branchId) return
      if (res.data.run.displayState) this.currentRun = { ...this.currentRun, displayState: res.data.run.displayState }
      this.$message.info('对账结果：' + res.data.run.reconcile.outcome)
    },
    async retry (run) {
      if (this.runInFlight || this.pendingRunKey) return
      const context = this.beginRunAction(run.id)
      if (!context || run.branchId !== context.branchId) return
      const res = await this.guarded(() => api.retryRun(run.id), { isCurrent: () => this.isRunActionCurrent(context) })
      if (res) await this.afterRun(res.data.run, context)
    },
    async toReview (run) {
      const context = this.captureBranchActionContext()
      if (!context || run.branchId !== context.branchId) return
      const seq = ++this.reviewRequestSeq
      const current = () => this.isBranchActionContextCurrent(context) && seq === this.reviewRequestSeq
      const res = await this.guarded(() => api.reviewContext(context.branchId, run.id), { isCurrent: current })
      if (!res || !current()) return
      if (!res.data || res.data.runId !== run.id || res.data.branchId !== context.branchId) { this.$message.error('复盘上下文身份不匹配，未载入'); return }
      await this.fetchEvidenceReferences(res.data.evidenceReferences, current)
      if (!current()) return
      this.reviewCtx = { ...this.reviewCtx, [context.branchId]: run.id }
      this.writeSession(this.scopedKey('review', context.branchId), { runId: run.id })
      this.reviewCtxData = res.data
      this.aiCallSeq += 1; this.aiCalling = false; this.aiResult = null
      this.goModule('ai')
    },
    // ---------------------------------------------------------------- admin switch
    async openAdmission () {
      const res = await this.guarded(() => api.getAdmission())
      if (!res) return
      this.adm = { visible: true, data: res.data, writes: res.data.stored.writes, runs: res.data.stored.runs, reason: '' }
    },
    async saveAdmission () {
      const page = this.capturePageContext()
      const res = await this.guarded(() => api.putAdmission({ writesEnabled: this.adm.writes, runsEnabled: this.adm.runs, expectedRevision: this.adm.data.revision, reason: this.adm.reason }))
      if (res && this.isPageContextCurrent(page)) {
        this.adm.visible = false
        const c = await api.capabilities()
        if (this.isPageContextCurrent(page)) this.caps = c.data
      }
    }
  }
}
</script>

<style lang="less" scoped>
.rl2 { padding: 12px 16px; font-size: 13px; }
.rl2-card { background: #fff; border: 1px solid #eee; border-radius: 6px; padding: 12px 14px; margin-bottom: 12px; }
.rl2-banner { background: #fff7e6; border: 1px solid #ffd591; padding: 8px 12px; border-radius: 6px; margin-bottom: 10px; }
.rl2-banner--error { background: #fff1f0; border-color: #ffa39e; }
.rl2-bar { background: #fff; border: 1px solid #eee; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; }
.rl2-bar-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.rl2-title { font-size: 15px; }
.rl2-evidence-links { display: flex; flex-wrap: wrap; gap: 10px 24px; padding: 12px 0; font-size: 12px; }
.rl2-tag { border: 1px solid #ddd; border-radius: 4px; padding: 0 6px; font-size: 12px; }
.rl2-state { font-weight: 600; }
.rl2-state--warn { color: #d46b08; }
.rl2-state--ok { color: #389e0d; }
.rl2-state--info { color: #1677ff; }
.rl2-state--muted, .rl2-muted { color: #999; }
.rl2-steps { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.rl2-step { border: 1px solid #ddd; border-radius: 16px; padding: 3px 12px; color: #333; }
.rl2-step--on { border-color: #52c41a; color: #389e0d; font-weight: 600; }
.rl2-step-no { display: inline-block; width: 18px; }
.rl2-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 12px; }
.rl2-main, .rl2-side { min-width: 0; }
@media (max-width: 900px) { .rl2-grid { grid-template-columns: minmax(0, 1fr); } }
.rl2-table { width: 100%; margin: 8px 0; border-collapse: collapse; }
.rl2-table th, .rl2-table td { border-bottom: 1px solid #f0f0f0; padding: 4px 6px; text-align: left; }
.rl2-param-scroll { width: 100%; min-width: 0; max-width: 100%; overflow-x: auto; }
.rl2-schema-identity { overflow-wrap: anywhere; }
.rl2-mapping-table { min-width: 760px; }
.rl2-sel { background: #f6ffed; }
.rl2-row { display: flex; gap: 10px; align-items: center; padding: 4px 0; }
.rl2-form { display: flex; gap: 8px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
.rl2-actions { display: flex; gap: 8px; margin: 8px 0; flex-wrap: wrap; align-items: center; }
.rl2-parameter-summary { padding: 10px 12px; background: #f3f7f4; color: #4b6555; border-radius: 6px; white-space: normal; overflow-wrap: anywhere; }
.rl2-dark { color: #e3ede7; }
.rl2-dark .rl2-card, .rl2-dark .rl2-bar { background: #1e2923; border-color: #42574a; }
.rl2-dark h3, .rl2-dark h4, .rl2-dark strong { color: #e3ede7; }
.rl2-dark .rl2-muted, .rl2-dark .rl2-state--muted { color: #b3c4b9; }
.rl2-dark .rl2-step, .rl2-dark .rl2-tag, .rl2-dark .rl2-table th, .rl2-dark .rl2-table td { color: #e3ede7; border-color: #42574a; }
.rl2-dark .rl2-step--on { color: #9ce4b8; border-color: #9ce4b8; }
.rl2-dark .rl2-parameter-summary { color: #bdd5c6; background: #28372e; }
.rl2-dark .rl2-banner { background: #403522; color: #f3cf94; border-color: #7b6135; }
.rl2-dark .rl2-banner--error { background: #412a29; color: #ffb2ab; border-color: #82504b; }
.rl2-dark /deep/ .ant-input, .rl2-dark /deep/ .ant-select-selection, .rl2-dark /deep/ .ant-btn:not(.ant-btn-primary) { background: #28372e; color: #e3ede7; border-color: #42574a; }
.rl2-dark /deep/ .ant-input::placeholder { color: #b3c4b9; }
.rl2-kpis { display: flex; gap: 10px; div { border: 1px solid #eee; padding: 6px 12px; border-radius: 4px; b { display: block; font-size: 16px; } } }
.rl2-art { border: 1px solid #f0f0f0; border-radius: 4px; padding: 4px 6px; margin-bottom: 4px; }
.rl2-side h4 { margin: 0 0 6px; }
.rl2-side pre { font-size: 11px; white-space: pre-wrap; word-break: break-all; }
.rl2-back { margin-right: 4px; }
</style>
