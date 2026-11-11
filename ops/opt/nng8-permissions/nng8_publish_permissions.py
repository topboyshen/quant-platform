"""Uninstalled Linux four-target publisher; no CLI and no source/business parsing.

The source publisher and root are trusted: finite path checks cannot protect
against a malicious same-UID publisher. Published inodes must remain immutable.
Only the fixed production adapter selects accounts/paths. Publisher injection is
for the isolated permission suite, not an environment or command-line interface.
"""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import grp
import os
from pathlib import Path
import pwd
import re
import stat
import uuid

NAMES = frozenset({"today.json", "progress.json", "delivery.json", "projects.json"})
PUBLIC = Path("/var/lib/nng8-dashboard")
PRIVATE = Path("/var/lib/nng8-dashboard-publish-private")
EXPORT_ACCOUNT = "nng8-qd-export"
TEMP_PATTERN = re.compile(r"\.nng8-publish-[0-9a-f]{32}\.tmp")
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 1, 2, 4, 8, 16, 32


class PublicationError(RuntimeError):
    def __init__(self, code, *, published=False):
        self.code, self.published = code, published
        super().__init__(code)  # Never include paths, body, usernames or exceptions.


class ACL:
    """Linux libacl, fd based; compare numeric entries, not locale/NSS text."""
    def __init__(self):
        self.lib = ctypes.CDLL("libacl.so.1", use_errno=True)
        p, i = ctypes.c_void_p, ctypes.c_int
        signatures = {
            "acl_from_text": ([ctypes.c_char_p], p), "acl_valid": ([p], i),
            "acl_set_fd": ([i, p], i), "acl_get_fd": ([i], p),
            "acl_get_entry": ([p, i, ctypes.POINTER(p)], i),
            "acl_get_tag_type": ([p, ctypes.POINTER(i)], i),
            "acl_get_qualifier": ([p], p),
            "acl_get_permset": ([p, ctypes.POINTER(p)], i),
            "acl_get_perm": ([p, i], i), "acl_free": ([p], i),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result

    def _check(self, result):
        if result == -1:
            raise PublicationError("acl_failure")
        return result

    def set_text(self, fd, text):
        acl = self.lib.acl_from_text(text.encode("ascii"))
        if not acl:
            raise PublicationError("acl_failure")
        try:
            self._check(self.lib.acl_valid(acl))
            self._check(self.lib.acl_set_fd(fd, acl))
        finally:
            self.lib.acl_free(acl)

    def entries(self, fd):
        acl = self.lib.acl_get_fd(fd)
        if not acl:
            raise PublicationError("acl_failure")
        found = {}
        try:
            self._check(self.lib.acl_valid(acl))
            entry = ctypes.c_void_p()
            flag = 0
            while self._check(self.lib.acl_get_entry(acl, flag, ctypes.byref(entry))):
                flag = 1
                tag, perms = ctypes.c_int(), ctypes.c_void_p()
                self._check(self.lib.acl_get_tag_type(entry, ctypes.byref(tag)))
                self._check(self.lib.acl_get_permset(entry, ctypes.byref(perms)))
                qualifier = None
                if tag.value in (USER, GROUP):
                    pointer = self.lib.acl_get_qualifier(entry)
                    if not pointer:
                        raise PublicationError("acl_failure")
                    try:
                        qualifier = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint)).contents.value
                    finally:
                        self.lib.acl_free(pointer)
                key = (tag.value, qualifier)
                if key in found:
                    raise PublicationError("acl_failure")
                found[key] = sum(bit for bit in (1, 2, 4)
                                 if self._check(self.lib.acl_get_perm(perms, bit)))
            return found
        finally:
            self.lib.acl_free(acl)


def file_acl(uid=None):
    row = {(USER_OBJ, None): 6, (GROUP_OBJ, None): 4, (OTHER, None): 0}
    if uid is not None:
        row.update({(USER, uid): 4, (MASK, None): 4})
    return row


def _no_default(fd):
    try:
        os.getxattr(fd, "system.posix_acl_default")
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return
        raise PublicationError("acl_failure") from None
    raise PublicationError("unexpected_default_acl")


