import io
import json
import sys
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

PW = "m2 password"


def snap(now=None, **over):
    now = now or datetime.now(UTC)
    s = {
        "schema": "qp.legacy.v1",
        "collected_at": now.isoformat(),
        "duration_s": 3.2,
        "systemd_system": {
            "ok": True,
            "units": [
                {
                    "name": "stock-radar-swing-eod.service",
                    "load": "loaded",
                    "active": "inactive",
                    "sub": "dead",
                    "description": "EOD",
                    "result": "success",
                    "exit_status": "0",
                    "last_exit": now.isoformat(),
                },
                {
                    "name": "stock-radar-swing-eod.timer",
                    "load": "loaded",
                    "active": "active",
                    "sub": "waiting",
                    "description": "t",
                },
                {
                    "name": "qd-redacted-log-root-health.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "description": "x",
                    "result": "exit-code",
                    "exit_status": "1",
                },
                {
                    "name": "apt-daily.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "result": "exit-code",
                },
            ],
            "timers": [
                {
                    "timer": "stock-radar-swing-eod.timer",
                    "activates": "stock-radar-swing-eod.service",
                    "next": (now + timedelta(hours=2)).isoformat(),
                    "last": (now - timedelta(hours=20)).isoformat(),
                }
            ],
        },
        "systemd_user": {
            "ok": True,
            "units": [
                {
                    "name": "astock-cninfo-precollect.service",
                    "load": "loaded",
                    "active": "inactive",
                    "sub": "dead",
                    "result": "success",
                    "exit_status": "0",
                }
            ],
            "timers": [],
        },
        "containers": [
            {"name": "radar-quantdinger-backend", "state": "running", "health": "healthy"},
            {"name": "radar-mihomo", "state": "exited", "health": None},
            {"name": "radar-grafana", "state": "exited", "health": None},
            {"name": "radar-legacy-check", "state": "running", "health": "unhealthy"},
        ],
        "ports": [{"addr": "127.0.0.1", "port": 18800}],
        "certs": [
            {"target": "443", "ok": True, "hours_left": 30.0, "not_after": "x"},
            {"target": "8443", "ok": True, "hours_left": 88.0},
        ],
        "egress": [
            {"target": "telegram_api", "via_proxy": True, "ok": True, "ms": 900},
            {"target": "gstatic_204", "via_proxy": True, "ok": False, "error": "URLError"},
            {"target": "telegram_direct", "via_proxy": False, "ok": False},
        ],
        "logs": {
            "system": {"ok": True, "window_min": 15, "counts": {"ssh.service": 3, "noisy.service": 80}, "total": 83},
            "user": {"ok": True, "window_min": 15, "counts": {}, "total": 0},
        },
        "host": {"disk_root": {"free_gb": 11.2}, "mem_mb": {"MemAvailable": 2000}},
    }
    s.update(over)
    return s


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    f = tmp_path / "legacy.json"
    import qp.jobs.inventory as inv

    monkeypatch.setattr(inv, "INBOX", f)
    return f


def test_categorize():
    from qp.jobs.inventory import categorize

    assert categorize("apt-daily.service") == "os"
    assert categorize("astock-cninfo-precollect.service") == "morning_chain"
    assert categorize("stock-radar-swing-eod.timer") == "radar"
    assert categorize("research-ledger.service") == "research"
    assert categorize("qp-api.service") == "qp"


def test_collector_helpers():
    from qp.collect.legacy import _unix_ts, us_to_iso

    assert us_to_iso(1794376800969723).startswith("2026-11-1")
    assert us_to_iso(0) is None and _unix_ts("@1794376800").startswith("2026") and _unix_ts("") is None


def test_inventory_alert_lifecycle(engine, inbox):
    from qp.jobs import run_job
    from qp.models.notify import Outbox
    from qp.models.ops import Alert, Inventory, LegacyUnit

    inbox.write_text(json.dumps(snap()))
    r = run_job(engine, "ops.inventory", trigger="test")
    assert r.status == "succeeded", r.error
    with Session(engine) as s:
        keys = {a.key: a.severity for a in s.execute(select(Alert).where(Alert.state == "firing")).scalars()}
        assert "unit:system:qd-redacted-log-root-health.service" in keys
        assert "unit:system:apt-daily.service" not in keys  # OS units ignored
        assert "container:radar-mihomo" not in keys  # retired
        assert "container:radar-grafana" not in keys  # retired
        assert keys["container:radar-legacy-check"] == "warning"
        assert keys["cert:443"] == "warning" and "cert:8443" not in keys
        assert "egress:gstatic_204" in keys and "egress:telegram_direct" not in keys
        assert "logs:system:noisy.service" in keys and "logs:system:ssh.service" not in keys
        eod = s.get(LegacyUnit, ("system", "stock-radar-swing-eod.service"))
        assert eod.triggered_by == "stock-radar-swing-eod.timer" and eod.category == "radar"
        assert s.get(LegacyUnit, ("user", "astock-cninfo-precollect.service")).category == "morning_chain"
        assert s.get(Inventory, "dash.coverage") is None
        n_out = len(s.execute(select(Outbox).where(Outbox.dedup_key.like("alert:%"))).scalars().all())
        assert n_out == len(keys) and all(o.mode == "shadow" for o in s.execute(select(Outbox)).scalars())
    # second run: same state -> no new outbox rows; then fix grafana -> resolved
    run_job(engine, "ops.inventory", trigger="test")
    fixed = snap()
    next(c for c in fixed["containers"] if c["name"] == "radar-legacy-check")["health"] = "healthy"
    inbox.write_text(json.dumps(fixed))
    run_job(engine, "ops.inventory", trigger="test")
    with Session(engine) as s:
        assert len(s.execute(select(Outbox).where(Outbox.dedup_key.like("alert:%"))).scalars().all()) == n_out
        g = s.execute(select(Alert).where(Alert.key == "container:radar-legacy-check")).scalar_one()
        assert g.state == "resolved" and g.resolved_at is not None
    sent = run_job(engine, "notify.send", trigger="test")
    assert sent.result["sent"] == 0


