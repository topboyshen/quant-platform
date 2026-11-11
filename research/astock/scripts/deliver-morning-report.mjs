#!/usr/bin/env node
// Deliver an existing, sealed daily report; never invoke the report workflow.
import { createHash } from 'node:crypto';
import { lstat, readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { classifyRunOutcome, dateInTimeZone, notificationEventId } from '../src/run-notification.mjs';
import { isMainEntry } from '../src/main-entry.mjs';
import { buildReportPresentation, isResultPresentation } from '../src/report-presentation.mjs';
import { validateMorningSealProof } from '../src/morning-seal-proof.mjs';
import { deliverRunEvent } from '../src/notification-queue.mjs';
import { sendViaHermes } from '../src/notification-transport.mjs';
import { sentConfirmation } from '../src/notification-proof.mjs';
import { validateDeliveryPolicy } from '../src/notification-contract.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATES=['pending','sending','sent','ambiguous','dead'];
const MAX_AUTO_AGE_MS=120*60*1000;
const hash=(value)=>createHash('sha256').update(value).digest('hex');
class Held extends Error { constructor(reason){super(reason); this.reason=reason;} }
const hold=(reason)=>{throw new Held(reason);};

async function artifact(root,relative,limit=65536){
  // All callers use fixed paths derived only from date/hash, not provider text.
  const full=path.join(root,relative);
  for(let at=path.dirname(full); at!==root; at=path.dirname(at)){
    if(!at.startsWith(root+path.sep))hold('artifact_path_invalid');
    const info=await lstat(at);
    if(!info.isDirectory()||info.isSymbolicLink())hold('artifact_path_invalid');
  }
  const info=await lstat(full);
  if(!info.isFile()||info.isSymbolicLink()||info.size<=0||info.size>limit)hold('artifact_file_invalid');
  if(await realpath(full)!==full)hold('artifact_path_invalid');
  const body=await readFile(full);
  if(body.length<=0||body.length>limit)hold('artifact_file_invalid');
  return {body,sha256:hash(body)};
}
async function jsonArtifact(root,relative,limit){
  const result=await artifact(root,relative,limit);
  const value=JSON.parse(result.body.toString('utf8'));
  if(!value||typeof value!=='object'||Array.isArray(value))hold('artifact_json_invalid');
  return {...result,value};
}
function instant(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)
      ||!Number.isFinite(Date.parse(value)))hold('run_timestamp_invalid');
  return Date.parse(value);
}

/* 只用于“证明候选”探测：文件/目录不存在返回 null；其余错误照旧抛出。 */
async function readOptionalArtifact(projectRoot,relative,limit){
  try{
    return await artifact(projectRoot,relative,limit);
  }catch(error){
    if(error&&error.code==='ENOENT')return null;
    throw error;
  }
}

async function readOptionalJsonArtifact(projectRoot,relative,limit){
  try{
    return await jsonArtifact(projectRoot,relative,limit);
  }catch(error){
    if(error&&error.code==='ENOENT')return null;
    throw error;
  }
}

function sealProofFromResearchSeal(seal,targetDate,reportFile,report,run){
  return sealedProof(seal,'research',targetDate,reportFile,report,run);
}

function sealProofFromDraft(draft,targetDate,reportFile,report,run){
  return sealedProof(draft,'legacy',targetDate,reportFile,report,run);
}

function sealedProof(artifact,proofKind,targetDate,reportFile,report,run){
  try{
    return {sealSha256:artifact.sha256,...validateMorningSealProof({seal:artifact.value,proofKind,
      targetDate,reportFile,reportSha256:report.sha256,lastRunSha256:run.sha256,lastRun:run.value})};
  }catch{hold('sealed_report_proof_invalid');}
}

