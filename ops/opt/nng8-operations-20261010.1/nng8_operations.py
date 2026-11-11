#!/usr/bin/env python3
"""Fixed nnG8 operations; no client-supplied command, unit or filesystem path."""

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import stat
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


SCHEMA = "nng8.external-operations.v1"
SYSTEMCTL = "/usr/bin/systemctl"
PRIVILEGED_HELPER = "/opt/nng8-operations-20261010.1/privileged_timer.py"
UNIT_PROPERTIES = (
    "Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
    "Persistent", "OnSuccess", "OnFailure", "FragmentPath", "DropInPaths",
    "ExecMainStartTimestamp", "ExecMainExitTimestamp", "InactiveExitTimestamp",
    "ActiveEnterTimestamp", "LastTriggerUSec", "NextElapseUSecRealtime", "ExecMainStatus",
)
CONFIG_PROPERTIES = ("Id", "LoadState", "Persistent", "OnSuccess", "OnFailure")
MAX_BODY = 8192
REVIEWED_UNIT_SHA256 = {
    "astock-cninfo-precollect.timer": "a064c914d4decfe687fc8626b8c5f6596d48c58e506d1aadf254c78f21600076",
    "astock-cninfo-precollect.service": "91a8d468e722c949479056ae371ec1b6fc0bd0c74e832ad1488e3ea062a96d7f",
    "astock-xhs-source.timer": "60d1b487ce7868beee56342c92fe787e37ec9254b777f6c74e7ef15d703ce1fa",
    "nng8-research-digest.timer": "dbe924b471891685facdd6d4b6fc66d20ed1974852f694cc2fe298bd668ad80f",
    "research-chain-alert.timer": "187268f7c45311e8941d16ddead9f88a52e3806e93620d5a63ce6864de9d0255",
    "research-chain-alert-post.timer": "2b7d3f45d6ce8b480b213fd2b3cb954f431dfa46cf54c5f00d414f68a0aabd4a",
    "nng8-dashboard-refresh.timer": "2b186ab9c0de507b2ae63faf1b7e0614893bba28b596d07bdd6c49b9cbb8491b",
    "nng8-dashboard-refresh.service": "e195b5431c475ad3e464d3fdf4735fb6b0e2e5c4c7b82d74358755061023cf12",
    "research-data-health.timer": "b9316a906e0e473a385a75fe467b65e2d3ac763a955fc63572148ef9df176043",
    "research-data-health-alert.timer": "ea9bcc78573d3b528016bd9514c947a1df6d63f681c9c2dbcf0f3e6fa6edc0e0",
}


def task(task_id, project, label, scope, units, timer=None, run_unit=None,
         effect=None, frozen=False, reason=None):
    main_service = None if frozen or task_id == "hermes" else next(
        (name for name in units if name.endswith(".service")), None)
    return dict(task_id=task_id, project=project, label=label, scope=scope,
                units=tuple(units), timer=timer, run_unit=run_unit,
                run_effect=effect, frozen=frozen, readonly_reason=reason,
                main_service=main_service)


