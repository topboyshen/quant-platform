"""Normal fixed canaries, no foreign /proc, Docker, incident or notifier call."""
import os
import platform
import signal
import stat
import time
import canary_child as original
import effect_core as e
import periodic_contract as c
import periodic_store
HELD=None
def bootstrap_bound():
    return type(HELD) is dict and set(HELD)=={'sourceBindingSha256','childSha256'} and all(c.digest(v) for v in HELD.values())
def local_binding(ctx,probe,binding):
    value=ctx['dualContext'];c.d.validate_context(value)
    parent=os.lstat(c.ROOT+'/bound')
    c.require(stat.S_ISDIR(parent.st_mode) and parent.st_uid==parent.st_gid==0 and stat.S_IMODE(parent.st_mode)==0o700,'private_bound_directory')
    raw=original.self_read('/proc/self/mountinfo',1048576)
    c.require(probe.readonly_self_mount(raw.decode('utf-8'),target=c.BOUND),'self_readonly_bind')
    return binding.check_binding(expected=value['expected'],runtime=value['runtime'],
      parent=os.lstat(os.path.dirname(c.SOURCE)),source=os.lstat(c.SOURCE),mounted=os.lstat(c.BOUND))
def credentials():
    got=original.own_credentials()
    row=[x for x in original.self_read('/proc/self/status',65536).decode('ascii').splitlines() if x.startswith('CapInh:')]
    c.require(len(row)==1,'self_cap_inheritable');got['CapInh']=int(row[0].split(':')[1].strip(),16);return got
def run(ctx):
    c.context(ctx);deadline=ctx['dualContext']['deadlineMonotonic'];c.left(deadline,True)
    with original.modules(deadline) as (probe,binding):
        c.require(probe.ROLES=={'host':('/run/qd-redacted-log/syslog.sock','/var/lib/qd-redacted-log'),
          'frontend':(c.SOURCE,'/var/lib/qd-redacted-log-frontend')},'fixed_collector_roles')
        value=probe.probe_roles(ctx['dualContext']['expected'],binding_probe=lambda expected:
          local_binding(ctx,probe,binding) if c.exact(expected,ctx['dualContext']['expected']) else original.deny())
        c.result(value);c.left(deadline,True)
        ready={'schema':'qd.P.periodic-log-health.child-ready.v1',**c.tag(ctx),'contextSha256':c.sha(c.encoded(ctx)),
          'child':{'pid':os.getpid(),'startTicks':original.own_ticks(),'result':value,'credentials':credentials(),
            'mountNamespace':os.readlink('/proc/self/ns/mnt')}}
        return c.ready(ready,ctx)
def current_channel(pair,ctx,kind):
    if pair is None:return None
    value=c.strict(pair[0]);c.require(type(value) is dict and set(value)=={'schema','generation','invocationId','nonceSha256','kind','value'}
      and value['schema']=='qd.P.periodic-log-health.channel.v1' and value['kind']==kind
      and type(value.get('generation')) is int and 0<value['generation']<=2**53-1
      and type(value['invocationId']) is str and len(value['invocationId'])==32
      and all(x in 'abcdef0123456789' for x in value['invocationId']) and c.digest(value['nonceSha256']),'channel_prior_shape')
    if value['generation']<ctx['generation']:return None
    return c.channel(value,ctx,kind)
def main():
    if not bootstrap_bound():return 2  # Pure memory, before all environment IO.
    deadline=time.monotonic()+c.TOTAL;tree=None;ctx=None;store=None
    try:
        c.require(os.geteuid()==os.getegid()==0 and platform.system()=='Linux'
          and platform.machine() in ('x86_64','amd64'),'native_root_child')
        tree=e.Tree();store=periodic_store.Store(tree,deadline)
        context_pair=store.read(c.CONTEXT);ctx=c.context(c.strict(context_pair[0]));deadline=ctx['dualContext']['deadlineMonotonic'];store.deadline=deadline
        c.require(ctx['sourceBindingSha256']==HELD['sourceBindingSha256'] and ctx['dualContext']['childSourceSha256']==HELD['childSha256'],'child_exact_source_context')
        signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(c.Deadline()));signal.setitimer(signal.ITIMER_REAL,c.left(deadline))
        ready=run(ctx);store.publish(c.READY,c.envelope(ctx,'ready',ready),child=True)
        while True:
            c.left(deadline,True);release=current_channel(store.read(c.RELEASE,missing=True),ctx,'release')
            if release is not None:break
            time.sleep(.02)
        c.require(c.exact(release,{'pid':ready['child']['pid'],'startTicks':ready['child']['startTicks'],
          'readySha256':c.sha(c.encoded(ready))}),'same_owned_child_release')
        c.require(store.read(c.CONTEXT)==context_pair,'context_unchanged')
        store.publish(c.DONE,c.envelope(ctx,'done',{'contextSha256':c.sha(c.encoded(ctx)),
          'readySha256':c.sha(c.encoded(ready)),'actualChildCompleted':True}),child=True)
        return 0
    except BaseException as exc:
        if ctx is not None and store is not None:
            try:store.publish(c.FAILURE,c.envelope(ctx,'failure',{'failureClass':c.d.safe_failure(exc),'normalProbeVerified':False}),child=True)
            except BaseException:pass
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        if tree is not None:tree.close()
if __name__=='__main__':raise SystemExit(main())
