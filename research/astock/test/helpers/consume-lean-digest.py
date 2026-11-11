"""Synthetic cross-repository relay contract: injected receipt, network forbidden."""
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def deny_network(*args, **kwargs):
    raise AssertionError("OFFLINE_TEST_NETWORK_FORBIDDEN")


socket.socket.connect = deny_network
socket.create_connection = deny_network
consumer_root = Path(os.environ.get(
    "ASTOCK_CONSUMER_TEST_ROOT", str(Path(__file__).resolve().parents[3] / "signal-bus")
)).resolve()
consumer_path = consumer_root / "scripts/research_relay.py"
if not consumer_path.is_file():
    raise FileNotFoundError(
        f"relay contract needs ASTOCK_CONSUMER_TEST_ROOT or sibling signal-bus: {consumer_path}"
    )
spec = importlib.util.spec_from_file_location("isolated_relay", consumer_path)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
root = Path(sys.argv[1])
wire = []


def fake_sender(payload, config, now):
    raw = relay.encode_payload(payload)
    wire.append(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "status": "confirmed", "reason": "accepted", "ack_scope": "http_accepted",
        "receiver_storage": "receiver_reported_stored", "receiver_record_id": "rr_test_lean_digest",
        "payload_sha256": digest, "receiver_content_sha256": digest, "content_match": True,
    }


with tempfile.TemporaryDirectory(prefix="nng8-lean-bus-") as busdir:
    # The relay rejects symlinked artifact roots; macOS /tmp is an alias.
    bus = Path(busdir).resolve()
    (bus / "config").mkdir()
    (bus / "config/pending.env").write_text(
        "export DEDIROCK_DIGEST_URL=https://receiver.invalid/ingest\n"
        "export DEDIROCK_PENDING_SECRET=fixed-synthetic-only\n"
        "export X_SOURCE=nng8\nexport TLS_VERIFY=1\n"
    )
    args = dict(project_root=str(root), bus_root=str(bus),
                now=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc), sender=fake_sender)
    result = relay.run_relay(**args)
    assert result["status"] == "confirmed", result
    assert len(wire) == 1
    wire_hash = hashlib.sha256(wire[0]).hexdigest()
    assert result["payload_sha256"] == result["receiver_content_sha256"] == wire_hash
    retry = relay.run_relay(**args)
    assert retry["status"] == "already_confirmed" and len(wire) == 1
    print(json.dumps({
        "status": result["status"], "senderCalls": len(wire), "retry": retry["status"],
        "wireSha256": wire_hash, "receiptMatchesWire": True, "networkCalls": 0,
    }))
