"""One natural generation. UNKNOWN retains a barrier; no retries or notification."""
import time
import periodic_contract as c
import periodic_state as state
def execute(gate,port,store,deadline,invocation,nonce,source_binding):
    c.require(gate.deadline==port.deadline==store.deadline==deadline and c.digest(source_binding),'one_original_budget')
    current=None;pair=None;claimed=False;cleanup=False;child_sha=None
    captures={k:None for k in ('before','childAlive','after')};phase='preflight'
    with store.held():
        try:
            c.left(deadline,True);port.root_identity(invocation);port.inactive_child();store.clean_channels()
            old,old_pair=store.checkpoint()
            # Pure transition refuses unresolved STARTED/UNKNOWN before even recapturing.
            next_state=state.begin(old,invocation,nonce,deadline,lambda:c.left(deadline))
            bundle=gate.prepare();actual=bundle['actual']
            captures['before']=c.d.capture_proof(gate.recapture(bundle),actual)
            protection=gate.protection();c.require(protection.get('protectedEntriesVerified') is True,'current_protection')
            gate.recheck(bundle)
            context=c.context({'schema':'qd.P.periodic-log-health.context.v1','generation':next_state['generation'],
              'invocationId':invocation,'nonceSha256':nonce,'sourceBindingSha256':source_binding,
              'dualContext':{'schema':'qd.P.normal-probe.dual-context.v1','expected':bundle['expected'],'runtime':bundle['runtime'],
                'actualSha256':c.sha(c.encoded(actual)),'readyReceiptSha256':bundle['readyReceiptSha256'],
                'parentNamespace':port.parent_namespace(),'deadlineMonotonic':deadline,
                'childSourceSha256':port.child_source_sha,'rootCaptureBeforeSha256':c.sha(c.encoded(captures['before']))}})
            c.left(deadline,True);phase='claim_checkpoint';claimed=True;current=next_state
            pair=store.save_checkpoint(current,old_pair)
            phase='publish_context';port.prepare(context,store)
            phase='start_dac_child';port.start(context)
            phase='normal_canaries';ready=c.ready(port.await_ready(context,store),context)
            child_sha=c.sha(c.encoded(ready));observed=port.observe_live(ready,context)
            phase='middle_root_capture';captures['childAlive']=c.d.capture_proof(gate.recapture(bundle),actual)
            c.require(c.exact(captures['before'],captures['childAlive']),'middle_capture_changed');gate.recheck(bundle)
            phase='complete_dac_child';port.complete(ready,context,store)
            phase='owned_cleanup';cleanup=port.cleanup() is True;c.require(cleanup,'cleanup_unconfirmed')
            phase='after_root_capture';captures['after']=c.d.capture_proof(gate.recapture(bundle),actual)
            c.require(c.exact(captures['before'],captures['after']),'after_capture_changed')
            c.require(c.exact(protection,gate.protection()),'protection_changed');gate.recheck(bundle)
            resources=port.root_identity(invocation);c.left(deadline)
            result=ready['child']['result'];health='healthy' if result['ok'] else 'unhealthy'
            summary=c.summary(source_binding,time.time(),captures,child_sha,True,health,result['reason'])
            current=state.commit(current,invocation,nonce,deadline,summary,lambda:c.left(deadline))
            phase='publish_cycle_proof'
            store.save_proof({'schema':'qd.P.periodic-log-health.cycle-proof.v1',
              'generation':current['generation'],'invocationId':invocation,'nonceSha256':nonce,
              'sourceBindingSha256':source_binding,'checkpointSha256':c.sha(state.encoded(current)),
              'rootResource':resources,'childResource':observed,'normalResult':result,
              'contextSha256':c.sha(c.encoded(context)),'readySha256':child_sha,
              'rootCaptures':captures,'protectionSha256':c.sha(c.encoded(protection)),
              'cleanupOwned':True,'observedUnix':summary['observedUnix'],
              'messagesSent':False,'notificationsEnabled':False,'metricsPublished':False})
            phase='commit_checkpoint';pair=store.save_checkpoint(current,pair)
            return {'schema':'qd.P.periodic-log-health.cycle-result.v1','state':'COMMITTED','health':health,
              'generation':current['generation'],'invocationId':invocation,'checkpointSha256':c.sha(pair[0]),
              'checkpointIdentity':list(pair[1]),'rootResource':resources,'childResource':observed,
              'normalProbeVerified':health=='healthy','messagesSent':False,'notificationsEnabled':False,
              'metricsPublished':False,'G1Passed':False,'productionSwitched':False}
        except BaseException as exc:
            if claimed:
                try:cleanup=port.cleanup() is True
                except BaseException:cleanup=False
                # Budget expiry may leave STARTED. Never obtain a fresh write budget.
                try:
                    c.left(deadline)
                    if pair is not None and current['current']['state']=='STARTED':
                        summary=c.summary(source_binding,time.time(),captures,child_sha,cleanup,'unknown',
                          'deadline' if isinstance(exc,c.Deadline) else 'OtherException')
                        current=state.unknown(current,invocation,nonce,deadline,summary)
                        pair=store.save_checkpoint(current,pair)
                except BaseException:pass
            return {'schema':'qd.P.periodic-log-health.cycle-result.v1','state':'UNKNOWN' if claimed else 'BLOCKED',
              'phase':phase,'failureClass':c.d.safe_failure(exc),'cleanupOwned':cleanup,
              'normalProbeVerified':False,'messagesSent':False,'notificationsEnabled':False,'metricsPublished':False,
              'G1Passed':False,'productionSwitched':False,'automaticRetry':False}
