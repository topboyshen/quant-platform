"""Fixed-stub process tests only. Never starts the live snapshot entry."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit, unquote
from unittest import mock

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))

with tempfile.TemporaryDirectory(prefix="research-fixed-process-") as directory:
    scratch = Path(directory).resolve()
    tempfile.tempdir = directory
    os.environ.update({"RESEARCH_DATA_SNAPSHOT_DB": str(scratch / "snapshot.db"),
                       "RESEARCH_DATA_STATE_DIR": str(scratch / "state"),
                       "RESEARCH_DATA_AKSHARE_STAGE_DIR": str(scratch / "stage")})
    original_open = os.open
    def fd_aware_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and not os.path.isabs(path):
            if sys.platform == "darwin":
                import fcntl
                directory = os.fsdecode(fcntl.fcntl(dir_fd, 50, bytes(1024)).split(b"\0", 1)[0])
            else:
                directory = f"/proc/self/fd/{dir_fd}"
            path = str(Path(directory) / os.fsdecode(path))
            dir_fd = None
        return original_open(path, flags, mode, dir_fd=dir_fd)
    os.open = fd_aware_open
    # Fixed watchdog/worker scripts are the only exec children. Forked collector
    # fixtures inherit this network and file guard and deny helpers themselves.
    allowed = {str(root / "snapshot_watchdog.py"),
               *(str(p) for p in (root / "tests/fixtures").glob("snapshot_*_stub.py"))}
    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "os.system"}:
            raise RuntimeError("fault tests forbid network/system")
        if event == "subprocess.Popen":
            command = args[1]
            if (not isinstance(command, (tuple, list)) or str(command[0]) != sys.executable
                    or not any(str(arg) in allowed for arg in command[1:])):
                raise RuntimeError("fault tests permit only fixed Python worker/watchdog fixtures")
        if event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database.startswith("file:"):
                database = unquote(urlsplit(database).path)
            if not Path(database).resolve().is_relative_to(scratch):
                raise RuntimeError("fault tests forbid production database")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(Path("/home/ubuntu")) and not path.is_relative_to(root) and not path.is_relative_to(Path(sys.prefix).resolve()):
                raise RuntimeError("fault tests forbid production file")
            mode, flags = args[1:3]
            writing = ((isinstance(mode, str) and any(c in mode for c in "wax+")) or
                       (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))))
            if writing and path != Path("/dev/null") and not path.is_relative_to(scratch):
                raise RuntimeError("fault tests only write scratch fixtures")
    sys.addaudithook(guard)
    with mock.patch("syslog.syslog"):
        suite = unittest.defaultTestLoader.discover(str(root / "process_tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"PROCESS_TESTS={result.testsRun} skipped={len(result.skipped)} success={result.wasSuccessful()}")
    success = result.testsRun > 0 and result.wasSuccessful()
    tempfile.tempdir = None
raise SystemExit(0 if success else 1)