def _directory(path, uid):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PublicationError("invalid_directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parts[1:]:
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
            row = os.fstat(fd)
            if row.st_uid not in (0, uid) or row.st_mode & 0o022:
                raise PublicationError("invalid_directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


class Publisher:
    def __init__(self, public, private, export_uid, group_gid, *, acl=None, event=None):
        if type(export_uid) is not int or export_uid <= 0 or export_uid == os.geteuid():
            raise PublicationError("invalid_identity")
        if type(group_gid) is not int or group_gid < 0 or os.geteuid() == 0:
            raise PublicationError("invalid_identity")
        self.public, self.private = Path(public), Path(private)
        self.export_uid, self.group_gid = export_uid, group_gid
        self.acl, self.event = acl or ACL(), event or (lambda *_: None)

    def _directories(self, public, private):
        visible, hidden = os.fstat(public), os.fstat(private)
        if (visible.st_dev != hidden.st_dev or visible.st_ino == hidden.st_ino
                or visible.st_uid != os.geteuid() or hidden.st_uid != os.geteuid()
                or visible.st_gid != self.group_gid or stat.S_IMODE(visible.st_mode) != 0o750
                or stat.S_IMODE(hidden.st_mode) != 0o700):
            raise PublicationError("invalid_directory")
        expected = {(USER_OBJ, None): 7, (USER, self.export_uid): 5,
                    (GROUP_OBJ, None): 5, (MASK, None): 5, (OTHER, None): 0}
        if self.acl.entries(public) != expected or self.acl.entries(private) != {
                (USER_OBJ, None): 7, (GROUP_OBJ, None): 0, (OTHER, None): 0}:
            raise PublicationError("invalid_directory_acl")
        _no_default(public)
        _no_default(private)

    @contextmanager
    def _lock(self, private):
        fd = os.open("publish.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=private)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise PublicationError("invalid_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PublicationError("busy") from None
            yield
        finally:
            os.close(fd)

    def _target(self, directory, name):
        try:
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_gid != self.group_gid
                or stat.S_IMODE(before.st_mode) != 0o640):
            raise PublicationError("invalid_target")
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise PublicationError("target_changed")
            if self.acl.entries(fd) not in (file_acl(), file_acl(self.export_uid)):
                raise PublicationError("unexpected_target_acl")
        finally:
            os.close(fd)

    def _cleanup(self, private):
        # Lock serializes cooperating publishers. Never unlink unknown objects.
        names = os.listdir(private)
        if len(names) > 256:
            raise PublicationError("private_directory_full")
        for name in names:
            if name == "publish.lock":
                continue
            if not TEMP_PATTERN.fullmatch(name):
                raise PublicationError("unknown_private_object")
            info = os.stat(name, dir_fd=private, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise PublicationError("unsafe_private_object")
        for name in names:
            if name != "publish.lock":
                os.unlink(name, dir_fd=private)
        if len(names) > 1:
            os.fsync(private)

    def publish(self, name, body):
        if type(name) is not str or name not in NAMES or type(body) is not bytes:
            raise PublicationError("invalid_target")
        public = private = fd = None
        temporary = None
        renamed = False
        try:
            public = _directory(self.public, os.geteuid())
            private = _directory(self.private, os.geteuid())
            self._directories(public, private)
            with self._lock(private):
                self._target(public, name)
                self._cleanup(private)
                temporary = ".nng8-publish-" + uuid.uuid4().hex + ".tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=private)
                self.event("created", self.private / temporary)
                remaining = memoryview(body)
                while remaining:
                    count = os.write(fd, remaining[:65536])
                    if count <= 0:
                        raise PublicationError("short_write")
                    remaining = remaining[count:]
                os.fchown(fd, -1, self.group_gid)
                os.fchmod(fd, 0o640)  # No chmod/chown after ACL installation.
                self.acl.set_text(fd, "u::rw-,u:%d:r--,g::r--,m::r--,o::---" % self.export_uid)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid() or info.st_gid != self.group_gid
                        or stat.S_IMODE(info.st_mode) != 0o640
                        or self.acl.entries(fd) != file_acl(self.export_uid)):
                    raise PublicationError("acl_verification_failed")
                self.event("permissions_verified", self.private / temporary)
                os.fsync(fd)  # Content AND final owner/mode/ACL before publication.
                self.event("before_rename", self.private / temporary)
                self._target(public, name)
                self._directories(public, private)
                os.replace(temporary, name, src_dir_fd=private, dst_dir_fd=public)
                renamed = True
                self.event("after_rename", self.public / name)
                os.fsync(public)
                os.fsync(private)  # Cross-directory rename: confirm both parents.
                return self.public / name
        except PublicationError as exc:
            if renamed and not exc.published:
                raise PublicationError("publication_uncertain", published=True) from None
            raise
        except OSError:
            raise PublicationError("publication_uncertain" if renamed else "publication_failed",
                                   published=renamed) from None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary and not renamed and private is not None:
                try:
                    os.unlink(temporary, dir_fd=private)
                except OSError:
                    pass  # Next locked call cleans ONLY this helper's private names.
            for opened in (public, private):
                if opened is not None:
                    os.close(opened)


def publish_dashboard(root, relative, body, *, group_gid=None, allow_symlinked_ancestors=False):
    """Fixed adapter called only by the separately reviewed source patch."""
    if Path(root) != PUBLIC or allow_symlinked_ancestors or relative not in NAMES:
        raise PublicationError("invalid_target")
    identity = pwd.getpwnam(EXPORT_ACCOUNT)
    if group_gid is None or group_gid in os.getgrouplist(EXPORT_ACCOUNT, identity.pw_gid):
        raise PublicationError("overbroad_identity")
    return Publisher(PUBLIC, PRIVATE, identity.pw_uid, group_gid).publish(relative, body)
