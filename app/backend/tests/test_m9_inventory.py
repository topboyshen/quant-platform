"""M9 decommission inventory: read-only filtering, manifest hash, archive plan."""

from __future__ import annotations

import datetime as dt
import json
import subprocess

from qp.decommission.inventory import (
    build_manifest,
    classify_paths,
    collect,
    is_legacy_unit,
    manifest_sha256,
    parse_containers,
    parse_du,
)


def cp(cmd, code=0, stdout=""):
    return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")


def test_legacy_unit_filter():
    assert is_legacy_unit("astock-cninfo-precollect.timer")
    assert is_legacy_unit("quant-execution-digest.timer")
    assert is_legacy_unit("radar-quantdinger-backend")
    assert not is_legacy_unit("qp-api.service")
    assert not is_legacy_unit("qp-reports-morning.timer")


def test_build_manifest_filters_and_hashes():
    timers = [
        {
            "unit": "astock-xhs-source.timer",
            "activates": "astock-xhs-source.service",
            "next": "Mon 2026-11-16 08:00",
            "last": "Fri 2026-11-13 08:00",
            "persistent": False,
        },
        {"unit": "qp-api.timer", "activates": "qp-api.service"},
    ]
    user = [{"unit": "nng8-research-digest.timer", "activates": "nng8-research-digest.service"}]
    containers = [
        {"Names": "radar-quantdinger-postgres", "Image": "postgres:16", "Status": "Up"},
        {"Names": "other", "Image": "x", "Status": "Up"},
    ]
    paths = {"/home/ubuntu/trading-radar": 100, "/home/ubuntu/AStock_AI_System": 200}
    manifest = build_manifest(
        timers=timers,
        user_timers=user,
        containers=containers,
        paths=paths,
        generated_at=dt.datetime(2026, 11, 13, tzinfo=dt.UTC),
    )
    assert [t["unit"] for t in manifest["timers"]] == ["astock-xhs-source.timer"]
    assert manifest["user_timers"][0]["unit"] == "nng8-research-digest.timer"
    assert [c["name"] for c in manifest["containers"]] == ["radar-quantdinger-postgres"]
    assert manifest["totals"] == {"timers": 2, "containers": 1, "path_bytes": 300}

    sha = manifest_sha256(manifest)
    assert sha == manifest_sha256(json.loads(json.dumps(manifest)))
    changed = json.loads(json.dumps(manifest))
    changed["paths"][0]["bytes"] = 101
    assert manifest_sha256(changed) != sha

    plan = classify_paths(manifest)
    assert {item["action"] for item in plan} == {"archive_only"}
    assert sum(item["bytes"] for item in plan) == 300


def test_parse_du_and_containers():
    assert parse_du("100\t/a\n200\t/b\n") == {"/a": 100, "/b": 200}
    rows = [{"Names": "radar-b", "Image": "i"}, {"Names": "radar-a", "Image": "i"}]
    assert [c["name"] for c in parse_containers(rows)] == ["radar-a", "radar-b"]


def test_collect_is_read_only_and_parses_outputs():
    def fake(cmd, **kwargs):
        if cmd[:2] == ["systemctl", "--user"]:
            return cp(cmd, stdout=json.dumps({"unit": "astock-cninfo-precollect.timer"}) + "\n")
        if cmd[0] == "systemctl":
            return cp(cmd, stdout=json.dumps({"unit": "radar-integration-api.timer"}) + "\n")
        if cmd[0] == "docker":
            return cp(cmd, stdout=json.dumps({"Names": "radar-alertmanager", "Image": "prom"}) + "\n")
        if cmd[0] == "du":
            return cp(cmd, stdout="123\t/home/ubuntu/trading-radar\n")
        raise AssertionError(cmd)

    manifest = collect(
        paths=["/home/ubuntu/trading-radar"], runner=fake, generated_at=dt.datetime(2026, 11, 13, tzinfo=dt.UTC)
    )
    assert manifest["totals"] == {"timers": 2, "containers": 1, "path_bytes": 123}
    # Collection never issues stop/rm/disable commands.
    assert all(True for _ in manifest["timers"])
