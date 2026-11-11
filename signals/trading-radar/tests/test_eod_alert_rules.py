"""PromQL fault matrix; use PROMTOOL pointing to an existing trusted promtool binary."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMTOOL = os.environ.get("PROMTOOL") or shutil.which("promtool")
LABELS = {"job": "node-exporter", "instance": "quant-host:9100", "service": "swing_eod"}
DESCRIPTIONS = {
    "QuantSwingEodBusinessFailedFinal": ("critical", "Swing EOD 日级终态仍失败",
        "已确认失败，或应验收交易日 15:50 后仍缺失/运行/等待重试；不因经过 18h 自动恢复。保留首次失败审计，对照 watchdog JSON 与同日 retry。"),
    "QuantSwingEodEvidenceUnknown": ("warning", "Swing EOD 审计证据缺失或不可判定",
        "JSON 缺失/非法、交易日历不可用、应验收日期不匹配或指标缺失/过期；不能视为成功。周末/节假日沿用上一交易日，不读取生产库或自动补跑。"),
}


def rule_case(name, changes, *, final=False, unknown=False):
    values = {"recovery_status": "2x11", "evidence_valid": "1x11", "calendar_valid": "1x11",
              "evidence_current": "1x11", "terminal_due": "1x11"}
    values.update(changes)
    suffix = '{job="node-exporter",instance="quant-host:9100"}'
    series = [{"series": "radar_swing_eod_" + key + suffix, "values": value}
              for key, value in values.items() if value is not None]
    series += [{"series": "up" + suffix, "values": "1x11"},
               {"series": "radar_datasource_metrics_unixtime" + suffix, "values": "0+60x11"}]
    alerts = []
    for alertname, active in zip(DESCRIPTIONS, (final, unknown)):
        severity, summary, description = DESCRIPTIONS[alertname]
        expected = [{"exp_labels": {**LABELS, "severity": severity},
                     "exp_annotations": {"summary": summary, "description": description}}] if active else []
        alerts.append({"eval_time": "10m", "alertname": alertname, "exp_alerts": expected})
    return {"name": name, "interval": "1m", "input_series": series, "alert_rule_test": alerts}


@pytest.mark.skipif(PROMTOOL is None, reason="promtool required for PromQL execution")
def test_promql_missing_invalid_stale_and_recovery_matrix(tmp_path):
    cases = [
        rule_case("healthy weekend previous-session success", {}),
        rule_case("confirmed failed", {"recovery_status": "3x11"}, final=True),
        rule_case("missing audit", {"recovery_status": "4x11"}, final=True),
        rule_case("15:40 expected deferral", {"recovery_status": "1x11", "terminal_due": "0x11"}),
        rule_case("15:50 missed retry", {"recovery_status": "1x11"}, final=True),
        rule_case("15:50 still running", {"recovery_status": "6x11"}, final=True),
        rule_case("unknown run", {"recovery_status": "7x11"}, final=True),
        rule_case("invalid/missing JSON", {"recovery_status": " ".join(["NaN"] * 12), "evidence_valid": "0x11"}, unknown=True),
        rule_case("invalid calendar", {"calendar_valid": "0x11"}, unknown=True),
        rule_case("old success on next session", {"evidence_current": "0x11"}, unknown=True),
        rule_case("next session before deadline", {"evidence_current": "0x11", "terminal_due": "0x11"}),
        rule_case("textfile metrics absent", {key: None for key in ("recovery_status", "evidence_valid", "calendar_valid", "evidence_current", "terminal_due")}, unknown=True),
        rule_case("partial metric set", {"recovery_status": None}, unknown=True),
        rule_case("NaN validity cannot be green", {"evidence_valid": " ".join(["NaN"] * 12)}, unknown=True),
        rule_case("actual recovery clears failure", {"recovery_status": "3x5 2x6"}),
    ]
    fixture = {"rule_files": [str(ROOT / "deploy/prometheus/alerts.yml")],
               "evaluation_interval": "1m", "tests": cases}
    path = tmp_path / "rules.test.json"; path.write_text(json.dumps(fixture, ensure_ascii=False))
    result = subprocess.run([PROMTOOL, "test", "rules", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