def test_inventory_stale_and_missing(engine, inbox):
    from qp.jobs import run_job
    from qp.models.ops import Alert

    assert run_job(engine, "ops.inventory").status == "failed"  # no file yet
    old = datetime.now(UTC) - timedelta(hours=2)
    inbox.write_text(json.dumps(snap(now=old)))
    assert run_job(engine, "ops.inventory").status == "succeeded"
    with Session(engine) as s:
        assert s.execute(select(Alert).where(Alert.key == "collector:stale", Alert.state == "firing")).first()


def _seed_alerts(engine, keys):
    from qp.models.ops import Alert

    with Session(engine) as s:
        s.execute(delete(Alert).where(Alert.key.in_(keys)))
        for key in keys:
            s.add(
                Alert(key=key, severity="warning", state="firing", summary="old fault", last_seen_at=datetime.now(UTC))
            )
        s.commit()


def _alert_states(engine, keys):
    from qp.models.ops import Alert

    with Session(engine) as s:
        return {a.key: a.state for a in s.execute(select(Alert).where(Alert.key.in_(keys))).scalars()}


def test_inventory_missing_collection_keeps_old_alerts(engine, inbox):
    """A failed unit collector or a missing snapshot section is not recovery evidence."""
    from qp.jobs import run_job

    keys = ["unit:system:qd-redacted-log-root-health.service", "container:radar-grafana", "cert:443"]
    _seed_alerts(engine, keys)
    data = snap()
    data["systemd_system"] = {"ok": False, "units": [], "timers": []}
    data["containers"] = []
    data["certs"] = []
    inbox.write_text(json.dumps(data))
    r = run_job(engine, "ops.inventory", trigger="test")
    assert r.status == "succeeded", r.error
    assert _alert_states(engine, keys) == {k: "firing" for k in keys}


def test_inventory_healthy_evidence_resolves_old_alerts(engine, inbox):
    """Only positive healthy observations close the matching old alerts."""
    from qp.jobs import run_job

    keys = ["unit:system:stock-radar-swing-eod.service", "cert:8443", "egress:telegram_api", "host:disk"]
    _seed_alerts(engine, keys)
    inbox.write_text(json.dumps(snap()))
    r = run_job(engine, "ops.inventory", trigger="test")
    assert r.status == "succeeded", r.error
    assert _alert_states(engine, keys) == {k: "resolved" for k in keys}


def test_inventory_absent_records_keep_old_alerts(engine, inbox):
    """A record missing from an otherwise healthy snapshot stays unknown, not resolved."""
    from qp.jobs import run_job

    keys = ["unit:system:removed.service", "container:ghost"]
    _seed_alerts(engine, keys)
    inbox.write_text(json.dumps(snap()))
    r = run_job(engine, "ops.inventory", trigger="test")
    assert r.status == "succeeded", r.error
    assert _alert_states(engine, keys) == {k: "firing" for k in keys}


@pytest.fixture()
def authed(engine, monkeypatch):
    from fastapi.testclient import TestClient

    from qp.api.main import app
    from qp.cli import main

    b = bcrypt.hashpw(PW.encode(), bcrypt.gensalt(rounds=4)).decode()
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"admin\t{b}\n"))
    main(["user", "import-legacy", "--stdin", "--force"])
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    c.cookies.set("qp_session", r.cookies.get("qp_session"))
    return c


