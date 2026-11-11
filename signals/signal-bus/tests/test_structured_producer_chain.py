"""Real local compiler -> digest/seal -> Python relay, fixed data and fake sender."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from test_alert_repair_boundaries import rca

ASTOCK = Path(os.environ.get("ASTOCK_TEST_ROOT", "/home/ubuntu/AStock_AI_System")).resolve()
NODE = shutil.which("node")
DAY = "2026-09-21"
NOW = datetime(2026, 9, 21, 0, 5, tzinfo=timezone.utc)
BUILD = r'''
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {mkdir,writeFile} from 'node:fs/promises';
const [repo,root,day] = process.argv.slice(1);
const {writeFixture} = await import(pathToFileURL(path.join(repo,'test/helpers/research-seal-fixture.mjs')));
const {buildResearchSeal} = await import(pathToFileURL(path.join(repo,'src/xhs-draft.mjs')));
await writeFixture(root,{targetDate:day,marketDate:'2026-09-18'});
const state = path.join(root,'work/orchestrator/states');
await mkdir(state,{recursive:true});
await writeFile(path.join(state,`morning-report-${day}.json`),JSON.stringify({schemaVersion:'1.0',task:'morning-report',
 targetDate:day,startedAt:`${day}T00:00:01Z`,completedAt:`${day}T00:01:17Z`,status:'success',
 childExitCode:0,childSignal:null,timedOut:false,interrupted:false}));
const sealed = await buildResearchSeal({projectRoot:root,now:new Date(`${day}T00:05:00Z`)});
console.log(JSON.stringify(sealed));
'''


@unittest.skipUnless(NODE and (ASTOCK / "src/xhs-draft.mjs").is_file(),
                     "set ASTOCK_TEST_ROOT to the paired local compiler source")
class StructuredProducerChainTests(unittest.TestCase):
    def test_real_compiler_seal_relay_fake_receipt_and_immutable_retry(self):
        relay = rca._RELAY
        with tempfile.TemporaryDirectory() as tmp, patch.object(socket, "socket", side_effect=AssertionError("network")):
            base = Path(tmp).resolve()
            project, bus = base / "project", base / "bus"
            project.mkdir()
            (bus / "config").mkdir(parents=True)
            built = subprocess.run([NODE, "--input-type=module", "-e", BUILD, str(ASTOCK), str(project), DAY],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(built.returncode, 0, built.stderr[-2000:])
            self.assertEqual(json.loads(built.stdout)["status"], "sealed")
            (bus / "config/pending.env").write_text(
                "DEDIROCK_DIGEST_URL=https://receiver.invalid/ingest\nDEDIROCK_PENDING_SECRET=test-secret\nTLS_VERIFY=1\n")
            prepared = relay.prepare_research(project, NOW)
            self.assertEqual(prepared["as_of"], "2026-09-18")
            sent = []
            def sender(payload, config, now):
                body = relay.encode_payload(payload)
                sent.append(body)
                content_hash = hashlib.sha256(body).hexdigest()
                return {"status": "confirmed", "reason": "accepted", "ack_scope": "http_accepted",
                        "receiver_storage": "receiver_reported_stored", "content_match": True,
                        "receiver_record_id": "fixed-record", "payload_sha256": content_hash,
                        "receiver_content_sha256": content_hash}
            result = relay.run_relay(project, bus, now=NOW, sender=sender)
            self.assertEqual(result["status"], "confirmed", result)
            again = relay.run_relay(project, bus, now=NOW, sender=sender)
            self.assertEqual(again["status"], "already_confirmed")
            self.assertEqual(len(sent), 1)
            self.assertEqual(result["payload_sha256"], hashlib.sha256(sent[0]).hexdigest())
            # An altered existing digest cannot be silently reconstructed or used.
            digest = next((project / f"work/research-seal/{DAY}").glob("*.digest.json"))
            digest.write_text("{}")
            held = relay.run_relay(project, bus, now=NOW, sender=sender, dry_run=True)
            self.assertEqual(held["status"], "held")
            self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