export async function prepareSealedMorning({projectRoot=ROOT,now=new Date(),manualSameDay=false}={}){
  if(!(now instanceof Date)||!Number.isFinite(now.getTime()))hold('observation_time_invalid');
  const targetDate=dateInTimeZone('Asia/Shanghai',now);
  const occurrence=await jsonArtifact(projectRoot,`work/orchestrator/states/morning-report-${targetDate}.json`);
  const run=await jsonArtifact(projectRoot,'work/last-run.json');
  const o=occurrence.value,r=run.value;
  if(!['1.0','2.0'].includes(o.schemaVersion)||o.task!=='morning-report'||o.targetDate!==targetDate||r.targetDate!==targetDate)
    hold('today_run_missing');
  const os=instant(o.startedAt),oe=instant(o.completedAt),rs=instant(r.startedAt),re=instant(r.completedAt);
  if(!(os<=rs&&rs<=re&&re<=oe&&oe<=now.getTime())
      ||dateInTimeZone('Asia/Shanghai',new Date(os))!==targetDate)hold('run_time_order_invalid');
  if(o.schemaVersion==='2.0'&&(o.frozenCutoffInstant!==`${targetDate}T00:00:00.000Z`
      ||r.frozenCutoffInstant!==o.frozenCutoffInstant||Date.parse(o.frozenCutoffInstant)>os))
    hold('source_occurrence_cutoff_invalid');
  if(!manualSameDay&&now.getTime()-re>MAX_AUTO_AGE_MS)hold('automatic_delivery_window_expired');
  if(o.childExitCode!==0||o.childSignal!==null||o.timedOut!==false||o.interrupted!==false)
    hold('source_occurrence_not_successful');
  if(o.status==='skipped'&&r.status==='skipped')return {status:'skipped',reason:'non_trading_day',targetDate};
  if(o.status!=='success'||r.status!=='success')hold('source_occurrence_not_successful');
  const args={exitCode:0,invocationStartedAt:o.startedAt,invocationEndedAt:o.completedAt,expectedTargetDate:targetDate};
  const outcome=classifyRunOutcome({...args,lastRun:r});
  if(outcome.outcome!=='success')hold('content_gate_not_passed');
  const reportFile=`reports/daily/${targetDate}.md`;
  const report=await artifact(projectRoot,reportFile,1024*1024);
  // 证明解析（单一顺序）：首选独立研究密封；仅当它不存在时才回退草稿清单；
  // 其它错误（权限/损坏/不符）一律阻断，不得靠回退绕过。
  let researchSeal;
  try{
    researchSeal=await readOptionalJsonArtifact(
      projectRoot,`work/research-seal/${targetDate}/${report.sha256}.json`,512*1024);
  }catch{
    // 损坏或权限错误一律阻断：不得回退到草稿绕过。
    hold('sealed_report_proof_invalid');
  }
  let proof;
  if(researchSeal!==null){
    proof=sealProofFromResearchSeal(researchSeal,targetDate,reportFile,report,run);
  }else{
    let draft;
    try{
      draft=await readOptionalJsonArtifact(
        projectRoot,`work/xhs/drafts/${targetDate}/${report.sha256}.json`,512*1024);
    }catch{
      hold('sealed_report_proof_invalid');
    }
    if(draft===null)hold('sealed_report_proof_missing');
    proof=sealProofFromDraft(draft,targetDate,reportFile,report,run);
  }
  return {status:'ready',targetDate,eventId:notificationEventId(targetDate,'success'),args,
          proof:{occurrenceSha256:occurrence.sha256,lastRunSha256:run.sha256,reportSha256:report.sha256,
                 sealSha256:proof.sealSha256,reportBytes:report.body.length,
                 proofKind:proof.proofKind,sealGeneratedAt:proof.sealGeneratedAt,
                 contextSha256:proof.contextSha256},reportFile};
}

