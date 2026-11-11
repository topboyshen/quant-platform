"""Explicit, CAS-guarded installation of this reviewed Qlib repair only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from atomic import scan_tree, sync_dir

SOURCE = Path(__file__).resolve().parent
RESEARCH = Path("/home/ubuntu/research-data")
INSTALL = Path("/home/ubuntu/qlib/scripts/qlib_dolt_incremental")
CRON_SHA = "db8b709174a3062b6bece44ce60bd88eb4c938c6e25cdcf53651e45d24ebaed2"
OLD_SCRIPT_SHA = "ef24e92483bc983de42dd39dbb5c7b6c8bb9af7eeb1edcd97faee10c66713b6d"
OLD_LINE = "0 22 * * * test -x /opt/ai/research/qlib/scripts/update_qlib_data.sh && /opt/ai/research/qlib/scripts/update_qlib_data.sh"
NEW_LINE = ("0 22 * * * /usr/bin/timeout --signal=TERM --kill-after=30s 25m "
            "/usr/bin/nice -n 10 /usr/bin/ionice -c2 -n7 /usr/bin/python3 -B "
            "/home/ubuntu/qlib/scripts/qlib_dolt_incremental/run.py "
            ">> /home/ubuntu/qlib/logs/update_data.log 2>&1")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError("unsafe_install_file:" + path.name)
    return path.read_bytes()


def replace(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=".qlib-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def consumer():
    baseline = json.loads(read(SOURCE / "consumer-compat/deployment-base.json"))
    head = subprocess.check_output(["git", "-C", str(RESEARCH), "rev-parse", "HEAD"], text=True).strip()
    if any(item["gitHead"] != head for item in baseline):
        raise ValueError("consumer_repository_head_changed")
    expected = {item["file"]: item["sha256"] for item in baseline}
    expected["tests/test_qlib_incremental.py"] = None
    replacements, originals = {}, {}
    for name, before_sha in expected.items():
        path = RESEARCH / name
        replacements[name] = read(SOURCE / "consumer-compat" / name)
        if before_sha is None:
            if path.exists() or path.is_symlink():
                raise ValueError("new_consumer_test_already_exists")
            originals[name] = (None, 0o644)
        else:
            old = read(path)
            if digest(old) != before_sha:
                raise ValueError("consumer_file_changed:" + name)
            originals[name] = (old, stat.S_IMODE(path.stat().st_mode))
    if subprocess.check_output(["git", "-C", str(RESEARCH), "status", "--porcelain"], text=True):
        raise ValueError("consumer_worktree_not_clean")
    written = []
    try:
        for name, data in replacements.items():
            written.append(name)
            replace(RESEARCH / name, data, originals[name][1])
        for pattern in ("test_data_health.py", "test_qlib_incremental.py"):
            subprocess.run(["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-q"],
                           cwd=RESEARCH, check=True)
    except BaseException:
        # In-memory rollback of only this attempted change; no retained backup.
        for name in reversed(written):
            before, mode = originals[name]
            target = RESEARCH / name
            current = read(target) if target.exists() or target.is_symlink() else None
            if current == before:
                continue
            if current != replacements[name]:
                raise ValueError("parallel_consumer_change_requires_manual_review")
            if before is None:
                (RESEARCH / name).unlink()
                sync_dir((RESEARCH / name).parent)
            else:
                replace(RESEARCH / name, before, mode)
        raise
    return {"status": "consumer_installed", "files": {name: digest(read(RESEARCH / name)) for name in replacements}}


def verify_promoted():
    from postcheck import verify
    return verify(Path("/home/ubuntu/qlib/data"), "2026-09-08", RESEARCH / "data_health.py")


def schedule():
    manifest_path = Path("/home/ubuntu/qlib/data/cn_data/.qlib-dolt-incremental.json")
    manifest_bytes = read(manifest_path)
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") != "validated" or manifest.get("targetDate") != "2026-09-08":
        raise ValueError("expected_promoted_generation_missing")
    if read(RESEARCH / "data_health.py") != read(SOURCE / "consumer-compat/data_health.py"):
        raise ValueError("installed_consumer_not_reviewed_version")
    receipt = verify_promoted()
    if (receipt["status"] != "postcheck_passed" or receipt["manifestSha256"] != digest(manifest_bytes)
            or receipt["generationId"] != manifest["generationId"]):
        raise ValueError("promoted_postcheck_disagrees")
    original_script = Path("/home/ubuntu/qlib/scripts/update_qlib_data.sh")
    if digest(read(original_script)) != OLD_SCRIPT_SHA:
        raise ValueError("canonical_updater_changed")
    cron = subprocess.check_output(["crontab", "-l"])
    if digest(cron) != CRON_SHA or cron.decode().splitlines().count(OLD_LINE) != 1:
        raise ValueError("crontab_changed")
    if INSTALL.exists() or INSTALL.is_symlink() or INSTALL.parent.resolve() != INSTALL.parent:
        raise ValueError("install_target_exists_or_unsafe")
    for command in ("/usr/bin/timeout", "/usr/bin/nice", "/usr/bin/ionice", "/usr/bin/python3"):
        if not os.access(command, os.X_OK):
            raise ValueError("scheduled_runtime_missing")
    INSTALL.mkdir(mode=0o755)
    sync_dir(INSTALL.parent)
    for source in sorted(SOURCE.iterdir()):
        if source.is_file() and (source.suffix == ".py" or source.name in ("README.md", "api_contract.md")):
            replace(INSTALL / source.name, read(source), 0o644)
    scan_tree(SOURCE / "consumer-compat")
    shutil.copytree(SOURCE / "consumer-compat", INSTALL / "consumer-compat")
    scan_tree(INSTALL)
    for directory, _, names in os.walk(INSTALL):
        for name in names:
            descriptor = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        sync_dir(Path(directory))
    lines = []
    for line in cron.decode().splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if body == OLD_LINE:
            body = NEW_LINE
        elif body == "# Auto-update qlib CN data daily 22:00 CST (after chenditc release ~20:38)":
            body = "# Qlib same-source validated incremental update daily 22:00 CST; canonical importer retained manually"
        lines.append(body + ending)
    revised = "".join(lines).encode()
    # Final compare-and-set immediately before the sole scheduled-entry change.
    if subprocess.check_output(["crontab", "-l"]) != cron:
        raise ValueError("crontab_changed_before_install")
    if read(manifest_path) != manifest_bytes:
        raise ValueError("promoted_generation_changed_before_schedule")
    subprocess.run(["crontab", "-"], input=revised, check=True)
    if subprocess.check_output(["crontab", "-l"]) != revised:
        raise ValueError("installed_crontab_disagrees")
    return {"status": "single_22h_entry_installed", "path": str(INSTALL),
            "cronSha256": digest(revised), "snapshotEntryUnchanged": True,
            "postcheckManifestSha256": receipt["manifestSha256"],
            "canonicalUpdaterUnchanged": digest(read(original_script)) == OLD_SCRIPT_SHA}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("consumer", "schedule"))
    args = parser.parse_args()
    print(json.dumps(consumer() if args.mode == "consumer" else schedule(), sort_keys=True))
