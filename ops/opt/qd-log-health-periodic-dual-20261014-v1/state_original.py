"""Pure bounded periodic checkpoint protocol. No filesystem, child or network.

The owning helper must hold its one fixed nonblocking operational lock and
compare-and-swap the exact bytes through its NOFOLLOW, root-private store. This
module never changes old one-shot journals or resolves an uncertain checkpoint.
"""
import copy
import hashlib
import json
import math
import re

ROOT='/var/lib/qd-log-health-periodic-dual-20261014-v1'
STATE=ROOT+'/checkpoint.json'
LOCK=ROOT+'/cycle.lock'
SCHEMA='qd.P.periodic-log-health.checkpoint.v1'
MAX_BYTES=8192
TOTAL=20
KEYS={'schema','generation','current','previous_unknown'}
CURRENT_KEYS={'invocationId','nonceSha256','deadlineMonotonic','state','summary'}
SUMMARY_KEYS={'health','normalProbeVerified','reason','rootCapturesSha256','childSha256','cleanupOwned',
 'sourceBindingSha256','originalBudgetSeconds','messagesSent','notificationsEnabled','metricsPublished'}
REASONS={'collector_canary_acknowledged','probe_failed','deadline','ownership_changed','source_changed',
 'child_unconfirmed','cleanup_unconfirmed','capture_unconfirmed','OtherException'}
class Refused(ValueError):pass
def need(value,code):
    if not value:raise Refused(code)
def digest(value):return type(value) is str and re.fullmatch('[a-f0-9]{64}',value) is not None
def finite_positive(value):return type(value) in (int,float) and math.isfinite(value) and value>0
def budget(remaining):
    need(callable(remaining),'original_remaining_callback_required')
    value=remaining();need(finite_positive(value) and value<=TOTAL,'original_budget_expired_or_invalid')
    return value
def encoded(value):
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    need(len(raw)<=MAX_BYTES,'bounded_checkpoint_required');return raw
def sha(raw):return hashlib.sha256(raw).hexdigest()
def strict(raw):
    need(type(raw) is bytes and 0<len(raw)<=MAX_BYTES,'bounded_checkpoint_input')
    def pairs(rows):
        out={}
        for key,value in rows:need(key not in out,'duplicate_checkpoint_key');out[key]=value
        return out
    doc=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _x:need(False,'nonfinite_checkpoint'))
    validate(doc);need(raw==encoded(doc),'canonical_checkpoint_required');return doc
def summary(value,committed):
    need(type(value) is dict and set(value)==SUMMARY_KEYS,'summary_fields')
    need(type(value['health']) is str and value['health'] in {'healthy','unhealthy','unknown'}
        and type(value['normalProbeVerified']) is bool and type(value['reason']) is str and value['reason'] in REASONS
        and type(value['cleanupOwned']) is bool and type(value['notificationsEnabled']) is bool
        and type(value['metricsPublished']) is bool and value['messagesSent'] is False
        and type(value['originalBudgetSeconds']) is int and value['originalBudgetSeconds']==TOTAL
        and digest(value['sourceBindingSha256']),'summary_scope')
    captures=value['rootCapturesSha256'];need(type(captures) is dict and set(captures)=={'before','childAlive','after'},'three_capture_fields')
    if committed:
        observation=(value['health']=='healthy' and value['normalProbeVerified'] is True
                     and value['reason']=='collector_canary_acknowledged') or (
                     value['health']=='unhealthy' and value['normalProbeVerified'] is False
                     and value['reason']=='probe_failed')
        need(observation and value['cleanupOwned'] is True and all(digest(v) for v in captures.values())
            and digest(value['childSha256']),'committed_requires_full_proof')
    else:
        need(value['health']=='unknown' and value['normalProbeVerified'] is False
            and value['reason'] not in {'collector_canary_acknowledged','probe_failed'}
            and value['notificationsEnabled'] is False and value['metricsPublished'] is False
            and all(v is None or digest(v) for v in captures.values())
            and (value['childSha256'] is None or digest(value['childSha256'])),'unknown_not_green')
def current(value):
    need(type(value) is dict and set(value)==CURRENT_KEYS,'current_fields')
    need(type(value['invocationId']) is str and re.fullmatch('[a-f0-9]{32}',value['invocationId'])
        and digest(value['nonceSha256']) and type(value['deadlineMonotonic']) is float
        and finite_positive(value['deadlineMonotonic']) and type(value['state']) is str
        and value['state'] in {'STARTED','COMMITTED','UNKNOWN'},'current_identity')
    if value['state']=='STARTED':need(value['summary'] is None,'started_not_a_health_result')
    else:summary(value['summary'],value['state']=='COMMITTED')
def validate(value):
    need(type(value) is dict and set(value)==KEYS and value['schema']==SCHEMA
        and type(value['generation']) is int and 0<=value['generation']<=2**53-1,'checkpoint_fields')
    now=value['current'];old=value['previous_unknown']
    if now is None:need(value['generation']==0 and old is None,'initial_checkpoint_shape')
    else:need(value['generation']>0,'current_generation');current(now)
    if old is not None:
        need(type(old) is dict and set(old)=={'generation','current'} and type(old['generation']) is int
            and 0<old['generation']<=value['generation'],'previous_unknown_shape')
        current(old['current']);need(old['current']['state']=='UNKNOWN','previous_unknown_must_stay_unknown')
    encoded(value);return value
def empty():return {'schema':SCHEMA,'generation':0,'current':None,'previous_unknown':None}
def identity(invocation_id,nonce_sha,deadline):
    doc={'invocationId':invocation_id,'nonceSha256':nonce_sha,'deadlineMonotonic':deadline,'state':'STARTED','summary':None}
    current(doc);return doc
def begin(value,invocation_id,nonce_sha,deadline,remaining):
    """Compute a next claim only after the caller's original remaining check."""
    budget(remaining)
    validate(value);need(value['previous_unknown'] is None,'previous_unknown_retained_no_new_cycle')
    old=value['current']
    need(old is None or old['state']=='COMMITTED','unfinished_checkpoint_retained_no_new_cycle')
    need(old is None or invocation_id!=old['invocationId'],'invocation_reuse_refused')
    need(value['generation']<2**53-1,'generation_exhausted')
    out=copy.deepcopy(value);out['generation']+=1;out['current']=identity(invocation_id,nonce_sha,deadline)
    validate(out);return out
def owned(value,invocation_id,nonce_sha,deadline):
    validate(value);row=value['current'];need(row is not None and row['state']=='STARTED'
        and row['invocationId']==invocation_id and row['nonceSha256']==nonce_sha
        and row['deadlineMonotonic']==deadline,'exact_owned_started_required')
def commit(value,invocation_id,nonce_sha,deadline,result,remaining):
    budget(remaining)
    owned(value,invocation_id,nonce_sha,deadline);summary(result,True)
    out=copy.deepcopy(value);out['current'].update(state='COMMITTED',summary=copy.deepcopy(result));validate(out);return out
def unknown(value,invocation_id,nonce_sha,deadline,result):
    """Only the current owned invocation may record failure. No retry implied.

    Expired callers may leave STARTED intact instead. This pure transition
    grants neither a write after deadline nor permission to resolve UNKNOWN.
    """
    owned(value,invocation_id,nonce_sha,deadline);summary(result,False)
    out=copy.deepcopy(value);out['current'].update(state='UNKNOWN',summary=copy.deepcopy(result));validate(out);return out
