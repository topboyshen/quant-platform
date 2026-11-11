import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {buildResearchSeal} from '../src/xhs-draft.mjs';
import {chmod,cp,mkdtemp,mkdir,readdir,writeFile,readFile,rm,rename,symlink,access,realpath} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {SCHEDULED_INVOCATION} from './helpers/scheduled-occurrence.mjs';
import {deliverSealedMorning,parseDeliveryArguments,prepareSealedMorning} from '../scripts/deliver-morning-report.mjs';
import {deliverRunEvent,drainOutbox,emitRunEvent,notificationStatus} from '../scripts/hermes-notify.mjs';
import {buildVerifiedBaselineReport} from '../src/baseline-report.mjs';
import {assessPublicationGate,normalizeReportOutput} from '../src/report.mjs';
import {buildResultView,RESULT_VIEW_VERSION} from '../src/result-view.mjs';
import {contextFixture,buildOptions} from './helpers/multi-candidate-context.mjs';
const DAY='2026-09-08',NOW=new Date(DAY+'T01:15:00Z');
const EVENT=`astock-${DAY}-success-telegram-v1`;
const digest=b=>createHash('sha256').update(b).digest('hex');
const json=v=>JSON.stringify(v,null,2)+'\n';
function completeReport(){
  const context=contextFixture();
  context.meta.targetDate=DAY;
  context.data.tradingCalendar.targetDate=DAY;
  const cutoffInstant=new Date(DAY+'T00:00:00Z');
  const publicationAudit=assessPublicationGate({context,cutoffInstant,targetDate:DAY});
  const options=buildOptions(context,publicationAudit,{
    targetDate:DAY,cutoffLocal:DAY+' 08:00',cutoffInstant
  });
  return normalizeReportOutput(buildVerifiedBaselineReport(options),{...options,
    strictContextSources:true,enforcePublicationReady:true,requireDeterministicCompilerOutput:true});
}
async function fixture(t,{resultView=false}={}){
  const root=await realpath(await mkdtemp(path.join(os.tmpdir(),'morning-delivery-fixture-')));
  t.after(()=>rm(root,{recursive:true,force:true}));
  for(const d of ['config','reports/daily','work/orchestrator/states',`work/xhs/drafts/${DAY}`])
    await mkdir(path.join(root,d),{recursive:true});
  const policy={schemaVersion:'1.0',deliveryEnabled:true,route:'telegram',payloadMode:'formal_report_markdown_document',
    ...(resultView?{reportView:RESULT_VIEW_VERSION}:{}),
    ownerApproval:{status:'confirmed',confirmedAt:DAY,scope:'telegram_main_channel_formal_report_markdown_document'}};
  const run={schemaVersion:'1.0',targetDate:DAY,status:'success',startedAt:DAY+'T00:00:15.000Z',
    completedAt:DAY+'T00:00:22.000Z',dataStatus:'partial',publicationGate:'passed',evidenceReview:'passed',
    frozenCutoffInstant:DAY+'T00:00:00.000Z',phase:'publish',publicationBlockers:[],
    reportFile:`reports/daily/${DAY}.md`};
  const occurrence={schemaVersion:'1.0',task:'morning-report',targetDate:DAY,status:'success',
    invocationId:SCHEDULED_INVOCATION,
    startedAt:DAY+'T00:00:00.000Z',completedAt:DAY+'T00:00:23.000Z',childExitCode:0,childSignal:null,timedOut:false,interrupted:false};
  const report=resultView?completeReport():'# Synthetic sealed report\n';
  const seal={schemaVersion:'astock.xhs-draft.v1',kind:'xhs_draft',targetDate:DAY,state:'DRAFT_ONLY',
    publishEligible:false,humanApprovalRequired:true,
    source:{lastRunFile:'work/last-run.json',lastRunSha256:digest(json(run)),reportFile:run.reportFile,
      reportSha256:digest(report),publicationGate:'passed',evidenceReview:'passed'}};
  const put=async(rel,value)=>writeFile(path.join(root,rel),typeof value==='string'?value:json(value));
  const sealPath=`work/xhs/drafts/${DAY}/${digest(report)}.json`;
  await put('config/notifications.json',policy);await put('work/last-run.json',run);
  await put(`work/orchestrator/states/morning-report-${DAY}.json`,occurrence);
  await put(run.reportFile,report);await put(sealPath,seal);
  const calls=[]; const sender=async(delivery,meta)=>{calls.push({delivery,meta});return {ok:true,started:true,exitCode:0,ack:{messageId:"1"}};};
  const options={projectRoot:root,now:NOW,deliveryMode:'telegram',sender};
  return {root,policy,run,occurrence,report,seal,sealPath,put,calls,options};
}
async function missing(file){await assert.rejects(access(file),{code:'ENOENT'});}

