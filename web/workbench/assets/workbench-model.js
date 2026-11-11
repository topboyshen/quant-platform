/* Pure adapter for the read-only workbench. Freshness belongs to the existing
 * NNG8 core; this file neither replaces that policy nor infers confirmations.
 * Strings remain inert data. The renderer must use textContent for all of them.
 */
"use strict";

(function (global, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory;
  else global.NNG8WorkbenchModel = factory(global.NNG8);
})(typeof window === "undefined" ? globalThis : window, function (core) {
  if (!core || typeof core.recomputeFreshness !== "function"
      || typeof core.reportFreshnessPolicy !== "function" || typeof core.parseInstant !== "function") {
    throw new TypeError("NNG8 freshness core is required");
  }

  const LAYERS = { seal: "本地密封", telegram: "Telegram", relay: "接收端 relay" };
  const STATES = new Set(["pass", "fail", "stopped", "disabled", "pending", "insufficient"]);
  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value) => typeof value === "string" ? value : null;
  const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const count = (value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
  const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value) ? value : null;
  const FULL_CONTRACT = "research.full-snapshot.v1";
  const CORE_CONTRACT = "research.core-market.v1";
  const CORE_LABELS = {
    unresolved_core_market_gaps: "核心行情合同缺口",
    scheduled_core_market_missing: "核心行情应运行未运行",
    last_core_market_not_successful: "核心行情最近运行未成功",
    core_market_run_missing_or_mismatched: "核心行情运行与目标记录不一致",
    core_market_evidence_unavailable: "核心行情合同证据未读取",
    core_market_material_warning: "核心行情附加材料提示",
    legacy_full_rows_after_cutover: "切换后出现旧完整快照记录",
    core_market_outside_known_trading_calendar: "核心行情日期不在已知交易日历内",
    core_calendar_schedule_conflict: "核心市场闭市声明与交易日历冲突",
  };
  const SNAPSHOT_MARKET_ENDPOINTS = new Set([
    "stock_board_industry_summary_ths", "stock_market_activity_legu",
  ]);
  const SNAPSHOT_FAILURE_TYPES = new Set([
    "ValueError", "RuntimeError", "TypeError", "KeyError", "IndexError",
    "OSError", "ConnectionError", "TimeoutError", "HTTPError", "SSLError",
    "JSONDecodeError", "ParserError", "OtherError",
  ]);
  const SNAPSHOT_RUN_STATUSES = new Set([
    "deferred", "rejected", "failed", "temporary_failure", "usage_error",
    "inserted", "skipped_existing", "idempotent", "success",
  ]);

  function snapshotFailureEvidence(failure) {
    if (!isObject(failure) || failure.provider !== "akshare"
        || !Number.isSafeInteger(failure.attemptCount) || failure.attemptCount < 1
        || failure.attemptCount > 8 || !Array.isArray(failure.attempts)
        || failure.attempts.length !== failure.attemptCount) return null;
    const attempts = [];
    for (const [index, item] of failure.attempts.entries()) {
      if (!isObject(item) || item.attempt !== index + 1
          || (item.endpoint !== undefined && !SNAPSHOT_MARKET_ENDPOINTS.has(item.endpoint))
          || typeof item.errorType !== "string"
          || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(item.errorType)) return null;
      attempts.push({ attempt: index + 1,
        ...(item.endpoint === undefined ? {} : { endpoint: item.endpoint }),
        errorType: SNAPSHOT_FAILURE_TYPES.has(item.errorType) ? item.errorType : "OtherError" });
    }
    return { provider: "akshare", attemptCount: failure.attemptCount, attempts };
  }

  function snapshotFailureDescription(failure, label) {
    if (failure === null) return "";
    const attempts = failure.attempts.map((item) =>
      `第${item.attempt}次 ${item.endpoint || "端点未记录"}（${item.errorType}）`);
    return `；${label} ${failure.attemptCount} 次：${attempts.join("、")}`;
  }

  function date(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value ? value : null;
  }

  function instant(value) {
    return typeof value === "string" && core.parseInstant(value) !== null ? value : null;
  }

  // Export inputs are JSON. Make independent JSON-compatible evidence copies,
  // including hostile strings and keys, without assigning prototype setters.
  function copy(value, parents = new Set()) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value !== "object" || parents.has(value)) return null;
    const next = new Set(parents);
    next.add(value);
    if (Array.isArray(value)) return value.map((item) => copy(item, next));
    return Object.fromEntries(Object.keys(value).map((key) => [key, copy(value[key], next)]));
  }

  function same(value) {
    if (Array.isArray(value)) return `[${value.map(same).join(",")}]`;
    if (isObject(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${same(value[key])}`).join(",")}}`;
    return JSON.stringify(value);
  }

  function unique(values) {
    const seen = new Set();
    return values.filter((value) => {
      const key = same(value);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function metric(numerator, denominator, label) {
    const n = count(numerator), d = count(denominator);
    const percent = n !== null && d !== null && d > 0 && n <= d ? n / d * 100 : null;
    return { numerator: n, denominator: d, percent,
      label: percent === null ? "未知" : `${Number(percent.toFixed(2))}%`, description: label };
  }

  function auditedContractView(progress, base, error) {
    const raw = progress.activeRunDecision;
    const coreContract = base.activeContract === CORE_CONTRACT;
    const aware = value => !!instant(value) && /(Z|[+-]\d\d:\d\d)$/i.test(value);
    const nullable = (value, check) => value === null || check(value);
    const sameInstant = (left, right) => left === null && right === null
      || aware(left) && aware(right) && Date.parse(left) === Date.parse(right);
    const codes = values => Array.isArray(values) && values.every(x => typeof x === "string" && /^[A-Za-z0-9_:-]{1,150}$/.test(x));
    const run = raw?.run, artifact = raw?.artifact, verification = raw?.verification, calendar = raw?.calendar;
    const last = progress.activeLastRun;
    const q = artifact?.quality;
    // Transport/schema and same-object identity only. The audit owns calendar,
    // coverage and scheduled-run business decisions; do not recalculate them here.
    let valid = isObject(raw) && raw.schema === "research.active-run-decision.v1"
      && raw.contract === base.activeContract && raw.scope === (coreContract ? "core_market" : "full_snapshot")
      && raw.fullSnapshot === !coreContract && raw.artifactKind === (coreContract ? "qlib_market_summary" : "full_snapshot")
      && date(raw.scheduledDate) && aware(raw.evaluatedAt) && sameInstant(raw.evaluatedAt, progress.sourceTime)
      && new Date(Date.parse(raw.evaluatedAt)+8*3600000).toISOString().slice(0,10) === raw.scheduledDate
      && aware(raw.scheduledFor) && aware(raw.deadlineAt) && Date.parse(raw.scheduledFor) < Date.parse(raw.deadlineAt)
      && [raw.scheduledFor,raw.deadlineAt].every(value => new Date(Date.parse(value)+8*3600000).toISOString().slice(0,10) === raw.scheduledDate)
      && nullable(raw.expectedTargetDate, date)
      && isObject(verification) && verification.scope === "local_run_artifact_calendar_identity"
      && ["pass", "fail", "insufficient", "pending", "not_required"].includes(verification.status)
      && ["reasons", "currentIssueCodes", "materialWarningCodes", "historicalIssueCodes"].every(k => codes(verification[k]))
      && isObject(run) && isObject(last)
      && nullable(run.runId, x => typeof x === "string" && /^[a-f0-9]{32}$/.test(x))
      && typeof run.status === "string" && /^[a-z_]{1,60}$/.test(run.status)
      && nullable(run.targetDate, date) && nullable(run.startedAt, aware) && nullable(run.completedAt, aware)
      && run.runId === last.runId && run.status === last.status && run.targetDate === last.targetDate
      && sameInstant(run.startedAt, last.startedAt) && sameInstant(run.completedAt, last.timestamp)
      && isObject(artifact) && nullable(artifact.payloadSha256, hash)
      && artifact.payloadSha256 === last.payloadSha256 && ["valid", "invalid", "missing"].includes(artifact.rowState)
      && (q === null || coreContract && isObject(q) && q.scope === "sealed_core_qlib_summary"
        && ["partial", "complete"].includes(q.priceStatus) && date(q.targetDate) === run.targetDate
        && hash(q.payloadSha256) && q.payloadSha256 === artifact.payloadSha256
        && [q.valid,q.invalid,q.eligible].every(x => count(x) !== null)
        && typeof q.ratio === "number" && Number.isFinite(q.ratio)
        && typeof q.generationTag === "string" && /^[A-Za-z0-9_.-]{1,100}$/.test(q.generationTag))
      && (calendar === null || isObject(calendar) && ["open", "closed"].includes(calendar.status)
        && date(calendar.targetDate) === raw.scheduledDate
        && [calendar.previousTradingDate,calendar.nextTradingDate,calendar.sourceDate].every(date)
        && hash(calendar.evidenceSha256) && hash(calendar.sourceEvidenceSha256) && aware(calendar.observedAt)
        && ["sealed_archive", "validated_terminal_context", "validated_non_trading_skip", "validated_closed_interval"].includes(calendar.proofKind));
    const core = progress.coreMarket;
    if (coreContract) valid = valid && isObject(core) && core.contract === base.activeContract
      && core.fullSnapshot === false && core.artifactKind === "qlib_market_summary"
      && core.includesCompletePriceArchive === false && core.status === verification.status;
    base.runDecision = { status: valid ? verification.status : "insufficient",
      scheduledDate: valid ? raw.scheduledDate : null, runId: valid ? run.runId : null,
      runStatus: valid ? run.status : null, targetDate: valid ? run.targetDate : null,
      startedAt: valid ? run.startedAt : null, completedAt: valid ? run.completedAt : null,
      materialWarningCodes: valid ? [...verification.materialWarningCodes] : [],
      detail: valid ? copy(raw) : null };
    if (coreContract) {
      const coreLedger = Array.isArray(core?.ledger) ? core.ledger : [];
      const coreLatest = date(core?.latestValidDate);
      const recentMissingDates = coreLatest
        ? coreLedger.filter(row => isObject(row) && date(row.date)
          && row.state === "missing" && row.pendingWindow !== true
          && row.date > coreLatest).map(row => row.date)
        : [];
      base.coreMarket = { status: base.runDecision.status,
      runStatus: base.runDecision.runStatus, runAt: base.runDecision.completedAt,
      targetDate: base.runDecision.targetDate,
      calendarTargetDate: valid ? date(calendar?.targetDate) : null,
      previousTradingDate: valid ? date(calendar?.previousTradingDate) : null,
      stageTradeDate: date(last?.stageTradeDate), usedStageTradeDate: date(last?.usedStageTradeDate),
      latestValidDate: valid ? date(core?.latestValidDate) : null, quality: valid ? copy(q) : null,
      recentMissingDates: valid ? recentMissingDates : [],
      coverage: metric(valid ? core.validDates : null, valid ? core.knownDates : null, "核心行情合同有效日期（非完整快照）"),
      detail: valid ? { auditedRun: copy(raw), coreMarket: copy(core) } : null };
    }
    else if (core?.historicalOnly === true) {
      const historyValid = isObject(core) && core.contract === CORE_CONTRACT
        && core.fullSnapshot === false && core.artifactKind === "qlib_market_summary"
        && core.includesCompletePriceArchive === false && Array.isArray(core.ledger)
        && core.ledger.every(row => isObject(row) && date(row.date)
          && row.contract === CORE_CONTRACT && row.fullSnapshot === false
          && ["valid", "invalid", "missing"].includes(row.state) && typeof row.pendingWindow === "boolean"
          && (row.payloadSha256 === null || hash(row.payloadSha256)));
      // Historical observations cannot certify the current or another core run.
      const histLedger = Array.isArray(core?.ledger) ? core.ledger : [];
      const histLatest = historyValid ? date(core.latestValidDate) : null;
      const histMissing = histLatest
        ? histLedger.filter(row => isObject(row) && date(row.date)
          && row.state === "missing" && row.pendingWindow !== true
          && row.date > histLatest).map(row => row.date)
        : [];
      base.coreMarket = { historicalOnly: true, status: "insufficient", quality: null,
        runStatus: historyValid ? text(core.lastRun?.status) : null,
        runAt: historyValid ? instant(core.lastRun?.timestamp) : null,
        targetDate: historyValid ? date(core.lastRun?.targetDate) : null,
        latestValidDate: histLatest,
        recentMissingDates: histMissing,
        coverage: metric(historyValid ? core.validDates : null, historyValid ? core.knownDates : null,
          "历史核心行情合同有效日期（非完整快照）"),
        detail: historyValid ? copy(core) : null };
      if (!historyValid) error("progress.json", "core_history_projection_invalid", "历史核心行情投影不完整，不能以本次完整快照成功替代。", null);
    }
    if (!valid) error("progress.json", "active_run_decision_invalid", "本次审计运行结论未读取到或身份不一致，不回退旧成功记录。", null);
    return base;
  }

  function contractView(progress, error) {
    const active = progress?.activeContract === undefined ? FULL_CONTRACT : progress.activeContract;
    const base = { activeContract: active, effectiveTradeDate: date(progress?.effectiveTradeDate),
      label: active === CORE_CONTRACT ? "核心市场摘要（非完整快照）" : active === FULL_CONTRACT ? "完整快照" : "合同未确认",
      fullSnapshot: active === FULL_CONTRACT, coreMarket: null, runDecision: null, contractIntervals: null };
    if (has(progress || {}, "contractIntervals")) {
      const intervals = progress.contractIntervals;
      if (Array.isArray(intervals) && intervals.length <= 16 && intervals.every(row => isObject(row)
          && [CORE_CONTRACT, FULL_CONTRACT].includes(row.activeContract) && date(row.fromInclusive)
          && (row.untilExclusive === null || date(row.untilExclusive))))
        base.contractIntervals = intervals.map(({ activeContract, fromInclusive, untilExclusive }) =>
          ({ activeContract, fromInclusive, untilExclusive }));
      else error("progress.json", "contract_intervals_projection_invalid", "合同区间证据未完整读取，不推算过去日期归属。", null);
    }
    if (![FULL_CONTRACT, CORE_CONTRACT].includes(active)) {
      error("progress.json", "active_contract_invalid", "当前数据合同未确认，不能认定任何快照成功。", active);
      return base;
    }
    if (isObject(progress) && has(progress, "activeRunDecision")) return auditedContractView(progress, base, error);
    if (active !== CORE_CONTRACT) return base;
    const raw = progress?.coreMarket;
    const ledger = isObject(raw) && Array.isArray(raw.ledger) ? raw.ledger : null;
    const run = isObject(raw?.lastRun) ? raw.lastRun : null;
    const q = isObject(raw?.quality) ? raw.quality : null;
    const qualityValid = q && q.scope === "sealed_core_qlib_summary" && q.targetDate === run?.targetDate
      && !!hash(q.payloadSha256) && q.payloadSha256 === run?.payloadSha256
      && [q.valid,q.invalid,q.eligible].every(x => Number.isSafeInteger(x) && x >= 0) && q.eligible > 0
      && q.valid + q.invalid === q.eligible && q.priceStatus === (q.invalid ? "partial" : "complete")
      && typeof q.ratio === "number" && Number.isFinite(q.ratio) && q.ratio >= 0 && q.ratio <= 1
      && Math.abs(q.ratio-q.valid/q.eligible) <= 0.000001;
    let valid = isObject(raw) && raw.contract === CORE_CONTRACT && raw.fullSnapshot === false
      && raw.artifactKind === "qlib_market_summary" && raw.includesCompletePriceArchive === false
      && (raw.readState === "checked" || (raw.readState === "missing" && raw.status === "not_required"
        && raw.rowCount === 0 && ledger?.length === 0 && raw.latestValidDate === null))
      && date(raw.expectedFrom) === base.effectiveTradeDate && !!base.effectiveTradeDate
      && progress?.contractPolicy?.activeContract === CORE_CONTRACT
      && date(progress.contractPolicy.effectiveTradeDate) === base.effectiveTradeDate
      && progress?.legacyFullSnapshot?.contract === FULL_CONTRACT
      && progress.legacyFullSnapshot.status === "historical_only"
      && Array.isArray(raw.evidenceProblems) && raw.evidenceProblems.length === 0
      && Array.isArray(raw.blockingReasons) && raw.blockingReasons.every(x => typeof x === "string")
      && ledger && ledger.every((row) => isObject(row) && date(row.date) && row.date >= base.effectiveTradeDate
        && row.contract === CORE_CONTRACT && row.fullSnapshot === false
        && ["valid", "invalid", "missing"].includes(row.state) && typeof row.pendingWindow === "boolean"
        && (row.state !== "valid" || hash(row.payloadSha256)))
      && new Set(ledger.map(row => row.date)).size === ledger.length
      && run && ["pass", "fail", "pending", "not_required"].includes(raw.status);
    if (valid && raw.status === "pass") {
      const target = ledger.find(row => row.date === run.targetDate);
      valid = ["inserted", "skipped_existing", "idempotent", "success"].includes(run.status)
        && !!qualityValid
        && !!instant(run.timestamp) && /(Z|[+-]\d\d:\d\d)$/i.test(run.timestamp)
        && target?.state === "valid" && !!hash(run.payloadSha256) && run.payloadSha256 === target.payloadSha256;
    }
    if (valid && raw.status === "not_required") {
      const runMs=instant(run.timestamp) ? Date.parse(run.timestamp) : NaN;
      valid=run.status === "skipped_non_trading" && run.calendarClosedVerified === true
        && !!date(run.calendarTargetDate) && !!date(run.previousTradingDate)
        && run.previousTradingDate === run.targetDate && run.targetDate < run.calendarTargetDate
        && typeof run.runId === "string" && /^[0-9a-f]{32}$/.test(run.runId)
        && !!hash(run.calendarEvidenceSha256) && run.payloadSha256 === null
        && Number.isFinite(runMs) && /(Z|[+-]\d\d:\d\d)$/i.test(run.timestamp)
        && new Date(runMs+8*3600000).toISOString().slice(0,10) === run.calendarTargetDate
        && !!instant(progress.sourceTime) && new Date(Date.parse(progress.sourceTime)+8*3600000).toISOString().slice(0,10) === run.calendarTargetDate;
    }
    const due = valid ? ledger.filter(row => !row.pendingWindow) : null;
    if (valid && (count(raw.knownDates) !== due.length || count(raw.validDates) !== due.filter(row => row.state === "valid").length)) valid = false;
    if (valid && ["pass", "not_required"].includes(raw.status) && (raw.blockingReasons.length || due.some(row => row.state !== "valid"))) valid = false;
    base.coreMarket = { status: valid ? raw.status : "insufficient", runStatus: text(run?.status),
      runAt: instant(run?.timestamp), stageTradeDate: date(run?.stageTradeDate), usedStageTradeDate: date(run?.usedStageTradeDate),
      calendarTargetDate: date(run?.calendarTargetDate), previousTradingDate: date(run?.previousTradingDate),
      targetDate: date(run?.targetDate), latestValidDate: valid ? date(raw.latestValidDate) : null,
      quality: qualityValid ? copy(q) : null,
      coverage: metric(valid ? raw.validDates : null, valid ? raw.knownDates : null, "核心行情合同有效日期（非完整快照）"),
      detail: isObject(raw) ? copy(raw) : null };
    if (!valid) error("progress.json", "core_contract_evidence_invalid", "核心行情合同证据缺失或不一致；不能以旧完整快照记录替代。", raw);
    return base;
  }

  function build(inputs, nowMs) {
    if (!Number.isFinite(nowMs)) throw new TypeError("A finite reader clock is required");
    const input = isObject(inputs) ? inputs : {};
    const errors = [];
    const panels = {};
    const freshness = { today: null, progress: null, delivery: null };

    function error(file, code, message, detail) {
      errors.push({ kind: "source_error", label: "来源待核", text: message,
        status: "insufficient", targetDate: null,
        sourceTime: instant(panels[file.replace(/\.json$/, "")]?.sourceTime),
        sourceFile: file, detail: { code, ...(detail === undefined ? {} : { recorded: copy(detail) }) } });
    }

    function refs(values, file) {
      if (!Array.isArray(values)) {
        if (values !== undefined) error(file, "report_refs_invalid", "报告来源引用结构异常。", values);
        return [];
      }
      const valid = values.filter((value) => Number.isSafeInteger(value) && value > 0);
      if (valid.length !== values.length) error(file, "report_refs_invalid", "报告来源引用含无法读取的编号。", values);
      return valid;
    }

    for (const name of Object.keys(freshness)) {
      const value = input[name];
      if (!isObject(value) || value.schemaVersion !== 1 || value.page !== name) {
        panels[name] = null;
        error(`${name}.json`, "panel_unreadable", `${name}.json 未读取到符合版本与页面标识的数据。`);
        continue;
      }
      panels[name] = value;
      // Pass the original source to the canonical policy, never another
      // panel's exportedAt/sourceTime or a clock synthesized by this adapter.
      freshness[name] = core.recomputeFreshness(value, nowMs);
      if (!Array.isArray(value.readErrors)) {
        error(`${name}.json`, "read_errors_unavailable", `${name}.json 未提供可读取的来源检查记录。`);
      } else {
        for (const issue of value.readErrors) {
          // Older exports counted absent Telegram/relay evidence as a read error as
          // well as an insufficient delivery layer. Keep the latter verdict;
          // do not manufacture a second, generic exporter failure.
          if (name === "delivery" && ["telegram_record_missing", "relay_record_missing"].includes(issue)) {
            const layer = value.layers?.[issue === "telegram_record_missing" ? "telegram" : "relay"];
            if (isObject(layer) && layer.status === "insufficient") continue;
          }
          error(`${name}.json`, "exporter_read_error", typeof issue === "string"
            ? `${name}.json：${issue}` : `${name}.json 含结构异常的来源检查记录。`, issue);
        }
      }
      if (freshness[name].stale) {
        const reasons = typeof core.freshnessReasons === "function" ? core.freshnessReasons(freshness[name]) : [];
        error(`${name}.json`, "source_stale", `${name}.json 时效不足：${reasons.join("；") || "来源标记为过期"}。`,
          { sourceTime: value.sourceTime, exportedAt: value.exportedAt });
      }
    }

    const today = panels.today, progress = panels.progress, delivery = panels.delivery;
    const recordedAuditStatus = text(progress?.health?.status);
    const auditStatus = ["critical", "warning", "ok"].includes(recordedAuditStatus)
      ? recordedAuditStatus : null;
    if (progress && isObject(progress.health) && !auditStatus) {
      error("progress.json", "audit_status_unrecognized", "健康审计整体级别未记录或无法识别。", progress.health.status);
    }
    const audit = { recordedStatus: auditStatus, sourceTime: instant(progress?.sourceTime),
      current: !!(freshness.progress && !freshness.progress.stale) };
    const rawReport = today && isObject(today.report) ? today.report : null;
    if (today && !rawReport) error("today.json", "report_unavailable", "最近报告内容未读取到。", today.report);
    const report = { date: date(rawReport?.targetDate), cutoff: text(rawReport?.cutoffLocal),
      dataStatus: text(rawReport?.dataStatus), gate: text(rawReport?.publicationGate),
      conclusions: [], sha256: hash(rawReport?.sha256), sources: [],
      publicationLevel: text(rawReport?.publicationLevel), variant: text(rawReport?.variant) };

    if (rawReport) {
      if (!report.date) error("today.json", "report_date_unavailable", "报告目标日期缺失或无法解析。", rawReport.targetDate);
      if (!report.sha256) error("today.json", "report_hash_unavailable", "报告哈希缺失或结构异常，无法绑定密封证据。", rawReport.sha256);
      if (!Array.isArray(rawReport.conclusion)) {
        error("today.json", "conclusion_unavailable", "报告结论区未提供可读取的文本块。", rawReport.conclusion);
      } else {
        for (const block of rawReport.conclusion) {
          if (!isObject(block) || typeof block.text !== "string") {
            error("today.json", "conclusion_block_invalid", "报告含结构异常的结论块。", block);
            continue;
          }
          report.conclusions.push({ text: block.text, refs: refs(block.refs, "today.json") });
        }
      }
      if (Array.isArray(rawReport.sources)) {
        for (const source of rawReport.sources) {
          if (!isObject(source) || typeof source.text !== "string") {
            error("today.json", "report_source_invalid", "报告含结构异常的来源记录。", source);
            continue;
          }
          const sourceId = Number.isSafeInteger(source.id) && source.id > 0 ? source.id : null;
          if (sourceId === null || (source.url !== null && typeof source.url !== "string")) {
            error("today.json", "report_source_invalid", "报告来源编号或链接结构异常。", source);
          }
          report.sources.push({ id: sourceId, text: source.text, url: text(source.url) });
        }
      } else error("today.json", "report_sources_unavailable", "报告来源列表未读取到。", rawReport.sources);
    }

    const deliveryDate = date(delivery?.targetDate);
    const rawLayers = delivery && isObject(delivery.layers) ? delivery.layers : null;
    const seal = rawLayers && isObject(rawLayers.seal) ? rawLayers.seal : null;
    const sealEvidence = seal && isObject(seal.evidence) ? seal.evidence : null;
    const deliveryHash = hash(isObject(sealEvidence?.recomputed) ? sealEvidence.recomputed.report : null);
    const binding = { status: "pass", reasons: [], reportDate: report.date,
      deliveryDate, reportSha256: report.sha256, deliveryReportSha256: deliveryHash };
    if (!today || !rawReport || !delivery || !report.date || !deliveryDate || !report.sha256 || !deliveryHash) {
      binding.status = "insufficient";
      binding.reasons.push("缺少有效的报告日期或报告哈希，投递记录尚不能与这份报告绑定。");
    }
    if (report.date && deliveryDate && report.date !== deliveryDate) {
      binding.status = "fail";
      binding.reasons.push("投递记录的目标日期与最近报告不一致。");
    }
    if (report.sha256 && deliveryHash && report.sha256.toLowerCase() !== deliveryHash.toLowerCase()) {
      binding.status = "fail";
      binding.reasons.push("投递密封件的报告哈希与最近报告不一致。");
    }
    if (binding.status !== "pass") {
      error("delivery.json", "report_binding_unconfirmed", binding.reasons.join("；"), binding);
    }

    const layers = {};
    const layerAttention = [];
    for (const [key, label] of Object.entries(LAYERS)) {
      const raw = rawLayers && isObject(rawLayers[key]) ? rawLayers[key] : null;
      const recordedStatus = text(raw?.status);
      const limitations = Array.isArray(raw?.limitations) ? raw.limitations.filter((value) => typeof value === "string") : [];
      const valid = raw && STATES.has(recordedStatus) && isObject(raw.evidence)
        && Array.isArray(raw.limitations) && limitations.length === raw.limitations.length;
      if (!valid) error("delivery.json", "delivery_layer_invalid", `${label}证据缺失或结构异常。`, raw);
      const current = valid && freshness.delivery && !freshness.delivery.stale && binding.status === "pass";
      const status = current ? recordedStatus : "insufficient";
      const evidence = raw && isObject(raw.evidence) ? copy(raw.evidence) : {};
      // Retain the recorded verdict even when current freshness/binding fails.
      // No receipt, sender hash or seal result is promoted to another layer.
      layers[key] = { label, status, recordedStatus, limitations: [...limitations], evidence,
        targetDate: deliveryDate, sourceTime: instant(delivery?.sourceTime) };
      if (!current) {
        if (!valid) layers[key].limitations.push("该层证据未完整读取，当前无法判定。");
        if (!freshness.delivery || freshness.delivery.stale) layers[key].limitations.push("投递来源时效不足，原记录仅作历史证据。");
        if (binding.status !== "pass") layers[key].limitations.push(...binding.reasons);
      }
      if (status !== "pass") {
        layerAttention.push({ kind: "delivery", label, text: `${label}：${layers[key].limitations.join("；") || "证据不足"}`,
          status, targetDate: deliveryDate, sourceTime: instant(delivery?.sourceTime), sourceFile: "delivery.json",
          detail: { layer: key, recordedStatus, evidence: copy(evidence), limitations: [...layers[key].limitations], binding: copy(binding) } });
      }
    }

    const reportLimitations = [];
    const attention = [];
    const dataNotes = [];
    function repeatedAuditFocus(item) {
      if (item.kind !== "audit_unconfirmed" || !Array.isArray(progress?.currentIssues)) return false;
      return progress.currentIssues.some((issue) => {
        if (!isObject(issue) || issue.classification !== "not_in_retained_baseline"
            || typeof issue.code !== "string" || typeof issue.severity !== "string"
            || !Array.isArray(issue.dates)) return false;
        // New exports bind the focus entry structurally. The exact old text
        // form is accepted only for backward-compatible deduplication.
        if (typeof item.issueCode === "string" && Array.isArray(item.issueDates)) {
          return item.issueCode === issue.code && item.issueSeverity === issue.severity
            && same(item.issueDates) === same(issue.dates);
        }
        return item.text === `审计记录不在已确认保留范围：${issue.code} / ${issue.severity}；`
          + `${issue.dates.join("、") || "未提供日期"}。新增或变化原因待核。`;
      });
    }
    if (today && !Array.isArray(today.focus)) error("today.json", "focus_unavailable", "报告关注事项列表未读取到。", today.focus);
    for (const item of Array.isArray(today?.focus) ? today.focus : []) {
      if (!isObject(item) || typeof item.text !== "string") {
        error("today.json", "focus_item_invalid", "关注事项含结构异常的记录。", item);
        continue;
      }
      if (item.kind === "historical_gap") continue;
      if (repeatedAuditFocus(item)) continue;
      if (item.kind === "report_limitation" || item.kind === "risk"
          || (typeof item.source === "string" && item.source.startsWith("report"))) {
        reportLimitations.push({ text: item.text, refs: refs(item.refs, "today.json"),
          reportDate: date(item.reportDate), sourceTime: instant(item.sourceTime), reportSha256: hash(item.reportSha256),
          sealVerified: item.sealVerified === true && layers.seal.status === "pass"
            && date(item.reportDate) === report.date && hash(item.reportSha256) !== null
            && item.reportSha256.toLowerCase() === report.sha256?.toLowerCase() });
        continue;
      }
      if (item.kind === "delivery" && typeof item.layer === "string" && has(LAYERS, item.layer)) {
        const raw = rawLayers && rawLayers[item.layer];
        const corresponding = layerAttention.find((entry) => entry.detail.layer === item.layer);
        const repeatedText = Array.isArray(raw?.limitations) && raw.limitations.every((value) => typeof value === "string")
          ? `${LAYERS[item.layer]}：${raw.limitations.join("；") || "证据不足"}` : null;
        if (corresponding && date(item.targetDate) === deliveryDate && instant(item.sourceTime) === instant(delivery?.sourceTime)
            && date(item.targetDate) !== null && instant(item.sourceTime) !== null && item.status === raw?.status
            && item.text === repeatedText) {
          // The exporter repeats this exact delivery source in today.focus.
          // Normalize it to the authoritative source so exact deduplication
          // below removes the duplicate while retaining every distinct issue.
          attention.push(copy(corresponding));
          continue;
        }
        attention.push({ kind: "source_mismatch", label: "投递关注记录待核", text: item.text,
          status: "insufficient", targetDate: date(item.targetDate), sourceTime: instant(item.sourceTime),
          sourceFile: "today.json", detail: { recorded: copy(item), reason: "关注记录与单独读取的投递来源未绑定。" } });
        continue;
      }
      const source = item.sourceFile === "progress.json" ? "progress" : item.sourceFile === "delivery.json" ? "delivery" : "today";
      const sourceTime = instant(item.auditTime) || instant(item.sourceTime);
      const sourceCurrent = freshness[source] && !freshness[source].stale
        && sourceTime !== null && sourceTime === instant(panels[source]?.sourceTime);
      attention.push({ kind: text(item.kind) || "unconfirmed", label: text(item.label) || "待核事项", text: item.text,
        status: sourceCurrent && item.status === "fail" ? "fail" : "insufficient",
        targetDate: date(item.targetDate), sourceTime, sourceFile: `${source}.json`, detail: copy(item) });
    }
    attention.push(...layerAttention);

    if (progress && !isObject(progress.health)) error("progress.json", "health_unavailable", "健康审计未读取到，当前问题与历史保留范围无法确认。", progress.health);
    if (progress && !Array.isArray(progress.currentIssues)) {
      error("progress.json", "current_issues_unavailable", "审计当前问题列表未读取到，不能据此认定没有问题。", progress.currentIssues);
    }
    for (const issue of Array.isArray(progress?.currentIssues) ? progress.currentIssues : []) {
      if (!isObject(issue) || typeof issue.code !== "string") {
        error("progress.json", "current_issue_invalid", "审计当前问题中有结构异常的记录。", issue);
        continue;
      }
      const dates = Array.isArray(issue.dates) ? issue.dates : Array.isArray(issue.affectedDates) ? issue.affectedDates : null;
      const boundary = core.legacyUnquotedEvidence(issue);
      const coreIssue = CORE_LABELS[issue.code];
      const snapshotFailure = ["last_snapshot_not_successful", "last_core_market_not_successful"].includes(issue.code);
      const snapshotWarning = ["last_snapshot_material_warning", "core_market_material_warning"].includes(issue.code);
      const runLabel = issue.code.startsWith("last_core_market") || issue.code.startsWith("core_market_") ? "核心行情（非完整快照）" : "完整快照";
      const snapshotTarget = snapshotFailure || snapshotWarning ? date(issue.targetDate) : null;
      const sourceFailure = snapshotFailure ? snapshotFailureEvidence(issue.sourceFailure) : null;
      const stageSourceFailure = snapshotWarning ? snapshotFailureEvidence(issue.stageSourceFailure) : null;
      const snapshotRunStatus = SNAPSHOT_RUN_STATUSES.has(issue.runStatus) ? issue.runStatus : null;
      const snapshotRunAt = instant(issue.runAt) && /(Z|[+-]\d\d:\d\d)$/i.test(issue.runAt)
        ? issue.runAt : null;
      const baselineUnconfirmed = issue.classification === "not_in_retained_baseline";
      const newGap = baselineUnconfirmed && issue.code === "unresolved_trading_day_gaps";
      const description = snapshotFailure
        ? `最近一次${runLabel}运行未成功（状态：${snapshotRunStatus || "未记录"}；目标日期：${snapshotTarget || "未记录"}）${snapshotFailureDescription(sourceFailure, "AkShare 来源尝试")}${snapshotRunAt ? `；运行时间：${snapshotRunAt}` : ""}`
        : snapshotWarning
        ? `最近一次${runLabel}伴随材料提示（运行状态：${snapshotRunStatus || "未记录"}；目标日期：${snapshotTarget || "未记录"}）${issue.stageUnavailable === true ? "；同次 AkShare 当前市场阶段采集不可用" : ""}${snapshotFailureDescription(stageSourceFailure, "当前阶段来源尝试")}${snapshotRunAt ? `；运行时间：${snapshotRunAt}` : ""}`
        : coreIssue ? `${coreIssue}；合同 ${text(issue.contract) || "未记录"}；对应日期 ${date(issue.targetDate) || dates?.filter(date).join("、") || "未记录"}。仅按本合同证据判断，不抵消历史完整快照缺口。`
        : newGap
        ? `审计记录中的新增或变化交易日缺口：${dates?.filter((value) => date(value)).join("、") || "日期未记录"}；原级别 ${text(issue.severity) || "未记录"}；不属于已确认的历史保留范围，原因待核。`
        : boundary ? [boundary.summary, text(issue.text) || text(issue.message)]
        .filter(Boolean).join("。") : text(issue.text) || text(issue.message) || [issue.code, text(issue.severity),
        dates && dates.length ? dates.filter((value) => typeof value === "string").join("、") : "日期未记录"].filter(Boolean).join(" / ");
      const recordedFail = issue.severity === "critical" || issue.status === "fail";
      const informational = !baselineUnconfirmed && !recordedFail
        && (issue.severity === "information" || issue.severity === "info");
      const sourceCurrent = freshness.progress && !freshness.progress.stale;
      const status = !sourceCurrent ? "insufficient"
        : recordedFail ? "fail" : baselineUnconfirmed ? "insufficient" : informational ? "information"
        : issue.severity === "warning" ? "warning" : "insufficient";
      // Informational records remain readable even when their source is stale;
      // the independent source_stale error continues to require attention.
      const entry = { kind: "current_issue", label: boundary ? boundary.label : coreIssue || issue.code, text: description,
        status, severity: text(issue.severity),
        targetDate: boundary ? boundary.targetDate : snapshotFailure || snapshotWarning ? snapshotTarget
          : date(issue.targetDate) || (dates?.length === 1 ? date(dates[0]) : null),
        ...(boundary ? { boundaryEvidence: copy(boundary.rows) } : {}),
        sourceTime: instant(progress.sourceTime), sourceFile: "progress.json",
        detail: snapshotFailure || snapshotWarning
          ? { code: issue.code, severity: text(issue.severity),
              runStatus: snapshotRunStatus, targetDate: snapshotTarget,
              ...(issue.status === "fail" ? { status: "fail" } : {}),
              ...(snapshotRunAt ? { runAt: snapshotRunAt } : {}),
              ...(typeof issue.runId === "string" && /^[0-9a-f]{32}$/.test(issue.runId) ? { runId: issue.runId } : {}),
              ...(sourceFailure ? { sourceFailure } : {}),
              ...(snapshotWarning && typeof issue.stageUnavailable === "boolean"
                ? { stageUnavailable: issue.stageUnavailable } : {}),
              ...(stageSourceFailure ? { stageSourceFailure } : {}) }
          : copy(issue) };
      (informational ? dataNotes : attention).push(entry);
    }

    const rawCoverage = progress && isObject(progress.coverage) ? progress.coverage : null;
    if (progress && !rawCoverage) error("progress.json", "coverage_unavailable", "审计覆盖统计未读取到，缺失数字保持未知。", progress.coverage);
    const rawPrice = rawCoverage && isObject(rawCoverage.qlibCoverage) ? rawCoverage.qlibCoverage : null;
    const price = { ...metric(rawPrice?.valid, rawPrice?.eligible, "Qlib 行情覆盖"), date: date(rawPrice?.targetDate) };
    const contract = contractView(progress, error);
    if (contract.runDecision && (!freshness.progress || freshness.progress.stale)) {
      contract.runDecision.recordedStatus = contract.runDecision.status;
      contract.runDecision.status = "insufficient";
    }
    if (contract.coreMarket && (!freshness.progress || freshness.progress.stale)) {
      contract.coreMarket.recordedStatus = contract.coreMarket.status;
      contract.coreMarket.status = "insufficient";
    }
    const snapshot = metric(rawCoverage?.snapshotValiditySource === "audit_ledger" ? rawCoverage.validSnapshotDates : null,
      rawCoverage?.knownDates, contract.activeContract === CORE_CONTRACT ? "历史完整快照有效日期占比" : "完整快照有效日期占比");
    const coverage = { price, snapshot, sourceTime: instant(progress?.sourceTime),
      snapshotValiditySource: text(rawCoverage?.snapshotValiditySource), auditSince: date(rawCoverage?.auditSince),
      referenceCompleteDate: date(rawCoverage?.referenceCompleteDate) };
    if (contract.runDecision) {
      const current = contract.runDecision;
      const notes = current.materialWarningCodes.length ? `材料提示：${current.materialWarningCodes.join("、")}。` : "";
      const unlistedWarnings = current.materialWarningCodes.filter(code =>
        !attention.some(item => item.detail?.code === code && item.status === "warning"));
      const item = { kind: "audited_run", label: "本次数据运行（审计结论）",
        text: `计划日期 ${current.scheduledDate || "未确认"}；运行 ${current.runStatus || "未确认"}；目标日 ${current.targetDate || "未记录"}。${notes}只核本次本地运行、产物与日历身份；历史问题及审计整体级别保持单列，自然触发来源仍以日志验收。`,
        status: unlistedWarnings.length && current.status === "pass" ? "warning"
          : ["pass", "pending", "not_required"].includes(current.status) ? "information" : current.status,
        targetDate: current.targetDate, sourceTime: instant(progress?.sourceTime), sourceFile: "progress.json",
        detail: current.detail };
      (item.status === "information" ? dataNotes : attention).push(item);
    }
    if (contract.activeContract === CORE_CONTRACT) {
      const current = contract.coreMarket;
      const qualityText = current?.status === "not_required" ? "本次休市不生成新的价格覆盖记录。"
        : current?.quality ? `同次摘要价格覆盖 ${current.quality.priceStatus}：有效 ${current.quality.valid} / ${current.quality.eligible}，未完全有效 ${current.quality.invalid}；缩减合同门禁通过不等于全部价格完整。`
        : "同次摘要价格覆盖证据不足，不借当前安装或其他日期统计替代。";
      const item = { kind: "data_contract", label: "当前合同：核心市场摘要（非完整快照）",
        text: `${current?.status === "not_required" ? `已核实 ${current.calendarTargetDate} 休市，无需新记录；上一交易日 ${current.previousTradingDate}。` : ""}生效交易日 ${contract.effectiveTradeDate || "未记录"}；最近运行 ${current?.runStatus || "未记录"}；目标日 ${current?.targetDate || "未记录"}。${qualityText}仅核 Qlib 市场摘要，不含完整价格归档，不表示公告、概念、资金等完整快照材料已齐备；旧完整快照与历史缺口继续单列。`,
        status: ["pass", "pending", "not_required"].includes(current?.status) ? "information" : current?.status || "insufficient",
        targetDate: current?.targetDate || null, sourceFile: "progress.json", sourceTime: instant(progress?.sourceTime),
        detail: { activeContract: contract.activeContract, effectiveTradeDate: contract.effectiveTradeDate, coreMarket: current?.detail } };
      if (!freshness.progress || freshness.progress.stale) item.status = "insufficient";
      (item.status === "information" ? dataNotes : attention).push(item);
    }

    if (contract.coreMarket?.historicalOnly) dataNotes.push({ kind: "core_contract_history",
      label: "历史核心市场摘要（非完整快照）", status: "information", targetDate: null,
      text: "当前已使用完整快照合同；历史核心行情记录及其区间继续保留，不代表本次运行成功，也不补齐旧完整快照缺口。",
      sourceFile: "progress.json", sourceTime: instant(progress?.sourceTime),
      detail: { contractIntervals: contract.contractIntervals, coreMarket: contract.coreMarket.detail } });

    const rawHistory = progress && isObject(progress.historicalGaps) ? progress.historicalGaps : null;
    const rawCurrentHistory = isObject(rawHistory?.current) ? rawHistory.current : null;
    function historyDates(key) {
      const values = rawCurrentHistory ? rawCurrentHistory[key] : rawHistory?.[key];
      if (!Array.isArray(values) || values.some((value) => !date(value))) {
        if (progress) error("progress.json", "historical_dates_unavailable", `当前审计保留范围 ${key} 未完整读取，数量未知。`, values);
        return null;
      }
      return [...values];
    }
    const historyEvidence = Array.isArray(rawHistory?.evidence) ? rawHistory.evidence : null;
    const validHistoryEvidence = historyEvidence && historyEvidence.every(isObject);
    if (rawHistory && !validHistoryEvidence) {
      error("progress.json", "historical_evidence_unavailable", "历史保留范围的逐日证据未完整读取。", rawHistory.evidence);
    }
    const rawBaseline = isObject(rawHistory?.baseline) ? rawHistory.baseline : null;
    let baseline = null;
    if (rawBaseline) {
      const originalCritical = rawBaseline.criticalDates;
      const originalLegacy = rawBaseline.legacyDates;
      if (Array.isArray(originalCritical) && Array.isArray(originalLegacy)
          && originalCritical.every(date) && originalLegacy.every(date)
          && date(rawBaseline.baselineDate) && hash(rawBaseline.baselineSha256)) {
        baseline = { criticalDates: [...originalCritical], legacyDates: [...originalLegacy],
          baselineDate: rawBaseline.baselineDate, baselineSha256: rawBaseline.baselineSha256 };
      } else error("progress.json", "historical_baseline_invalid", "原始保留基线证据结构异常。", rawBaseline);
    }
    let addenda = null;
    if (Array.isArray(rawHistory?.addenda)) {
      if (rawHistory.addenda.every((item) => isObject(item) && date(item.date)
          && instant(item.firstObservedAt) && text(item.firstObservedSource)
          && hash(item.reconstructedJournalPayloadSha256)
          && item.code === "unresolved_trading_day_gaps"
          && item.aggregateIssueSeverity === "critical" && !has(item, "acceptedAt"))) {
        addenda = copy(rawHistory.addenda);
      } else error("progress.json", "historical_addenda_invalid", "后续缺口登记证据结构异常。", rawHistory.addenda);
    } else if (rawHistory && rawHistory.addenda !== undefined) {
      error("progress.json", "historical_addenda_invalid", "后续缺口登记证据结构异常。", rawHistory.addenda);
    }
    const history = { unresolvedDates: historyDates("criticalDates"), legacyDates: historyDates("legacyDates"),
      observedAt: instant(rawHistory?.observedAt), baselineDate: date(rawHistory?.baselineDate),
      baselineSha256: hash(rawHistory?.baselineSha256), note: text(rawHistory?.note),
      baseline, addenda, evidence: validHistoryEvidence ? copy(historyEvidence) : null };
    if (rawCurrentHistory && history.unresolvedDates && history.legacyDates
        && (same(rawCurrentHistory.criticalDates) !== same(rawHistory.criticalDates)
        || same(rawCurrentHistory.legacyDates) !== same(rawHistory.legacyDates)
        || count(rawCurrentHistory.count) !== history.unresolvedDates?.length + history.legacyDates?.length)) {
      error("progress.json", "historical_current_conflict", "当前审计计数与逐日记录不一致。", rawCurrentHistory);
    }
    if (baseline && addenda && history.unresolvedDates && history.legacyDates) {
      const registeredCritical = new Set([...baseline.criticalDates, ...addenda.map((item) => item.date)]);
      const registeredLegacy = new Set(baseline.legacyDates);
      if (history.unresolvedDates.some((value) => !registeredCritical.has(value))
          || history.legacyDates.some((value) => !registeredLegacy.has(value))
          || new Set(history.unresolvedDates).size !== history.unresolvedDates.length
          || new Set(history.legacyDates).size !== history.legacyDates.length) {
        error("progress.json", "historical_scope_conflict", "当前审计日期超出原基线和后续登记。",
          { baseline, addenda, criticalDates: history.unresolvedDates, legacyDates: history.legacyDates });
      }
    }

    // Only the report's own validated, sealed calendar defines its cadence.
    const policy = today ? core.reportFreshnessPolicy(today, nowMs) : null;
    // Batch 1: a validated market holiday (exporter-bound to the same report
    // date and next trading day) reads 无需新稿 instead of a missing report.
    const holiday = isObject(today?.expectedReport) && today.expectedReport.status === "not_required"
      && policy && freshness.today.cadenceState === "latest"
      && date(today.expectedReport.latestTradingDate) === report.date
      && date(today.expectedReport.nextTradingDate) === policy.nextTradingDate ? today.expectedReport : null;
    const cadence = { label: holiday ? `无需新稿，最近有效交易日为 ${holiday.latestTradingDate}`
      : policy ? ({ latest: "最近应有报告", pending: "等待下一份报告", overdue: "应出报时限已过" }[freshness.today.cadenceState] || "出报节奏未确认")
      : "出报节奏未确认", holiday: holiday ? copy(holiday) : null, nextAt: policy ? policy.expectedAt : null, deadlineAt: policy ? policy.deadlineAt : null,
      nextTradingDate: policy ? policy.nextTradingDate : null, state: policy ? freshness.today.cadenceState : null };

    const manifest = isObject(input.manifest) && input.manifest.schemaVersion === 1
      && isObject(input.manifest.files) ? input.manifest : null;
    if (!manifest) error("manifest.json", "manifest_unavailable", "导出清单未读取到符合版本的记录。", input.manifest);
    const exportedAts = Object.fromEntries(Object.keys(freshness).map((name) => [name, instant(panels[name]?.exportedAt)]));
    const metadata = { exportedAt: instant(manifest?.exportedAt), exportedAts,
      sourceTimes: Object.fromEntries(Object.keys(freshness).map((name) => [name, instant(panels[name]?.sourceTime)])),
      readErrors: unique(errors), checkedAt: new Date(nowMs).toISOString(), schemaVersion: 1,
      availableDates: Array.isArray(today?.availableDates) ? today.availableDates.filter((value) => date(value)) : null,
      manifest: manifest ? copy(manifest) : null, readOnly: true };
    return { report, freshness, cadence, attention: unique([...attention, ...errors]), dataNotes: unique(dataNotes),
      reportLimitations: unique(reportLimitations), coverage, contract, history, audit, layers, binding, metadata };
  }

  return { build };
});
