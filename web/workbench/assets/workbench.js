/* Live presentation only: existing exported fields and existing time policy. */
"use strict";
(() => {
  if (document.body.dataset.page !== "workbench") return;
  const $ = id => document.getElementById(id);
  const core = window.NNG8;
  const model = window.NNG8WorkbenchModel;
  let loader;
  try { loader = window.NNG8WorkbenchLoader.create({ fetch: window.fetch.bind(window), crypto: window.crypto,
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window) }); }
  catch { $("load-error").hidden=false; $("load-error").textContent="浏览器无法完成来源完整性核对，请使用支持HTTPS与安全校验的浏览器；当前状态未确认。";
    $("report-headline").textContent="无法读取已核验的报告数据";return; }
  const state = { panels: {}, view: null, fetchedAt: null, lastAttempt: null, loading: false, errors: [], signature: "", notesSignature: "" };
  let opener = null, activeDetail = null;
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon small-icon"); svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`); svg.append(use); return svg;
  }
  function setText(id, text) { $(id).textContent = text ?? "未记录"; }
  function setOptionalText(id, text) { const node=$(id); if(node) node.textContent=text ?? "未记录"; }
  function day(date) { return typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date)
    ? `${Number(date.slice(5,7))}月${Number(date.slice(8,10))}日` : "未记录"; }
  function time(stamp) {
    if (typeof stamp === "string" && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(stamp)) return `${day(stamp.slice(0,10))} ${stamp.slice(11)} CST`;
    const parsed = core.parseInstant(stamp);
    return parsed === null ? "未记录" : new Date(parsed).toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) + " CST";
  }
  function number(value) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "未知"; }
  function status(value) {
    return ({pass:["已核对","success"],fail:["失败","danger"],warning:["提示","warning"],information:["说明","information"],pending:["待完成","warning"],
      not_required:["已核实休市，无需新记录","information"],stopped:["已停止","warning"],disabled:["未启用","neutral"]})[value] || ["未确认","neutral"];
  }
  function badge(value) { const [label,tone] = status(value); return el("span", `badge state-tag tone-${tone}`, label); }
  function row(label,value) { const item=el("div","detail-row"); item.append(el("span",null,label),el("span",null,value??"未记录")); return item; }
  function section(title,text) { const item=el("section","detail-section"); item.append(el("h3",null,title)); if(text) item.append(el("p",null,text)); return item; }
  function details(title, value) { const item=el("details","raw-detail"); item.append(el("summary",null,title),el("pre",null,JSON.stringify(value,null,2))); return item; }
  function issueRows(item) { return item.boundaryEvidence
    ? item.boundaryEvidence.map(([label,value])=>row(label,value)) : [row("对应日期",item.targetDate)]; }
  function fullReportUrl(v) { return v.report.date ? `report.html?date=${encodeURIComponent(v.report.date)}` : "report.html"; }
  function reportRefs(block,v) {
    const item=el("p",null,block.text);
    for (const ref of block.refs || []) { const link=el("a","source-ref",`[来源 ${ref}]`); link.href=`${fullReportUrl(v)}#source-${encodeURIComponent(ref)}`; item.append(link); }
    return item;
  }
  function paintMetric(key,item) {
    const known=typeof item.percent==="number" && Number.isFinite(item.percent);
    $(`${key}-track`).hidden=!known;
    if(known) { $(`${key}-fill`).style.width=`${item.percent}%`; $(`${key}-track`).setAttribute("aria-valuenow",String(item.percent));
      $(`${key}-track`).setAttribute("aria-valuemin","0"); $(`${key}-track`).setAttribute("aria-valuemax","100");
      $(`${key}-track`).setAttribute("aria-valuetext",item.label); }
    else $(`${key}-track`).removeAttribute("aria-valuenow");
  }
  function paintAttention(v) {
    const signature=JSON.stringify(v.attention);
    if(signature===state.signature) return;
    state.signature=signature;
    const box=$("attention-list"); box.replaceChildren();
    setText("pending-count",v.attention.length);
    $("pending-count").classList.toggle("is-empty",v.attention.length===0);
    setText("attention-link",`查看全部 ${v.attention.length} 项与依据`);
    setText("attention-context","告警与未确认项 · 不含数据说明和历史保留项");
    if(!v.attention.length) box.append(el("p","empty-note","本批次未列出其他问题；不据此认定一切正常。"));
    v.attention.slice(0,3).forEach((item,index)=>{
      const entry=el("article","attention-item"); const mark=el("div",`attention-icon ${item.status==="fail"?"is-failure":""}`);
      mark.append(icon(item.kind==="delivery"?"send":"info")); const body=el("div");
      const title=item.kind==="delivery" ? `${item.label} · ${status(item.status)[0]}` : item.label;
      const summary=item.text.startsWith(`${item.label}：`)?item.text.slice(item.label.length+1):item.text;
      body.append(el("h3",null,title),el("p","attention-summary",summary));
      const button=el("button","text-action","查看说明"); button.dataset.open=`attention:${index}`;
      button.setAttribute("aria-label",`查看${item.label}说明`); button.append(icon("chevron")); body.append(button); entry.append(mark,body); box.append(entry);
    });
  }
  function paintDataNotes(v) {
    // Keep audit explanations visible without counting them as action items.
    // The model still emits a source-stale attention item when they expire.
    const notes=v.dataNotes || [];
    const signature=JSON.stringify(notes);
    const container=$("data-notes");
    if(!container || signature===state.notesSignature)return;
    state.notesSignature=signature;
    container.hidden=notes.length===0;
    setText("data-notes-count",notes.length);
    const box=$("data-notes-list");box.replaceChildren();
    notes.forEach((item,index)=>{
      const entry=el("article","data-note-item");
      const head=el("div","data-note-heading");head.append(badge(item.status),el("h3",null,item.label));
      entry.append(head,el("p",null,item.text));
      entry.append(el("p","data-note-context",`${item.boundaryEvidence?"目标日":"对应日期"} ${item.targetDate || "未记录"} · 审计 ${time(item.sourceTime)}`));
      if(item.status!=="information")entry.append(el("p","data-note-stale","来源时效不足；仅保留当时说明，当前状态未确认。"));
      const button=el("button","text-action","查看依据");button.dataset.open=`data-note:${index}`;
      button.setAttribute("aria-label",`查看${item.label}数据说明依据`);button.append(icon("chevron"));
      entry.append(button);box.append(entry);
    });
  }
  function paint(v,content=false) {
    if(content) {
      setText("report-date",day(v.report.date));
      const label=({partial:"部分可用",full:"完整",complete:"完整",unavailable:"不可用",missing:"缺失"})[v.report.dataStatus] || v.report.dataStatus || "未确认";
      setText("data-status",`数据${label}`); $("data-status").className="badge state-tag tone-neutral";
      if(v.report.dataStatus==="partial") $("data-status").className="badge state-tag tone-warning";
      const lines=v.report.conclusions || [];
      setText("report-headline",lines[0]?.text || "没有可展示的报告结论");
      $("report-summary").replaceChildren(...lines.slice(1).map(item=>reportRefs(item,v)));
      if(lines[0]?.refs?.length) $("report-summary").prepend(reportRefs({text:"",refs:lines[0].refs},v));
      $("read-report").href=fullReportUrl(v);
      setText("report-cutoff",`截至 ${time(v.report.cutoff)}`);
      const gate=({passed:"通过",pass:"通过",failed:"失败",fail:"失败"})[v.report.gate] || "未记录";
      setText("report-gate",`门禁${gate} ≠ 人工审校`);
      setText("reading-note",v.reportLimitations.length ? `原报告另有 ${v.reportLimitations.length} 项数据限制，可在“来源与限制”查看。` : "原报告结论，不在看板中生成新判断。");
      const p=v.coverage.price,s=v.coverage.snapshot;
      const snapshotHeading=$("snapshot-count")?.parentNode?.parentNode?.querySelector(".metric-heading")?.querySelector("span");
      if(snapshotHeading) snapshotHeading.textContent=v.contract?.activeContract === "research.core-market.v1" ? "历史完整快照" : "审计有效完整快照";
      setText("price-percent",p.label); setText("price-count",`${number(p.numerator)} / ${number(p.denominator)}`);
      setText("coverage-date",`${day(p.date)}记录`);
      setText("price-note",p.percent===null?"缺少可用数量，不绘制比例":`${number(p.denominator-p.numerator)} 只未覆盖`);
      setText("snapshot-count",`${number(s.numerator)} / ${number(s.denominator)}`); setText("snapshot-total",`${v.contract?.activeContract === "research.core-market.v1" ? "历史完整快照 · " : "完整快照 · "}${s.percent===null?"有效性未确认":`${s.label} 有效`}`);
      paintMetric("price",p); paintMetric("snapshot",s);
      setText("gap-count",Array.isArray(v.history.unresolvedDates)?v.history.unresolvedDates.length:"未知");
      setText("legacy-count",Array.isArray(v.history.legacyDates)?v.history.legacyDates.length:"未知");
      const base=v.history.baseline,addenda=v.history.addenda;
      setOptionalText("history-lineage",base&&Array.isArray(addenda)
        ? `原基线 ${base.criticalDates.length}+${base.legacyDates.length}；后续登记 ${addenda.length} 个（${addenda.map(item=>day(item.date)).join("、")||"无"}）`
        : "原基线与后续登记证据未完整读取");
    }
    const audit=v.audit||{};
    const auditLabel=({critical:"critical · 严重",warning:"warning · 提示",ok:"ok"})[audit.recordedStatus]
      ||"级别未确认";
    const auditNode=$("audit-status");
    if(auditNode) {
      auditNode.textContent=`审计 ${auditLabel}${audit.current?"":" · 来源过期或未读"}`;
      auditNode.className=`badge state-tag ${audit.current&&audit.recordedStatus==="critical"?"tone-danger":audit.current&&audit.recordedStatus==="warning"?"tone-warning":"tone-neutral"}`;
    }
    const f=v.freshness.today;
    setText("session-label",!f?"来源未确认":f.stale?"来源已过期":v.cadence.label || "来源在阈值内");
    setText("next-report",v.cadence.nextAt?time(v.cadence.nextAt):"未确认");
    const warning=$("freshness-alert");
    warning.hidden=!!f&&!f.stale&&f.cadenceState!=="pending";
    warning.textContent=!f?"报告来源未读取，当前状态未确认。":f.stale
      ? `报告仅作历史参考：${core.freshnessReasons(f).join("、") || "来源标记为过期"}。不会把旧记录显示成当前正常。`
      : `等待本次报告，尚未看到新报告。检查时限 ${time(v.cadence.deadlineAt)}；旧报告日期不变。`;
    const pf=v.freshness.progress,df=v.freshness.delivery;
    setText("progress-freshness",!pf?"覆盖与历史记录来源未读取，当前情况未确认。":pf.stale
      ? `审计已过期：上次级别 ${audit.recordedStatus||"未知"}；以下覆盖与留存仅代表 ${time(state.panels.progress?.sourceTime)} 的记录。`
      : `审计 ${audit.recordedStatus||"级别未确认"} · ${time(state.panels.progress?.sourceTime)} · ${Array.isArray(v.history.unresolvedDates)?`${v.history.unresolvedDates.length} 个保留范围内缺口日仍未解决`:"当前缺口数量未确认"}`);
    $("progress-freshness").classList.toggle("is-stale",!pf||pf.stale);
    $("data-section").classList.toggle("source-stale",!pf||pf.stale);
    $("delivery-chips").replaceChildren();
    for(const key of ["seal","telegram","relay"]) {
      const item=v.layers[key],[label,tone]=status(item.status); const chip=el("span","delivery-chip");chip.dataset.tone=tone;
      chip.append(icon(item.status==="pass"?"check":"clock"),el("span",null,({seal:"密封",telegram:"Telegram",relay:"接收端"})[key]),el("strong",null,label));
      $("delivery-chips").append(chip);
    }
    if(!df||df.stale) $("delivery-chips").append(el("span","delivery-age-note","来源已过期或未读取"));
    setText("export-note",`导出 ${time(v.metadata.exportedAt)} · 每5分钟读取 · 各来源独立判断`);
    paintAttention(v);
    paintDataNotes(v);
    $("load-error").hidden=!state.errors.length;
    $("load-error").textContent=state.errors.length?`部分导出未能确认：${state.errors.map(e=>`${e.file}（${e.reason}）`).join("、")}。相关状态按未确认显示，页面会自动重试。`:"";
    document.body.dataset.ready="true";
    if(activeDetail && $("detail-dialog").open) paintDialog(activeDetail);
  }
  function paintDialog(kind) {
    const v=state.view; if(!v) return;
    const body=$("dialog-body"), previousScroll=body.scrollTop;
    const expanded=[...body.querySelectorAll("details[open]")].map(item=>item.querySelector("summary")?.textContent);
    body.replaceChildren();
    const titles={"report-evidence":"报告来源与数据限制",attention:"已记录的问题与未确认项",coverage:"数据覆盖口径",history:"历史留存与逐日证据",delivery:"报告留存与投递",about:"来源与更新说明"};
    setText("dialog-title",kind.startsWith("data-note:")?"数据说明与依据":kind.startsWith("attention:")?"问题记录与依据":titles[kind]||"记录依据");
    if(kind==="report-evidence") {
      body.append(row("报告日期",v.report.date),row("数据截止",v.report.cutoff),row("报告SHA-256",v.report.sha256));
      for(const item of v.reportLimitations) body.append(section("报告当时的数据限制",item.text),row("对应报告日",item.reportDate),row("来源时间",item.sourceTime));
      const src=section("来源原记录");
      for(const source of v.report.sources || []) {
        const part=section(`来源 ${source.id}`,source.text); const safe=core.safeUrl(source.url);
        if(safe) {const a=el("a","source-ref","打开来源");a.href=safe;a.target="_blank";a.rel="noreferrer noopener";part.append(a);}
        src.append(part);
      }
      body.append(src);
    } else if(kind==="attention"||kind.startsWith("attention:")) {
      const index=Number(kind.split(":")[1]); const items=kind==="attention"?v.attention:[v.attention[index]].filter(Boolean);
      if(!items.length) body.append(el("p",null,"本批次没有登记其他问题，不据此认定当前正常。"));
      for(const item of items) { const part=section(item.label,item.text); part.append(badge(item.status),...issueRows(item),row("来源时间",item.sourceTime),row("来源文件",item.sourceFile),details("查看记录依据",item.detail));body.append(part); }
    } else if(kind.startsWith("data-note:")) {
      const item=(v.dataNotes || [])[Number(kind.split(":")[1])];
      if(item) {
        body.append(section(item.label,item.text),badge(item.status),...issueRows(item),row("来源时间",item.sourceTime),row("来源文件",item.sourceFile),details("查看记录依据",item.detail));
        body.append(el("p",null,item.status==="information"?"该记录为审计说明，不计入需要关注；不代表缺失行情已补齐或对研究没有影响。":"来源已过期或未确认；以上只代表当时记录，当前情况请看需要关注中的来源提示。"));
      } else body.append(el("p",null,"该说明已随最新批次重新分类，请返回总览查看当前记录。"));
    } else if(kind==="coverage") {
      const contract=v.contract;
      body.append(section("当前数据合同",contract?.label || "未确认"),row("合同标识",contract?.activeContract),row("生效交易日",contract?.effectiveTradeDate));
      if(Array.isArray(contract?.contractIntervals)) {
        const part=section("审计合同区间","区间含起日、不含止日；只展示审计记录，不重新判定过去日期归属。");
        for(const item of contract.contractIntervals) part.append(row(item.activeContract,`${item.fromInclusive} → ${item.untilExclusive || "未设止日"}`));
        body.append(part);
      }
      if(contract?.runDecision) {
        const current=contract.runDecision;
        const part=section("本次数据运行（审计结论）","历史问题与本次结果分开；不以最近一次成功顶替当前运行，也不单凭产物认定自然触发。");
        part.append(badge(current.status),row("计划日期",current.scheduledDate),row("运行身份",current.runId),
          row("运行终态",current.runStatus),row("目标日",current.targetDate),row("开始",current.startedAt),row("完成",current.completedAt),
          row("材料提示",current.materialWarningCodes?.join("、") || "无已记录提示"),details("审计运行依据",current.detail));body.append(part);
      }
      if(contract?.coreMarket) {
        const c=contract.coreMarket;const part=section(c.historicalOnly ? "历史核心市场摘要（非完整快照）" : "核心市场摘要（非完整快照）",
          c.historicalOnly ? "历史记录继续保留；不代表本次运行成功，不补齐旧完整快照缺口。" : "只核 Qlib 市场摘要，不含完整价格归档；旧完整快照与历史缺口不被覆盖。");
        part.append(badge(c.status),row("最近运行目标日",c.targetDate),row("实际运行时间",c.runAt),
          row("已核日历日期",c.calendarTargetDate),row("上一交易日",c.previousTradingDate),
          row("同次摘要价格质量",c.quality?.priceStatus),row("同次有效 / 未完全有效 / 范围",c.quality ? `${c.quality.valid} / ${c.quality.invalid} / ${c.quality.eligible}` : "证据不足或本次休市无新记录"),
          row("当次阶段观察日",c.stageTradeDate),row("实际使用的封存阶段日",c.usedStageTradeDate),
          row("Qlib 数据日",p.date || "未记录"),
          row("核心原快照缺口",c.recentMissingDates?.length ? `${c.recentMissingDates.length} 天：${c.recentMissingDates.join("、")}` : "未记录"),
          row("本合同有效 / 应有日期",`${number(c.coverage.numerator)} / ${number(c.coverage.denominator)}`),details("核心行情合同依据",c.detail));body.append(part);
      }
      for(const [key,label] of [["price","Qlib 行情覆盖"],["snapshot",contract?.activeContract === "research.core-market.v1" ? "历史完整快照（不含核心行情记录）" : "审计有效完整快照"]]) {
        const c=v.coverage[key];const part=section(label);part.append(row(key==="snapshot"?"有效 / 完整快照合同内日期":"数量 / 范围",`${number(c.numerator)} / ${number(c.denominator)}`),row("比例",c.label));body.append(part);
      }
      body.append(row("审计时间",state.panels.progress?.sourceTime),el("p",null,"比例只取审计给出的分子与分母。缺字段不算0，有哈希不等于有效；行情覆盖也不代表全部字段完整。"),details("覆盖原字段",state.panels.progress?.coverage ?? null));
    } else if(kind==="history") {
      body.append(el("p",null,"原基线和后续登记是历史来源记录；仍未解决的日期与数量取自本次审计。保留记录不改变审计整体级别，也不补写快照。"),
        row("审计整体级别",v.audit.recordedStatus),row("审计时间",v.audit.sourceTime));
      const baseline=v.history.baseline;
      body.append(row("原保留基线日期",baseline?.baselineDate),row("原审计文件 SHA-256",baseline?.baselineSha256));
      for(const [key,label] of [["criticalDates","原基线登记的缺口日"],["legacyDates","原基线登记的遗留问题日"]]) {
        const dates=baseline?.[key];const part=section(`${label} · ${Array.isArray(dates)?dates.length:"未知"}`);
        const chips=el("div","date-chips");for(const date of dates||[])chips.append(el("span","date-chip",date));part.append(chips);body.append(part);
      }
      for(const item of v.history.addenda||[]) {
        const part=section(`后续登记的缺口日 · ${item.date}`);
        part.append(row("首次见于审计（日志重建）",item.firstObservedAt),row("首次观察来源",item.firstObservedSource),
          row("日志 JSON 重建 SHA-256",item.reconstructedJournalPayloadSha256),
          row("确认纳入保留范围的时间",null),
          el("p",null,"critical 为审计对全部未解决日期的聚合级别，不是该日单独评级。"));
        body.append(part);
      }
      body.append(row("本次审计仍未解决缺口日",Array.isArray(v.history.unresolvedDates)?v.history.unresolvedDates.join("、")||"0":null),
        row("本次审计仍有遗留问题日",Array.isArray(v.history.legacyDates)?v.history.legacyDates.join("、")||"0":null));
      body.append(details("逐日已有证据（未提供字段保持未记录）",v.history.evidence));
    } else if(kind==="delivery") {
      for(const key of ["seal","telegram","relay"]) {const item=v.layers[key];const part=section(item.label);part.append(badge(item.status),row("记录原状态",core.stateLabel(item.recordedStatus)),row("对应报告日",item.targetDate),row("来源时间",item.sourceTime));for(const text of item.limitations||[])part.append(el("p",null,text));part.append(details("分层证据",item.evidence));body.append(part);}
      body.append(el("p",null,"本地密封不代表平台送达；平台回执不代表用户已读；接收端的存储与内容一致性单独判断。"));
    } else {
      body.append(el("p",null,"只读取服务器已导出的数据，不触发采集、密封、投递或写库。每5分钟重读，常开页面每分钟重算时效；读取失败后自动重试。"));
      body.append(row("当前报告日",v.report.date),row("导出时间",v.metadata.exportedAt),row("浏览器检查时间",new Date().toISOString()));
      body.append(section("交易日规则", "沿用已验证的密封交易日历，不按周末猜测休市。未到下一次出报时显示最近应有报告；导出停止超过15分钟仍判过期，审计和服务不享受报告的交易日豁免。"));
      body.append(details("来源时效与批次核验",{sourceTimes:v.metadata.sourceTimes,exportedAts:v.metadata.exportedAts,binding:v.binding,readErrors:v.metadata.readErrors,loadErrors:state.errors}));
    }
    for(const item of body.querySelectorAll("details")) item.open=expanded.includes(item.querySelector("summary")?.textContent);
    body.scrollTop=previousScroll;
  }
  document.addEventListener("click",event=>{
    const trigger=event.target.closest("[data-open]");if(!trigger||!state.view)return;
    opener=trigger;activeDetail=trigger.dataset.open;window.NNG8WorkbenchNav?.close();paintDialog(activeDetail);$("detail-dialog").showModal();$("dialog-body").scrollTop=0;
  });
  for(const id of ["dialog-close","dialog-done"]) $(id).addEventListener("click",()=>$("detail-dialog").close());
  $("detail-dialog").addEventListener("close",()=>{activeDetail=null;if(opener?.isConnected&&!opener.closest("[inert]"))opener.focus();else $("menu-toggle").focus();});
  function refreshView(content=false) {state.view=model.build(state.panels,Date.now());paint(state.view,content);}
  async function load() {
    if(state.loading)return;
    state.loading=true;state.lastAttempt=Date.now();
    try {const result=await loader.load();state.panels=result.panels;state.errors=result.errors;state.fetchedAt=Date.now();refreshView(true);}
    catch {state.errors=[{file:"manifest.json",reason:"读取或批次核验失败"}];refreshView(!state.fetchedAt);}
    finally {state.loading=false;}
  }
  function tick() {
    if(state.view)refreshView(false);
    const interval=state.errors.length||!state.fetchedAt?core.RETRY_MS:core.REFRESH_MS;
    const last=state.errors.length||!state.fetchedAt?state.lastAttempt:state.fetchedAt;
    if(!Number.isFinite(last)||Date.now()-last>=interval)load();
  }
  window.NNG8Workbench = { tick, reload:load, getState:()=>({fetchedAt:state.fetchedAt,loading:state.loading,errors:state.errors,view:state.view}) };
  setInterval(tick,core.FRESHNESS_TICK_MS);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)tick();});
  load();
})();
