"""Minimal producer-clock successor; all original state rules remain exact.

No IO or native operations. An exact-source helper must first load_original
with the original reviewed v1 bytes. Only summary.observedUnix is added; neither
STARTED nor mtime is a result clock. Old v1 records and freezes are untouched.
"""
import copy
import hashlib
import json
import math
import types
ORIGINAL_SHA='afc4b554a3c0e9940db75bc73b9e28a248809edf66ae6865c7818b8ef4d75596'
ORIGINAL=None
SCHEMA='qd.P.periodic-log-health.checkpoint.v2'
ROOT='/var/lib/qd-log-health-periodic-dual-20261014-v1'
STATE=ROOT+'/checkpoint.json';LOCK=ROOT+'/cycle.lock';MAX_BYTES=8192;TOTAL=20
class Refused(ValueError):pass
def need(v,code):
    if not v:raise Refused(code)
def load_original(raw):
    global ORIGINAL
    need(ORIGINAL is None and type(raw) is bytes and hashlib.sha256(raw).hexdigest()==ORIGINAL_SHA,
         'original_exact_state_source_required')
    m=types.ModuleType('fixed_periodic_state_v1');m.__file__='<held-periodic-state-v1>'
    exec(compile(raw,m.__file__,'exec'),m.__dict__);ORIGINAL=m;return m
def original():
    need(ORIGINAL is not None,'original_state_not_loaded');return ORIGINAL
def summary(value,committed):
    o=original();need(type(value) is dict and set(value)==o.SUMMARY_KEYS|{'observedUnix'},'clock_summary_fields')
    observed=value['observedUnix'];need(type(observed) in (int,float) and math.isfinite(observed)
                                      and observed>0,'actual_producer_clock_required')
    o.summary({k:v for k,v in value.items() if k!='observedUnix'},committed)
def v1(value):
    o=original();need(type(value) is dict and value.get('schema')==SCHEMA,'v2_schema_required')
    out=copy.deepcopy(value);out['schema']=o.SCHEMA
    for row in (out.get('current'),out.get('previous_unknown',{}).get('current')
                if type(out.get('previous_unknown')) is dict else None):
        if type(row) is dict and row.get('state')!='STARTED':
            summary(row.get('summary'),row.get('state')=='COMMITTED')
            row['summary'].pop('observedUnix')
    o.validate(out);return out
def validate(value):
    v1(value);encoded(value);return value
def encoded(value):
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    need(len(raw)<=MAX_BYTES,'bounded_checkpoint_required');return raw
def sha(raw):return hashlib.sha256(raw).hexdigest()
def strict(raw):
    need(type(raw) is bytes and 0<len(raw)<=MAX_BYTES,'bounded_checkpoint_input')
    def pairs(rows):
        out={}
        for k,v in rows:need(k not in out,'duplicate_checkpoint_key');out[k]=v
        return out
    v=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:need(False,'nonfinite_checkpoint'))
    validate(v);need(raw==encoded(v),'canonical_checkpoint_required');return v
def empty():
    out=original().empty();out['schema']=SCHEMA;return out
def identity(invocation_id,nonce_sha,deadline):return original().identity(invocation_id,nonce_sha,deadline)
def begin(value,invocation_id,nonce_sha,deadline,remaining):
    out=original().begin(v1(value),invocation_id,nonce_sha,deadline,remaining)
    out['schema']=SCHEMA;validate(out);return out
def owned(value,invocation_id,nonce_sha,deadline):return original().owned(v1(value),invocation_id,nonce_sha,deadline)
def finish(kind,value,invocation_id,nonce_sha,deadline,result,remaining=None):
    o=original();summary(result,kind=='COMMITTED')
    plain={k:v for k,v in result.items() if k!='observedUnix'}
    out=(o.commit(v1(value),invocation_id,nonce_sha,deadline,plain,remaining) if kind=='COMMITTED'
         else o.unknown(v1(value),invocation_id,nonce_sha,deadline,plain))
    out['schema']=SCHEMA;out['current']['summary']=copy.deepcopy(result);validate(out);return out
def commit(value,invocation_id,nonce_sha,deadline,result,remaining):
    return finish('COMMITTED',value,invocation_id,nonce_sha,deadline,result,remaining)
def unknown(value,invocation_id,nonce_sha,deadline,result):
    return finish('UNKNOWN',value,invocation_id,nonce_sha,deadline,result)
