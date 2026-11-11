"""Fixed periodic dual-authority protocol; no effect, notification or CLI pins."""
import math
import re
import time
import dual_core as d
import periodic_state as state
CODE='/opt/qd-log-health-periodic-dual-20261014-v1'
ROOT=state.ROOT
CONTROL=ROOT+'/control'
ROOT_UNIT='qd-redacted-log-periodic-root.service'
CHILD_UNIT='qd-redacted-log-periodic-dac.service'
TIMER='qd-redacted-log-periodic-root.timer'
MODE=ROOT+'/mode.json'
CONTEXT=CONTROL+'/context.json'
READY=CONTROL+'/ready.json'
RELEASE=CONTROL+'/release.json'
DONE=CONTROL+'/done.json'
FAILURE=CONTROL+'/failure.json'
CYCLE_PROOF=ROOT+'/cycle-proof.json'
BOUND=ROOT+'/bound/frontend.sock'
SOURCE=d.SOURCE
TOTAL=20.0;RESERVE=3.0;MAXIMUM=32768
MASK=0xa00425ff
require=d.require;Refused=d.Refused;Deadline=d.Deadline
sha=d.sha;encoded=d.encoded;strict=d.strict;exact=d.exact;digest=d.digest
def left(deadline,work=False):
    require(type(deadline) is float and math.isfinite(deadline),'original_deadline')
    raw=deadline-time.monotonic()
    require(raw<=TOTAL,'extended_deadline')
    n=raw-(RESERVE if work else 0)
    if n<=0:raise Deadline()
    require(n<=TOTAL,'extended_deadline');return n
def result(x):
    require(type(x) is dict and set(x)=={'ok','reason','roles','frontend_binding_ok'} and type(x['ok']) is bool
      and type(x['frontend_binding_ok']) is bool and type(x['roles']) is dict and set(x['roles'])=={'host','frontend'}
      and all(type(y) is bool for y in x['roles'].values()),'closed_normal_probe_result')
    require(x['ok']==all(x['roles'].values()) and (x['frontend_binding_ok'] or not x['roles']['frontend'])
      and x['reason']==('collector_canary_acknowledged' if x['ok'] else 'probe_failed'),'consistent_normal_probe_result')
    return x
def context(v):
    require(type(v) is dict and set(v)=={'schema','generation','invocationId','nonceSha256','dualContext','sourceBindingSha256'},'cycle_context_fields')
    require(v['schema']=='qd.P.periodic-log-health.context.v1' and type(v['generation']) is int and 0<v['generation']<=2**53-1
      and type(v['invocationId']) is str and re.fullmatch('[a-f0-9]{32}',v['invocationId']) and digest(v['nonceSha256'])
      and digest(v['sourceBindingSha256']),'cycle_context_identity')
    d.validate_context(v['dualContext']);left(v['dualContext']['deadlineMonotonic']);return v
def ready(v,ctx):
    context(ctx)
    require(type(v) is dict and set(v)=={'schema','generation','invocationId','nonceSha256','contextSha256','child'},'periodic_ready_fields')
    require(v['schema']=='qd.P.periodic-log-health.child-ready.v1' and all(exact(v[k],ctx[k]) for k in
      ('generation','invocationId','nonceSha256')) and v['contextSha256']==sha(encoded(ctx)),'periodic_ready_identity')
    x=v['child'];require(type(x) is dict and set(x)=={'pid','startTicks','result','credentials','mountNamespace'},'periodic_child_fields')
    result(x['result']);require(type(x['pid']) is int and 1<x['pid']<2**31 and type(x['startTicks']) is int and x['startTicks']>0,'periodic_child_pid')
    require(exact(x['credentials'],{'uid':[0]*4,'gid':[0]*4,'CapEff':4,'CapPrm':4,'CapBnd':4,'CapInh':0,'CapAmb':0,'NoNewPrivs':1}), 'periodic_dac_only')
    require(type(x['mountNamespace']) is str and re.fullmatch(r'mnt:\[[0-9]+\]',x['mountNamespace'])
      and x['mountNamespace']!=ctx['dualContext']['parentNamespace'],'private_child_namespace');return v
def tag(ctx):return {k:ctx[k] for k in ('generation','invocationId','nonceSha256')}
def envelope(ctx,kind,value):return {'schema':'qd.P.periodic-log-health.channel.v1',**tag(ctx),'kind':kind,'value':value}
def channel(v,ctx,kind):
    require(type(v) is dict and set(v)=={'schema','generation','invocationId','nonceSha256','kind','value'}
      and v['schema']=='qd.P.periodic-log-health.channel.v1' and v['kind']==kind
      and all(exact(v[k],ctx[k]) for k in tag(ctx)),'cycle_channel_identity');return v['value']
def summary(source,observed,captures,child_sha,cleanup,health,reason):
    value={'health':health,'normalProbeVerified':health=='healthy','reason':reason,
      'rootCapturesSha256':{k:sha(encoded(v)) if v is not None else None for k,v in captures.items()},
      'childSha256':child_sha,'cleanupOwned':cleanup,'sourceBindingSha256':source,'originalBudgetSeconds':20,
      'messagesSent':False,'notificationsEnabled':False,'metricsPublished':False,'observedUnix':observed}
    state.summary(value,health!='unknown');return value
