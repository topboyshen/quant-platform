"""Offline regression: retired CLIs never generate or transmit anything."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class LegacyDigestGuardTests(unittest.TestCase):
    def setUp(self):
        self.builder=module('build_research_digest')
        self.poster=module('post_digest')
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.builder.OUT=self.root
        self.day='2026-09-18'
        self.path=self.root/f'research-digest-{self.day}.json'
        self.record={'as_of':self.day,'target_date':'2026-09-21','produced_at':'2026-09-21T08:01:00+08:00',
            'signals':[{'private':'SYNTHETIC_NOT_FOR_OUTPUT'}],'plain_text':'SYNTHETIC_NOT_FOR_OUTPUT',
            'health':{'research_data':{'status':'inserted','error':'SYNTHETIC_NOT_FOR_OUTPUT'}},
            'url':'https://example.invalid/?token=SYNTHETIC_NOT_FOR_OUTPUT'}

    def invoke(self,args):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err), \
                mock.patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            code=self.builder.main(args)
        return code,out.getvalue(),err.getvalue()

    def test_default_builder_stops_before_any_file_or_network_access(self):
        with mock.patch.object(self.builder.os,'open',side_effect=AssertionError('file read forbidden')), \
                mock.patch.object(Path,'read_text',side_effect=AssertionError('file read forbidden')):
            code,out,err=self.invoke([])
        self.assertEqual(code,78);self.assertFalse(out)
        self.assertEqual(json.loads(err)['sent'],0)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_no_override_can_reenable_old_generation(self):
        for args in (['--send'],['--force'],['--allow-legacy'],['--inspect','../secret'],['--inspect','2026-02-30']):
            code,out,err=self.invoke(args)
            self.assertEqual(code,78);self.assertEqual(json.loads(err)['reason'],'legacy_digest_path_disabled')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_direct_legacy_poster_never_reads_config_or_opens_network(self):
        err=io.StringIO()
        with mock.patch.object(Path,'read_text',side_effect=AssertionError('credential read forbidden')), \
                mock.patch('builtins.open',side_effect=AssertionError('file read forbidden')), \
                mock.patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')), \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.poster.main(),78)
        self.assertEqual(json.loads(err.getvalue())['sent'],0)

    def test_shell_wrapper_blocks_before_mkdir_log_or_child_process(self):
        env={'PATH':'/nonexistent','HOME':str(self.root),'LEGACY_DIGEST_ALLOW_SEND':'1'}
        result=subprocess.run(['/bin/bash',str(ROOT/'scripts/run_research_digest.sh'),'--force'],
            cwd=self.root,env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,78);self.assertFalse(result.stdout)
        self.assertIn('legacy_digest_path_disabled',result.stderr)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_python_cli_blocks_with_sender_like_arguments(self):
        for file in ('build_research_digest.py','post_digest.py'):
            result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts'/file),'--force'],
                cwd=self.root,env={'PATH':os.defpath,'LEGACY_DIGEST_ALLOW_SEND':'1'},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,78);self.assertEqual(json.loads(result.stderr)['sent'],0)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_explicit_inspect_is_bounded_whitelisted_and_does_not_modify_history(self):
        raw=json.dumps(self.record).encode();self.path.write_bytes(raw)
        before=self.path.stat();code,out,err=self.invoke(['--inspect',self.day]);after=self.path.stat()
        self.assertEqual(code,0);self.assertFalse(err);evidence=json.loads(out)
        self.assertEqual(evidence['fileSha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(evidence['fileBytes'],len(raw));self.assertEqual(evidence['signalsCount'],1)
        self.assertFalse(evidence['currentStateConfirmed']);self.assertEqual(evidence['sent'],0)
        self.assertEqual(evidence['contract'],'legacy_unverified')
        self.assertEqual(evidence['recordedFullSnapshotStatus'],'inserted')
        self.assertNotIn('SYNTHETIC_NOT_FOR_OUTPUT',out)
        self.assertEqual(self.path.read_bytes(),raw);self.assertEqual(before.st_mtime_ns,after.st_mtime_ns)

    def test_inspect_rejects_missing_mismatched_duplicate_nonfinite_and_oversize(self):
        samples=[None,json.dumps({**self.record,'as_of':'2026-09-17'}),
            '{"as_of":"2026-09-18","as_of":"2026-09-18"}',
            '{"as_of":"2026-09-18","value":NaN}',' '* (self.builder.MAX_BYTES+1),
            '['*2000+']'*2000]
        for raw in samples:
            if raw is not None:self.path.write_text(raw)
            code,out,err=self.invoke(['--inspect',self.day])
            self.assertEqual(code,65);self.assertFalse(out)
            self.assertEqual(json.loads(err)['reason'],'legacy_history_unreadable_or_invalid')

    def test_inspect_rejects_symlink_and_directory(self):
        target=self.root/'safe.json';target.write_text(json.dumps(self.record));self.path.symlink_to(target)
        self.assertEqual(self.invoke(['--inspect',self.day])[0],65)
        self.path.unlink();self.path.mkdir()
        self.assertEqual(self.invoke(['--inspect',self.day])[0],65)

    def test_inspect_does_not_invent_timezone_or_current_date(self):
        self.record['produced_at']='2026-09-21T08:01:00';self.record['target_date']='not-a-date'
        self.path.write_text(json.dumps(self.record));code,out,_=self.invoke(['--inspect',self.day])
        self.assertEqual(code,0);evidence=json.loads(out)
        self.assertIsNone(evidence['produced_at']);self.assertIsNone(evidence['report_date'])

    def test_help_is_read_only(self):
        code,out,err=self.invoke(['--help'])
        self.assertEqual(code,0);self.assertIn('never sends',out);self.assertFalse(err)


if __name__=='__main__':unittest.main()