async function validatePreparedEvent(root,prepared,e){
  if(e.schemaVersion!=='1.0'||e.eventId!==prepared.eventId||e.source!=='AStock_AI_System'
      ||e.route!=='telegram'||e.outcome!=='success'||e.targetDate!==prepared.targetDate
      ||e.report?.reportFile!==prepared.reportFile||e.report?.payloadFile!==`payloads/${prepared.eventId}.md`)
    hold('event_report_proof_mismatch');
  const resultView=isResultPresentation(e.report.presentation);
  if(Object.hasOwn(e.report,'presentation')&&!resultView)hold('event_report_proof_mismatch');
  if(resultView){
    if(e.report.sourceSha256!==prepared.proof.reportSha256||e.report.sourceBytes!==prepared.proof.reportBytes)
      hold('event_report_proof_mismatch');
  }else if(e.report.sha256!==prepared.proof.reportSha256||e.report.bytes!==prepared.proof.reportBytes
      ||Object.hasOwn(e.report,'sourceSha256')||Object.hasOwn(e.report,'sourceBytes')){
    hold('event_report_proof_mismatch');
  }
  const payload=await artifact(root,`work/notifications/payloads/${prepared.eventId}.md`,1024*1024);
  if(payload.sha256!==e.report.sha256||payload.body.length!==e.report.bytes)hold('event_payload_changed');
  if(resultView){
    const source=await artifact(root,prepared.reportFile,1024*1024);
    if(source.sha256!==prepared.proof.reportSha256||source.body.length!==prepared.proof.reportBytes)
      hold('event_report_proof_mismatch');
    const expected=buildReportPresentation(new TextDecoder('utf-8',{fatal:true}).decode(source.body),
      {targetDate:prepared.targetDate,presentation:e.report.presentation});
    if(!payload.body.equals(Buffer.from(expected,'utf8')))hold('event_projection_changed');
  }
}

async function currentEventState(root,prepared){
  const found=[];
  for(const state of STATES){
    const relative=`work/notifications/${state}/${prepared.eventId}.json`;
    try { await lstat(path.join(root,relative)); found.push({state,relative}); }
    catch(error){if(error.code!=='ENOENT')throw error;}
  }
  if(found.length>1)hold('event_has_multiple_states');
  if(!found.length)return null;
  const {state,relative}=found[0];
  if(['sending','ambiguous','dead'].includes(state))hold('delivery_requires_manual_review');
  const {value:e}=await jsonArtifact(root,relative,128*1024);
  await validatePreparedEvent(root,prepared,e);
  return state;
}