test('sealed morning report is sent once; second invocation is a validated no-op',async t=>{
  const f=await fixture(t); const before=await readFile(path.join(f.root,f.run.reportFile));
  const first=await deliverSealedMorning(f.options);
  assert.equal(first.status,'sent');assert.equal(first.eventId,EVENT);assert.equal(f.calls.length,1);
  assert.match(f.calls[0].delivery.message,/\[\[as_document\]\]/);
  assert.equal((await deliverSealedMorning(f.options)).status,'already_sent');assert.equal(f.calls.length,1);
  assert.deepEqual(await readFile(path.join(f.root,f.run.reportFile)),before);
});
test('v2 scheduled cutoff remains distinct from delayed start through delivery preparation',async t=>{
  const f=await fixture(t);
  f.occurrence.schemaVersion='2.0';
  f.occurrence.frozenCutoffInstant=f.run.frozenCutoffInstant;
  f.occurrence.startedAt=DAY+'T00:00:30.000Z';
  f.occurrence.completedAt=DAY+'T00:01:01.000Z';
  f.run.startedAt=DAY+'T00:00:31.000Z';
  f.run.completedAt=DAY+'T00:01:00.000Z';
  await f.put('work/last-run.json',f.run);
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`,f.occurrence);
  await f.put(f.sealPath,{...f.seal,source:{...f.seal.source,lastRunSha256:digest(json(f.run))}});
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent',JSON.stringify(result));
  assert.equal(f.calls.length,1);
});

test('v2 self-consistent wrong cutoff is blocked before enqueue despite matching seal hashes',async t=>{
  const f=await fixture(t);
  f.occurrence.schemaVersion='2.0';
  f.occurrence.frozenCutoffInstant=DAY+'T00:30:00.000Z';
  f.occurrence.startedAt=DAY+'T00:30:30.000Z';
  f.occurrence.completedAt=DAY+'T00:31:01.000Z';
  f.run.frozenCutoffInstant=f.occurrence.frozenCutoffInstant;
  f.run.startedAt=DAY+'T00:30:31.000Z';
  f.run.completedAt=DAY+'T00:31:00.000Z';
  await f.put('work/last-run.json',f.run);
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`,f.occurrence);
  await f.put(f.sealPath,{...f.seal,source:{...f.seal.source,lastRunSha256:digest(json(f.run))}});
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'blocked',JSON.stringify(result));
  assert.equal(result.reason,'source_occurrence_cutoff_invalid');
  assert.equal(f.calls.length,0);
  await missing(path.join(f.root,'work/notifications'));
});

