"""Registered nested progress must remain tied to its live execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from health_watchdog import sync_progress_sources
from common import read_progress

class ProgressAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.jid='a'*32
        self.folder=self.root/'jobs'/self.jid;self.work=self.folder/'work';(self.work/'nested').mkdir(parents=True)
        self.now=time.time();self.source=self.work/'nested/progress.json'
        (self.folder/'runner.json').write_text(json.dumps(dict(pid=123,create_time=self.now-100)))
        (self.folder/'usage.json').write_text(json.dumps(dict(at=self.now,phase='running')))
        (self.root/'config').mkdir();self.config=self.root/'config/progress-sources.json'
        self.config.write_text(json.dumps({self.jid:dict(path='nested/progress.json',completed_key='time_ms',total_key='duration_ms')}))
        self.source.write_text(json.dumps(dict(time_ms=39,duration_ms=600)));os.utime(self.source,(self.now-1,self.now-1))
        self.proc=Mock();self.proc.create_time.return_value=self.now-100;self.proc.is_running.return_value=True
        self.mock=patch('health_watchdog.psutil.Process',return_value=self.proc);self.mock.start();self.addCleanup(self.mock.stop)
    def sync(self):return sync_progress_sources(self.root,self.now)[self.jid]
    def test_real_nested_progress_reaches_runner_contract_without_refreshing_unchanged_count(self):
        self.assertEqual(self.sync()['status'],'current');self.assertEqual(read_progress(self.work),dict(completed=39,total=600))
        target=self.work/'progress.json';stamp=target.stat().st_mtime_ns
        self.sync();self.assertEqual(target.stat().st_mtime_ns,stamp)
    def test_stale_source_and_old_generation_are_not_published(self):
        for stamp in (self.now-200,self.now+10):
            os.utime(self.source,(stamp,stamp));self.assertEqual(self.sync()['status'],'source_stale')
            self.assertFalse((self.work/'progress.json').exists())
    def test_reused_pid_is_not_live_execution(self):
        self.proc.create_time.return_value=self.now-10
        self.assertEqual(self.sync()['status'],'error');self.assertFalse((self.work/'progress.json').exists())
    def test_paused_or_unobserved_runner_does_not_publish(self):
        for data in (dict(phase='paused',at=self.now),dict(phase='running',at=self.now-60)):
            (self.folder/'usage.json').write_text(json.dumps(data));self.assertEqual(self.sync()['status'],'inactive_or_unobserved')
    def test_finished_runner_does_not_publish(self):
        (self.folder/'exit.json').write_text('{}');self.assertEqual(self.sync()['status'],'finished')
    def test_does_not_overwrite_task_owned_progress(self):
        target=self.work/'progress.json';target.write_text('{"completed":2,"total":3}')
        self.assertEqual(self.sync()['status'],'error');self.assertEqual(read_progress(self.work)['completed'],2)
    def test_regression_and_nonfinite_values_keep_last_true_progress(self):
        self.sync()
        for done,total in ((1,600),(40,700),('NaN',600),(900,600)):
            self.source.write_text(json.dumps(dict(time_ms=done,duration_ms=total)));os.utime(self.source,(self.now-1,self.now-1))
            self.assertEqual(self.sync()['status'],'error');self.assertEqual(read_progress(self.work)['completed'],39)
    def test_escape_path_and_symlink_are_not_followed(self):
        self.config.write_text(json.dumps({self.jid:dict(path='../usage.json')}));self.assertEqual(self.sync()['status'],'error')
        self.config.write_text(json.dumps({self.jid:dict(path='nested/progress.json')}))
        self.source.unlink();self.source.symlink_to(self.folder/'usage.json');self.assertEqual(self.sync()['status'],'error')
    def enable_rule(self):
        self.config.unlink()
        (self.root/'config/progress-source-rules.json').write_text(json.dumps([dict(path_glob='nested/*.json',completed_key='time_ms',total_key='duration_ms')]))
    def test_registered_format_discovers_future_job_without_per_job_mapping(self):
        self.enable_rule();self.assertEqual(self.sync()['status'],'current')
        self.assertEqual(read_progress(self.work)['completed'],39)
        self.source.write_text('{"time_ms":40,"duration_ms":600}');os.utime(self.source,(self.now-1,self.now-1))
        self.assertEqual(self.sync()['completed'],40)
    def test_discovery_rejects_ambiguity_and_old_sources(self):
        self.enable_rule();other=self.work/'nested/another.json';other.write_text(self.source.read_text());os.utime(other,(self.now-1,self.now-1))
        self.assertEqual(self.sync()['status'],'ambiguous_sources');self.assertFalse((self.work/'progress.json').exists())
        other.unlink();os.utime(self.source,(self.now-200,self.now-200))
        self.assertEqual(sync_progress_sources(self.root,self.now),{})
    def test_discovery_keeps_identity_and_task_progress_guards(self):
        self.enable_rule();self.proc.create_time.return_value=self.now-10
        self.assertEqual(self.sync()['status'],'error');self.assertFalse((self.work/'progress.json').exists())
        self.proc.create_time.return_value=self.now-100;(self.work/'progress.json').write_text('{"completed":2,"total":4}')
        self.assertEqual(sync_progress_sources(self.root,self.now),{});self.assertEqual(read_progress(self.work)['completed'],2)
    def test_missing_config_is_noop(self):
        self.config.unlink();self.assertEqual(sync_progress_sources(self.root),{})
    def test_initialization_is_not_a_read_error_but_lost_progress_is(self):
        self.source.unlink();self.assertEqual(self.sync()['status'],'awaiting_first_progress')
        self.assertFalse((self.work/'progress.json').exists())
        self.source.write_text('{"time_ms":39,"duration_ms":600}');os.utime(self.source,(self.now-1,self.now-1))
        self.assertEqual(self.sync()['status'],'current')
        self.source.unlink();self.assertEqual(self.sync()['status'],'error')
        self.assertEqual(read_progress(self.work)['completed'],39)

if __name__=='__main__':unittest.main()