export async function deliverSealedMorning({projectRoot=ROOT,now=new Date(),manualSameDay=false,
    deliveryMode='disabled',checkOnly=false,sender=sendViaHermes}={}){
  const wallStarted=Date.now();
  let prepared;
  try {
    if(!['disabled','telegram'].includes(deliveryMode))hold('delivery_mode_invalid');
    const policy=validateDeliveryPolicy((await jsonArtifact(projectRoot,'config/notifications.json',16384)).value);
    if(deliveryMode!=='telegram'||!policy.deliveryEnabled)return {status:'held',reason:'delivery_not_authorized',sent:0};
    prepared=await prepareSealedMorning({projectRoot,now,manualSameDay});
    if(prepared.status==='skipped')return {...prepared,sent:0};
    const state=await currentEventState(projectRoot,prepared);
    if(state==='sent'){
      // 已发送记录必须由回执支撑；没有回执就只报“确认依据不足”，绝不重发。
      const confirmation=await sentConfirmation(projectRoot,prepared.eventId);
      return {status:'already_sent',eventId:prepared.eventId,confirmed:confirmation.confirmed,
              confirmationReason:confirmation.reason,sent:0};
    }
    // Validate new result-view generation before any outbox mutation. Existing
    // events retain their own full/result contract regardless of current policy.
    if(!state&&isResultPresentation(policy.reportView)){
      const source=await artifact(projectRoot,prepared.reportFile,1024*1024);
      if(source.sha256!==prepared.proof.reportSha256||source.body.length!==prepared.proof.reportBytes)
        hold('event_report_proof_mismatch');
      const projected=Buffer.from(buildReportPresentation(new TextDecoder('utf-8',{fatal:true}).decode(source.body),
        {targetDate:prepared.targetDate,presentation:policy.reportView}),'utf8');
      if(!projected.length||projected.length>1024*1024)hold('result_view_invalid');
    }
    if(checkOnly)return {status:'ready',eventId:prepared.eventId,reportSha256:prepared.proof.reportSha256,sent:0};
    // Re-check the sealed proof and persistent authorization at the actual send
    // boundary. A changed report cannot become a failure notification or leak.
    const guardedSender=async(delivery,{event})=>{
      try {
        const fresh=await prepareSealedMorning({projectRoot,now:new Date(now.getTime()+Date.now()-wallStarted),manualSameDay});
        const activePolicy=validateDeliveryPolicy((await jsonArtifact(projectRoot,'config/notifications.json',16384)).value);
        if(!activePolicy.deliveryEnabled||event.eventId!==prepared.eventId||event.outcome!=='success'
            ||JSON.stringify(fresh.proof)!==JSON.stringify(prepared.proof))return {ok:false,started:false};
        await validatePreparedEvent(projectRoot,prepared,event);
      } catch {return {ok:false,started:false};}
      return sender(delivery,{event});
    };
    const result=await deliverRunEvent({projectRoot,...prepared.args,deliveryMode,
                                      drainNow:now.getTime(),sender:guardedSender});
    // 恢复生成的事件不会自动发送：它停在 pending 并等待一次性二次授权。
    // 这是正常留档状态，不是投递失败，因此单独报出，便于验收区分。
    if(result.drain.awaitingAuthorization>0
        &&result.finalState!=='sent'&&!result.confirmed)
      return {status:'held',reason:'awaiting_send_authorization',eventId:prepared.eventId,
              finalState:result.finalState,sent:0,confirmed:false,
              awaitingReasons:result.drain.awaitingReasons};
    if(result.eventId!==prepared.eventId||result.finalState!=='sent'||!result.confirmed)
      return {status:'blocked',reason:'delivery_not_confirmed',eventId:prepared.eventId,
              finalState:result.finalState,sent:result.drain.sent,confirmed:false};
    // Reconcile the acknowledged event and immutable payload. CLI exit 0 is
    // transport acknowledgement, not proof that the human opened the message.
    if(await currentEventState(projectRoot,prepared)!=='sent')hold('acknowledgement_state_invalid');
    return {status:result.drain.sent?'sent':'already_sent',eventId:prepared.eventId,
            reportSha256:prepared.proof.reportSha256,confirmed:true,sent:result.drain.sent};
  }catch(error){
    return {status:'blocked',reason:error instanceof Held?error.reason:'required_artifact_or_policy_invalid',
            ...(prepared?.eventId?{eventId:prepared.eventId}:{}),sent:0,confirmed:false};
  }
}

export function parseDeliveryArguments(args){
  const allowed=new Set(['--scheduled','--manual-same-day','--check']);
  if(args.some(a=>!allowed.has(a))||new Set(args).size!==args.length
      ||Number(args.includes('--scheduled'))+Number(args.includes('--manual-same-day'))!==1)
    throw new Error('one fixed scheduled/manual-same-day mode required; dates, recipients and paths are not arguments');
  return {manualSameDay:args.includes('--manual-same-day'),checkOnly:args.includes('--check')};
}
export async function main(args=process.argv.slice(2)){
  let result;
  try{result=await deliverSealedMorning({...parseDeliveryArguments(args),deliveryMode:process.env.ASTOCK_FINAL_DELIVERY??'disabled'});}
  catch{result={status:'blocked',reason:'invalid_cli_arguments',sent:0};}
  console.log(JSON.stringify(result));
  return ['sent','already_sent','skipped','ready'].includes(result.status)?0:1;
}
if(isMainEntry(import.meta.url))process.exitCode=await main();