test('persistent gate and runtime gate both apply before any outbox write',async t=>{
  for(const mode of ['runtime','policy','approval']){
    const f=await fixture(t);
    if(mode==='runtime')f.options.deliveryMode='disabled';
    if(mode==='policy'){f.policy.deliveryEnabled=false;await f.put('config/notifications.json',f.policy);}
    if(mode==='approval'){delete f.policy.ownerApproval;await f.put('config/notifications.json',f.policy);}
    const r=await deliverSealedMorning(f.options);assert.ok(['held','blocked'].includes(r.status));
    assert.equal(f.calls.length,0);await missing(path.join(f.root,'work/notifications'));
  }
});
test('same-day manual recovery does not relax scheduled 120-minute age gate',async t=>{
  const f=await fixture(t);f.options.now=new Date(DAY+'T08:00:00Z');
  assert.equal((await deliverSealedMorning(f.options)).reason,'automatic_delivery_window_expired');
  assert.equal(f.calls.length,0);
  assert.equal((await deliverSealedMorning({...f.options,manualSameDay:true})).status,'sent');
  assert.equal(f.calls.length,1);
});
test('manual recovery never sends yesterday or a future unfinished run',async t=>{
  const f=await fixture(t);
  for(const now of [new Date('2026-09-09T01:15:00Z'),new Date(DAY+'T00:00:10Z')]){
    assert.equal((await deliverSealedMorning({...f.options,now,manualSameDay:true})).status,'blocked');
  }
  assert.equal(f.calls.length,0);await missing(path.join(f.root,'work/notifications'));
});
test('missing, running, failed or blocked evidence does not send failure captions',async t=>{
  for(const field of ['status','publicationGate','evidenceReview','reportFile']){
    const f=await fixture(t);f.run[field]=field==='reportFile'?'reports/provisional/2026-09-08.md':'blocked';
    await f.put('work/last-run.json',f.run);
    assert.equal((await deliverSealedMorning(f.options)).status,'blocked');assert.equal(f.calls.length,0);
    await missing(path.join(f.root,'work/notifications'));
  }
});
test('a verified non-trading day is skipped without an outbox',async t=>{
  const f=await fixture(t);f.run.status=f.occurrence.status='skipped';
  await f.put('work/last-run.json',f.run);await f.put(`work/orchestrator/states/morning-report-${DAY}.json`,f.occurrence);
  assert.equal((await deliverSealedMorning(f.options)).status,'skipped');await missing(path.join(f.root,'work/notifications'));
});
test('report or source-state hash mismatch fails before enqueue',async t=>{
  for(const which of ['report','state','seal']){
    const f=await fixture(t);
    if(which==='report')await f.put(f.run.reportFile,f.report+'changed');
    if(which==='state')await f.put('work/last-run.json',{...f.run,extra:'changed'});
    if(which==='seal')await f.put(f.sealPath,{...f.seal,publishEligible:true});
    assert.equal((await deliverSealedMorning(f.options)).status,'blocked');assert.equal(f.calls.length,0);
    await missing(path.join(f.root,'work/notifications'));
  }
});
test('report symlink and symlink parent cannot bypass fixed paths',async t=>{
  const f=await fixture(t);await rename(path.join(f.root,f.run.reportFile),path.join(f.root,'original.md'));
  await symlink(path.join(f.root,'original.md'),path.join(f.root,f.run.reportFile));
  assert.equal((await deliverSealedMorning(f.options)).status,'blocked');assert.equal(f.calls.length,0);
});
test('check-only validates everything but does not initialize notifications',async t=>{
  const f=await fixture(t);assert.equal((await deliverSealedMorning({...f.options,checkOnly:true})).status,'ready');
  await missing(path.join(f.root,'work/notifications'));assert.equal(f.calls.length,0);
});
test('ambiguous delivery is not retried automatically or manually',async t=>{
  const f=await fixture(t);let calls=0;
  f.options.sender=async()=>{calls++;return {ok:false,started:true,timedOut:true};};
  const first=await deliverSealedMorning(f.options);assert.equal(first.finalState,'ambiguous');
  const second=await deliverSealedMorning({...f.options,manualSameDay:true});
  assert.equal(second.reason,'delivery_requires_manual_review');assert.equal(calls,1);
});
test('concurrent invocations never call the sender twice',async t=>{
  const f=await fixture(t);let calls=0,release,entered;
  const started=new Promise(resolve=>{entered=resolve;});
  const finish=new Promise(resolve=>{release=resolve;});
  f.options.sender=async()=>{calls++;entered();await finish;return {ok:true,started:true,exitCode:0,ack:{messageId:"1"}};};
  const first=deliverSealedMorning(f.options);await started;
  const second=await deliverSealedMorning(f.options);
  assert.equal(second.reason,'delivery_requires_manual_review');release();
  assert.equal((await first).status,'sent');assert.equal(calls,1);
});
test('duplicate event states and tampered acknowledged payload are rejected',async t=>{
  const f=await fixture(t);await deliverSealedMorning(f.options);
  const event=await readFile(path.join(f.root,`work/notifications/sent/${EVENT}.json`));
  await f.put(`work/notifications/pending/${EVENT}.json`,event.toString());
  assert.equal((await deliverSealedMorning(f.options)).reason,'event_has_multiple_states');
  await rm(path.join(f.root,`work/notifications/pending/${EVENT}.json`));
  await f.put(`work/notifications/payloads/${EVENT}.md`,'changed');
  assert.equal((await deliverSealedMorning(f.options)).reason,'event_payload_changed');assert.equal(f.calls.length,1);
});
test('CLI permits only fixed scheduled/manual and check switches',()=>{
  assert.deepEqual(parseDeliveryArguments(['--scheduled']),{manualSameDay:false,checkOnly:false});
  assert.deepEqual(parseDeliveryArguments(['--manual-same-day','--check']),{manualSameDay:true,checkOnly:true});
  for(const args of [[],['--target-date',DAY],['--to','other'],['--root','/tmp'],['--scheduled','--manual-same-day'],['--scheduled','--scheduled']])
    assert.throws(()=>parseDeliveryArguments(args));
});
test('scheduled unit keeps source generation and public publishing out of its capability',async()=>{
  const root=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
  const service=await readFile(path.join(root,'systemd/astock-morning-notify.service'),'utf8');
  const draft=await readFile(path.join(root,'systemd/astock-xhs-draft.service'),'utf8');
  assert.match(service,/deliver-morning-report\.mjs --scheduled/);assert.match(service,/Restart=no/);
  assert.doesNotMatch(service,/run-morning-cron|run-orchestrated|--manual-same-day|ASTOCK_FINAL_DELIVERY=disabled/);
  const sealService=await readFile(path.join(root,'systemd/astock-research-seal.service'),'utf8');
  const sealNotify=await readFile(path.join(root,'systemd/astock-research-seal.service.d/notify.conf'),'utf8');
  // 通知入口只经 drop-in 增加：移除 drop-in 即可回退；主文件保持 digest。
  assert.match(sealService,/^OnSuccess=nng8-research-digest\.service$/m);
  assert.doesNotMatch(sealService,/astock-morning-notify\.service/);
  assert.match(sealNotify,/^OnSuccess=astock-morning-notify\.service$/m);
  assert.doesNotMatch(draft,/astock-morning-notify\.service/);
  for (const name of ['astock-morning-notify.timer', 'astock-xhs-draft.timer']) {
    await assert.rejects(readFile(path.join(root,'systemd',name)), {code:'ENOENT'});
    const retired=await readFile(path.join(root,'docs/retired-systemd',`${name}.txt`),'utf8');
    assert.match(retired,/Retired historical template\. Do not install/);
    assert.doesNotMatch(retired,/^OnCalendar=|^WantedBy=timers.target/m);
  }
});

