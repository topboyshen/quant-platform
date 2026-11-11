import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_pin_tests", ROOT / "scripts/verify_production_pin.py")
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def pin():
    return module.load_pin(ROOT / "deploy/production_pin.json")


def inspect_fixture():
    return {name: {"Image": service["image_id"], "Config": {"Image": service["image"]},
                   "State": {"Running": True, "Health": {"Status": "healthy"}}}
            for name, service in pin()["services"].items()}


def test_matching_running_healthy_passes():
    assert module.runtime_errors(pin(), inspect_fixture()) == []


@pytest.mark.parametrize("mutation", ["stopped", "unhealthy", "missing_health", "missing", "wrong_id", "wrong_tag"])
def test_matching_tag_cannot_hide_bad_runtime(mutation):
    fixture = inspect_fixture(); name = next(iter(fixture))
    if mutation == "stopped": fixture[name]["State"]["Running"] = False
    elif mutation == "unhealthy": fixture[name]["State"]["Health"]["Status"] = "unhealthy"
    elif mutation == "missing_health": fixture[name]["State"]["Health"] = None
    elif mutation == "missing": del fixture[name]
    elif mutation == "wrong_id": fixture[name]["Image"] = "sha256:" + "0" * 64
    elif mutation == "wrong_tag": fixture[name]["Config"]["Image"] = "wrong:v1"
    assert module.runtime_errors(pin(), fixture)


@pytest.mark.parametrize("mutation", ["empty", "missing_required", "missing_id", "short_id", "missing_image", "type", "version_bool"])
def test_malformed_pin_fails_before_docker(mutation, monkeypatch):
    data = copy.deepcopy(pin()); name = next(iter(data["services"]))
    if mutation == "empty": data["services"] = {}
    elif mutation == "missing_required": del data["services"][name]
    elif mutation == "missing_id": del data["services"][name]["image_id"]
    elif mutation == "short_id": data["services"][name]["image_id"] = "sha256:abc"
    elif mutation == "missing_image": del data["services"][name]["image"]
    elif mutation == "type": data["services"][name] = None
    elif mutation == "version_bool": data["schema_version"] = True
    with pytest.raises(ValueError): module.validate_pin(data)


def test_inspect_reads_only_safe_fields_and_checks_state(monkeypatch):
    fixture = inspect_fixture(); name = next(iter(fixture)); calls = []
    def run(args):
        calls.append(args)
        return json.dumps(fixture[name])
    monkeypatch.setattr(module, "_docker", run)
    normalized = module.inspect_running(name)
    assert normalized["running"] is True
    assert ".Config.Env" not in calls[0][-2] and "json .}}" not in calls[0][-2]
    assert "--type" in calls[0] and "State.Running" in calls[0][-2]


def test_cli_honors_pin_and_inspect_json_without_docker(tmp_path):
    pinfile, fixture = tmp_path / "pin.json", tmp_path / "inspect.json"
    pinfile.write_text(json.dumps(pin())); fixture.write_text(json.dumps(inspect_fixture()))
    command = [sys.executable, "-B", str(ROOT / "scripts/verify_production_pin.py"),
               "--pin", str(pinfile), "--inspect-json", str(fixture)]
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0
    pinfile.write_text('{"schema_version":1,"services":{}}')
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 2
    pinfile.unlink()
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 2


def test_preflight_matches_resolved_config_and_local_id_without_running(monkeypatch):
    data = pin()
    compose = {"services": {n.removeprefix("radar-"): {"image": s["image"]} for n, s in data["services"].items()}}
    images = {s["image"]: s["image_id"] for s in data["services"].values()}
    monkeypatch.setattr(module, "_docker", lambda args: images[args[-1]])
    assert module.preflight(data, compose) == []
    compose["services"]["quantdinger-backend"]["image"] = "unexpected:latest"
    assert module.preflight(data, compose)
    images.update({k: "sha256:" + "0" * 64 for k in images})
    assert module.preflight(data, compose)


def test_explicit_null_fixture_never_falls_back_to_docker(tmp_path, monkeypatch):
    fixture = tmp_path / "inspect.json"; fixture.write_text("null")
    monkeypatch.setattr(module, "_docker", lambda args: pytest.fail("offline fixture must never reach docker"))
    assert module.main(["--inspect-json", str(fixture)]) == 2
