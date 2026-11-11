"""Constrained child: no Docker, foreign /proc, observer, or notifier call.

Only own /proc metadata is read. The immutable original probe_roles function is
called with its explicit binding seam; that local mount check is NOT itself a
fresh container proof. Parent full captures must bracket the whole child.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import platform
import signal
import stat
import sys
import time
import types
import dual_core as c

RUNTIME = '/opt/qd-redacted-log-health-20261013'
SOURCES = {
    'redacted_log_collector': 'a9ec2a0abe79be3f35de943b498b7af3d6fef269c0aa0f69c184ab7a0d0d970c',
    'socket_binding': '0cbcd698c3c0afec4f9fd225e62ed37994496ec5ab8e432a38d0b59640b9c88f',
    'mount_snapshot_guard': '147f402762fc58518be3a983b8f21614b78a2b11e7c302ffa08cd4f924ecae62',
    'root_log_health_bound_master': '6920a4402eb97db4e991b0507f2afc166b1e55af3e56419c30b382d620835c2d',
}
HELD_CHILD_SHA = None
HELD_BOOTSTRAP_BINDING = None


def fp(info):
    return tuple(getattr(info, k) for k in ('st_dev','st_ino','st_uid','st_gid','st_mode',
        'st_nlink','st_size','st_mtime_ns','st_ctime_ns'))


def no_acl(fd, directory=False):
    c.require(callable(getattr(os, 'getxattr', None)), 'acl_api_missing')
    for key in ('system.posix_acl_access','system.posix_acl_default') if directory else ('system.posix_acl_access',):
        try: os.getxattr(fd, key)
        except OSError as exc: c.require(exc.errno == errno.ENODATA, 'acl_unknown')
        else: raise c.Refused('acl_present')


@contextmanager
def parent(path):
    c.require(type(path) is str and path.startswith('/') and not path.startswith('//')
        and str(Path(path)) == path and '..' not in path.split('/'), 'canonical_path_required')
    parts = path.split('/')[1:]; fds = [os.open('/', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)]
    links = []
    def safe(fd):
        info = os.fstat(fd)
        c.require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
            and not info.st_mode & 0o022, 'unsafe_parent')
        no_acl(fd, True); return fp(info)[:5]
    def attest():
        c.require(safe(fds[0]) == fp(os.lstat('/'))[:5], 'root_changed')
        for p, name, fd, identity in links:
            c.require(safe(fd) == identity == fp(os.stat(name, dir_fd=p, follow_symlinks=False))[:5], 'parent_changed')
    try:
        safe(fds[0])
        for name in parts[:-1]:
            fd = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=fds[-1])
            links.append((fds[-1], name, fd, safe(fd))); fds.append(fd)
        attest(); yield parts[-1], fds[-1]; attest()
    finally:
        for fd in reversed(fds): os.close(fd)


def read(path, deadline, maximum=1048576):
    c.left(deadline)
    with parent(path) as (name, p):
        fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK, dir_fd=p)
        try:
            first = os.fstat(fd); no_acl(fd)
            c.require(stat.S_ISREG(first.st_mode) and first.st_uid == first.st_gid == 0
                and stat.S_IMODE(first.st_mode) == 0o600 and first.st_nlink == 1
                and 0 < first.st_size <= maximum, 'private_file_required')
            def consume():
                out = bytearray()
                while len(out) <= maximum:
                    c.left(deadline); chunk = os.read(fd, min(65536, maximum+1-len(out)))
                    if not chunk: break
                    out.extend(chunk)
                c.require(len(out) == first.st_size and fp(os.fstat(fd)) == fp(first), 'file_changed')
                return bytes(out)
            one = consume(); os.lseek(fd, 0, os.SEEK_SET); two = consume(); no_acl(fd)
            c.require(one == two and fp(os.stat(name, dir_fd=p, follow_symlinks=False)) == fp(first), 'file_changed')
            return one, fp(first)
        finally: os.close(fd)


def write_new(path, value, deadline):
    c.left(deadline); c.require(path in (c.READY, c.DONE, c.FAILURE), 'child_fixed_output_only')
    raw = c.encoded(value)
    with parent(path) as (name, p):
        fd = os.open(name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600, dir_fd=p)
        try:
            cursor = memoryview(raw)
            while cursor:
                c.left(deadline); n = os.write(fd, cursor); c.require(n > 0, 'short_write'); cursor = cursor[n:]
            os.fchmod(fd, 0o600); os.fsync(fd); os.fsync(p)
        finally: os.close(fd)
    c.require(read(path, deadline)[0] == raw, 'child_result_readback')
    return c.sha(raw)


def self_read(path, maximum):
    c.require(path in ('/proc/self/status', '/proc/self/stat', '/proc/self/mountinfo'), 'self_proc_only')
    fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        out = bytearray()
        while len(out) <= maximum:
            chunk = os.read(fd, min(65536, maximum+1-len(out)))
            if not chunk: break
            out.extend(chunk)
        c.require(0 < len(out) <= maximum, 'self_proc_bound')
        return bytes(out)
    finally: os.close(fd)


def own_credentials():
    result = {}
    # Literal self paths only, never a container PID from the context.
    raw = self_read('/proc/self/status', 65536)
    for row in raw.decode('ascii').splitlines():
        key, _, value = row.partition(':')
        if key in ('Uid','Gid'): result[key.lower()] = [int(x) for x in value.split()]
        elif key in ('CapEff','CapPrm','CapBnd','CapAmb'): result[key] = int(value.strip(), 16)
        elif key == 'NoNewPrivs': result[key] = int(value.strip())
    return result


def own_ticks():
    raw = self_read('/proc/self/stat', 65536)
    return int(raw.decode('ascii').rpartition(')')[2].split()[19])


def deny(*_a, **_kw): raise c.Refused('observer_notification_or_foreign_query_forbidden')


@contextmanager
def modules(deadline):
    names = ('rollup_log_health_monitor', *SOURCES)
    c.require(all(c.digest(v) for v in SOURCES.values()), 'fixed_source_pins_missing')
    c.require(not any(x in sys.modules for x in names), 'preimported_module_refused')
    loaded, records = {}, {}
    try:
        monitor = types.ModuleType('rollup_log_health_monitor'); monitor.__getattr__ = lambda _x: deny()
        loaded['rollup_log_health_monitor'] = monitor; sys.modules[monitor.__name__] = monitor
        for name, digest in SOURCES.items():
            raw, identity = read(RUNTIME+'/'+name+'.py', deadline)
            c.require(c.sha(raw) == digest, 'frozen_probe_source_changed')
            module = types.ModuleType(name); module.__file__ = RUNTIME+'/'+name+'.py'
            loaded[name] = module; sys.modules[name] = module
            exec(compile(raw, module.__file__, 'exec'), module.__dict__)
            records[name] = (raw, identity)
        yield loaded['root_log_health_bound_master'], loaded['socket_binding']
        for name, pair in records.items():
            c.require(read(RUNTIME+'/'+name+'.py', deadline) == pair, 'frozen_source_changed_during_child')
    finally:
        for name, module in reversed(tuple(loaded.items())):
            if sys.modules.get(name) is module: del sys.modules[name]


def local_binding(context, probe, socket_binding):
    """Local manager mount only; parent supplies current runtime freshness."""
    c.validate_context(context)
    info = Path(c.RUN+'/bound').lstat()
    c.require(stat.S_ISDIR(info.st_mode) and info.st_uid == info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == 0o700, 'private_bind_directory')
    raw = self_read('/proc/self/mountinfo', 1048576)
    c.require(0 < len(raw) <= 1048576
        and probe.readonly_self_mount(raw.decode('utf-8'), target=c.BOUND), 'self_readonly_mount_required')
    return socket_binding.check_binding(expected=context['expected'], runtime=context['runtime'],
        parent=Path(c.SOURCE).parent.lstat(), source=Path(c.SOURCE).lstat(), mounted=Path(c.BOUND).lstat())


def run(context, *, probes=modules):
    c.validate_context(context); deadline = context['deadlineMonotonic']
    c.left(deadline, effects=True)
    with probes(deadline) as (probe, binding):
        # No role-path rewrite. Both normal canaries reach the existing collectors.
        c.require(probe.ROLES == {'host': ('/run/qd-redacted-log/syslog.sock','/var/lib/qd-redacted-log'),
            'frontend': (c.SOURCE, '/var/lib/qd-redacted-log-frontend')}, 'fixed_role_paths_changed')
        result = probe.probe_roles(context['expected'], binding_probe=lambda expected:
            local_binding(context, probe, binding) if c.exact(expected, context['expected']) else deny())
        c.require(c.exact(result, c.RESULT), 'normal_canary_failed')
        c.left(deadline, effects=True)
        ready = {'schema': 'qd.P.normal-probe.dual-child-ready.v1', 'pid': os.getpid(),
            'startTicks': own_ticks(), 'contextSha256': c.sha(c.encoded(context)), 'result': result,
            'credentials': own_credentials(), 'mountNamespace': os.readlink('/proc/self/ns/mnt'),
            'cycles': 1, 'foreignProcRead': False, 'dockerQueried': False}
        c.validate_ready(ready, context)
        return ready


def bootstrap_bound():
    """Pure memory gate: an unbound direct CLI must not inspect or write state."""
    value = HELD_BOOTSTRAP_BINDING
    return (type(value) is dict
        and set(value) == {'schema', 'coreSha256', 'childSha256'}
        and value['schema'] == 'qd.P.normal-probe.held-entry.v1'
        and c.digest(value['coreSha256']) and c.digest(value['childSha256'])
        and c.digest(getattr(c, 'HELD_SOURCE_SHA', None))
        and c.digest(HELD_CHILD_SHA)
        and value['coreSha256'] == c.HELD_SOURCE_SHA
        and value['childSha256'] == HELD_CHILD_SHA)


def main():
    # Before the try/except: refusal must not reach the failure-file writer,
    # context reader, clock, UID/platform probes, or any other environment IO.
    if not bootstrap_bound():
        return 2
    deadline = time.monotonic()+c.TOTAL
    try:
        c.require(os.geteuid() == os.getegid() == 0 and platform.system() == 'Linux'
            and platform.machine() in ('x86_64','amd64'), 'native_root_required')
        initial, identity = read(c.CONTEXT, deadline, 16384)
        context = c.validate_context(c.strict(initial)); deadline = context['deadlineMonotonic']
        c.require(c.digest(HELD_CHILD_SHA) and HELD_CHILD_SHA == context['childSourceSha256'], 'child_source_context_mismatch')
        c.require(c.left(deadline, effects=True) <= c.TOTAL, 'stale_or_future_context')
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(c.Deadline()))
        signal.setitimer(signal.ITIMER_REAL, c.left(deadline))
        ready = run(context); ready_sha = write_new(c.READY, ready, deadline)
        while c.left(deadline, effects=True) > 0:
            if os.path.lexists(c.RELEASE): break
            time.sleep(.02)
        release, _ = read(c.RELEASE, deadline)
        c.require(c.exact(c.strict(release), {'pid': ready['pid'], 'startTicks': ready['startTicks'],
            'readySha256': ready_sha}), 'release_not_same_child')
        c.require(read(c.CONTEXT, deadline, 16384) == (initial, identity), 'context_changed')
        write_new(c.DONE, {'contextSha256': c.sha(initial), 'readySha256': ready_sha,
            'actualChildCompleted': True}, deadline)
        return 0
    except BaseException as exc:
        try: write_new(c.FAILURE, {'schema': 'qd.P.normal-probe.dual-child-failure.v1',
            'failureClass': c.safe_failure(exc), 'normalProbeVerified': False}, deadline)
        except BaseException: pass
        return 2


if __name__ == '__main__': raise SystemExit(main())