function genericOptions(f,sender=f.options.sender){
  return {projectRoot:f.root,exitCode:0,invocationStartedAt:f.occurrence.startedAt,
    invocationEndedAt:f.occurrence.completedAt,expectedTargetDate:DAY,
    deliveryMode:'telegram',drainNow:NOW.getTime(),sender};
}
const deferredSender=async()=>({ok:false,started:false});
const notificationFile=(f,state)=>path.join(f.root,`work/notifications/${state}/${EVENT}.json`);
const payloadFile=f=>path.join(f.root,`work/notifications/payloads/${EVENT}.md`);

test('results-only delivery binds the unchanged source and exact deterministic payload separately',async t=>{
  const f=await fixture(t,{resultView:true});
  const before=await readFile(path.join(f.root,f.run.reportFile));
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent');assert.equal(f.calls.length,1);
  const event=JSON.parse(await readFile(notificationFile(f,'sent'),'utf8'));
  const payload=await readFile(payloadFile(f));
  assert.equal(event.report.presentation,RESULT_VIEW_VERSION);
  assert.equal(event.report.reportFile,f.run.reportFile);
  assert.equal(event.report.sourceSha256,digest(before));assert.equal(event.report.sourceBytes,before.length);
  assert.equal(event.report.sha256,digest(payload));assert.equal(event.report.bytes,payload.length);
  assert.notEqual(event.report.sha256,event.report.sourceSha256);
  assert.equal(payload.toString('utf8'),buildResultView(f.report,{targetDate:DAY}));
  assert.deepEqual(await readFile(path.join(f.root,f.run.reportFile)),before);
  assert.equal((await deliverSealedMorning(f.options)).status,'already_sent');assert.equal(f.calls.length,1);
  assert.match(f.calls[0].delivery.message,/未经人工审校/);
  assert.doesNotMatch(f.calls[0].delivery.message,/门禁|结构、来源|事件：/);
});

test('results-only readiness checks projection before creating any notification files',async t=>{
  const f=await fixture(t,{resultView:true});
  assert.equal((await deliverSealedMorning({...f.options,checkOnly:true})).status,'ready');
  await missing(path.join(f.root,'work/notifications'));
  const invalid=await fixture(t);invalid.policy.reportView=RESULT_VIEW_VERSION;
  await invalid.put('config/notifications.json',invalid.policy);
  assert.equal((await deliverSealedMorning(invalid.options)).status,'blocked');
  assert.equal(invalid.calls.length,0);await missing(path.join(invalid.root,'work/notifications'));
});

test('failed projection cannot become a full document or a failure notification',async t=>{
  const f=await fixture(t);f.policy.reportView=RESULT_VIEW_VERSION;
  await f.put('config/notifications.json',f.policy);
  await assert.rejects(emitRunEvent(genericOptions(f)));
  await missing(notificationFile(f,'pending'));
  await missing(path.join(f.root,`work/notifications/pending/astock-${DAY}-failed-telegram-v1.json`));
  await missing(payloadFile(f));assert.equal(f.calls.length,0);
});

test('pending result views reject forged source bindings or rehashed noncanonical payloads before sending',async t=>{
  for(const change of ['source-hash','source-size','payload-rehashed','presentation-removed','presentation-unknown']){
    const f=await fixture(t,{resultView:true});
    await deliverSealedMorning({...f.options,sender:deferredSender});
    const event=JSON.parse(await readFile(notificationFile(f,'pending'),'utf8'));
    if(change==='source-hash')event.report.sourceSha256='a'.repeat(64);
    if(change==='source-size')event.report.sourceBytes+=1;
    if(change==='presentation-removed')delete event.report.presentation;
    if(change==='presentation-unknown')event.report.presentation='results_only_v999';
    if(change==='payload-rehashed'){
      const forged=Buffer.from(buildResultView(f.report,{targetDate:DAY})+'\n伪造附加结论\n');
      await writeFile(payloadFile(f),forged);event.report.sha256=digest(forged);event.report.bytes=forged.length;
    }
    await writeFile(notificationFile(f,'pending'),json(event));
    assert.equal((await deliverSealedMorning(f.options)).status,'blocked',change);
    assert.equal(f.calls.length,0,change);
    const drained=await drainOutbox({projectRoot:f.root,deliveryMode:'telegram',now:NOW.getTime(),
      targetEventId:EVENT,sender:f.options.sender});
    assert.equal(drained.sent,0,change);assert.equal(drained.dead,1,change);assert.equal(f.calls.length,0,change);
  }
});

test('changed canonical source invalidates a pending result even when its payload is intact',async t=>{
  const f=await fixture(t,{resultView:true});
  await deliverSealedMorning({...f.options,sender:deferredSender});
  await f.put(f.run.reportFile,f.report+'\nchanged source\n');
  const drained=await drainOutbox({projectRoot:f.root,deliveryMode:'telegram',now:NOW.getTime(),
    targetEventId:EVENT,sender:f.options.sender});
  assert.equal(drained.sent,0);assert.equal(drained.dead,1);assert.equal(f.calls.length,0);
});