# This registry follows the live 2026-10-10 unit inventory. It is code, never
# supplied by HTTP clients. "Resume" only applies to non-persistent timers.
TASKS = {
    t["task_id"]: t for t in (
        task("astock-cninfo", "AStock", "公告证据采集", "user",
             ("astock-cninfo-precollect.timer", "astock-cninfo-precollect.service"),
             "astock-cninfo-precollect.timer", "astock-cninfo-precollect.service",
             "采集当前公告与 PDF 证据；写入采集缓存，不生成晨报或发送通知。"),
        task("astock-report", "AStock", "晨报生成计划", "user",
             ("astock-xhs-source.timer", "astock-xhs-source.service",
              "astock-research-seal.service", "astock-morning-notify.service"),
             "astock-xhs-source.timer", reason="源报告成功会自动封存并发送晨报；本入口不开放手工重跑。"),
        task("research-relay", "Signal Bus", "研究摘要投递计划", "user",
             ("nng8-research-digest.timer", "nng8-research-digest.service"),
             "nng8-research-digest.timer", reason="投递仍由原计划运行；本入口不开放手工发送。"),
        task("research-alert", "Signal Bus", "链路截止前检查计划", "user",
             ("research-chain-alert.timer", "research-chain-alert.service"),
             "research-chain-alert.timer"),
        task("research-alert-post", "Signal Bus", "链路截止后检查计划", "user",
             ("research-chain-alert-post.timer", "research-chain-alert-post.service"),
             "research-chain-alert-post.timer"),
        task("data-health", "Research Data / QLib", "行情与证据健康检查计划", "system",
             ("research-data-health.timer", "research-data-health.service",
              "research-data-health-alert.service"), "research-data-health.timer",
             reason="健康检查结束会触发告警检查；本入口不开放手工重跑。"),
        task("data-alert", "Research Data / QLib", "数据健康告警计划", "system",
             ("research-data-health-alert.timer", "research-data-health-alert.service"),
             "research-data-health-alert.timer"),
        task("dashboard-export", "Dashboard", "报告与状态出口", "system",
             ("nng8-dashboard-refresh.timer", "nng8-dashboard-refresh.service"),
             "nng8-dashboard-refresh.timer", "nng8-dashboard-refresh.service",
             "重新读取服务状态和已有报告，更新脱敏出口；不采集行情、不生成研究、不发送通知。"),
        task("hermes", "Hermes", "Hermes 网关与连接巡检", "user",
             ("hermes-gateway.service", "hermes-telegram-watchdog.timer",
              "hermes-telegram-watchdog.service"),
             reason="连接巡检的 Persistent=yes，可能立即修复连接；网关与巡检仅查看状态。"),
        task("ledger", "Research Ledger", "Ledger 校准与结果", "system",
             ("research-ledger.service", "research-ledger-worker.timer",
              "research-ledger-worker.service", "research-ledger-phase2-import.timer",
              "research-ledger-phase2-import.service"), frozen=True,
             reason="业务冻结保留；不开放运行、恢复生产者或改动校准协议。"),
        task("phase2", "Phase2", "Phase2 研究验证", "system",
             ("research-system-phase2.service", "research-system-phase2-producer.timer",
              "research-system-phase2-producer.service", "research-system-phase2-settler.timer",
              "research-system-phase2-settler.service"), frozen=True,
             reason="业务冻结保留；不开放运行、恢复生产者或改动验证协议。"),
    )
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def unit_time(value):
    """Normalize systemd's C-locale clocks; absent clocks remain unknown."""
    if not isinstance(value, str) or not value or value in {"n/a", "0"}:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"[A-Za-z]{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))? (UTC|CST)", value)
    if not match:
        return None
    try:
        stamp = dt.datetime.fromisoformat(match[1] + ("." + match[2] if match[2] else ""))
    except ValueError:
        return None
    zone = dt.timezone.utc if match[3] == "UTC" else dt.timezone(dt.timedelta(hours=8))
    return stamp.replace(tzinfo=zone).astimezone(dt.timezone.utc).isoformat()


def task_observation(entry, units):
    """Use the registered main execution, never a max over unrelated services."""
    by_name = {unit["Id"]: unit for unit in units}
    service = by_name.get(entry["main_service"], {})
    timer = by_name.get(entry["timer"], {})
    started = unit_time(service.get("ExecMainStartTimestamp"))
    finished = unit_time(service.get("ExecMainExitTimestamp"))
    scheduled = unit_time(timer.get("NextElapseUSecRealtime")) if timer.get("ActiveState") == "active" else None
    raw_result = service.get("Result") if finished else None
    result = ("success" if raw_result == "success" else "failed" if raw_result else "unknown")
    sections = {"astock-report": ("reports", "nng8/astock"),
                "research-relay": ("reports", "nng8/astock"),
                "data-health": ("quality", "nng8"), "data-alert": ("quality", "nng8"),
                "ledger": ("validation", "nng8/ledger"), "phase2": ("validation", "nng8/phase2")}
    route = sections.get(entry["task_id"])
    return {"main_service": entry["main_service"], "main_timer": entry["timer"],
            "last_run_at": started, "last_finished_at": finished, "next_run_at": scheduled,
            "last_result_status": result, "last_result_code": raw_result,
            "time_semantics": {"last_run_at": "main_service_execution_started",
                               "last_finished_at": "main_service_execution_finished",
                               "next_run_at": "active_timer_next_elapse"},
            "result_link": {"path": "/personal-reports", "section": route[0], "source": route[1]} if route else None,
            "result_correlation": "source_only" if route else None,
            "blocked_reason": entry["readonly_reason"] if entry["frozen"] else
                "现有证据只证明主服务的进程结果；尚未关联到具体业务产物。"}


