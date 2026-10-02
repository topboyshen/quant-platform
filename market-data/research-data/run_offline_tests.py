"""Synthetic tests: audit-deny network/processes, production data, external writes."""
import os
from pathlib import Path
import sys
import tempfile

try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    raise SystemExit(
        "run_offline_tests.py 需要 research-data 自带的依赖。"
        "请使用: ./.venv/bin/python run_offline_tests.py"
    )
import unittest
from unittest import mock
from urllib.parse import urlsplit, unquote

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))

with tempfile.TemporaryDirectory(prefix="source-repair-tests-") as directory:
    scratch = Path(directory).resolve()
    tempfile.tempdir = directory
    os.environ.update({"RESEARCH_DATA_SNAPSHOT_DB": str(scratch / "snapshot.db"),
                       "RESEARCH_DATA_STATE_DIR": str(scratch / "state"),
                       "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(scratch / "stage")})
    dependency = Path(sys.prefix).resolve()
    # Python's open audit event omits dir_fd. Resolve fd-relative operations via
    # the same pinned Linux descriptor so the audit sees the actual target.
    original_os_open = os.open
    def fd_aware_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and not os.path.isabs(path):
            if sys.platform == "darwin":
                import fcntl
                directory = os.fsdecode(fcntl.fcntl(dir_fd, 50, bytes(1024)).split(b"\0", 1)[0])
            else:
                directory = f"/proc/self/fd/{dir_fd}"
            path = str(Path(directory) / os.fsdecode(path))
            dir_fd = None
        return original_os_open(path, flags, mode, dir_fd=dir_fd)
    os.open = fd_aware_open

    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "subprocess.Popen", "os.system"}:
            raise RuntimeError("offline tests forbid network and subprocesses")
        if event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database.startswith("file:"):
                uri = urlsplit(database)
                if uri.netloc:
                    raise RuntimeError("offline tests forbid external database URIs")
                database = unquote(uri.path)
            target = Path(database).resolve()
            if not target.is_relative_to(scratch):
                raise RuntimeError("offline tests forbid production databases")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(args[0])).resolve()
            forbidden = (Path("/home/ubuntu"), Path("/opt/research-ledger"), Path("/opt/research-system-phase2"))
            if (any(target.is_relative_to(p) for p in forbidden)
                    and not target.is_relative_to(dependency) and not target.is_relative_to(root)):
                raise RuntimeError("offline tests forbid production files")
            mode, flags = args[1:3]
            writing = ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                       or (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))))
            if writing and not target.is_relative_to(scratch):
                raise RuntimeError("offline tests only write disposable fixtures")
        if event == "os.mkdir":
            target = Path(os.fsdecode(args[0])).resolve()
            if not target.is_relative_to(scratch):
                raise RuntimeError("offline tests only create disposable fixture directories")
    sys.addaudithook(guard)
    # Optional vendor imports may create their user configuration directory.
    # Route that test-only initialization to the same guarded disposable root;
    # never let import side effects write the actual operator home directory.
    with mock.patch("syslog.syslog"), mock.patch.object(Path, "home", return_value=scratch):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"OFFLINE_TESTS={result.testsRun} success={result.wasSuccessful()}")
    success = result.testsRun > 0 and result.wasSuccessful()
    tempfile.tempdir = None
raise SystemExit(0 if success else 1)
