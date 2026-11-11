"""Fixed two installed-unit observations and one owned child start/stop per cycle."""
import os
import re
import time
import bounded_capture as bc
import canary_child as legacy_child
import periodic_child as child
import periodic_contract as c
import periodic_units as units

SHOW=('LoadState','ControlGroup','MainPID','ExecMainPID','ActiveState','SubState','Result','ExecMainStatus','InvocationID',
 'ExecStart','MemoryMax','MemorySwapMax','TasksMax','TimeoutStartUSec','NoNewPrivileges','PrivateMounts','PrivateTmp',
 'PrivateDevices','ProtectSystem','ProtectHome','RestrictAddressFamilies','CapabilityBoundingSet','AmbientCapabilities','User',
 'Group','UMask','ReadWritePaths','InaccessiblePaths','KillMode','TimeoutStopUSec','LimitCORE','BindReadOnlyPaths','ReadOnlyPaths')
ENV={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'}
def show_argv(unit):
    c.require(unit in (c.ROOT_UNIT,c.CHILD_UNIT),'two_own_units_only')
    return ['/usr/bin/systemctl','show','--all',unit,'--property='+','.join(SHOW)]
def parse(raw):
    c.require(type(raw) is bytes and 0<len(raw)<=65536,'show_bound');out={}
    for row in raw.decode('ascii').splitlines():
        key,sep,value=row.partition('=');c.require(sep and key in SHOW and key not in out,'show_shape');out[key]=value
    c.require(set(out)==set(SHOW),'show_complete');return out
def exec_value(value,child_mode):
    stamp=r'(?:n/a|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [A-Z]{1,6})'
    entry=c.CODE+('/child_entry.py' if child_mode else '/root_entry.py')
    start='{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I -B '+entry+' ; ignore_errors=no ; start_time=['
    expr=re.escape(start)+stamp+r'\] ; stop_time=\['+stamp+r'\] ; pid=[0-9]+ ; code=(?:\(null\)|exited|killed|dumped) ; status=[0-9]+(?:/(?:[0-9]+|SUCCESS|FAILURE))? \}'
    c.require(type(value) is str and re.fullmatch(expr,value),'fixed_unit_entry')
def validate_loaded(value,child_mode):
    c.require(type(value) is dict and set(value)==set(SHOW) and all(type(v) is str for v in value.values()),'loaded_fields')
    wanted={'LoadState':'loaded','MemoryMax':'67108864','MemorySwapMax':'0','TasksMax':'32','TimeoutStartUSec':'24s',
      'NoNewPrivileges':'yes','PrivateMounts':'yes','PrivateTmp':'yes','PrivateDevices':'yes','ProtectSystem':'strict',
      'ProtectHome':'yes' if child_mode else 'read-only','RestrictAddressFamilies':'AF_UNIX','AmbientCapabilities':'',
      'User':'','Group':'root','UMask':'0077','ReadWritePaths':c.CONTROL if child_mode else c.ROOT,
      'KillMode':'control-group','TimeoutStopUSec':'2s','LimitCORE':'0'}
    c.require(all(value[k]==v for k,v in wanted.items()),'unit_limits')
    caps=value['CapabilityBoundingSet'].upper().split();expected=units.properties(child_mode)['CapabilityBoundingSet'].split()
    c.require(len(caps)==len(expected) and set(caps)==set(expected),'unit_capability_ceiling')
    for key in ('InaccessiblePaths','ReadOnlyPaths'):
        a=value[key].split();b=units.properties(child_mode).get(key,'').split()
        c.require(len(a)==len(b) and set(a)==set(b),'unit_path_restrictions')
    allowed=tuple(c.SOURCE+':'+c.BOUND+x for x in ('',':rbind',':norbind')) if child_mode else ('',)
    c.require(value['BindReadOnlyPaths'] in allowed,'private_readonly_child_bind');exec_value(value['ExecStart'],child_mode)
    c.require(value['ActiveState'] in ('active','inactive','activating','deactivating','failed')
      and value['SubState'] in ('dead','start','exited','stop','stop-sigterm','stop-sigkill','failed')
      and (value['InvocationID']=='' or re.fullmatch('[a-f0-9]{32}',value['InvocationID']))
      and value['Result'] in ('success','exit-code','signal','timeout','resources','oom-kill','protocol','start-limit-hit')
      and value['ExecMainStatus'].isdigit() and value['MainPID'].isdigit() and value['ExecMainPID'].isdigit(),'unit_public_state')
    return value
def read_kernel(path,maximum=65536):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        out=bytearray()
        while len(out)<=maximum:
            block=os.read(fd,min(65536,maximum+1-len(out)))
            if not block:break
            out.extend(block)
        c.require(0<len(out)<=maximum,'kernel_read_bound');return bytes(out)
    finally:os.close(fd)
def status(raw):
    got={}
    for row in raw.decode('ascii').splitlines():
        key,_,v=row.partition(':')
        if key in ('Uid','Gid'):got[key.lower()]=[int(x) for x in v.split()]
        elif key in ('CapEff','CapPrm','CapBnd','CapInh','CapAmb'):got[key]=int(v.strip(),16)
        elif key=='NoNewPrivs':got[key]=int(v.strip())
    return got
def identity(pid,mask):
    c.require(type(pid) is int and 1<pid<2**31,'owned_pid')
    root='/proc/'+str(pid)
    tick=lambda:int(read_kernel(root+'/stat').decode().rpartition(')')[2].split()[19])
    before=tick();creds=status(read_kernel(root+'/status'))
    c.require(c.exact(creds,{'uid':[0]*4,'gid':[0]*4,'CapEff':mask,'CapPrm':mask,'CapBnd':mask,'CapInh':0,'CapAmb':0,'NoNewPrivs':1}), 'actual_capability_ceiling')
    ns=os.readlink(root+'/ns/mnt');c.require(re.fullmatch(r'mnt:\[[0-9]+\]',ns) and tick()==before,'owned_pid_changed')
    return {'pid':pid,'startTicks':before,'credentials':creds,'mountNamespace':ns}
def resources(unit,pid):
    c.require(unit in (c.ROOT_UNIT,c.CHILD_UNIT),'own_cgroup_only');root='/sys/fs/cgroup/system.slice/'+unit
    def read(name):return read_kernel(root+'/'+name,4096).decode('ascii').strip()
    members=[int(x) for x in read('cgroup.procs').split()];c.require(pid in members and len(members)<=32,'own_cgroup_membership')
    out={k:int(read(k)) for k in ('memory.max','memory.peak','memory.swap.max','memory.swap.current','memory.swap.peak','pids.max','pids.current')}
    for k in ('memory.events','pids.events'):out[k]={a:int(b) for a,b in (row.split() for row in read(k).splitlines())}
    c.require(out['memory.max']==67108864 and 0<out['memory.peak']<=67108864 and out['pids.max']==32
      and 0<out['pids.current']<=32 and all(out[k]==0 for k in ('memory.swap.max','memory.swap.current','memory.swap.peak')),'actual_resource_limits')
    for key,fields in (('memory.events',('max','oom','oom_kill')),('pids.events',('max',))):
        c.require(all(type(v) is int and v>=0 for v in out[key].values()) and all(out[key].get(k)==0 for k in fields),'actual_resource_events')
    return out
class Port:
    def __init__(self,tree,deadline,source_pins,child_source_sha):
        self.tree,self.deadline=tree,deadline;self.source_pins=source_pins;self.child_source_sha=child_source_sha
        c.require(c.digest(child_source_sha) and type(source_pins) is dict and source_pins,'exact_periodic_source_pins')
        self.started=False;self.invocation=None;self.old_invocation=None;self.context=None;self.records={};self.commands=[]
        for path,pin in source_pins.items():
            c.require(path in units.files() or path.startswith(c.CODE+'/') and path.count('/')==c.CODE.count('/')+1,'source_path_scope')
            raw,fp=tree.read(path,mode=0o644 if path in units.files() else 0o600,maximum=4194304)
            c.require(set(pin)=={'sha256','identity'} and c.sha(raw)==pin['sha256'] and c.exact(list(fp),pin['identity']),'periodic_source_identity')
            self.records[path]=(raw,fp)
    def recheck(self):
        for path,pair in self.records.items():
            c.left(self.deadline);c.require(self.tree.read(path,mode=0o644 if path in units.files() else 0o600,maximum=4194304)==pair,'periodic_source_changed')
    def command(self,argv,cleanup=False):
        c.require(argv in (show_argv(c.ROOT_UNIT),show_argv(c.CHILD_UNIT)) or argv==['/usr/bin/systemctl','start','--no-block',c.CHILD_UNIT]
          or cleanup and argv==['/usr/bin/systemctl','stop',c.CHILD_UNIT],'fixed_read_and_own_child_only')
        # All captures can need a bounded two-second kill confirmation; reserve
        # it inside the original application budget, not after its deadline.
        available=c.left(self.deadline,not cleanup)-2
        if available<=0:raise c.Deadline()
        got=bc.bounded_capture(argv,b'',timeout=min(3,available),env=ENV,limit=65536);self.commands.append(bc.capture_metadata(got))
        c.require(got['returnCode']==0 and got['termination']=='completed' and got['cleanupConfirmed'] is True and not got['stderr'],'unit_command_unknown')
        return got['stdout']
    def show(self,unit,cleanup=False):return validate_loaded(parse(self.command(show_argv(unit),cleanup)),unit==c.CHILD_UNIT)
    def root_identity(self,invocation):
        self.recheck();x=self.show(c.ROOT_UNIT);pid=os.getpid()
        c.require(x['InvocationID']==invocation and x['ControlGroup']=='/system.slice/'+c.ROOT_UNIT and x['MainPID']==x['ExecMainPID']==str(pid)
          and x['ActiveState']=='activating' and x['SubState']=='start','own_root_invocation')
        return {'identity':identity(pid,c.MASK),'cgroup':resources(c.ROOT_UNIT,pid)}
    def inactive_child(self):
        x=self.show(c.CHILD_UNIT);c.require(x['ActiveState']=='inactive' and x['SubState']=='dead' and x['MainPID']=='0','previous_child_not_inactive')
        self.old_invocation=x['InvocationID']
    def parent_namespace(self):return os.readlink('/proc/self/ns/mnt')
    def prepare(self,context,store):
        self.recheck();self.context=context;store.publish(c.CONTEXT,context)
    def own(self,x,completed=False):
        c.require(x['InvocationID'] and x['InvocationID']==self.invocation,'owned_child_invocation')
        if completed:
            pid=getattr(self,'child_pid',None)
            c.require(type(pid) is int and pid>1 and x['ControlGroup'] in ('','/system.slice/'+c.CHILD_UNIT)
              and x['MainPID']=='0' and x['ExecMainPID']==str(pid) and '; pid='+str(pid)+' ;' in x['ExecStart']
              and x['ActiveState']=='active' and x['SubState']=='exited'
              and x['Result']=='success' and x['ExecMainStatus']=='0','owned_completed_child')
        else:c.require(x['ControlGroup']=='/system.slice/'+c.CHILD_UNIT,'owned_child_invocation')
    def start(self,context):
        c.require(context is self.context,'same_context');self.recheck();self.started=True
        self.command(['/usr/bin/systemctl','start','--no-block',c.CHILD_UNIT]);x=self.show(c.CHILD_UNIT)
        c.require(x['InvocationID'] and x['InvocationID']!=self.old_invocation,'fresh_child_invocation');self.invocation=x['InvocationID'];self.own(x)
    def await_ready(self,ctx,store):
        while True:
            c.left(self.deadline,True);failure=child.current_channel(store.read(c.FAILURE,missing=True),ctx,'failure')
            c.require(failure is None,'owned_child_failure')
            ready=child.current_channel(store.read(c.READY,missing=True),ctx,'ready')
            if ready is not None:return c.ready(ready,ctx)
            x=self.show(c.CHILD_UNIT);self.own(x);c.require(x['ActiveState']=='activating' and x['SubState']=='start','child_not_ready');time.sleep(.02)
    def observe_live(self,ready,ctx):
        self.recheck();x=self.show(c.CHILD_UNIT);self.own(x);r=ready['child'];pid=r['pid']
        c.require(x['ActiveState']=='activating' and x['SubState']=='start' and x['MainPID']==x['ExecMainPID']==str(pid),'child_not_live')
        self.child_pid=pid
        actual=identity(pid,4);c.require(c.exact(actual,{k:r[k] for k in ('pid','startTicks','credentials','mountNamespace')}),'child_kernel_changed')
        return {'identity':actual,'cgroup':resources(c.CHILD_UNIT,pid)}
    def complete(self,ready,ctx,store):
        r=ready['child'];store.publish(c.RELEASE,c.envelope(ctx,'release',{'pid':r['pid'],'startTicks':r['startTicks'],'readySha256':c.sha(c.encoded(ready))}))
        while True:
            c.left(self.deadline,True);x=self.show(c.CHILD_UNIT)
            if x['ActiveState']=='active' and x['SubState']=='exited':self.own(x,completed=True);break
            self.own(x)
            c.require(x['ActiveState']=='activating' and x['SubState']=='start','child_completion_unknown');time.sleep(.02)
        c.require(x['Result']=='success' and x['ExecMainStatus']=='0','child_failed')
        done=child.current_channel(store.read(c.DONE),ctx,'done')
        c.require(c.exact(done,{'contextSha256':c.sha(c.encoded(ctx)),'readySha256':c.sha(c.encoded(ready)),'actualChildCompleted':True}),'child_done_identity')
        self.recheck()
    def cleanup(self):
        c.left(self.deadline)
        if self.started:
            c.require(self.invocation is not None,'unknown_child_do_not_stop');x=self.show(c.CHILD_UNIT,True)
            self.own(x,completed=x['ActiveState']=='active' and x['SubState']=='exited')
            self.command(['/usr/bin/systemctl','stop',c.CHILD_UNIT],True);x=self.show(c.CHILD_UNIT,True)
            c.require(x['ActiveState']=='inactive' and x['SubState']=='dead' and x['MainPID']=='0'
              and x['InvocationID'] in ('',self.invocation),'cleanup_not_confirmed');self.started=False
        return True
