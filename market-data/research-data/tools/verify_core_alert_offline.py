#!/usr/bin/env python3
"""Real alert shell/helper with fixed clock, isolated state and fake Hermes only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CoreAlertShellTests(unittest.TestCase):
    def test_same_due_failure_sends_once_and_commits_only_stub_receipt(self):
        with tempfile.TemporaryDirectory(prefix="core-alert-fixed-") as temporary:
            root = Path(temporary).resolve()
            scripts, config, state, binary = [root / p for p in ("scripts", "config", "state", "bin")]
            for path in (scripts, config, state, binary): path.mkdir()
            helper = scripts / "health_alert.py"
            helper.write_text((ROOT / "scripts/health_alert.py").read_text())
            (config / "health-alert-retained.json").write_text((ROOT / "config/health-alert-retained.json").read_text())
            wrapper = scripts / "run_health_alert.sh"
            wrapper.write_text((ROOT / "scripts/run_health_alert.sh").read_text().replace('${HOME}/.local/state/research-data', str(state)))
            driver = binary / "fixed-python"
            driver.write_text("#!" + sys.executable + "\n" + '''import datetime, importlib.util, pathlib, sys
helper=pathlib.Path(__file__).resolve().parents[1]/"scripts/health_alert.py"
assert pathlib.Path(sys.argv[1])==helper
spec=importlib.util.spec_from_file_location("fixed_alert",helper)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class Fixed(datetime.datetime):
    @classmethod
    def now(cls,tz=None):
        return cls.fromisoformat("2026-09-28T22:45:30+08:00").astimezone(tz)
module.dt.datetime=Fixed
raise SystemExit(module.main(sys.argv[2:]))
''')
            hermes = binary / "fake-hermes"
            hermes.write_text("#!" + sys.executable + "\n" + '''import json,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[1]
assert sys.argv[1:]==["-m","hermes_cli.main","send","--to","telegram","--file","-","--json"]
sys.stdin.read()
count=root/"sender-count"
count.write_text(str(int(count.read_text())+1 if count.exists() else 1))
print(json.dumps({"success":True,"message_id":12345,"skipped":False}))
''')
            for name, body in {"systemctl": "#!/bin/sh\nexit 3\n", "hostname": "#!/bin/sh\nprintf 'fixed-fixture\\n'\n",
                               "timeout": '#!/bin/sh\nshift\nexec "$@"\n'}.items():
                path = binary / name; path.write_text(body); path.chmod(0o700)
            driver.chmod(0o700); hermes.chmod(0o700)
            health = root / "health.json"
            health.write_text(json.dumps({"schemaVersion": 1, "status": "warning", "readOnly": True,
                "generatedAt": "2026-09-28T22:45:00+08:00", "issues": [{"code": "last_core_market_not_successful",
                    "severity": "warning", "dueDate": "2026-09-28", "status": "deferred", "target": "2026-09-24"}]}))
            env = {**os.environ, "RESEARCH_DATA_HEALTH_JSON": str(health), "RESEARCH_DATA_PYTHON": str(driver),
                   "HERMES_PYTHON": str(hermes), "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
            env.pop("ALERT_DRY_RUN", None)
            for _ in range(2):
                result = subprocess.run(["/bin/bash", str(wrapper)], env=env, cwd=root, text=True,
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "sender-count").read_text(), "1")
            committed = json.loads((state / "health-alert-state.json").read_text())
            self.assertEqual(committed["atoms"], ["last_core_market_not_successful|2026-09-28"])
            self.assertEqual(json.loads((state / "health-alert-receipt.json").read_text())["messageId"], "12345")


if __name__ == "__main__":
    unittest.main(verbosity=2)