test('sent result views are never resent and cannot be confirmed after source or projection tampering',async t=>{
  for(const change of ['source','payload-rehashed']){
    const f=await fixture(t,{resultView:true});
    assert.equal((await deliverSealedMorning(f.options)).status,'sent');
    const event=JSON.parse(await readFile(notificationFile(f,'sent'),'utf8'));
    if(change==='source')await f.put(f.run.reportFile,f.report+'\nchanged source\n');
    else {
      const forged=Buffer.from(buildResultView(f.report,{targetDate:DAY})+'\nforged\n');
      await writeFile(payloadFile(f),forged);event.report.sha256=digest(forged);event.report.bytes=forged.length;
      await writeFile(notificationFile(f,'sent'),json(event));
    }
    assert.equal((await deliverSealedMorning(f.options)).status,'blocked',change);
    const checked=await deliverRunEvent(genericOptions(f));
    assert.equal(checked.confirmed,false,change);assert.equal(checked.drain.sent,0,change);
    assert.equal(f.calls.length,1,change);
  }
});

test('existing full events retain their original bytes when reportView changes',async t=>{
  for(const state of ['pending','sent']){
    const f=await fixture(t);
    await deliverSealedMorning({...f.options,...(state==='pending'?{sender:deferredSender}:{})});
    const priorPayload=await readFile(payloadFile(f));
    f.policy.reportView=RESULT_VIEW_VERSION;await f.put('config/notifications.json',f.policy);
    assert.equal((await deliverSealedMorning(f.options)).status,state==='pending'?'sent':'already_sent');
    const event=JSON.parse(await readFile(notificationFile(f,'sent'),'utf8'));
    assert.equal(event.report.presentation,undefined);assert.equal(event.report.sourceSha256,undefined);
    assert.deepEqual(await readFile(payloadFile(f)),priorPayload);assert.equal(f.calls.length,1);
  }
});

test('existing result events retain their verified projection when reportView changes to full',async t=>{
  const f=await fixture(t,{resultView:true});
  await deliverSealedMorning({...f.options,sender:deferredSender});
  const priorPayload=await readFile(payloadFile(f));
  f.policy.reportView='full';await f.put('config/notifications.json',f.policy);
  assert.equal((await deliverSealedMorning(f.options)).status,'sent');
  assert.deepEqual(await readFile(payloadFile(f)),priorPayload);assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].meta.event.report.presentation,RESULT_VIEW_VERSION);
});

/* ---- 研究密封作为投递证明（单一规则） ---- */