def test_m2_api(authed, engine, inbox):
    from qp.jobs import run_job

    inbox.write_text(json.dumps(snap()))
    run_job(engine, "ops.inventory")
    u = authed.get("/api/ops/units").json()
    assert all(x["category"] != "os" for x in u) and any(x["name"] == "stock-radar-swing-eod.timer" for x in u)
    assert any(x["category"] == "os" for x in authed.get("/api/ops/units?include_os=true").json())
    inv = authed.get("/api/ops/inventory").json()
    assert inv["certs"]["data"][0]["target"] == "443" and inv["pg"]["data"]["connections"] >= 1
    by_container = {c["name"]: c for c in inv["containers"]["data"]}
    assert by_container["radar-grafana"]["retired"] is True
    assert by_container["radar-quantdinger-backend"]["retired"] is True
    assert by_container["radar-legacy-check"]["retired"] is False
    assert len(authed.get("/api/ops/alerts").json()) >= 1
    svc = authed.get("/api/dash/services").json()
    assert "unit_states" in svc
    att = {u["name"]: u for u in svc["attention"]}
    assert att["qd-redacted-log-root-health.service"]["active_state"] == "failed"
    assert "apt-daily.service" not in att  # os units are excluded from the page
    by_name = {c["name"]: c for c in svc["containers"]["data"]}
    assert by_name["radar-grafana"]["retired"] is True
    assert by_name["radar-legacy-check"]["retired"] is False
    cov = authed.get("/api/dash/coverage").json()
    assert {"qlib", "snapshot", "compare", "index_close", "calendar", "recent", "issues"} <= set(cov)
    assert isinstance(cov["recent"], list) and isinstance(cov["issues"], list)
    # the qlib card needs the update status/stage so an interrupted nightly is visible
    assert {"status", "planned_date", "failure_stage", "update_at"} <= set(cov["qlib"])


def test_log_counts_skip_ssh_noise():
    import json as _json

    from qp.collect.legacy import count_log_lines

    def line(unit, msg):
        return _json.dumps({"_SYSTEMD_UNIT": unit, "MESSAGE": msg})

    rows = [
        line("ssh.service", "error: kex_exchange_identification: read: Connection reset by peer"),
        line("ssh.service", "Connection closed by authenticating user root 1.2.3.4 port 5 [preauth]"),
        line("ssh.service", "error: Bind to port 22 on 0.0.0.0 failed: Address already in use."),
        line("nginx.service", "Connection reset by peer"),
    ]
    counts = count_log_lines(rows)
    assert counts["ssh.service"] == 1  # only the real failure
    assert counts["nginx.service"] == 1  # filter is scoped to ssh units


def test_ops_jobs_and_health(authed, engine, inbox):
    from qp.jobs import run_job
    from qp.models.ops import JobRun

    inbox.write_text(json.dumps(snap()))
    run_job(engine, "ops.inventory")
    with Session(engine) as s:
        s.add(JobRun(job_name="ops.inventory", status="skipped_locked", trigger="manual", started_at=datetime.now(UTC)))
        s.commit()
    jobs = {j["name"]: j for j in authed.get("/api/ops/jobs").json()}
    # a no-op attempt must not mask the real outcome
    assert jobs["ops.inventory"]["last_status"] == "succeeded"

    h = authed.get("/api/ops/health").json()
    names = [c["check"] for c in h["checks"]]
    assert len(names) == len(set(names)) and "inv:host.disk_free_gb" in names
    assert all(c["at"] for c in h["checks"])
    assert {"pending", "shadowed", "sent", "failed", "mode", "signals_mode"} <= set(h["outbox"])


def test_tokens(authed):
    from fastapi.testclient import TestClient

    from qp.api.main import app

    r = authed.post("/api/ops/tokens", json={"name": "probe-m2", "scopes": ["ops:read"]})
    assert r.status_code == 200 and r.json()["token"].startswith("qpst_") and isinstance(r.json()["id"], int)
    tok = r.json()["token"]
    assert authed.post("/api/ops/tokens", json={"name": "probe-m2"}).status_code == 409
    assert authed.post("/api/ops/tokens", json={"name": "bad", "scopes": ["root"]}).status_code == 422
    lst = authed.get("/api/ops/tokens").json()
    assert all("token" not in t and "token_sha256" not in t for t in lst)
    svc = TestClient(app, base_url="https://testserver")
    h = {"authorization": f"Bearer {tok}"}
    assert svc.get("/api/ops/units", headers=h).status_code == 200
    assert svc.get("/api/ops/tokens", headers=h).status_code == 403  # tokens can't manage tokens
    # scopes are enforced: ops:read does not grant other groups
    for p in ("/api/market/instruments", "/api/research/backtests", "/api/signals/runs", "/api/reports"):
        assert svc.get(p, headers=h).status_code == 403, p
    assert svc.get("/api/settings", headers=h).status_code == 200  # settings is ops:read
    r2 = authed.post("/api/ops/tokens", json={"name": "probe-market", "scopes": ["market:read"]})
    h2 = {"authorization": f"Bearer {r2.json()['token']}"}
    assert svc.get("/api/market/instruments", headers=h2).status_code == 200
    assert svc.get("/api/ops/units", headers=h2).status_code == 403
    tid = next(t["id"] for t in lst if t["name"] == "probe-m2")
    assert authed.delete(f"/api/ops/tokens/{tid}").status_code == 200
    assert svc.get("/api/ops/units", headers=h).status_code == 401


def test_m2_unauth(client):
    for p in (
        "/api/ops/units",
        "/api/ops/inventory",
        "/api/ops/alerts",
        "/api/dash/services",
        "/api/dash/coverage",
        "/api/ops/tokens",
    ):
        assert client.get(p).status_code == 401
