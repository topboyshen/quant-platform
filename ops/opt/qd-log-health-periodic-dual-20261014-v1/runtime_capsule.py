"""Real periodic session wiring to the frozen actual/recovery root validators.

Caller establishes the original 20 seconds before source decoding. No fake old
G3 installation record is constructed. Only the actual recovery consumer is used.
"""
import hashlib
import json
import os
import re
import sys
import types
from contextlib import ExitStack,contextmanager
OLD_CAPSULE_SHA='c1f0228f48c967177e8f526957d65c47a44384eb77b3317011e87c4edb53deae'
ORIGINAL_STATE_SHA='afc4b554a3c0e9940db75bc73b9e28a248809edf66ae6865c7818b8ef4d75596'
STATE_SHA='3dd6943d12b888c85cfffa4201c4771d4e0d5ac49b822679973ff77eb6a659e6'
AUTH_SHA='82e4a36137bbdc51f3d8f946fde950da63a9bdceff3ee3e94b0a266959240042'
ORDER=('canary_child','periodic_state','periodic_contract','periodic_store','periodic_core','periodic_child','periodic_units','periodic_native')
def need(v,code):
    if not v:raise ValueError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(v):return (json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
def source_binding(data,periodic,expected,live,capsule_sha):
    need(type(live) is dict and set(live)=={'stage','inputs','roles','ready'},'actual_live_input_required')
    need(type(expected) is dict and set(expected)==set(ORDER) and type(periodic) is dict and set(periodic)==set(ORDER)
      and all(type(periodic[n]) is bytes and sha(periodic[n])==expected[n] for n in ORDER),'fixed_periodic_source_closure')
    need(expected['periodic_state']==STATE_SHA and expected['canary_child']=='fc24b7eacfe78b9837c7187753761cd98dfe1ad57bbc79043ee6e8898a65bb63','unchanged_child_and_state')
    need(type(capsule_sha) is str and re.fullmatch('[a-f0-9]{64}',capsule_sha),'runtime_capsule_digest')
    return sha(encoded({'schema':'qd.P.periodic-log-health.source-binding.v1','authorizationSha256':AUTH_SHA,'runtimeCapsuleSha256':capsule_sha,
      'periodicSources':expected,'stateOriginalSha256':ORIGINAL_STATE_SHA,'baseSources':data['expected'],
      'frozenSources':data['frozen_expected'],'payloadSources':data['payload_expected'],
      'recoveryBootstrapSha256':data['recovery_boot_sha'],'recoveryCloseoutSha256':sha(data['closeout_raw']),
      'actualLiveInput':live}))
def runtime_ready(tree,c,units,expected,source,deadline,capsule_sha):
    path=c.ROOT+'/RUNTIME-READY.json';raw,identity=tree.read(path,maximum=65536);value=c.strict(raw)
    need(type(value) is dict and set(value)=={'schema','sourceBindingSha256','files','sourceRootIdentity','notificationsEnabled','servicesActivated'},'runtime_ready_fields')
    need(value['schema']=='qd.P.periodic-log-health.ready-inactive.v1' and value['sourceBindingSha256']==source
      and value['notificationsEnabled'] is False and value['servicesActivated'] is False,'actual_runtime_ready_required')
    code={c.CODE+'/'+n+'.py':h for n,h in expected.items()}
    code.update({c.CODE+'/runtime_capsule.py':capsule_sha,c.CODE+'/state_original.py':ORIGINAL_STATE_SHA})
    wanted=set(code)|{c.CODE+'/root_entry.py',c.CODE+'/child_entry.py'}|set(units.files())
    need(type(value['files']) is dict and set(value['files'])==wanted,'exact_periodic_installed_files')
    need(c.exact(list(tree.directory(c.CODE)[:5]),value['sourceRootIdentity']) and tree.names(c.CODE)==sorted(p.rsplit('/',1)[1] for p in wanted if p.startswith(c.CODE+'/')),'code_directory_inventory')
    records={path:(raw,identity)}
    for name,pin in value['files'].items():
        c.left(deadline);need(type(pin) is dict and set(pin)=={'sha256','identity'} and c.digest(pin['sha256'])
          and type(pin['identity']) is list and len(pin['identity'])==9 and all(type(v) is int for v in pin['identity']),'runtime_file_pin')
        got,fp=tree.read(name,mode=0o644 if name in units.files() else 0o600,maximum=4194304)
        need(sha(got)==pin['sha256'] and c.exact(list(fp),pin['identity']),'runtime_file_changed')
        if name in code:need(pin['sha256']==code[name],'runtime_source_hash')
        if name in units.files():need(got==units.files()[name],'runtime_unit_bytes')
        records[name]=(got,fp)
    return value['files'],records
@contextmanager
def session(data,old_capsule_raw,periodic,expected,state_original,live,deadline,capsule_sha):
    # The source and real input closure is checked before any path is opened.
    need(sha(old_capsule_raw)==OLD_CAPSULE_SHA and sha(state_original)==ORIGINAL_STATE_SHA,'fixed_legacy_raw_required')
    source=source_binding(data,periodic,expected,live,capsule_sha)
    old=types.ModuleType('fixed_periodic_legacy_capsule');exec(compile(old_capsule_raw,'<fixed-periodic-legacy>','exec'),old.__dict__)
    old.validate_raw_closure(data['base'],data['expected'],data['frozen'],data['frozen_expected'],data['payload'],data['payload_expected'],data['dual'])
    with ExitStack() as stack:
        base=stack.enter_context(old.load_modules(data['base'],data['expected'],old.BASE_ORDER,path_for=lambda n:'<held-periodic-base-'+n+'>'))
        e=base['effect_core'];e.left(deadline);tree=e.Tree();stack.callback(tree.close)
        wanted=base['source_stage'].wanted_paths(data['frozen'],data['payload'],data['dual'])
        records,staged=old.stage_binding(tree,base,wanted,live['stage'],deadline)
        frozen=stack.enter_context(old.load_modules(data['frozen'],data['frozen_expected'],old.FROZEN_ORDER,path_for=lambda n:e.CODE+'/frozen/'+n+'.py'))
        need(type(data['recovery_boot_sha']) is str and re.fullmatch('[a-f0-9]{64}',data['recovery_boot_sha'])
          and sha(data['recovery_boot_raw'])==data['recovery_boot_sha'],'fixed_recovery_consumer')
        boot=types.ModuleType('fixed_periodic_recovery');boot.__file__='<fixed-periodic-recovery>'
        exec(compile(data['recovery_boot_raw'],boot.__file__,'exec'),boot.__dict__)
        consumer=boot.load_fixed(data['recovery_raws'],data['closeout_raw'])
        new=stack.enter_context(old.load_modules(periodic,expected,ORDER,path_for=lambda n:'/opt/qd-log-health-periodic-dual-20261014-v1/'+n+'.py'))
        new['periodic_state'].load_original(state_original);c=new['periodic_contract'];e.left(deadline)
        current_session=base['recovery_session'].RuntimeRecoverySession(tree,frozen,deadline,records,live['inputs'],live['roles'],consumer)
        gate=base['recovery_session'].RecoveryRootGate(tree,current_session,e,base['pure_probe'],deadline,live['ready'])
        # One shared metadata target throughout; no installer or metadata write.
        with base['binding_publisher'].exclusive_metadata_target():
            yield {'tree':tree,'base':base,'new':new,'gate':gate,'source':source}
def execute(data,old_capsule_raw,periodic,expected,state_original,live,deadline,capsule_sha):
    with session(data,old_capsule_raw,periodic,expected,state_original,live,deadline,capsule_sha) as s:
        tree=s['tree'];new=s['new'];gate=s['gate'];c=new['periodic_contract'];source=s['source']
        c.left(deadline,True)
        files,periodic_records=runtime_ready(tree,c,new['periodic_units'],expected,source,deadline,capsule_sha)
        port=new['periodic_native'].Port(tree,deadline,files,expected['periodic_child'])
        # The runtime proof is not a mutable source path or a CLI override.
        recheck=port.recheck
        def check_all():
            recheck()
            for path,pair in periodic_records.items():
                c.left(deadline);need(tree.read(path,mode=0o644 if path in new['periodic_units'].files() else 0o600,maximum=4194304)==pair,'runtime_ready_or_source_changed')
        port.recheck=check_all
        store=new['periodic_store'].Store(tree,deadline)
        invocation=os.environ.get('INVOCATION_ID')
        need(type(invocation) is str and re.fullmatch('[a-f0-9]{32}',invocation),'own_systemd_invocation_required')
        nonce=sha(os.urandom(32))
        return new['periodic_core'].execute(gate,port,store,deadline,invocation,nonce,source)
def capture_current(data,old_capsule_raw,periodic,expected,state_original,live,deadline,capsule_sha):
    """Fixed root-side read-only facade for the final caller's original deadline.

    No child/control, secret/notification or mutable checkpoint operation. This
    validates the fixed frontend only; the final caller separately owns the
    eleven-sibling and host-retirement checks, never inferred here.
    """
    with session(data,old_capsule_raw,periodic,expected,state_original,live,deadline,capsule_sha) as s:
        tree=s['tree'];new=s['new'];gate=s['gate'];c=new['periodic_contract']
        # runtime_ready does not replace genuine recapture with historical pins.
        files,records=runtime_ready(tree,c,new['periodic_units'],expected,s['source'],deadline,capsule_sha)
        bundle=gate.prepare();before=gate.recapture(bundle);protection=gate.protection();gate.recheck(bundle)
        after=gate.recapture(bundle);need(c.exact(before,after),'current_frontend_capture_changed');gate.recheck(bundle)
        for path,pair in records.items():
            s['base']['effect_core'].left(deadline)
            need(tree.read(path,mode=0o644 if path in new['periodic_units'].files() else 0o600,maximum=4194304)==pair,'runtime_ready_or_source_changed')
        return {'schema':'qd.P.periodic-log-health.current-fixed-front.v1','sourceBindingSha256':s['source'],
          'actualSha256':before['actualSha256'],'runtime':bundle['expected'],
          'rootCapture':before,'staticFileCount':598,'staticAclVerified':True,
          'inactiveReadySha256':bundle['readyReceiptSha256'],
          'periodicReadySha256':sha(records[c.ROOT+'/RUNTIME-READY.json'][0]),
          'periodicFilesSha256':sha(encoded(files)),'currentProtectionSha256':sha(encoded(protection)),
          'elevenSiblingContainersVerified':False,'hostRetirementVerified':False,
          'messagesSent':False,'notificationsEnabled':False,'effectsExecuted':False,'G1Passed':False}