class OperationError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


class Systemd:
    """Constrained executor. Command output is never included in audit/API data."""

    @staticmethod
    def command(scope, args, timeout=20, capture=False):
        prefix = [SYSTEMCTL]
        if scope == "user":
            prefix.append("--user")
        elif scope == "system" and args[0] != "show":
            prefix = ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", PRIVILEGED_HELPER]
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "HOME": "/home/ubuntu", "XDG_RUNTIME_DIR": "/run/user/1000",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
        process = subprocess.Popen(prefix + list(args), env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            output = process.communicate(timeout=timeout)[0]
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise OperationError("command_timeout", 504) from None
        if process.returncode:
            raise OperationError("command_failed", 503)
        if output is not None and len(output) > 65536:
            raise OperationError("unit_response_too_large", 503)
        return output.decode("utf-8", errors="strict") if output is not None else ""

    def inspect(self, entry):
        args = ["show", "--no-pager"]
        for key in UNIT_PROPERTIES:
            args += ["-p", key]
        args.extend(entry["units"])
        text = self.command(entry["scope"], args, timeout=5, capture=True)
        units = []
        for block in text.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            unit = values.get("Id")
            if unit not in entry["units"]:
                raise OperationError("unexpected_unit_identity", 503)
            # Config bytes are hashed without returning paths or their contents.
            config_sha = []
            paths = [values.get("FragmentPath", "")] + values.get("DropInPaths", "").split()
            for path in paths:
                if not path:
                    continue
                try:
                    config_sha.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
                except OSError:
                    raise OperationError("unit_config_unreadable", 503) from None
            safe = {key: values.get(key, "") for key in UNIT_PROPERTIES
                    if key not in ("FragmentPath", "DropInPaths")}
            safe["config_sha256"] = config_sha
            units.append(safe)
        if {unit["Id"] for unit in units} != set(entry["units"]):
            raise OperationError("incomplete_unit_response", 503)
        return sorted(units, key=lambda unit: unit["Id"])

    def act(self, entry, action):
        if action == "pause":
            self.command(entry["scope"], ["disable", "--now", entry["timer"]])
        elif action == "resume":
            self.command(entry["scope"], ["enable", "--now", entry["timer"]])
        elif action == "run":
            # Waiting systemctl permits a definitive exit result. If this times
            # out, the service may still run and the outcome must be unknown.
            self.command(entry["scope"], ["start", entry["run_unit"]])
        else:
            raise OperationError("action_not_allowed", 422)


def revision(units):
    return hashlib.sha256(canonical(units).encode()).hexdigest()


def actions(entry, units):
    by_name = {unit["Id"]: unit for unit in units}
    offered = []
    timer = by_name.get(entry["timer"])
    if timer and timer.get("LoadState") == "loaded" and timer.get("Persistent") == "no":
        offered.extend((
            dict(id="pause", label="暂停计划", effect="停止未来定时触发；已经在运行的任务继续执行。",
                 confirmation=True, enabled=timer.get("ActiveState") == "active",
                 reason=None if timer.get("ActiveState") == "active" else "计划当前未运行。",
                 starts_execution=False, risk="会暂停后续自动触发。"),
            dict(id="resume", label="恢复计划", effect="恢复原定时间表；不补跑暂停期间的任务。",
                 confirmation=True, enabled=timer.get("ActiveState") == "inactive"
                 and timer.get("UnitFileState") == "disabled",
                 reason=None if timer.get("ActiveState") == "inactive" and timer.get("UnitFileState") == "disabled"
                 else "只有已暂停且未被屏蔽的计划可以恢复。",
                 starts_execution=False, risk="后续原定时刻会继续执行。"),
        ))
    run = by_name.get(entry["run_unit"])
    if run and run.get("LoadState") == "loaded" and not run.get("OnSuccess") and not run.get("OnFailure"):
        offered.append(dict(id="run", label="刷新出口" if entry["task_id"] == "dashboard-export"
                            else "采集公告证据", effect=entry["run_effect"], confirmation=True,
                            enabled=run.get("ActiveState") in ("inactive", "failed"),
                            reason=None if run.get("ActiveState") in ("inactive", "failed") else "任务已经在运行。",
                            starts_execution=True, risk="写入脱敏出口。" if entry["task_id"] == "dashboard-export"
                            else "使用公告来源网络并写入采集缓存。"))
    targets = [entry["timer"], entry["run_unit"]]
    for target in filter(None, targets):
        expected = REVIEWED_UNIT_SHA256.get(target)
        observed = by_name.get(target, {}).get("config_sha256")
        if expected is None or observed != [expected]:
            for action in offered:
                action.update(enabled=False, reason="服务配置与已核验版本不同，请先核对变更。")
            break
    return offered


def config_signature(units):
    return [{key: unit.get(key) for key in CONFIG_PROPERTIES + ("config_sha256",)} for unit in units]


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise RuntimeError("operation state directory must be private and not a symlink")
        self.db = self.root / "operations.sqlite3"
        if self.db.is_symlink():
            raise RuntimeError("operation database must not be a symlink")
        with self.connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, "
                         "idempotency_key TEXT NOT NULL, request_sha TEXT NOT NULL, task_id TEXT NOT NULL, "
                         "action TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                         "before_revision TEXT NOT NULL, after_revision TEXT, error_code TEXT, result TEXT, "
                         "UNIQUE(actor_id,idempotency_key))")
        os.chmod(self.db, 0o600)

    def connection(self):
        conn = sqlite3.connect(self.db, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    @staticmethod
    def export(row):
        if row is None:
            return None
        value = dict(row)
        value.pop("request_sha", None)
        result = json.loads(value["result"]) if value.get("result") else None
        return dict(operation_id=value["id"], idempotency_key=value["idempotency_key"],
                    actor_id=value["actor_id"], task_id=value["task_id"], action=value["action"],
                    status=value["state"], requested_at=value["created_at"], updated_at=value["updated_at"],
                    finished_at=value["updated_at"] if value["state"] in ("succeeded", "failed", "unknown") else None,
                    detail={"error_code": value["error_code"], "result": result},
                    before_revision=value["before_revision"], after_revision=value["after_revision"])

    def by_key(self, actor_id, key):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM operations WHERE actor_id=? AND idempotency_key=?",
                                (actor_id, key)).fetchone()

    def get(self, actor_id, operation_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM operations WHERE actor_id=? AND id=?",
                               (actor_id, operation_id)).fetchone()
        if row is None:
            raise OperationError("operation_not_found", 404)
        return self.export(row)

    def list(self, actor_id):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM operations WHERE actor_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
                                (actor_id,)).fetchall()
        return [self.export(row) for row in rows]

    def insert(self, payload, request_sha):
        operation_id = uuid.uuid4().hex
        timestamp = now()
        with self.connection() as conn:
            conn.execute("INSERT INTO operations(id,actor_id,idempotency_key,request_sha,task_id,action,state,"
                         "created_at,updated_at,before_revision) VALUES(?,?,?,?,?,?,'accepted',?,?,?)",
                         (operation_id, payload["actor_id"], payload["idempotency_key"], request_sha,
                          payload["task_id"], payload["action"], timestamp, timestamp, payload["observed_revision"]))
        return self.get(payload["actor_id"], operation_id)

    def update(self, operation_id, state, error=None, after=None, result=None):
        with self.connection() as conn:
            conn.execute("UPDATE operations SET state=?,updated_at=?,error_code=?,after_revision=?,result=? WHERE id=?",
                         (state, now(), error, after, canonical(result) if result is not None else None, operation_id))

    def recover(self):
        with self.connection() as conn:
            conn.execute("UPDATE operations SET state='unknown',error_code='executor_restarted',updated_at=? "
                         "WHERE state IN ('accepted','running')", (now(),))


