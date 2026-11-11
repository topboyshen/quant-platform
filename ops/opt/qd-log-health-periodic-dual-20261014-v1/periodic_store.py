"""Bounded private operational CAS; never removes unknown or one-shot records."""
from contextlib import contextmanager
import fcntl
import os
import stat
import effect_core as e
import periodic_contract as c
import periodic_state as state
CHANNELS={c.CONTEXT,c.READY,c.RELEASE,c.DONE,c.FAILURE}
class Store:
    def __init__(self,tree,deadline):self.tree,self.deadline=tree,deadline;self.lock_fd=None;self.lock_identity=None
    def attest_lock(self):
        c.left(self.deadline);c.require(self.lock_fd is not None,'operational_lock_required')
        self.tree.no_acl(self.lock_fd)
        c.require(e.fp(os.fstat(self.lock_fd))==self.lock_identity==e.fp(self.tree.info(state.LOCK)),'operational_lock_changed')
    @contextmanager
    def held(self):
        c.left(self.deadline);self.tree.directory(state.ROOT);self.tree.directory(c.CONTROL)
        with self.tree.parent(state.LOCK) as (name,parent):
            fd=os.open(name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
            try:
                x=os.fstat(fd);self.tree.no_acl(fd)
                c.require(stat.S_ISREG(x.st_mode) and x.st_uid==x.st_gid==0 and x.st_nlink==1
                  and stat.S_IMODE(x.st_mode)==0o600 and x.st_size==0,'operational_lock_private')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.lock_fd=fd;self.lock_identity=e.fp(x);self.attest_lock()
                yield self
                self.attest_lock()
            finally:
                self.lock_fd=None;os.close(fd)
    def read(self,path,*,missing=False):
        c.left(self.deadline);c.require(path in CHANNELS|{state.STATE,c.CYCLE_PROOF},'operational_fixed_read')
        try:return self.tree.read(path,maximum=c.MAXIMUM)
        except FileNotFoundError:
            if missing:return None
            raise
    def checkpoint(self):
        pair=self.read(state.STATE);return state.strict(pair[0]),pair
    def cas(self,path,expected,raw,*,locked=True):
        c.left(self.deadline)
        c.require(path in CHANNELS|{state.STATE,c.CYCLE_PROOF} and type(raw) is bytes and 0<len(raw)<=c.MAXIMUM,'operational_fixed_cas')
        if locked:self.attest_lock()
        c.require(self.read(path,missing=True)==expected,'operational_prior_changed')
        with self.tree.parent(path) as (name,parent):
            temporary=name+'.next'
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
            try:
                data=memoryview(raw)
                while data:
                    c.left(self.deadline);n=os.write(fd,data);c.require(n>0,'operational_short_write');data=data[n:]
                os.fchmod(fd,0o600);os.fsync(fd);self.tree.no_acl(fd)
                c.require(e.fp(os.fstat(fd))==e.fp(os.stat(temporary,dir_fd=parent,follow_symlinks=False)), 'operational_temporary_changed')
                c.require(self.read(path,missing=True)==expected,'operational_prior_changed')
                if locked:self.attest_lock()
                c.left(self.deadline);os.replace(temporary,name,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
            finally:os.close(fd)
        pair=self.read(path);c.require(pair[0]==raw,'operational_commit_readback');return pair
    def save_checkpoint(self,value,expected):
        state.validate(value);return self.cas(state.STATE,expected,state.encoded(value))
    def save_proof(self,value):
        return self.cas(c.CYCLE_PROOF,self.read(c.CYCLE_PROOF,missing=True),c.encoded(value))
    def publish(self,path,value,*,child=False):
        c.require(path in ({c.READY,c.DONE,c.FAILURE} if child else {c.CONTEXT,c.RELEASE}),'channel_write_scope')
        return self.cas(path,self.read(path,missing=True),c.encoded(value),locked=not child)
    def clean_channels(self):
        # Incomplete atomic writes are preserved and block a new child, never deleted.
        for path in CHANNELS|{state.STATE,c.CYCLE_PROOF}:
            c.left(self.deadline);self.tree.absent(path+'.next')
