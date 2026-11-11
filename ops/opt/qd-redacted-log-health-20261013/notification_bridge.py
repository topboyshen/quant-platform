#!/usr/bin/env python3
"""Single-writer Telegram API bridge. No approval, raw logs or retries.

A persisted `sending` record is a write-ahead fence: after any uncertain outcome
the same event can only be queried, never sent again. A returned Message is an
API acknowledgement, NOT proof that a person received/read the notification.
"""
import argparse
import fcntl
import hashlib
import http.client
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import struct
import tempfile
import time

UNITS = ("qd-redacted-log", "qd-redacted-log-health", "qd-review-maintenance")
REASONS = ("collector_socket_unavailable", "collector_canary_unacknowledged",
           "collector_canary_acknowledged", "unit_failed", "probe_failed",
           "maintenance_over_budget", "sender_unreachable", "sender_running",
           "sender_reachable_maintenance_in_progress",
           "observer_state_invalid", "sender_status_invalid", "clock_discontinuity")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,63}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def restricted_read(path, uid, allowed_group=None, maximum=65536):
    """No symlink, bounded bytes, exact owner; a 0640 group must be explicit."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_mode & 0o007 or info.st_mode & 0o131
                or (info.st_mode & 0o040 and info.st_gid != allowed_group)
                or info.st_size > maximum):
            raise ValueError("restricted_file_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("restricted_file_invalid")
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


def secret_value(spec):
    if set(spec) - {"path", "uid", "gid", "format", "key"}:
        raise ValueError("secret_contract_invalid")
    if type(spec.get("uid")) is not int or not Path(spec.get("path", "")).is_absolute():
        raise ValueError("secret_contract_invalid")
    raw = restricted_read(spec["path"], spec["uid"], spec.get("gid"))
    if spec.get("format") == "raw":
        values = raw.splitlines()
        if len(values) != 1 or not values[0] or values[0] != values[0].strip():
            raise ValueError("secret_format_invalid")
        return values[0]
    if spec.get("format") != "env" or not re.fullmatch(r"[A-Z][A-Z0-9_]*", spec.get("key", "")):
        raise ValueError("secret_contract_invalid")
    values = []
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == spec["key"]:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value or any(char in value for char in "\r\n\x00$`"):
                raise ValueError("secret_format_invalid")
            values.append(value)
    if len(values) != 1:
        raise ValueError("secret_key_missing_or_duplicate")
    return values[0]


def load_binding(path, expected_uid=0):
    config = json.loads(restricted_read(path, expected_uid))
    allowed = {"schema", "owner_id", "channel_id", "approved", "allow_network",
               "token", "chat", "expected_bot_id_sha256", "expected_chat_sha256",
               "proxy", "allowed_peer_uids"}
    if set(config) != allowed or config.get("schema") != 1:
        raise ValueError("binding_schema_invalid")
    for key in ("owner_id", "channel_id"):
        if not IDENTIFIER.fullmatch(str(config[key])) or "PLACEHOLDER" in config[key].upper():
            raise ValueError("owner_or_channel_unbound")
    if config["approved"] is not True or config["allow_network"] is not True:
        raise ValueError("notification_send_not_approved")
    if not isinstance(config["allowed_peer_uids"], list) or not config["allowed_peer_uids"] or any(
            type(value) is not int or value < 0 for value in config["allowed_peer_uids"]):
        raise ValueError("peer_identity_unbound")
    token, chat = secret_value(config["token"]), secret_value(config["chat"])
    if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("bot_token_format_invalid")
    if not re.fullmatch(r"-?[0-9]{1,24}", chat):
        raise ValueError("numeric_chat_required")
    for key in ("expected_bot_id_sha256", "expected_chat_sha256"):
        if not DIGEST.fullmatch(str(config[key])):
            raise ValueError("destination_identity_unbound")
    if digest(token.split(":", 1)[0]) != config["expected_bot_id_sha256"] or digest(chat) != config["expected_chat_sha256"]:
        raise ValueError("destination_identity_mismatch")
    proxy = config["proxy"]
    if proxy == {"mode": "direct"}:
        pass  # Do not read proxy environment or system proxy configuration.
    elif (set(proxy) == {"mode", "host", "port"} and proxy["mode"] == "http_connect"
          and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", str(proxy["host"]))
          and type(proxy["port"]) is int and 1 <= proxy["port"] <= 65535):
        pass
    else:
        raise ValueError("proxy_contract_invalid")
    public = {key: config[key] for key in ("owner_id", "channel_id", "expected_bot_id_sha256",
                                          "expected_chat_sha256", "proxy", "allowed_peer_uids")}
    return {"config": config, "token": token, "chat": chat,
            "identity_sha256": digest(json.dumps(public, sort_keys=True, separators=(",", ":")))}


def validate_event(event):
    if not isinstance(event, dict) or set(event) != {"schema", "event_id", "unit", "status", "reason", "observed_unix"}:
        raise ValueError("event_schema_invalid")
    if (event["schema"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", str(event["event_id"]))
            or event["unit"] not in UNITS or event["status"] not in ("firing", "resolved")
            or event["reason"] not in REASONS or type(event["observed_unix"]) not in (int, float)
            or not math.isfinite(event["observed_unix"]) or event["observed_unix"] < 0):
        raise ValueError("event_value_invalid")
    return event


def telegram_request(binding, event):
    """One HTTPS call with mandatory certificate checks; no redirects/retries."""
    config = binding["config"]
    text = ("QuantDinger safety: " + event["status"] + "\n" + event["unit"] + ": " + event["reason"]
            + "\nEvent: " + event["event_id"] + "\nOwner: " + config["owner_id"])
    body = json.dumps({"chat_id": binding["chat"], "text": text}, separators=(",", ":")).encode()
    proxy = config["proxy"]
    context = ssl.create_default_context()
    if proxy["mode"] == "direct":
        connection = http.client.HTTPSConnection("api.telegram.org", 443, timeout=5, context=context)
    else:
        connection = http.client.HTTPSConnection(proxy["host"], proxy["port"], timeout=5, context=context)
        connection.set_tunnel("api.telegram.org", 443)
    try:
        connection.request("POST", "/bot" + binding["token"] + "/sendMessage", body,
                           {"Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            return {"state": "unknown", "reason": "api_reply_unusable"}
        if response.status != 200:
            return {"state": "unknown", "reason": "api_not_acknowledged"}
        reply = json.loads(raw)
        message = reply.get("result") if isinstance(reply, dict) else None
        if (reply.get("ok") is not True or not isinstance(message, dict)
                or type(message.get("message_id")) is not int or message["message_id"] <= 0
                or str(message.get("chat", {}).get("id")) != binding["chat"]):
            return {"state": "unknown", "reason": "api_reply_unusable"}
        return {"state": "accepted", "message_id": message["message_id"], "reason": "telegram_api_accepted"}
    finally:
        connection.close()


def transport_child(pipe, binding, event):
    try:
        os.setsid()
        pipe.send(telegram_request(binding, event))
    except BaseException:
        # Neither exceptions nor response bodies (which may contain the token)
        # are serialized, printed, or logged.
        pipe.send({"state": "unknown", "reason": "transport_unconfirmed"})
    finally:
        pipe.close()


def bounded_transport(binding, event, timeout=6, _child_target=transport_child):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_target, args=(sender, binding, event))
    child.start()
    sender.close()
    try:
        if receiver.poll(timeout):
            return receiver.recv()
        return {"state": "unknown", "reason": "transport_deadline"}
    except Exception:
        return {"state": "unknown", "reason": "transport_unconfirmed"}
    finally:
        receiver.close()
        if child.is_alive():
            # Child has no subprocesses; kill only this process, never a broad
            # name/pattern and never the caller's process group.
            child.kill()
        child.join(2)


def atomic(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".notify-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Bridge:
    def __init__(self, binding, state_dir, transport=bounded_transport):
        self.binding, self.transport = binding, transport
        self.directory = Path(state_dir)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("state_directory_invalid")
        self.lock = os.open(self.directory / "bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock)
            raise

    def close(self):
        os.close(self.lock)

    def handle(self, event):
        event = validate_event(event)
        path = self.directory / (event["event_id"] + ".json")
        body_digest = digest(json.dumps(event, sort_keys=True, separators=(",", ":")))
        if path.exists() or path.is_symlink():
            saved = json.loads(restricted_read(path, os.geteuid(), maximum=8192))
            if saved.get("event_sha256") != body_digest or saved.get("binding_sha256") != self.binding["identity_sha256"]:
                return {"event_id": event["event_id"], "state": "unknown", "reason": "event_identity_conflict"}
            return self.ack(saved)
        saved = {"schema": 1, "event_id": event["event_id"], "event_sha256": body_digest,
                 "binding_sha256": self.binding["identity_sha256"], "state": "sending",
                 "reason": "transport_unconfirmed", "created_unix": time.time()}
        atomic(path, saved)  # Must be durable before the one allowed POST.
        try:
            outcome = self.transport(self.binding, event)
        except Exception:
            outcome = {"state": "unknown", "reason": "transport_unconfirmed"}
        if (outcome.get("state") == "accepted" and type(outcome.get("message_id")) is int
                and outcome["message_id"] > 0):
            saved.update(state="accepted", reason="telegram_api_accepted", message_id=outcome["message_id"])
        else:
            saved.update(state="unknown", reason="transport_unconfirmed")
        saved["completed_unix"] = time.time()
        atomic(path, saved)  # On failure the durable sending fence is retained.
        return self.ack(saved)

    @staticmethod
    def ack(saved):
        if saved.get("state") == "accepted":
            return {"event_id": saved["event_id"], "state": "accepted", "human_read": False,
                    "receipt": "telegram-message:" + str(saved["message_id"]),
                    "reason": "telegram_api_accepted"}
        return {"event_id": saved["event_id"], "state": "unknown", "reason": "manual_reconciliation_required"}


def receive_event(client, timeout=2):
    deadline = time.monotonic() + timeout
    raw = bytearray()
    while b"\n" not in raw and len(raw) <= 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("event_framing_deadline")
        client.settimeout(remaining)
        part = client.recv(4096)
        if not part:
            break
        raw.extend(part)
    if len(raw) > 4096 or b"\n" not in raw:
        raise ValueError("event_frame_invalid")
    return json.loads(raw.split(b"\n", 1)[0])


def serve(config_path, state_dir, socket_path, socket_gid):
    if socket_gid != os.getegid():
        raise ValueError("runtime_and_socket_group_mismatch")
    binding = load_binding(config_path)
    bridge = Bridge(binding, state_dir)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Existing sockets are never unlinked by a daemon that cannot establish
        # that they are its own. RuntimeDirectory is prepared by systemd.
        listener.bind(str(socket_path))
        os.chown(socket_path, 0, socket_gid)
        os.chmod(socket_path, 0o660)
        listener.listen(4)
        while True:
            client, _ = listener.accept()
            with client:
                try:
                    _pid, uid, _gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid not in binding["config"]["allowed_peer_uids"]:
                        continue
                    response = bridge.handle(receive_event(client))
                    client.sendall(json.dumps(response).encode() + b"\n")
                except Exception:
                    # A dropped reply is unknown to the caller; retrying the
                    # same event only queries the durable fenced result.
                    pass
    finally:
        listener.close()
        bridge.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--socket-gid", type=int, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            binding = load_binding(args.config)
            print(json.dumps({"ok": True, "binding_sha256": binding["identity_sha256"], "network_called": False}))
        else:
            serve(args.config, args.state_dir, args.socket, args.socket_gid)
        return 0
    except Exception:
        print('{"ok":false,"reason":"bridge_configuration_or_runtime_failure"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