class Executor:
    def __init__(self, state_root, systemd=None, registry=None):
        self.store = Store(state_root)
        self.systemd = systemd or Systemd()
        self.registry = registry if registry is not None else TASKS
        self.mutex = threading.Lock()
        self.worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="nng8-operation")
        # A lifetime lock prevents another process from recovery-marking work
        # that this process is still running.
        self.lock_fd = os.open(self.store.root / "executor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise RuntimeError("another executor owns this state directory") from None
        self.store.recover()

    def close(self):
        self.worker.shutdown(wait=True)
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        os.close(self.lock_fd)

    def catalog(self):
        entries = []
        for entry in self.registry.values():
            try:
                units = self.systemd.inspect(entry)
                item = dict(task_id=entry["task_id"], project=entry["project"], label=entry["label"],
                            kind="timer" if entry["timer"] else "service",
                            scope=entry["scope"], state="available", units=units, revision=revision(units),
                            actions=actions(entry, units), frozen=entry["frozen"], readonly_reason=entry["readonly_reason"])
                item.update(task_observation(entry, units))
            except (OperationError, OSError, ValueError):
                item = dict(task_id=entry["task_id"], project=entry["project"], label=entry["label"],
                            kind="timer" if entry["timer"] else "service",
                            scope=entry["scope"], state="unavailable", units=[], revision=None,
                            actions=[], frozen=entry["frozen"], readonly_reason="服务状态暂时不可读取。")
                item.update(task_observation(entry, []))
            entries.append(item)
        return dict(contract_version=SCHEMA, observed_at=now(), tasks=entries)

    @staticmethod
    def validate(payload):
        expected = {"actor_id", "task_id", "action", "idempotency_key", "observed_revision", "confirmed"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise OperationError("invalid_request", 422)
        if type(payload["actor_id"]) is not int or payload["actor_id"] <= 0:
            raise OperationError("invalid_actor", 422)
        for key, pattern in (("idempotency_key", r"[A-Za-z0-9_-]{16,128}"),
                             ("task_id", r"[a-z0-9-]{1,60}"), ("action", r"[a-z]{1,20}"),
                             ("observed_revision", r"[0-9a-f]{64}")):
            if not isinstance(payload[key], str) or re.fullmatch(pattern, payload[key]) is None:
                raise OperationError("invalid_request", 422)
        if payload["confirmed"] is not True:
            raise OperationError("confirmation_required", 422)
        if payload["action"] not in ("pause", "resume", "run"):
            raise OperationError("action_not_allowed", 422)

    def submit(self, payload):
        self.validate(payload)
        request_sha = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.mutex:
            old = self.store.by_key(payload["actor_id"], payload["idempotency_key"])
            if old is not None:
                if not hmac.compare_digest(old["request_sha"], request_sha):
                    raise OperationError("idempotency_conflict", 409)
                return self.store.export(old), False
            entry = self.registry.get(payload["task_id"])
            if entry is None or entry["frozen"]:
                raise OperationError("action_not_allowed", 422)
            units = self.systemd.inspect(entry)
            if not hmac.compare_digest(revision(units), payload["observed_revision"]):
                raise OperationError("state_changed", 409)
            eligible = next((action for action in actions(entry, units)
                             if action["id"] == payload["action"] and action["enabled"]), None)
            if eligible is None:
                raise OperationError("action_not_available", 409)
            # At most one outstanding operation globally; no queue of stale
            # user confirmations is allowed to accumulate.
            with self.store.connection() as conn:
                if conn.execute("SELECT 1 FROM operations WHERE state IN ('accepted','running') LIMIT 1").fetchone():
                    raise OperationError("executor_busy", 409)
            operation = self.store.insert(payload, request_sha)
            self.worker.submit(self._execute, payload, operation["operation_id"], units)
            return operation, True

    def _execute(self, payload, operation_id, before):
        entry = self.registry[payload["task_id"]]
        try:
            current = self.systemd.inspect(entry)
            if revision(current) != payload["observed_revision"]:
                raise OperationError("state_changed", 409)
            self.store.update(operation_id, "running")
            self.systemd.act(entry, payload["action"])
            after = self.systemd.inspect(entry)
            if config_signature(after) != config_signature(before):
                self.store.update(operation_id, "unknown", "unit_config_changed", revision(after))
                return
            by_name = {unit["Id"]: unit for unit in after}
            timer = by_name.get(entry["timer"], {})
            if payload["action"] == "pause":
                correct = timer.get("ActiveState") == "inactive" and timer.get("UnitFileState") == "disabled"
            elif payload["action"] == "resume":
                correct = timer.get("ActiveState") == "active" and timer.get("UnitFileState") == "enabled"
            else:
                run = by_name.get(entry["run_unit"], {})
                correct = run.get("ActiveState") == "inactive" and run.get("Result") == "success"
            if not correct:
                self.store.update(operation_id, "unknown", "postcondition_not_confirmed", revision(after))
                return
            self.store.update(operation_id, "succeeded", after=revision(after),
                              result={"units": after, "effect": payload["action"]})
        except OperationError as error:
            # Command failure may happen after disable/enable partly changed
            # the timer, or after systemd accepted a start. Never claim rollback.
            state = "unknown" if error.code in ("command_timeout", "command_failed") else "failed"
            self.store.update(operation_id, state, error.code)
        except Exception:
            self.store.update(operation_id, "unknown", "executor_error")


def read_token(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("service credential must be a private regular file")
        value = os.read(descriptor, 257).decode("ascii").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) is None:
            raise RuntimeError("invalid service credential")
        return value
    finally:
        os.close(descriptor)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind, executor, token, allowed_source, tls_context=None):
        self.executor, self.token = executor, token
        sources = [allowed_source] if isinstance(allowed_source, str) else list(allowed_source)
        self.allowed_sources = frozenset(ipaddress.ip_address(source) for source in sources)
        super().__init__(bind, Handler)
        if tls_context is not None:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "nnG8Ops/1"

    def log_message(self, *_):
        # No URLs, headers, bodies or command output in request logs.
        return

    def respond(self, status, value):
        data = canonical(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def error(self, error):
        self.respond(error.status, dict(contract_version=SCHEMA,
                                       error={"type": error.code, "retriable": error.status in (503, 504)},
                                       message=error.code))

    def authorize(self):
        try:
            source = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            raise OperationError("forbidden_source", 403) from None
        if source not in self.server.allowed_sources:
            raise OperationError("forbidden_source", 403)
        value = self.headers.get("Authorization", "")
        if not hmac.compare_digest(value, "Bearer " + self.server.token):
            raise OperationError("unauthorized", 401)

    @staticmethod
    def actor(query, allow_key=False):
        allowed = {"actor_id", "idempotency_key"} if allow_key else {"actor_id"}
        if not set(query).issubset(allowed) or "actor_id" not in query or len(query["actor_id"]) != 1:
            raise OperationError("invalid_actor", 422)
        raw = query["actor_id"][0]
        if re.fullmatch(r"[1-9][0-9]{0,15}", raw) is None:
            raise OperationError("invalid_actor", 422)
        return int(raw)

    def do_GET(self):
        try:
            self.authorize()
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            if url.path == "/v1/catalog" and not query:
                result = self.server.executor.catalog()
            elif url.path == "/v1/operations":
                actor_id = self.actor(query, allow_key=True)
                if "idempotency_key" in query:
                    keys = query["idempotency_key"]
                    if len(keys) != 1 or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", keys[0]) is None:
                        raise OperationError("invalid_request", 422)
                    row = self.server.executor.store.by_key(actor_id, keys[0])
                    if row is None:
                        raise OperationError("operation_not_found", 404)
                    result = dict(contract_version=SCHEMA, operation=self.server.executor.store.export(row))
                else:
                    result = dict(contract_version=SCHEMA, operations=self.server.executor.store.list(actor_id))
            elif re.fullmatch(r"/v1/operations/[0-9a-f]{32}", url.path):
                result = dict(contract_version=SCHEMA, operation=self.server.executor.store.get(self.actor(query), url.path.rsplit("/", 1)[1]))
            else:
                raise OperationError("not_found", 404)
            self.respond(200, result)
        except OperationError as error:
            self.error(error)
        except Exception:
            self.error(OperationError("executor_unavailable", 503))

    def do_POST(self):
        try:
            self.authorize()
            if self.path != "/v1/operations":
                raise OperationError("not_found", 404)
            if self.headers.get("Transfer-Encoding"):
                raise OperationError("invalid_request", 422)
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise OperationError("invalid_content_type", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise OperationError("invalid_request", 422) from None
            if length <= 0 or length > MAX_BODY:
                raise OperationError("request_too_large", 413)
            self.connection.settimeout(5)
            data = self.rfile.read(length)
            if len(data) != length:
                raise OperationError("incomplete_request", 422)
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise OperationError("invalid_json", 422) from None
            operation, created = self.server.executor.submit(payload)
            self.respond(202 if created else 200, dict(contract_version=SCHEMA, operation=operation, replayed=not created))
        except OperationError as error:
            self.error(error)
        except Exception:
            self.error(OperationError("executor_unavailable", 503))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="10.0.0.3")
    parser.add_argument("--port", type=int, default=18771)
    parser.add_argument("--allow-source", action="append", choices=("10.0.0.11", "10.0.0.3"))
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    token = read_token(args.token_file)
    executor = Executor(args.state_root)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(args.tls_cert, args.tls_key)
    server = Server((args.bind, args.port), executor, token, args.allow_source or ["10.0.0.11"], tls_context)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        executor.close()


if __name__ == "__main__":
    main()