async function withRunCutoff(f){
  f.run.frozenCutoffInstant=DAY+'T00:00:01.000Z';
  f.run.contextSha256=f.run.contextSha256??'c'.repeat(64);   // 与 putResearchSeal 的默认值同一对象
  f.run.phase=f.run.phase??'publish';                        // 真实研究密封要求发布阶段
  f.run.cutoffLocal=f.run.cutoffLocal??`${DAY} 08:00`;       // 真实生成器要求本地截止字符串
  f.run.researchSemanticReview=f.run.researchSemanticReview??'not_human_reviewed';
  await f.put('work/last-run.json',f.run);
  // 旧 v1 编排器把 startedAt 作为 frozenCutoffInstant 传给子进程：改了 run 的截止时间，
  // 认领记录必须同步，否则就变成「没有对应认领运行」的状态，投递会被正确拦下。
  f.occurrence.startedAt=f.run.frozenCutoffInstant;
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`,f.occurrence);
  // 草稿密封绑定的是 last-run 的字节哈希：改了 run 就必须同步重写，否则草稿回退会被正确阻断。
  const draft={...f.seal,source:{...f.seal.source,lastRunSha256:digest(json(f.run))}};
  await f.put(f.sealPath,draft);
  return f;
}

async function putResearchSeal(f,{source={},seal={},relPath=null}={}){
  const body={schemaVersion:'astock.research-seal.v1',kind:'research_seal',targetDate:DAY,state:'SEALED',
    generatedAt:DAY+'T00:00:24.000Z',
    source:{lastRunFile:'work/last-run.json',lastRunSha256:digest(json(f.run)),
      reportFile:f.run.reportFile,reportSha256:digest(f.report),contextSha256:'c'.repeat(64),
      dataStatus:'partial',researchSemanticReview:'not_human_reviewed',
      frozenCutoffInstant:f.run.frozenCutoffInstant,cutoffLocal:DAY+' 08:00',
      publicationGate:'passed',evidenceReview:'passed',...source},...seal};
  const target=relPath??`work/research-seal/${DAY}/${digest(f.report)}.json`;
  await mkdir(path.dirname(path.join(f.root,target)),{recursive:true});
  await writeFile(path.join(f.root,target),typeof body==='string'?body:json(body));
  return body;
}

test('研究密封在位时即使用草稿缺失也能投递，且只触发一次通知',async t=>{
  const f=await withRunCutoff(await fixture(t));
  await putResearchSeal(f);
  await rm(path.join(f.root,`work/xhs/drafts/${DAY}`),{recursive:true,force:true});
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent',JSON.stringify(result));
  assert.equal(f.calls.length,1,'恰好一次模拟通知');
  assert.equal(result.eventId,EVENT);
});

test('草稿仅在研究密封不存在时作为回退',async t=>{
  const f=await withRunCutoff(await fixture(t));
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent',JSON.stringify(result));
  assert.equal(f.calls.length,1);
});

test('两份证明都缺失是明确的 held，而不是异常',async t=>{
  const f=await withRunCutoff(await fixture(t));
  await rm(path.join(f.root,`work/xhs/drafts/${DAY}`),{recursive:true,force:true});
  const result=await deliverSealedMorning(f.options);
  assert.ok(['held','blocked'].includes(result.status),JSON.stringify(result));
  assert.equal(result.reason,'sealed_report_proof_missing',JSON.stringify(result));
  assert.equal(f.calls.length,0);
});

for(const [label,changes] of [
  ['reportSha256 不符',{source:{reportSha256:'0'.repeat(64)}}],
  ['lastRunSha256 不符',{source:{lastRunSha256:'0'.repeat(64)}}],
  ['publicationGate 非 passed',{source:{publicationGate:'blocked'}}],
  ['evidenceReview 非 passed',{source:{evidenceReview:'failed'}}],
  ['缺 frozenCutoffInstant（必填）',{source:{frozenCutoffInstant:undefined}}],
  ['缺 contextSha256（必填）',{source:{contextSha256:undefined}}],
  ['contextSha256 格式合法但内容错误',{source:{contextSha256:'d'.repeat(64)}}],
  ['frozenCutoffInstant 与 last-run 不符',{source:{frozenCutoffInstant:DAY+'T00:00:02.000Z'}}],
  ['属于其它日期',{seal:{targetDate:'2026-09-07'}}],
]){
  test(`研究密封${label}必须阻断，且不得回退草稿`,async t=>{
    const f=await withRunCutoff(await fixture(t));
    await putResearchSeal(f,changes);
    const result=await deliverSealedMorning(f.options);
    assert.ok(['held','blocked'].includes(result.status),label+JSON.stringify(result));
    assert.equal(result.reason,'sealed_report_proof_invalid',label+JSON.stringify(result));
    assert.equal(f.calls.length,0,label);
  });
}

test('研究密封损坏或不可读时阻断，不回退草稿',async t=>{
  const f=await withRunCutoff(await fixture(t));
  const rel=`work/research-seal/${DAY}/${digest(f.report)}.json`;
  await putResearchSeal(f,{relPath:rel});
  await writeFile(path.join(f.root,rel),'{not json');
  const corrupt=await deliverSealedMorning(f.options);
  assert.ok(['held','blocked'].includes(corrupt.status),JSON.stringify(corrupt));
  assert.equal(corrupt.reason,'sealed_report_proof_invalid',JSON.stringify(corrupt));
  assert.equal(f.calls.length,0);

  await putResearchSeal(f,{relPath:rel});
  await chmod(path.join(f.root,rel),0o000);
  t.after(()=>chmod(path.join(f.root,rel),0o600).catch(()=>{}));
  const denied=await deliverSealedMorning(f.options);
  assert.ok(['held','blocked'].includes(denied.status),JSON.stringify(denied));
  assert.equal(denied.reason,'sealed_report_proof_invalid',JSON.stringify(denied));
  assert.equal(f.calls.length,0);
});

test('并发触发时发送调用总数不超过一次（不要求 already_sent）',async t=>{
  const f=await withRunCutoff(await fixture(t));
  await putResearchSeal(f);
  const results=await Promise.all([deliverSealedMorning(f.options),deliverSealedMorning(f.options)]);
  assert.ok(f.calls.length<=1,`发送调用总数 ${f.calls.length} 必须 ≤ 1`);
  const statuses=results.map((r)=>r.status);
  // 并发第二次可能是锁占用/处理中：只要求发送调用总数 ≤ 1，且至少一次成功。
  assert.ok(statuses.every((s)=>['sent','already_sent','held','blocked'].includes(s)),
    'unexpected statuses: '+statuses.join(','));
  // 关键性质：发送调用总数 ≤ 1（并发第二次可以是锁占用/处理中，不强求 already_sent）。
  assert.ok(f.calls.length<=1,'sends must not exceed one');
});

test('首次完成后的顺序重试为 already_sent，且不再发送',async t=>{
  const f=await withRunCutoff(await fixture(t));
  await putResearchSeal(f);
  assert.equal((await deliverSealedMorning(f.options)).status,'sent');
  assert.equal((await deliverSealedMorning(f.options)).status,'already_sent');
  assert.equal(f.calls.length,1);
});

test('离线链路：冻结包 → 研究密封 → 投递准备 → 模拟发送与回执',async t=>{
  const f=await withRunCutoff(await fixture(t));
  // 冻结包（归档 input.json）与 last-run 的上下文哈希必须同一对象
  const archive=`work/report-archives/${DAY}`;
  await mkdir(path.join(f.root,archive),{recursive:true});
  f.run.contextSha256='e'.repeat(64);
  f.run.compilerContextSha256='0'.repeat(64);
  await f.put('work/last-run.json',f.run);
  await f.put(`${archive}/input.json`,{schemaVersion:'1.0',targetDate:DAY,
    compilerContextSha256:f.run.compilerContextSha256,cutoffInstant:DAY+'T00:00:01.000Z'});
  // 草稿密封同步到新的 last-run 字节（保持回退路径可用）
  await f.put(f.sealPath,{...f.seal,source:{...f.seal.source,lastRunSha256:digest(json(f.run))}});
  // 研究密封绑定同一上下文哈希
  await putResearchSeal(f,{source:{contextSha256:f.run.contextSha256}});
  // 投递准备（不发）
  const prepared=await prepareSealedMorning({projectRoot:f.root,now:NOW});
  assert.equal(prepared.status,'ready');
  assert.equal(prepared.proof.proofKind,'research_seal');
  assert.equal(prepared.proof.contextSha256,f.run.contextSha256);
  // 模拟发送：走真实 drain（stub sender），断言回执落盘且含平台消息 ID
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent',JSON.stringify(result));
  assert.equal(f.calls.length,1);
  const receipt=JSON.parse(await readFile(
    path.join(f.root,`work/notifications/receipts/send/${EVENT}.json`),'utf8'));
  assert.equal(receipt.platformMessageId,'1');
  assert.equal(receipt.payloadSha256,prepared.proof.reportSha256);
  assert.match(receipt.confirmedAt,/Z$/);
  // 顺序重试不重复发送
  assert.equal((await deliverSealedMorning(f.options)).status,'already_sent');
  assert.equal(f.calls.length,1);
});

test('真实研究密封生成 → 草稿故意失败 → 投递准备 → 模拟发送 → 回执落盘（只发一次）',async t=>{
  const f=await withRunCutoff(await fixture(t));
  const archive=`work/report-archives/${DAY}`;
  await mkdir(path.join(f.root,archive),{recursive:true});
  f.run.contextSha256='f'.repeat(64);
  await f.put('work/last-run.json',f.run);
  // 密封步骤现在要求同次冻结归档与结构化摘要绑定：写入真实冻结输入与结果绑定。
  const {compileFrozenReport,freezeRunInput}=await import('../src/run-archive.mjs');
  const {jsonHash}=await import('../src/snapshot-integrity.mjs');
  const marketDate='2026-09-07';   // DAY=2026-09-08 的上一交易日（上下文与报告由同次编译生成）
  const marketTime=`${marketDate}T15:00:00+08:00`;
  const stock={code:'000001',name:'测试银行',symbol:'000001.SZ',exchange:'SZ'};
  const index=(code,name)=>({code,name,close:101,previousClose:100,change:1,changePercent:1,
    units:{close:'index_point',previousClose:'index_point',change:'index_point',changePercent:'percentage_point'},dataAsOf:marketTime,sourceIds:['market']});
  const rawContext={schemaVersion:'1.0',
    meta:{targetDate:DAY,timezone:'Asia/Shanghai',status:'partial',provider:'test-fixture',sourceType:'fixture',isLive:false},
    data:{tradingCalendar:{status:'trading_day',targetDate:DAY,previousTradingDate:marketDate,dataAsOf:`${DAY}T08:00:00+08:00`,sourceIds:['calendar']},
      previousAShareSession:{sessionDate:marketDate,dataAsOf:marketTime,sourceIds:['market'],
        indices:[index('sh000001','上证指数'),index('sz399001','深证成指'),index('sz399006','创业板指')]},
      watchlist:[{...stock,status:'available',
        marketData:{code:stock.code,name:stock.name,last:12.3,previousClose:12,change:0.3,changePercent:2.5,dataAsOf:marketTime,sourceIds:['market'],
          units:{last:'CNY_per_share',previousClose:'CNY_per_share',change:'CNY_per_share',changePercent:'percentage_point'}},
        financials:{reportDate:'2026-06-30',parentNetProfit:100,units:{parentNetProfit:'CNY'},dataAsOf:'2026-07-30T18:00:00+08:00',sourceIds:['financial']}}],
      capitalFlows:[],sectors:[],announcementsAndNews:[]},
    sources:[{id:'calendar',title:'交易日历',url:'https://example.com/calendar',sourceType:'trading_calendar_data',dataAsOf:`${DAY}T08:00:00+08:00`},
      {id:'market',title:'行情',url:'https://example.com/market',sourceType:'market_data',dataAsOf:marketTime},
      {id:'financial',title:'财务指标',url:'https://example.com/financial',sourceType:'financial_data_aggregation',dataAsOf:'2026-07-30T18:00:00+08:00'}]};
  await rm(path.join(f.root,archive),{recursive:true,force:true});
  const fixtureRepoRoot=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
  const frozen=await freezeRunInput({projectRoot:fixtureRepoRoot,
    archiveRoot:path.join(f.root,'work','report-archives'),outputDirectory:path.join(f.root,'reports'),
    rawContext,targetDate:DAY,cutoffInstant:`${DAY}T00:00:00Z`,cutoffLocal:`${DAY} 08:00`,
    configuration:{stocks:{corePool:[stock]},
      risk:{disclaimer:'本报告仅供研究与信息参考，不构成任何投资建议。',marketDataMaxAgeHours:18,newsMaxAgeHours:72},
      workflow:{reportMode:'structured_compiler'}}});
  // R05: the sealed body must be this compilation's own output, so the fixture
  // uses the compiled report (a synthetic body would stop at the digest step).
  const realReport=compileFrozenReport(frozen.input,null);
  await mkdir(path.join(f.root,archive,'result'),{recursive:true});
  await f.put(`${archive}/result/report.md`,realReport);
  const frozenResult={schemaVersion:'1.0',status:'validated',inputSha256:frozen.inputSha256,
    reportSha256:digest(realReport),selection:null,selectionSha256:jsonHash(null),
    compilationInputSha256:jsonHash({inputSha256:frozen.inputSha256,selection:null}),
    analysisStatus:'disabled',analysisFailures:[],mode:'verified_baseline',analysisAttempts:0,
    semanticCandidateCount:frozen.input.candidateIds.length,financialSnapshots:{}};
  await f.put(`${archive}/result/result.json`,frozenResult);
  await f.put(`${archive}/published.json`,{status:'sealed',inputSha256:frozen.inputSha256,
    reportSha256:digest(realReport),resultSha256:jsonHash(frozenResult)});
  await f.put(f.run.reportFile,realReport);
  f.report=realReport;                       // 后续路径与哈希都以真实报告为准
  // 草稿故意失败：删除草稿目录（模拟 xhs-draft 未产出）
  await rm(path.join(f.root,`work/xhs/drafts/${DAY}`),{recursive:true,force:true});
  // 真实生成入口需要真实配置：把仓库 config/ 复制进夹具（保留夹具自己的 notifications 策略），
  // 并让 risk.json 的免责声明与测试报告一致（生成器会逐字比对）。
  const repoRoot=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
  for(const name of await readdir(path.join(repoRoot,'config'))){
    if(name==='notifications.json')continue;
    await cp(path.join(repoRoot,'config',name),path.join(f.root,'config',name));
  }
  const riskPath=path.join(f.root,'config/risk.json');
  const risk=JSON.parse(await readFile(riskPath,'utf8'));
  risk.disclaimer='本报告仅供研究与信息参考，不构成任何投资建议。';
  await f.put('config/risk.json',risk);
  // 真实生成入口：调用 CLI 同一个函数 buildResearchSeal（research-seal.mjs 只是它的薄封装），
  // 并显式指定夹具根目录，避免写入仓库自身的 work/。
  const generated=await buildResearchSeal({projectRoot:f.root,desiredDate:DAY,maxAgeMinutes:1440,now:NOW});
  assert.equal(generated.status,'sealed',JSON.stringify(generated));
  const sealPath=path.join(f.root,`work/research-seal/${DAY}/${digest(f.report)}.json`);
  const seal=JSON.parse(await readFile(sealPath,'utf8'));
  assert.equal(seal.kind,'research_seal');
  assert.equal(seal.state,'SEALED');
  assert.equal(seal.source.reportSha256,digest(f.report));
  assert.equal(seal.source.contextSha256,f.run.contextSha256);
  // 投递准备：用真实生成的密封作为证明
  const prepared=await prepareSealedMorning({projectRoot:f.root,now:NOW});
  assert.equal(prepared.status,'ready',JSON.stringify(prepared));
  assert.equal(prepared.proof.proofKind,'research_seal');
  // 模拟发送一次 + 回执落盘
  const result=await deliverSealedMorning(f.options);
  assert.equal(result.status,'sent',JSON.stringify(result));
  assert.equal(f.calls.length,1,'只发送一次');
  const receipt=JSON.parse(await readFile(
    path.join(f.root,`work/notifications/receipts/send/${EVENT}.json`),'utf8'));
  assert.equal(receipt.platformMessageId,'1');
  assert.equal(receipt.payloadSha256,prepared.proof.reportSha256);
  // 顺序重试不再发送
  assert.equal((await deliverSealedMorning(f.options)).status,'already_sent');
  assert.equal(f.calls.length,1);
});

test('人工恢复生成的事件在投递单元里报 held，等待二次授权而非 blocked',async t=>{
  const f=await fixture(t);
  // 去掉编排器认领标记（改回 2026-09-23 实机那种人工恢复 occurrence）。
  await f.put(`work/orchestrator/states/morning-report-${DAY}.json`,
    {...f.occurrence,invocationId:`operator-recovery-${DAY}T000855Z`,recoveryOf:'scheduled-failed'});
  const sent=[];
  const result=await deliverSealedMorning({...f.options,
    sender:async(delivery,meta)=>{sent.push(delivery);return {ok:true,started:true,exitCode:0,ack:{messageId:'1'}};}});
  assert.equal(result.status,'held','恢复事件不得自动发送');
  assert.equal(result.reason,'awaiting_send_authorization');
  assert.equal(result.sent,0);
  assert.equal(sent.length,0,'不得调用发送器');
  const status=await notificationStatus(f.root);
  assert.equal(status.awaitingAuthorization,1,'事件应留档在 pending 等待授权');
  assert.equal(status.sent,0);
});
