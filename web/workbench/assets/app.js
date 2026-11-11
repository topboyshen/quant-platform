/* Read-only dashboard renderer.
 *
 * Rules (see dashboard/README.md):
 *  - report/JSON content is untrusted text: it is only ever assigned through
 *    textContent, never through HTML parsing, so Markdown or HTML inside a
 *    report is inert.
 *  - URLs are re-validated here (http/https only) before becoming links.
 *  - staleness is recomputed against the reader's current clock: an exporter
 *    that stopped must not look fresh forever.
 *  - progress bars are drawn only from numbers the audit itself produced; a
 *    missing number stays missing, it is never interpolated or estimated.
 *  - no trend chart is drawn at all: the export carries no per-day series, so
 *    a trend line would be invented.
 *  - this file performs no writes, no POSTs and no external requests.
 */
"use strict";

(function (global) {
  const STALL_SECONDS = 15 * 60;
  const FRESHNESS_TICK_MS = 60 * 1000;   // re-evaluate staleness every minute
  const REFRESH_MS = 5 * 60 * 1000;      // re-read the export at most every 5 minutes
  const RETRY_MS = 60 * 1000;            // after a failed read, retry on the next tick
  const STATES = {
    pass: { label: "通过", className: "bg-green-lt text-green" },
    fail: { label: "失败", className: "bg-red-lt text-red" },
    stopped: { label: "已停止", className: "bg-orange-lt text-orange" },
    disabled: { label: "未启用", className: "bg-secondary-lt text-secondary" },
    ended: { label: "已结束", className: "bg-secondary-lt text-secondary" },
    pending: { label: "待完成", className: "bg-yellow-lt text-yellow" },
    insufficient: { label: "证据不足", className: "bg-secondary-lt text-secondary" }
  };

  /* Coverage cell vocabulary: only states the audit can actually witness. */
  const COVERAGE = {
    valid: { label: "有效", className: "bg-green-lt text-green" },
    invalid: { label: "无效", className: "bg-red-lt text-red" },
    critical: { label: "缺口", className: "bg-red-lt text-red" },
    legacy: { label: "遗留", className: "bg-orange-lt text-orange" },
    staged: { label: "仅暂存", className: "bg-yellow-lt text-yellow" },
    recorded: { label: "已记录", className: "bg-blue-lt text-blue" },
    missing: { label: "缺失", className: "bg-secondary-lt text-secondary" },
    unknown: { label: "未观察", className: "bg-secondary-lt text-secondary" }
  };

  const INSTANT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$/;

  /* Only instants with an explicit zone are accepted: a zone-less timestamp
     would silently be read in the browser's own zone, which is how an old
     source can look fresh. Anything unreadable counts as stale. */
  function parseInstant(value) {
    if (value === null || value === undefined || value === "") return null;
    const text = String(value).trim();
    if (!INSTANT_RE.test(text)) return null;
    const ms = Date.parse(text);
    return Number.isFinite(ms) ? ms : null;
  }

  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const ms = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === value;
  }

  function cstDate(ms) {
    return Number.isFinite(ms) ? new Date(ms + 8 * 3600 * 1000).toISOString().slice(0, 10) : null;
  }

  /* The exporter binds this calendar to the sealed input and report. Never
     infer a holiday from weekdays or waive health/service freshness. The
     browser also validates its date/hash bindings so malformed exports fall
     back to the conservative wall-clock rule. */
  function reportFreshnessPolicy(payload, nowMs) {
    if (!payload || !["today", "delivery", "report"].includes(payload.page)) return null;
    const policy = payload.freshnessPolicy;
    if (!policy || policy.kind !== "sealed_trading_calendar_v1") return null;
    if (!validDate(policy.reportDate) || !validDate(policy.nextTradingDate)) return null;
    const span = (Date.parse(`${policy.nextTradingDate}T00:00:00Z`)
      - Date.parse(`${policy.reportDate}T00:00:00Z`)) / 86400000;
    if (!(span > 0 && span <= 31)) return null;
    const sourceMs = parseInstant(payload.sourceTime);
    const exportedMs = parseInstant(payload.exportedAt);
    const observedMs = parseInstant(policy.calendarObservedAt);
    const expectedMs = parseInstant(policy.expectedAt);
    const deadlineMs = parseInstant(policy.deadlineAt);
    if ([sourceMs, exportedMs, observedMs, expectedMs, deadlineMs].some((value) => value === null)) return null;
    if (sourceMs > nowMs + 300000 || observedMs > nowMs + 300000
        || observedMs > exportedMs + 300000 || cstDate(sourceMs) !== policy.reportDate
        || cstDate(observedMs) !== policy.reportDate) return null;
    const expected = Date.parse(`${policy.nextTradingDate}T08:00:00+08:00`);
    // 61 min is the deployed source service's timeout plus timer accuracy,
    // not an extension of the report cutoff or the relay sending window.
    if (expectedMs !== expected || deadlineMs !== expected + 61 * 60000) return null;
    const date = payload.page === "delivery" ? payload.targetDate
      : payload.report && payload.report.targetDate;
    if (date !== policy.reportDate || (payload.date !== undefined && payload.date !== policy.reportDate)) return null;
    const reportHash = payload.page === "delivery"
      ? payload.layers && payload.layers.seal && payload.layers.seal.evidence
        && payload.layers.seal.evidence.recomputed && payload.layers.seal.evidence.recomputed.report
      : payload.report && payload.report.sha256;
    const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value);
    if (!hash(policy.inputSha256) || !hash(policy.reportSha256)
        || !hash(reportHash) || reportHash.toLowerCase() !== policy.reportSha256.toLowerCase()) return null;
    if (!Array.isArray(policy.calendarSourceIds) || !policy.calendarSourceIds.length
        || policy.calendarSourceIds.some((value) => typeof value !== "string" || !value.trim())) return null;
    return { ...policy, expectedMs, deadlineMs };
  }

  /* Recompute freshness at read time. `payload.stale` is only the exporter's
     opinion at export time; a stalled exporter or an aging source must show. */
  function recomputeFreshness(payload, nowMs) {
    // A service collection is a current snapshot, never a day-old report.
    const threshold = payload && payload.page === "projects" ? STALL_SECONDS / 3600
      : Number(payload && payload.thresholdHours) || 26;
    const sourceMs = parseInstant(payload && payload.sourceTime);
    const exportMs = parseInstant(payload && payload.exportedAt);
    const ageSeconds = sourceMs === null ? null : Math.round((nowMs - sourceMs) / 1000);
    const exportAgeSeconds = exportMs === null ? null : Math.round((nowMs - exportMs) / 1000);
    const policy = reportFreshnessPolicy(payload, nowMs);
    const cadenceState = policy ? (nowMs < policy.expectedMs ? "latest"
      : nowMs < policy.deadlineMs ? "pending" : "overdue") : null;
    const sourceStale = sourceMs === null || ageSeconds < -300
      || (policy ? cadenceState === "overdue" : ageSeconds > threshold * 3600);
    const stalled = exportMs === null || exportAgeSeconds > STALL_SECONDS || exportAgeSeconds < -300;
    return {
      ageSeconds,
      exportAgeSeconds,
      sourceStale,
      stalled,
      policy,
      cadenceState,
      stale: Boolean(payload && payload.stale) || sourceStale || stalled,
      thresholdHours: threshold
    };
  }

  function freshnessReasons(freshness) {
    const reasons = [];
    if (freshness.stalled) reasons.push("导出停滞");
    if (freshness.sourceStale) reasons.push(freshness.cadenceState === "overdue"
      ? "应出报时限已过，尚未看到新报告" : "来源超出时效阈值");
    return reasons;
  }

  function safeUrl(value, base) {
    const text = String(value === null || value === undefined ? "" : value).trim();
    if (!text) return null;
    try {
      const url = new URL(text, base || "http://localhost/");
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  }

  function stateLabel(state) {
    return STATES[state] ? STATES[state].label : "证据不足";
  }

  /* The collector emits service health as ok/failed/stopped/disabled/unknown,
     while the page vocabulary is pass/fail/... . Without this mapping a real
     collector failure ('failed') fell through to the grey "证据不足". */
  function healthState(health) {
    if (health === "ok") return "pass";
    if (health === "failed") return "fail";
    if (health === "stopped") return "stopped";
    if (health === "disabled") return "disabled";
    if (health === "ended") return "ended";
    return "insufficient";
  }

  /* A rate the audit did not measure must not be filled in by the page.
     `null`, `undefined`, "" and booleans are unknown — they must never be
     coerced to 0, which would draw a 0% bar out of missing data. */
  function ratio(value, total) {
    if (typeof value !== "number" || typeof total !== "number") return null;
    if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0) return null;
    return Math.max(0, Math.min(1, value / total));
  }

  /* Numbers pass through; numeric strings are accepted; everything else
     (null/undefined/""/boolean/NaN) becomes null so it renders as “—”. */
  function finiteOrNull(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  /* The service snapshot has its own clock: a fresh report does not make an old
     service snapshot current, so the timeline judges itself. */
  function snapshotFreshness(snapshot, nowMs, stallSeconds) {
    const stamp = snapshot && (snapshot.checkedAt || snapshot.exportedAt);
    const parsed = parseInstant(stamp);
    if (parsed === null) return { checkedAt: stamp || null, ageSeconds: null, stalled: true };
    const ageSeconds = Math.round((nowMs - parsed) / 1000);
    const limit = Number.isFinite(stallSeconds) ? stallSeconds : STALL_SECONDS;
    return { checkedAt: stamp, ageSeconds,
             stalled: ageSeconds > limit || ageSeconds < -300 };
  }

  /* A long-open tab must not keep showing the freshness it saw at load time. */
  function needsRefresh(lastFetchMs, nowMs, intervalMs) {
    if (!Number.isFinite(lastFetchMs)) return true;
    return nowMs - lastFetchMs >= intervalMs;
  }

  /* Decide what the periodic tick should do. A failed first read must keep
     retrying: without a payload there is no banner and no other trigger, so an
     early failure would otherwise leave the page dead until a manual reload. */
  function nextAction(state, nowMs, retryMs, refreshMs) {
    const snapshot = state || {};
    if (!snapshot.payload) {
      return needsRefresh(snapshot.lastAttempt, nowMs, retryMs) ? "reload" : "idle";
    }
    if (needsRefresh(snapshot.fetchedAt, nowMs, refreshMs)) return "reload";
    return "refresh";
  }

  // These dates describe different evidence. Never derive the target day from
  // the inherited baseline, affected dates, last quote or audit timestamp.
  function legacyUnquotedEvidence(issue) {
    if (!issue || issue.code !== "qlib_legacy_unquoted_csi") return null;
    const recorded = (value) => value === null || value === undefined || value === ""
      ? "未记录" : typeof value === "string" ? value
      : typeof value === "boolean" ? (value ? "是" : "否") : JSON.stringify(value);
    const recordedDate = (value) => validDate(value) ? value : "未记录";
    const symbols = Array.isArray(issue.symbols)
      ? issue.symbols.filter((value) => typeof value === "string" && value.length) : [];
    const quoted = issue.lastQuotedDates && typeof issue.lastQuotedDates === "object"
      && !Array.isArray(issue.lastQuotedDates) ? issue.lastQuotedDates : {};
    const quoteSymbols = [...new Set([...symbols, ...Object.keys(quoted)])];
    const dates = Array.isArray(issue.dates) ? issue.dates.map(recordedDate) : [];
    const rows = [
      ["目标日", recordedDate(issue.targetDate)],
      ["受影响交易日", dates.length ? dates.join("、") : "未记录"],
      ["基线日", recordedDate(issue.legacyUnquotedBaseDate)],
      ["代码", symbols.length ? symbols.join("、") : "未记录"],
      ["最后报价日", quoteSymbols.length
        ? quoteSymbols.map((symbol) => `${symbol}：${recordedDate(quoted[symbol])}`).join("；") : "未记录"],
      ["含义", recorded(issue.meaning)], ["验证范围", recorded(issue.validationScope)],
      ["清单声明校验", recorded(issue.proven)], ["升级条件", recorded(issue.escalation)],
      ["说明限制", "仅核对本地清单；不代表停牌、报价恢复或对研究无影响"]
    ];
    return { label: "CSI 历史报价边界", targetDate: validDate(issue.targetDate) ? issue.targetDate : null,
      rows, summary: rows.slice(0, 5).map(([label, value]) => `${label} ${value}`).join("｜") };
  }

  const api = { parseInstant, reportFreshnessPolicy, recomputeFreshness, freshnessReasons, safeUrl, stateLabel,
                healthState, needsRefresh, nextAction, ratio, finiteOrNull, snapshotFreshness,
                legacyUnquotedEvidence,
                STATES, COVERAGE, STALL_SECONDS,
                INSTANT_RE, FRESHNESS_TICK_MS, REFRESH_MS, RETRY_MS };
  global.NNG8 = api;

  if (typeof document === "undefined") return; // Node test load: no DOM work.

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function stateBadge(state, extraClass) {
    const info = STATES[state] || STATES.insufficient;
    return el("span", `badge ${info.className}${extraClass ? ` ${extraClass}` : ""}`, stateLabel(state));
  }

  /* 常开页面的局部状态要能就地过期：tick 时重新判并回写，不用重读也不用重渲染。 */
  function setStateBadge(node, state, extraClass) {
    const info = STATES[state] || STATES.insufficient;
    node.className = `badge ${info.className}${extraClass ? ` ${extraClass}` : ""}`;
    node.textContent = stateLabel(state);
  }

  const updaters = [];
  let renderingUpdaters = null;

  function registerUpdater(fn) {
    (renderingUpdaters || updaters).push(fn);
    return fn;
  }

  function coverageBadge(state) {
    const info = COVERAGE[state] || COVERAGE.unknown;
    return el("span", `badge ${info.className}`, info.label);
  }

  function localTime(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "时间不可解析";
    return parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
  }

  function ageLabel(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const hours = Math.floor(seconds / 3600);
    if (hours >= 1) return `${hours} 小时前`;
    return `${Math.max(0, Math.floor(seconds / 60))} 分钟前`;
  }

  function percent(value, digits) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    return `${(Number(value) * 100).toFixed(digits === undefined ? 2 : digits)}%`;
  }

  /* Locale formatting for display only; never used to decide a state. */
  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (!Number.isFinite(Number(value))) return String(value);
    return Number(value).toLocaleString("zh-CN");
  }

  function freshnessBanner(payload, freshness) {
    const holder = el("div", "freshness-banner mb-3");
    fillFreshnessBanner(holder, payload, freshness);
    return holder;
  }

  function fillFreshnessBanner(holder, payload, freshness) {
    holder.replaceChildren();
    holder.classList.toggle("is-stale", Boolean(freshness && freshness.stale));
    if (freshness.stale) {
      const reasons = freshnessReasons(freshness);
      holder.appendChild(el("div", "alert alert-danger",
        `数据不可按当前正常解读：${reasons.join("、") || "来源标记为过期"}。`
        + `来源时间 ${localTime(payload.sourceTime)}（${ageLabel(freshness.ageSeconds)}，`
        + (freshness.policy ? `应出报检查时限 ${localTime(freshness.policy.deadlineAt)}`
          : `阈值 ${freshness.thresholdHours} 小时`)
        + `）；导出时间 ${localTime(payload.exportedAt)}`
        + `（${ageLabel(freshness.exportAgeSeconds)}）。`));
    } else if (freshness.policy) {
      const policy = freshness.policy;
      const pending = freshness.cadenceState === "pending";
      holder.appendChild(el("div", pending ? "alert alert-warning" : "alert alert-info",
        (pending ? "等待本次报告。" : "最近交易日报告。")
        + `当前展示 ${policy.reportDate} 报告，来源时间 ${localTime(payload.sourceTime)}；`
        + `下一交易日 ${policy.nextTradingDate}，计划出报 ${localTime(policy.expectedAt)}。`
        + (pending ? `应出报检查时限 ${localTime(policy.deadlineAt)}；尚未确认本次新报告。`
          : "尚未到下一次计划出报时间；不因跨非交易日累计小时数判为过期。")
        + `导出时间 ${localTime(payload.exportedAt)}（${ageLabel(freshness.exportAgeSeconds)}）。`
        + "报告日期与数据截止不变，不代表今天新增数据或投递已经确认。"));
    }
    if (payload.readErrors && payload.readErrors.length) {
      holder.appendChild(el("div", "alert alert-warning",
        `读取缺口：${payload.readErrors.join("、")}。缺失部分按“证据不足”处理。`));
    }
  }

  function sourceFreshnessLabel(freshness) {
    if (freshness.stale) return "已过期";
    if (freshness.cadenceState === "pending") return "等待本次报告";
    return freshness.policy ? "最近交易日报告" : "在阈值内";
  }

  function sourceFreshnessNote(freshness) {
    return freshness.policy
      ? `报告日 ${freshness.policy.reportDate}｜下一次 ${localTime(freshness.policy.expectedAt)}`
      : `阈值 ${freshness.thresholdHours} 小时｜${ageLabel(freshness.ageSeconds)}`;
  }

  /* Official demo pages keep the header for the title only and put any
     secondary line at the top of the body in text-secondary. */
  function cardOuter(title, subtitle) {
    const wrapper = el("div", "card");
    const header = el("div", "card-header");
    header.appendChild(el("h3", "card-title", title));
    wrapper.appendChild(header);
    const body = el("div", "card-body");
    if (subtitle) body.appendChild(el("div", "text-secondary small mb-2", subtitle));
    wrapper.appendChild(body);
    return { wrapper, body, header };
  }

  /* Tabler's official statistic card: subheader, big number, small note.
     Returns the nodes so a tick can update the ones that depend on the clock. */
  function statCard(title, value, note, badgeState) {
    const wrapper = el("div", "card");
    const body = el("div", "card-body");
    body.appendChild(el("div", "subheader", title));
    const line = el("div", "d-flex align-items-baseline");
    const valueNode = el("div", "h1 mb-0 me-2", value);
    line.appendChild(valueNode);
    if (badgeState) line.appendChild(stateBadge(badgeState));
    body.appendChild(line);
    const noteNode = el("div", "text-secondary small mt-1", note || "");
    body.appendChild(noteNode);
    wrapper.appendChild(body);
    return { wrapper, valueNode, noteNode };
  }

  /* Progress bar. `value`/`total` must both come from the export: when either
     is missing the bar renders as an explicit "证据不足" row instead. */
  function progressRow(title, value, total, note, className) {
    const row = el("div", "mb-3");
    const head = el("div", "d-flex justify-content-between align-items-baseline mb-1");
    const shown = finiteOrNull(total);
    const numerator = finiteOrNull(value);
    head.appendChild(el("div", "text-secondary", title));
    head.appendChild(el("div", "small text-secondary",
      shown === null ? "未知（未绘制）"
        : (numerator === null ? `未知 / ${formatNumber(total)}`
                              : `${formatNumber(value)} / ${formatNumber(total)}`)));
    row.appendChild(head);
    const bar = el("div", "progress progress-sm");
    const inner = el("div", `progress-bar ${className || "bg-primary"}`);
    const fraction = ratio(finiteOrNull(value), finiteOrNull(total));
    if (fraction !== null) {
      inner.style.width = `${(fraction * 100).toFixed(1)}%`;
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      inner.appendChild(el("span", "visually-hidden", `${Math.round(fraction * 100)}%`));
    } else {
      inner.appendChild(el("span", "visually-hidden", "无可用分母，未绘制比例"));
    }
    bar.appendChild(inner);
    row.appendChild(bar);
    if (note) row.appendChild(el("div", "small text-secondary mt-1", note));
    return row;
  }

  function renderBlocks(body, blocks, sources) {
    for (const block of blocks) {
      const kind = block.kind || "paragraph";
      if (kind.startsWith("h")) {
        body.appendChild(el(`h${Math.min(6, Math.max(1, Number(kind.slice(1)) + 1))}`, "mt-3", block.text));
        continue;
      }
      const paragraph = el("p", kind === "bullet" ? "mb-1" : "mb-2");
      if (kind === "bullet") paragraph.textContent = "• ";
      paragraph.appendChild(document.createTextNode(block.text));
      for (const ref of block.refs || []) {
        const link = el("a", "ms-1", `[来源 ${ref}]`);
        link.href = `#source-${ref}`;
        paragraph.appendChild(link);
      }
      for (const url of block.urls || []) {
        const safe = safeUrl(url);
        if (!safe) continue;
        paragraph.appendChild(document.createTextNode(" "));
        const link = el("a", "ms-1", "[链接]");
        link.href = safe;
        link.rel = "noreferrer noopener";
        paragraph.appendChild(link);
      }
      body.appendChild(paragraph);
    }
    if (sources && sources.length) {
      const details = el("details", "mt-3");
      details.appendChild(el("summary", "text-secondary", `来源与证据（${sources.length} 条，默认折叠）`));
      const list = el("ol", "mt-2");
      for (const source of sources) {
        const item = el("li", "mb-1");
        item.id = `source-${source.id}`;
        item.appendChild(document.createTextNode(source.text));
        const safe = source.url ? safeUrl(source.url) : null;
        if (safe) {
          item.appendChild(document.createTextNode(" "));
          const link = el("a", null, "打开来源");
          link.href = safe;
          link.rel = "noreferrer noopener";
          item.appendChild(link);
        }
        list.appendChild(item);
      }
      details.appendChild(list);
      body.appendChild(details);
    }
  }

  function rawDetails(title, value) {
    const details = el("details", "mt-3");
    details.appendChild(el("summary", "text-secondary", title));
    const pre = el("pre", "small mt-2");
    pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    details.appendChild(pre);
    return details;
  }

  function linkButton(label, href, primary) {
    const link = el("a", `btn ${primary ? "btn-primary" : ""}`.trim(), label);
    link.href = href;
    return link;
  }

  function definitionRow(label, value) {
    const row = el("div", "datagrid-item mb-2");
    row.appendChild(el("div", "datagrid-title text-secondary", label));
    row.appendChild(el("div", "datagrid-content", value === null || value === undefined ? "—" : value));
    return row;
  }

  function reasonList(body, reasons) {
    if (!reasons || !reasons.length) return;
    const list = el("ul", "mb-0 small text-secondary");
    for (const reason of reasons) list.appendChild(el("li", null, reason));
    body.appendChild(list);
  }

  /* ------------------------------------------------------------ 今日（首页） */

  function renderToday(payload, freshness, extras) {
    const fragment = document.createDocumentFragment();
    fragment.appendChild(freshnessBanner(payload, freshness));
    const report = payload.report;
    const manifest = extras && extras.manifest;
    const run = extras && extras.run;

    const stats = el("div", "row row-deck row-cards mb-3");
    const cards = [];
    const cells = [
      ["目标日", report ? report.targetDate : "—", report ? "报告与密封件对应日期" : "没有可显示的正式报告"],
      ["数据状态", report && report.dataStatus ? report.dataStatus : "—",
        report && report.publicationGate ? `门禁 ${report.publicationGate}` : "门禁未记录"],
      ["数据截止", report && report.cutoffLocal ? report.cutoffLocal : "—",
        report && report.variant ? `变体 ${report.variant}` : "生成方式未记录"],
      ["来源时效", sourceFreshnessLabel(freshness), sourceFreshnessNote(freshness)]
    ];
    for (const [title, value, note] of cells) {
      const col = el("div", "col-sm-6 col-lg-3");
      const card = statCard(title, String(value), note);
      cards.push(card);
      col.appendChild(card.wrapper);
      stats.appendChild(col);
    }
    fragment.appendChild(stats);
    // 这张卡也随时间走：不重读也能把“来源时效”改到过期。
    registerUpdater(() => {
      const now = recomputeFreshness(payload, Date.now());
      cards[3].valueNode.textContent = sourceFreshnessLabel(now);
      cards[3].noteNode.textContent = sourceFreshnessNote(now);
    });

    const spread = el("div", "row row-cards");
    const conclusionCol = el("div", "col-12 col-lg-7");
    const conclusion = cardOuter("今日结论",
      report ? `${report.targetDate}｜数据状态 ${report.dataStatus || "—"}` : "无报告");
    const lines = (report && report.conclusion) || [];
    if (!lines.length) {
      conclusion.body.appendChild(el("p", "text-secondary mb-0", "报告未提供可显示的结论行。"));
    }
    for (const block of lines) {
      const line = el("p", "mb-1");
      line.appendChild(document.createTextNode(block.text));
      for (const ref of block.refs || []) {
        const link = el("a", "ms-1", `[来源 ${ref}]`);
        link.href = `#source-${ref}`;
        line.appendChild(link);
      }
      conclusion.body.appendChild(line);
    }
    const actions = el("div", "btn-list mt-3");
    if (report && report.targetDate) {
      actions.appendChild(linkButton("查看完整报告", `report.html?date=${encodeURIComponent(report.targetDate)}`, true));
    }
    actions.appendChild(linkButton("数据进度与日期覆盖", "progress.html"));
    actions.appendChild(linkButton("投递与验收证据", "evidence.html"));
    conclusion.body.appendChild(actions);
    conclusionCol.appendChild(conclusion.wrapper);
    spread.appendChild(conclusionCol);

    const issuesCol = el("div", "col-12 col-lg-5");
    const issues = cardOuter("需要处理的问题", "只列已核验的缺口与未确认项，不写推断");
    const focus = payload.focus || [];
    const reportFocus = focus.filter((item) => item.kind === "report_limitation" || item.kind === "risk"
      || (typeof item.source === "string" && item.source.startsWith("report")));
    const currentFocus = focus.filter((item) => item.kind !== "historical_gap" && !reportFocus.includes(item));
    const historicalFocus = focus.filter((item) => item.kind === "historical_gap");
    const deliveryBadges = [];
    const currentBlock = el("section", "attention-current mb-3");
    currentBlock.appendChild(el("div", "text-secondary small mb-1", "已记录的问题与未确认项"));
    if (!currentFocus.length) {
      currentBlock.appendChild(el("p", "text-secondary small mb-0",
        "本批次未列出其他问题；不据此认定当前正常。"));
    } else {
      const list = el("ul", "list-unstyled mb-0");
      for (const item of currentFocus) {
        const entry = el("li", "mb-2");
        const badge = stateBadge(item.status === "fail" ? "fail" : "insufficient");
        entry.appendChild(badge);
        entry.appendChild(document.createTextNode(` ${item.text || "未记录具体问题"}`));
        if (item.kind === "delivery") {
          entry.appendChild(el("div", "text-secondary small",
            `对应报告日：${item.targetDate || "未记录"}；来源记录时间：${item.sourceTime || "未记录"}；`
            + `本次检查：${item.checkedAt || "未记录"}；来源：${item.sourceFile || "未记录"}`));
          const sourceNote = el("div", "text-secondary small");
          entry.appendChild(sourceNote);
          deliveryBadges.push({ badge, note: sourceNote, status: item.status });
        } else {
          entry.appendChild(el("div", "text-secondary small",
            `审计时间：${item.auditTime || item.sourceTime || "未记录"}；新增与否：待核实`));
        }
        list.appendChild(entry);
      }
      currentBlock.appendChild(list);
    }
    issues.body.appendChild(currentBlock);

    const historical = extras && extras.progress && extras.progress.historicalGaps;
    if (historicalFocus.length || historical) {
      const block = el("section", "attention-history mb-3");
      block.appendChild(el("div", "text-secondary small mb-1",
        "历史缺口（保留记录、不补造；与当前故障分开）"));
      if (historical) {
        const critical = Array.isArray(historical.criticalDates) ? historical.criticalDates : null;
        const legacy = Array.isArray(historical.legacyDates) ? historical.legacyDates : null;
        block.appendChild(el("div", "text-secondary small mb-2",
          `未解决缺口日：${critical ? critical.length : "未记录"}；遗留问题日：${legacy ? legacy.length : "未记录"}；`
          + `基线日期：${historical.baselineDate || "未记录"}；审计时间：${historical.observedAt || "未记录"}。`
          + "不回填、不补造；以下仅展示已有证据。"));
      }
      const list = el("ul", "list-unstyled mb-0");
      for (const item of historicalFocus) {
        const entry = el("li", "mb-2");
        entry.appendChild(el("span", "badge bg-secondary-lt text-secondary me-2", "历史缺口"));
        entry.appendChild(document.createTextNode((item.text || "").replace(/^历史缺口 /, "")));
        entry.appendChild(el("div", "text-secondary small", `审计时间：${item.auditTime || "未记录"}`));
        list.appendChild(entry);
      }
      block.appendChild(list);
      const evidence = historical && Array.isArray(historical.evidence) ? historical.evidence : [];
      const details = el("details", "historical-evidence mt-2");
      details.appendChild(el("summary", "small", `逐日已有证据（${evidence.length} 条）`));
      if (!evidence.length) {
        details.appendChild(el("p", "text-secondary small", "未读取到逐日证据；不推断缺失来源。"));
      } else {
        const holder = el("div", "table-responsive mt-2");
        const table = el("table", "table table-sm table-vcenter");
        const head = el("thead");
        const heading = el("tr");
        for (const label of ["日期", "快照 / 暂存", "真实观察时间", "记录哈希", "已记录原因"]) {
          heading.appendChild(el("th", null, label));
        }
        head.appendChild(heading);
        table.appendChild(head);
        const body = el("tbody");
        for (const item of evidence) {
          const row = el("tr");
          const known = (value) => value && value !== "unknown" ? String(value) : "未记录";
          const observed = parseInstant(item.stageObservedAt) === null ? "未记录" : item.stageObservedAt;
          const hash = typeof item.payloadSha256 === "string" && /^[a-f0-9]{64}$/i.test(item.payloadSha256)
            ? item.payloadSha256 : "未记录";
          for (const value of [known(item.date), `${known(item.snapshot)} / ${known(item.stage)}`,
                               observed, hash, known(item.snapshotReason)]) {
            row.appendChild(el("td", "small text-break", value));
          }
          body.appendChild(row);
        }
        table.appendChild(body);
        holder.appendChild(table);
        details.appendChild(holder);
      }
      block.appendChild(details);
      block.appendChild(linkButton("查看完整日期覆盖", "progress.html"));
      issues.body.appendChild(block);
    }
    if (reportFocus.length) {
      const block = el("section", "attention-report-limitations mb-3");
      block.appendChild(el("div", "text-secondary small mb-1", "报告当时的数据限制（原文记录，不代表当前状态）"));
      const list = el("ul", "list-unstyled mb-0");
      for (const item of reportFocus) {
        const entry = el("li", "mb-2");
        entry.appendChild(document.createTextNode(item.text || ""));
        const date = item.reportDate || (typeof item.source === "string" && item.source.startsWith("report:")
          ? item.source.slice(7) : null);
        entry.appendChild(el("div", "text-secondary small",
          `来自 ${date || "日期未记录"} ${item.sealVerified === true ? "密封报告" : "报告文件"}，当时措辞；`
          + `来源截止：${item.sourceTime || "未记录"}`));
        // Never render legacy supersededBy: keyword matches cannot establish a current rule or verification.
        if (typeof item.reportSha256 === "string" && /^[a-f0-9]{64}$/i.test(item.reportSha256)) {
          entry.appendChild(rawDetails("报告文件哈希", { reportSha256: item.reportSha256 }));
        }
        for (const ref of item.refs || []) {
          const link = el("a", "ms-1", `[来源 ${ref}]`);
          link.href = date ? `report.html?date=${encodeURIComponent(date)}#source-${encodeURIComponent(ref)}`
            : "report.html";
          entry.appendChild(link);
        }
        list.appendChild(entry);
      }
      block.appendChild(list);
      issues.body.appendChild(block);
    }
    const staleNotice = el("div", "attention-freshness mt-2");
    issues.body.appendChild(staleNotice);
    let batchFreshness = null;
    const updateAttentionFreshness = () => {
      const now = Date.now();
      const loaded = { "today.json": payload, "progress.json": extras && extras.progress,
                       "delivery.json": extras && extras.delivery };
      const stale = new Set();
      const unknown = [];
      for (const [name, value] of Object.entries(loaded)) {
        if (!value) unknown.push(name);
        else if (recomputeFreshness(value, now).stale) stale.add(name);
      }
      if (manifest && manifest.files) {
        for (const [name, value] of Object.entries(manifest.files)) {
          if (value && value.stale) stale.add(name);
        }
      } else unknown.push("manifest.json");
      const descriptions = [];
      if (stale.size) descriptions.push(`导出物已过期：${[...stale].sort().join("、")}。过期按过期展示，不写成当前正常。`);
      if (unknown.length) descriptions.push(`未读取到时效证据：${unknown.join("、")}；不能认定没有过期。`);
      if (!descriptions.length) descriptions.push("本页已读取来源未超出各自的报告日历或时效规则；不等于今天新增数据或所有业务正常。");
      staleNotice.className = `attention-freshness mt-2 small ${stale.size || unknown.length
        ? "alert alert-warning" : "text-secondary"}`;
      staleNotice.textContent = descriptions.join(" ");
      if (batchFreshness) batchFreshness.textContent = descriptions.join(" ");
      const deliveryStale = !loaded["delivery.json"] || recomputeFreshness(loaded["delivery.json"], now).stale;
      for (const item of deliveryBadges) {
        setStateBadge(item.badge, !deliveryStale && item.status === "fail" ? "fail" : "insufficient");
        item.note.textContent = deliveryStale ? "投递来源已过期或未读取；以上只代表该报告日记录，当前状态未确认。" : "";
      }
    };
    registerUpdater(updateAttentionFreshness);
    if (run && run.pendingAuthorization) {
      const pending = el("div", "mt-2");
      pending.appendChild(el("div", "text-secondary small", "待授权事项"));
      const list = el("ul", "small mb-0");
      for (const item of run.pendingAuthorization) list.appendChild(el("li", null, item));
      pending.appendChild(list);
      issues.body.appendChild(pending);
    }
    issuesCol.appendChild(issues.wrapper);
    spread.appendChild(issuesCol);
    fragment.appendChild(spread);

    const keyWrap = el("div", "mt-3");
    keyWrap.appendChild(keyDataCard(extras));
    fragment.appendChild(keyWrap);

    {
      const exportCard = cardOuter("导出批次", manifest
        ? `导出时间 ${localTime(manifest.exportedAt)}｜schema ${manifest.schemaVersion}` : "批次清单未读取");
      const grid = el("div", "datagrid");
      const names = manifest && manifest.files ? Object.keys(manifest.files) : [];
      grid.appendChild(definitionRow("文件", names.length ? names.join("、") : "—"));
      grid.appendChild(definitionRow("历史报告页", manifest && manifest.history ? String(manifest.history.count) : "—"));
      exportCard.body.appendChild(grid);
      batchFreshness = el("div", "batch-freshness small mt-2");
      exportCard.body.appendChild(batchFreshness);
      exportCard.body.appendChild(el("div", "text-secondary small",
        "页面只读取导出结果；刷新不会触发采集、密封、投递或任何生产任务。"));
      const wrap = el("div", "mt-3");
      wrap.appendChild(exportCard.wrapper);
      fragment.appendChild(wrap);
    }
    updateAttentionFreshness();
    return fragment;
  }

  /* 关键数据：首页只放导出里真实存在的数字，缺一个就不画那一条。 */
  function keyDataCard(extras) {
    const progress = (extras && extras.progress) || null;
    const delivery = (extras && extras.delivery) || null;
    const coverage = (progress && progress.coverage) || {};
    const qlib = coverage.qlibCoverage || null;
    const { wrapper, body } = cardOuter("关键数据", "全部来自导出结果；缺一个数字就不画那一条");
    const alertFor = (state, label) => {
      if (!state || !state.stale) return [];
      const why = freshnessReasons(state).join("、") || "来源标记为过期";
      return [el("div", "alert alert-warning mb-2",
        `${label}来源已过期（${why}，${ageLabel(state.ageSeconds)}，`
        + (state.policy ? `应出报检查时限 ${localTime(state.policy.deadlineAt)}` : `阈值 ${state.thresholdHours} 小时`)
        + "）："
        + "以下数值只代表当时，不作为当前状态。")];
    };
    const progressAlertSlot = el("div");
    body.appendChild(progressAlertSlot);
    const row = el("div", "row");
    const left = el("div", "col-12 col-md-6");
    const qlibRatio = qlib ? finiteOrNull(qlib.ratio) : null;
    if (qlibRatio !== null) {
      left.appendChild(progressRow(`Qlib 当日覆盖（${qlib.targetDate || "日期未记录"}）`,
        qlib.valid, qlib.eligible, `覆盖 ${percent(qlibRatio)}｜状态 ${qlib.status || "未记录"}`,
        "bg-primary"));
    } else {
      left.appendChild(el("div", "text-secondary small mb-3",
        "Qlib 覆盖：审计未提供分子与分母，未绘制比例。"));
    }
    row.appendChild(left);
    const right = el("div", "col-12 col-md-6");
    right.appendChild(progressRow("快照有效日期占比", coverage.validSnapshotDates,
      coverage.knownDates, coverage.auditSince ? `审计自 ${coverage.auditSince}` : null,
      "bg-green"));
    row.appendChild(right);
    body.appendChild(row);
    const counts = el("div", "row row-cards");
    const cells = [
      ["未解决缺口", finiteOrNull(coverage.criticalDates) === null
        ? "—" : formatNumber(coverage.criticalDates), "交易日缺口，未回填"],
      ["历史遗留", finiteOrNull(coverage.legacyDates) === null
        ? "—" : formatNumber(coverage.legacyDates), "早期非有限值行，保留记录"],
      ["已观察日期", finiteOrNull(coverage.knownDates) === null
        ? "—" : formatNumber(coverage.knownDates),
       coverage.referenceCompleteDate ? `参考完整日 ${coverage.referenceCompleteDate}` : "审计起点未记录"],
      ["快照有效日期", finiteOrNull(coverage.validSnapshotDates) === null
        ? "—" : formatNumber(coverage.validSnapshotDates),
       progress && progress.snapshot && progress.snapshot.latestValidDate
         ? `审计判有效；最新 ${progress.snapshot.latestValidDate}` : "有效性取审计状态"]
    ];
    for (const [title, value, note] of cells) {
      const col = el("div", "col-sm-6 col-lg-3");
      col.appendChild(statCard(title, String(value), note).wrapper);
      counts.appendChild(col);
    }
    body.appendChild(counts);
    const deliveryAlertSlot = el("div");
    body.appendChild(deliveryAlertSlot);
    const layers = (delivery && delivery.layers) || null;
    const line = el("div", "mt-3");
    line.appendChild(el("div", "text-secondary small mb-1", "投递三层（缺证据即证据不足）"));
    const badges = el("div");
    const layerBadges = [];
    if (layers) {
      for (const key of ["seal", "telegram", "relay"]) {
        const layer = layers[key] || { status: "insufficient" };
        badges.appendChild(el("span", "me-2", `${LAYER_LABELS[key]}:`));
        const badge = stateBadge(layer.status);
        layerBadges.push({ node: badge, status: layer.status });
        badges.appendChild(badge);
        badges.appendChild(document.createTextNode(" "));
      }
    } else {
      badges.appendChild(el("span", "text-secondary small", "投递导出不可读，按证据不足处理。"));
    }
    line.appendChild(badges);
    body.appendChild(line);
    // 两块各自随时间过期：tick 时不重读也能把告警与徽标改过来。
    const applyKeyData = () => {
      const now = Date.now();
      const pState = progress ? recomputeFreshness(progress, now) : null;
      const dState = delivery ? recomputeFreshness(delivery, now) : null;
      progressAlertSlot.replaceChildren(...alertFor(pState, "进度"));
      deliveryAlertSlot.replaceChildren(...alertFor(dState, "投递"));
      for (const item of layerBadges) {
        setStateBadge(item.node, dState && dState.stale ? "insufficient" : item.status);
      }
    };
    applyKeyData();
    registerUpdater(applyKeyData);
    return wrapper;
  }

  /* ------------------------------------------------------------ 报告详情页 */

  function renderReportPage(payload, freshness, extras) {
    const fragment = document.createDocumentFragment();
    const dates = (extras && extras.dates) || [];
    const pickerCard = cardOuter("选择日期", "仅切换已导出的报告，不触发任何采集或投递");
    const pickerRow = el("div", "row g-2 align-items-center");
    const selectCol = el("div", "col-auto");
    const select = el("select", "form-select");
    for (const date of dates) {
      const option = el("option", null, date);
      option.value = date;
      if (payload.date === date) option.selected = true;
      select.appendChild(option);
    }
    select.addEventListener("change", () => {
      const next = select.value;
      if (next) global.location.assign(`report.html?date=${encodeURIComponent(next)}`);
    });
    selectCol.appendChild(select);
    pickerRow.appendChild(selectCol);
    pickerRow.appendChild(el("div", "col text-secondary small",
      `已导出 ${dates.length} 天；逾期或缺失按“证据不足”处理。`));
    pickerCard.body.appendChild(pickerRow);
    fragment.appendChild(pickerCard.wrapper);

    fragment.appendChild(freshnessBanner(payload, freshness));
    const report = payload.report;
    const bodyCard = cardOuter("完整报告",
      report ? `${report.targetDate}｜截止 ${report.cutoffLocal || "—"}｜数据状态 ${report.dataStatus || "—"}` : "无报告");
    if (!report) {
      bodyCard.body.appendChild(stateBadge("insufficient"));
      bodyCard.body.appendChild(el("p", "text-secondary small mt-2 mb-0", "没有可显示的正式报告。"));
    } else {
      renderBlocks(bodyCard.body, report.blocks || [], report.sources || []);
    }
    fragment.appendChild(bodyCard.wrapper);

    const tech = cardOuter("技术证据", "密封与生成口径；正文之外不改写任何字段");
    const grid = el("div", "datagrid");
    grid.appendChild(definitionRow("正文 sha256", report ? report.sha256 : "—"));
    grid.appendChild(definitionRow("数据截止", report ? report.cutoffLocal : "—"));
    grid.appendChild(definitionRow("发布门禁", report ? report.publicationGate : "—"));
    grid.appendChild(definitionRow("发布级别", report ? report.publicationLevel : "—"));
    grid.appendChild(definitionRow("生成变体", report ? report.variant : "—"));
    grid.appendChild(definitionRow("导出时间", payload.exportedAt));
    tech.body.appendChild(grid);
    tech.body.appendChild(rawDetails("导出载荷（原始 JSON）", {
      page: payload.page, date: payload.date, sourceTime: payload.sourceTime,
      stale: payload.stale, readErrors: payload.readErrors
    }));
    const techWrap = el("div", "mt-3");
    techWrap.appendChild(tech.wrapper);
    fragment.appendChild(techWrap);
    return fragment;
  }

  /* ------------------------------------------------------------ 数据进度页 */

  function coverageState(item) {
    if (!item) return "unknown";
    if (item.gap === "critical") return "critical";
    if (item.gap === "legacy") return "legacy";
    if (item.snapshot === "valid") return "valid";
    if (item.snapshot === "invalid") return "invalid";
    if (item.snapshot === "recorded") return "recorded";
    if (item.stage === "valid") return "staged";
    if (item.snapshot === "missing") return "missing";
    return "unknown";
  }

  function renderProgress(payload, freshness, extras) {
    const fragment = document.createDocumentFragment();
    fragment.appendChild(freshnessBanner(payload, freshness));
    if (Array.isArray(payload.contractIntervals)) {
      const intervals = cardOuter("审计合同区间（含起日、不含止日）");
      for (const item of payload.contractIntervals.slice(0, 16)) {
        if (item && ["research.core-market.v1", "research.full-snapshot.v1"].includes(item.activeContract)
            && validDate(item.fromInclusive) && (item.untilExclusive === null || validDate(item.untilExclusive)))
          intervals.body.appendChild(el("p", null, `${item.activeContract}：${item.fromInclusive} → ${item.untilExclusive || "未设止日"}`));
      }
      fragment.appendChild(intervals.wrapper);
    }
    if (payload.activeContract === "research.core-market.v1") {
      const contract = cardOuter("当前合同：核心市场摘要（非完整快照）");
      const coreMarket = payload.coreMarket || {};
      if (coreMarket.status === "not_required") contract.body.appendChild(el("p", null,
        `已核实 ${coreMarket.lastRun?.calendarTargetDate || "未记录"} 休市，无需新记录；上一交易日 ${coreMarket.lastRun?.previousTradingDate || "未记录"}。`));
      contract.body.appendChild(el("p", null, `生效交易日：${payload.effectiveTradeDate || "未记录"}。仅核 Qlib 市场摘要，不含完整价格归档；核心摘要通过不等于完整快照通过。以下旧快照统计与历史缺口仍属 research.full-snapshot.v1。`));
      contract.body.appendChild(el("p", null, `核心行情证据状态：${coreMarket.status || "insufficient"}；最近运行目标：${coreMarket.lastRun?.targetDate || "未记录"}；有效/应有日期：${coreMarket.validDates ?? "未知"}/${coreMarket.knownDates ?? "未知"}`));
      const coreQuality=coreMarket.quality;
      contract.body.appendChild(el("p", null, coreQuality
        ? `同次摘要价格覆盖 ${coreQuality.priceStatus}：有效 ${coreQuality.valid} / ${coreQuality.eligible}，未完全有效 ${coreQuality.invalid}；合同门禁通过不等于全部价格完整。`
        : coreMarket.status === "not_required" ? "本次休市不生成新的价格覆盖记录。" : "同次摘要价格覆盖证据不足。"));
      contract.body.appendChild(rawDetails("新合同独立证据", coreMarket));
      fragment.appendChild(contract.wrapper);
    } else if (payload.activeContract === "research.full-snapshot.v1" && payload.coreMarket?.historicalOnly === true) {
      const history = cardOuter("历史核心市场摘要（非完整快照）");
      history.body.appendChild(el("p", null, "当前已使用完整快照合同；历史核心行情记录继续保留，不代表本次运行成功，也不补齐旧完整快照缺口。"));
      history.body.appendChild(el("p", null, `历史有效/应有日期：${payload.coreMarket.validDates ?? "未知"}/${payload.coreMarket.knownDates ?? "未知"}；最近有效日期：${payload.coreMarket.latestValidDate || "未记录"}`));
      history.body.appendChild(rawDetails("历史核心行情独立证据", payload.coreMarket));
      fragment.appendChild(history.wrapper);
    } else if (payload.activeContract && payload.activeContract !== "research.full-snapshot.v1") {
      fragment.appendChild(el("p", "text-red", "当前合同未确认；不能认定快照成功。"));
    }
    const coverage = payload.coverage || {};
    const qlib = coverage.qlibCoverage || null;

    const stats = el("div", "row row-deck row-cards mb-3");
    const snapshot = payload.snapshot || {};
    const validDates = finiteOrNull(coverage.validSnapshotDates);
    const recordedDates = finiteOrNull(coverage.recordedSnapshotDates);
    const knownDates = finiteOrNull(coverage.knownDates);
    const cells = [
      [payload.activeContract === "research.core-market.v1" ? "历史完整快照有效日期" : "完整快照有效日期", validDates === null ? "—" : formatNumber(validDates),
        coverage.snapshotValiditySource === "audit_ledger"
          ? (snapshot.latestValidDate ? `审计判有效；最新 ${snapshot.latestValidDate}` : "审计账本判有效")
          : "审计未提供有效性，按未知处理"],
      ["完整快照合同内日期", knownDates === null ? "—" : formatNumber(knownDates),
        coverage.auditSince ? `审计自 ${coverage.auditSince}` : "审计起点未记录"],
      ["未解决缺口", finiteOrNull(coverage.criticalDates) === null
        ? "—" : formatNumber(coverage.criticalDates),
        "交易日缺口，未回填"],
      ["历史遗留", finiteOrNull(coverage.legacyDates) === null
        ? "—" : formatNumber(coverage.legacyDates),
        "早期非有限值行，保留记录"]
    ];
    for (const [title, value, note] of cells) {
      const col = el("div", "col-sm-6 col-lg-3");
      col.appendChild(statCard(title, String(value), note).wrapper);
      stats.appendChild(col);
    }
    fragment.appendChild(stats);

    const spread = el("div", "row row-cards");
    const barsCol = el("div", "col-12 col-lg-6");
    const bars = cardOuter("覆盖与进度", "只使用审计给出的分子与分母");
    const qlibRatio = qlib ? finiteOrNull(qlib.ratio) : null;
    if (qlibRatio !== null) {
      bars.body.appendChild(progressRow(
        `Qlib 当日覆盖（${qlib.targetDate || "日期未记录"}）`,
        qlib.valid, qlib.eligible,
        `覆盖 ${percent(qlibRatio)}｜状态 ${qlib.status || "未记录"}`, "bg-primary"));
    } else {
      const row = el("div", "mb-3");
      row.appendChild(el("div", "text-secondary", "Qlib 当日覆盖"));
      row.appendChild(el("div", "text-secondary small", "审计未提供覆盖分子/分母，未绘制比例。"));
      bars.body.appendChild(row);
    }
    bars.body.appendChild(progressRow(
      "快照有效日期占比", validDates, knownDates,
      coverage.snapshotValiditySource === "audit_ledger"
        ? (coverage.referenceCompleteDate ? `有效性取审计状态；参考完整日 ${coverage.referenceCompleteDate}`
                                          : "有效性取审计状态")
        : "审计未提供有效性账本，未绘制比例", "bg-green"));
    bars.body.appendChild(progressRow(
      "记录存在（不代表有效）", recordedDates, knownDates,
      "哈希只证明记录存在；有效性以审计状态为准", "bg-blue"));
    bars.body.appendChild(progressRow(
      "已暂存日期", coverage.stagedDates, knownDates,
      "暂存只代表当日落地证据，不代表快照已生效", "bg-yellow"));
    bars.body.appendChild(el("div", "text-secondary small",
      "本页不画趋势线：导出里没有逐日序列，画线等于编造。"));
    barsCol.appendChild(bars.wrapper);
    spread.appendChild(barsCol);

    const dateCol = el("div", "col-12 col-lg-6");
    const dateCard = cardOuter("日期覆盖", "每个方块 = 一个已观察日期；空格子不补");
    const legs = el("div", "mb-2");
    for (const key of ["valid", "recorded", "staged", "critical", "legacy", "invalid", "missing", "unknown"]) {
      const item = el("span", "me-2");
      item.appendChild(coverageBadge(key));
      legs.appendChild(item);
    }
    dateCard.body.appendChild(legs);
    const dates = coverage.dates || [];
    if (!dates.length) {
      dateCard.body.appendChild(el("p", "text-secondary small mb-0", "审计未提供日期状态，按证据不足处理。"));
    }
    const grid = el("div", "row g-1");
    for (const item of dates) {
      const cell = el("div", "col-auto");
      const box = el("div", `badge ${(COVERAGE[coverageState(item)] || COVERAGE.unknown).className}`,
        item.date);
      box.title = `快照 ${item.snapshot || "unknown"}${item.snapshotRecorded ? "（有记录）" : ""}`
        + `${item.snapshotReason ? `（${item.snapshotReason}）` : ""}`
        + `｜暂存 ${item.stage || "unknown"}`
        + `｜归类 ${item.classification || "unclassified"}`
        + (item.gap ? `｜缺口 ${item.gap}` : "");
      cell.appendChild(box);
      grid.appendChild(cell);
    }
    dateCard.body.appendChild(grid);
    dateCol.appendChild(dateCard.wrapper);
    spread.appendChild(dateCol);
    fragment.appendChild(spread);

    const timeline = cardOuter("任务时间线", "来自只读服务快照；最近一次执行与下一次触发");
    const snapshotFile = (extras && extras.services) || null;
    const units = snapshotFile && Array.isArray(snapshotFile.units) ? snapshotFile.units.slice() : [];
    const alertSlot = el("div");
    timeline.body.appendChild(alertSlot);
    const unitRows = [];
    if (!units.length) {
      timeline.body.appendChild(el("p", "text-secondary small mb-0", "服务快照不可读，按证据不足处理。"));
    } else {
      units.sort((left, right) => String(right.lastStart || right.lastExit || "")
        .localeCompare(String(left.lastStart || left.lastExit || "")));
      const list = el("ul", "timeline mb-0");
      for (const unit of units.slice(0, 16)) {
        const item = el("li", "timeline-event");
        const dot = el("div", `timeline-event-icon ${unit.health === "ok" ? "bg-green" : "bg-secondary"}`);
        item.appendChild(dot);
        const event = el("div", "timeline-event-card card");
        const cardBody = el("div", "card-body");
        const head = el("div", "d-flex justify-content-between");
        head.appendChild(el("div", null, unit.unit));
        const unitBadge = stateBadge(healthState(unit.health), "unit-state");
        head.appendChild(unitBadge);
        cardBody.appendChild(head);
        cardBody.appendChild(el("div", "text-secondary small mt-1",
          `最近执行 ${localTime(unit.lastStart || unit.lastExit)}｜下次 ${unit.nextElapse ? localTime(unit.nextElapse) : "—"}`));
        const noteNode = el("div", "text-secondary small", unit.note || "");
        cardBody.appendChild(noteNode);
        unitRows.push({ unit, badge: unitBadge, note: noteNode });
        event.appendChild(cardBody);
        item.appendChild(event);
        list.appendChild(item);
      }
      timeline.body.appendChild(list);
    }
    // 服务快照有自己的时钟：tick 时就地过期，不依赖重新渲染。
    const applyTimeline = () => {
      const state = snapshotFreshness(snapshotFile, Date.now(), STALL_SECONDS);
      alertSlot.replaceChildren();
      if (snapshotFile && state.stalled) {
        alertSlot.appendChild(el("div", "alert alert-warning mb-3",
          `服务快照已过期：采集于 ${localTime(state.checkedAt)}`
          + `（${ageLabel(state.ageSeconds)}）。以下状态只代表采集当时，`
          + "不能当作当前服务状态；因此不显示“通过”。"));
      }
      for (const item of unitRows) {
        setStateBadge(item.badge,
          state.stalled ? "insufficient" : healthState(item.unit.health), "unit-state");
        if (item.unit.note) {
          item.note.textContent = state.stalled
            ? `快照过期，当时记录：${item.unit.note}` : item.unit.note;
        }
      }
    };
    applyTimeline();
    registerUpdater(applyTimeline);
    const timelineWrap = el("div", "mt-2");
    timelineWrap.appendChild(timeline.wrapper);
    fragment.appendChild(timelineWrap);

    const healthCard = cardOuter("审计问题基线",
      payload.health ? `审计时间 ${localTime(payload.health.generatedAt)}｜状态 ${payload.health.status}` : "审计不可读");
    const issues = Array.isArray(payload.health?.issues) ? payload.health.issues : null;
    const issueBadges = [];
    if (issues === null) {
      healthCard.body.appendChild(el("p", "text-secondary small mb-0", "审计问题列表未读取到，当前缺口日期与数量未确认。"));
    } else if (!issues.length) {
      healthCard.body.appendChild(el("p", "text-secondary small mb-0", "没有可显示的问题条目。"));
    }
    for (const issue of issues || []) {
      const row = el("div", "mb-2 audit-issue");
      const head = el("div");
      // 三级视觉区分：严重（红）/ 提示（黄，需要关注）/ 说明（蓝，已证明的有界缺失等）
      const severity = { critical: ["bg-red-lt text-red", "严重"],
                         warning: ["bg-yellow-lt text-yellow", "提示"],
                         information: ["bg-blue-lt text-blue", "说明"],
                         info: ["bg-blue-lt text-blue", "说明"] };
      const recordedFail = issue.status === "fail";
      const [badgeClass, badgeLabel] = recordedFail ? ["bg-red-lt text-red", "失败"]
        : severity[issue.severity] || ["bg-secondary-lt text-secondary", "证据不足"];
      const issueBadge = el("span", `badge ${badgeClass} me-2`, badgeLabel);
      head.appendChild(issueBadge);
      issueBadges.push({ node: issueBadge, badgeClass, badgeLabel, recordedFail });
      const boundary = legacyUnquotedEvidence(issue);
      head.appendChild(document.createTextNode(boundary ? boundary.label : issue.code));
      row.appendChild(head);
      const detail = [];
      if (boundary) {
        for (const [label, value] of boundary.rows) {
          row.appendChild(el("div", "small text-secondary", `${label}：${value}`));
        }
        row.appendChild(rawDetails("查看记录依据", issue));
      } else {
        if (issue.dates && issue.dates.length) detail.push(`日期 ${issue.dates.join("、")}`);
        if (issue.symbols && issue.symbols.length) detail.push(`代码 ${issue.symbols.join("、")}`);
      }
      if (detail.length) row.appendChild(el("div", "small text-secondary", detail.join("｜")));
      healthCard.body.appendChild(row);
    }
    const issueFreshness = el("p", "small text-secondary");
    healthCard.body.appendChild(issueFreshness);
    const applyIssueFreshness = () => {
      const stale = recomputeFreshness(payload, Date.now()).stale;
      issueFreshness.textContent = stale ? "审计来源已过期；以下只代表当时记录，当前状态未确认。" : "";
      for (const item of issueBadges) {
        item.node.className = `badge ${stale ? "bg-secondary-lt text-secondary" : item.badgeClass} me-2`;
        item.node.textContent = stale ? (item.recordedFail ? "未确认（当时失败）" : "未确认") : item.badgeLabel;
      }
    };
    applyIssueFreshness();
    registerUpdater(applyIssueFreshness);
    healthCard.body.appendChild(rawDetails("原始字段（qlib / snapshot / coverage）", {
      qlib: payload.qlib, snapshot: payload.snapshot, coverage: payload.coverage
    }));
    const healthWrap = el("div", "mt-3");
    healthWrap.appendChild(healthCard.wrapper);
    fragment.appendChild(healthWrap);
    return fragment;
  }

  /* --------------------------------------------------------- 投递与验收证据 */

  const LAYER_LABELS = { seal: "本地密封", telegram: "Telegram", relay: "接收端 relay" };

  function renderEvidence(payload, freshness) {
    const fragment = document.createDocumentFragment();
    fragment.appendChild(freshnessBanner(payload, freshness));
    const layers = payload.layers || {};
    const row = el("div", "row row-deck row-cards mb-3");
    const layerBadges = [];
    for (const key of ["seal", "telegram", "relay"]) {
      const layer = layers[key] || { status: "insufficient", evidence: {}, limitations: [] };
      const col = el("div", "col-12 col-lg-4");
      const wrapper = el("div", "card");
      const body = el("div", "card-body");
      const head = el("div", "d-flex justify-content-between align-items-baseline");
      head.appendChild(el("div", "subheader", LAYER_LABELS[key]));
      const badge = stateBadge(layer.status);
      layerBadges.push({ node: badge, status: layer.status });
      head.appendChild(badge);
      body.appendChild(head);
      const evidence = layer.evidence || {};
      for (const [label, value] of Object.entries(evidence)) {
        if (value === null || value === undefined || value === "") continue;
        // Flat objects become key=value pairs (readable and still factual);
        // nested ones are summarised by their keys. The full payload stays in
        // the folded raw section below.
        let text;
        if (value !== null && typeof value === "object") {
          const entries = Object.entries(value);
          text = entries.length && entries.every(([, item]) => item === null || typeof item !== "object")
            ? entries.map(([key, item]) => `${key}=${String(item).slice(0, 14)}`).join(", ")
            : entries.map(([key]) => key).join(", ") || "（空对象）";
        } else {
          text = String(value);
        }
        body.appendChild(el("div", "small text-secondary text-truncate",
          `${label}: ${text.length > 70 ? `${text.slice(0, 70)}…` : text}`));
      }
      if (!Object.keys(evidence).length) {
        body.appendChild(el("div", "small text-secondary", "没有可显示的证据字段。"));
      }
      if ((layer.limitations || []).length) {
        body.appendChild(el("div", "alert alert-secondary mt-2 mb-0 small",
          (layer.limitations || []).join("；")));
      }
      wrapper.appendChild(body);
      col.appendChild(wrapper);
      row.appendChild(col);
    }
    fragment.appendChild(row);

    const note = el("div", "alert alert-secondary",
      "判定口径：Telegram 需平台回执与完整哈希，relay 需接收端返回内容哈希；"
      + "两者只要缺少对应证据，只能是“证据不足”，不得写成端到端通过。");
    fragment.appendChild(note);

    const acceptance = payload.acceptance || {};
    const acceptCard = cardOuter("验收结论", `目标日 ${payload.targetDate || "—"}｜四态保留`);
    for (const phase of ["night", "monday"]) {
      const label = phase === "night" ? "夜间验收" : "周一验收";
      const record = acceptance[phase];
      const block = el("div", "mb-3");
      if (!record) {
        block.appendChild(el("div", "text-secondary", `${label}：尚无结果`));
        acceptCard.body.appendChild(block);
        continue;
      }
      block.appendChild(el("div", "text-secondary small",
        `${label}｜${localTime(record.generatedAt)}`));
      const badges = el("div", "mt-1");
      for (const [name, status] of Object.entries(record.statuses || {})) {
        badges.appendChild(el("span", "me-2", `${name}:`));
        badges.appendChild(stateBadge(status));
        badges.appendChild(document.createTextNode(" "));
      }
      block.appendChild(badges);
      reasonList(block, record.limitations || []);
      acceptCard.body.appendChild(block);
    }
    acceptCard.body.appendChild(rawDetails("分层证据原始字段", payload.layers || {}));
    fragment.appendChild(acceptCard.wrapper);
    // 整份投递导出过期时，分层徽标不得继续声称“通过”（tick 时就地生效）。
    const applyLayers = () => {
      const state = recomputeFreshness(payload, Date.now());
      for (const item of layerBadges) {
        setStateBadge(item.node, state.stale ? "insufficient" : item.status);
      }
    };
    applyLayers();
    registerUpdater(applyLayers);
    return fragment;
  }

  /* ------------------------------------------------------------ 服务与项目 */

  function boundSourceRepair(unit, nowMs) {
    const repair = unit.repair;
    if (unit.unit !== "astock-xhs-source.service" || unit.scope !== "user"
        || unit.kind !== "service" || unit.unitType !== "oneshot" || unit.health !== "failed"
        || unit.activeState !== "failed"
        || !repair || typeof repair !== "object" || Array.isArray(repair)
        || repair.state !== "deployed_pending_verification" || repair.codeHashesVerified !== true
        || typeof unit.invocationId !== "string" || !/^[a-f0-9]{32}$/i.test(unit.invocationId)
        || repair.failedInvocationId !== unit.invocationId
        || typeof repair.deployedCommit !== "string" || !/^[a-f0-9]{40}$/i.test(repair.deployedCommit)
        || typeof repair.proofSha256 !== "string" || !/^[a-f0-9]{64}$/i.test(repair.proofSha256)) return null;
    const stamp = (value) => typeof value === "string" && validDate(value.slice(0, 10))
      ? parseInstant(value) : null;
    const failed = stamp(repair.failedAt), exited = stamp(unit.lastExit);
    const recorded = stamp(repair.recordedAt), verified = stamp(repair.verifiedAt), expires = stamp(repair.expiresAt);
    const next = repair.nextRunAt === null ? null : stamp(repair.nextRunAt);
    if ([failed, exited, recorded, verified, expires].some((value) => value === null)
        || failed !== exited || failed >= recorded || recorded > verified || verified > nowMs
        || nowMs - verified > STALL_SECONDS * 1000 || expires <= nowMs
        || expires - recorded > 36 * 3600 * 1000
        || (repair.nextRunAt !== null && (next === null || next < recorded || next > expires))) return null;
    return repair;
  }

  function renderProjects(payload, freshness) {
    const fragment = document.createDocumentFragment();
    fragment.appendChild(freshnessBanner(payload, freshness));
    const services = cardOuter("服务快照", "服务运行正常不代表数据或投递正常");
    const table = el("table", "table table-sm table-vcenter");
    const head = el("tr");
    for (const text of ["单元", "级别", "状态", "说明", "下次"]) head.appendChild(el("th", null, text));
    table.appendChild(head);
    const serviceRows = [];
    for (const unit of payload.services || []) {
      const tr = el("tr", "service-row");
      tr.appendChild(el("td", null, unit.unit));
      tr.appendChild(el("td", "small text-secondary", unit.kind === "timer" ? "定时器" : "服务"));
      const state = el("td");
      const badge = stateBadge(healthState(unit.health), "service-state");
      state.appendChild(badge);
      tr.appendChild(state);
      const note = el("td", "small text-secondary service-note");
      const next = el("td", "small text-secondary service-next");
      tr.appendChild(note);
      tr.appendChild(next);
      serviceRows.push({ unit, badge, note, next });
      table.appendChild(tr);
    }
    const applyServices = () => {
      const now = Date.now(), stale = recomputeFreshness(payload, now).stale;
      for (const item of serviceRows) {
        const unit = item.unit, repair = boundSourceRepair(unit, now);
        setStateBadge(item.badge, stale ? "insufficient" : repair ? "pending" : healthState(unit.health), "service-state");
        if (stale) {
          item.note.textContent = unit.health === "failed"
            ? `服务快照已过期；上次执行 ${localTime(unit.lastExit)}失败；${unit.note || "原始说明未记录"}`
            : `服务快照已过期；当时记录：${unit.note || stateLabel(healthState(unit.health))}`;
          item.next.textContent = unit.nextElapse ? `当时记录：${unit.nextElapse}` : "—";
        } else if (repair) {
          item.badge.textContent = "待自然验收";
          item.note.textContent = `修复已部署；上次执行 ${localTime(unit.lastExit)}失败`;
          item.next.textContent = repair.nextRunAt ? localTime(repair.nextRunAt) : "未记录";
        } else {
          item.note.textContent = unit.note || "—";
          item.next.textContent = unit.nextElapse || "—";
        }
      }
    };
    applyServices();
    registerUpdater(applyServices);
    services.body.appendChild(table);
    if (!(payload.services || []).length) {
      services.body.appendChild(el("p", "text-secondary small mb-0", "服务快照不可读，按证据不足处理。"));
    }
    fragment.appendChild(services.wrapper);

    const spread = el("div", "row row-cards");
    const versionsCol = el("div", "col-12 col-lg-6");
    const versions = cardOuter("仓库版本", "本地 HEAD；远端授权未获批准前不推送");
    const vtable = el("table", "table table-sm table-vcenter");
    const vhead = el("tr");
    for (const text of ["仓库", "版本"]) vhead.appendChild(el("th", null, text));
    vtable.appendChild(vhead);
    for (const [name, value] of Object.entries(payload.versions || {})) {
      const tr = el("tr");
      tr.appendChild(el("td", null, name));
      tr.appendChild(el("td", "small", value.head ? `${String(value.head).slice(0, 12)}…` : "不可读"));
      vtable.appendChild(tr);
    }
    versions.body.appendChild(vtable);
    versionsCol.appendChild(versions.wrapper);
    spread.appendChild(versionsCol);

    const pendingCol = el("div", "col-12 col-lg-6");
    const pending = cardOuter("待授权事项", "未获批准前不动");
    const list = el("ul", "mb-0");
    for (const item of payload.pendingAuthorization || []) list.appendChild(el("li", null, item));
    if (!(payload.pendingAuthorization || []).length) {
      list.appendChild(el("li", "text-secondary", "没有登记的待授权事项。"));
    }
    pending.body.appendChild(list);
    pendingCol.appendChild(pending.wrapper);
    spread.appendChild(pendingCol);
    fragment.appendChild(spread);

    fragment.appendChild(rawDetails("服务与版本原始字段", {
      versions: payload.versions, services: payload.services, exportedAt: payload.exportedAt
    }));
    return fragment;
  }

  /* ------------------------------------------------------------ 载入与计时 */

  const VIEWS = {
    today: { file: "today.json", render: renderToday,
             extra: ["manifest.json", "progress.json", "delivery.json"] },
    report: { file: null, render: renderReportPage, extra: [] },
    progress: { file: "progress.json", render: renderProgress, extra: ["services.json"] },
    evidence: { file: "delivery.json", render: renderEvidence, extra: [] },
    projects: { file: "projects.json", render: renderProjects, extra: [] }
  };

  const state = { page: null, payload: null, fetchedAt: null, lastAttempt: null,
                  banner: null, target: null, extras: {} };

  async function readJson(name) {
    const response = await fetch(`data/${name}`, { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  function reportDate() {
    const params = new URLSearchParams(global.location.search || "");
    const value = params.get("date") || "";
    return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
  }

  async function loadExtras(names) {
    const result = {};
    for (const name of names) {
      try {
        result[name.replace(".json", "")] = await readJson(name);
      } catch {
        result[name.replace(".json", "")] = null;
      }
    }
    return result;
  }

  async function load(page) {
    const view = VIEWS[page];
    state.page = page;
    state.lastAttempt = Date.now();
    if (!state.payload) {
      state.target.replaceChildren(el("div", "text-secondary", "正在读取导出数据…"));
    }
    try {
      let payload;
      if (page === "report") {
        const dates = await collectReportDates();
        const date = reportDate() || dates[0];
        if (!date) throw new Error("no-reports");
        payload = await readJson(`reports/${date}.json`);
        state.extras = { dates };
      } else {
        payload = await readJson(view.file);
        state.extras = await loadExtras(view.extra || []);
      }
      if (page === "today") {
        state.extras = Object.assign({}, state.extras, { run: null });
      }
      const holder = document.createDocumentFragment();
      const nextUpdaters = [];
      // Keep the displayed page's clock checks until its replacement renders
      // successfully. Callbacks from a failed, detached render are discarded.
      renderingUpdaters = nextUpdaters;
      try {
        holder.appendChild(view.render(payload, recomputeFreshness(payload, Date.now()), state.extras));
      } finally {
        renderingUpdaters = null;
      }
      state.target.replaceChildren(holder);
      updaters.splice(0, updaters.length, ...nextUpdaters);
      state.payload = payload;
      state.fetchedAt = Date.now();
      state.banner = state.target.querySelector(".freshness-banner");
    } catch {
      if (!state.payload) {
        state.target.replaceChildren(el("div", "alert alert-secondary",
          "读取导出数据失败，按证据不足处理；页面会按间隔自动重试，且不会触发任何采集或生产任务。"));
      }
    }
  }

  async function collectReportDates() {
    const dates = new Set();
    try {
      const today = await readJson("today.json");
      for (const date of today.availableDates || []) dates.add(date);
      if (today.report && today.report.targetDate) dates.add(today.report.targetDate);
    } catch {
      // A missing today.json must not hide the history pages.
    }
    try {
      const manifest = await readJson("manifest.json");
      const pages = (manifest.history && manifest.history.pages) || {};
      for (const date of Object.keys(pages)) dates.add(date);
    } catch {
      // Same here: the date list degrades to whatever is readable.
    }
    return Array.from(dates).sort().reverse();
  }

  function markMode(mode) {
    const badge = document.getElementById("data-mode");
    if (!badge) return;
    const sample = mode === "sample";
    badge.textContent = sample ? "只读・示例数据" : "只读";
    badge.className = sample
      ? "badge bg-yellow-lt text-yellow"
      : "badge bg-green-lt text-green";
  }

  function tick() {
    const now = Date.now();
    // Always refresh the banner first: if reloads keep failing, the content on
    // screen is still the old export and must turn stale on its own.
    if (state.payload && state.banner) {
      fillFreshnessBanner(state.banner, state.payload, recomputeFreshness(state.payload, now));
    }
    // Then let every time-dependent block re-judge itself from the same clock:
    // a service snapshot or a sub-source ages even when the re-read keeps
    // failing, and it must not keep its green badge until the next reload.
    for (const update of updaters) {
      try {
        update();
      } catch {
        // One broken block must not stop the tick or the retry decision below.
      }
    }
    if (nextAction(state, now, RETRY_MS, REFRESH_MS) === "reload") {
      load(state.page);
    }
  }

  /* The official sidebar collapses below `lg`. Without Bootstrap's JS the
     toggler did nothing, so the menu could not be opened on a phone; this
     restores exactly the class the official markup expects (`show`). */
  function wireSidebarToggle() {
    const toggler = document.querySelector(".navbar-toggler");
    const menu = document.getElementById("sidebar-menu");
    if (!toggler || !menu || !menu.classList || !toggler.addEventListener) return;
    toggler.addEventListener("click", () => {
      const open = menu.classList.toggle("show");
      toggler.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function main() {
    const page = document.body.dataset.page;
    const mode = document.body.dataset.mode || "live";
    const target = document.getElementById("view");
    markMode(mode);
    wireSidebarToggle();
    const stamp = document.getElementById("rendered-at");
    if (stamp) stamp.textContent = `本地读取时间 ${localTime(new Date().toISOString())}`;
    if (!target || !VIEWS[page]) return;
    state.target = target;
    load(page);
    setInterval(tick, FRESHNESS_TICK_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });
  }

  document.addEventListener("DOMContentLoaded", main);
})(typeof globalThis !== "undefined" ? globalThis : this);
