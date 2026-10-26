import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from legacy_terminal import guard_evidence,merge_terminal_evidence


class LegacyTerminalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.progress=root/'progress.json';self.log=root/'guard.jsonl'
        self.progress.write_text(json.dumps(dict(time_ms=1437,duration_ms=5000,input_condition='weak')))
        os.utime(self.progress,(1000,1000))
        self.entry=dict(progress=str(self.progress),events=str(self.log),environment='H01_L6_R05',script='C:/research/scripts/solver.py')
        self.event=dict(time=dt.datetime.fromtimestamp(1005,dt.timezone.utc).isoformat(),pid=9796,
            freeVirtualGiBBefore=15.932,privatePageGiB=73.375,
            command='python C:/research/scripts/solver.py --environment H01_L6_R05 --condition weak --duration-ms 5000',
            action='stop_one_exact_H01_Win3_network_science_process_to_preserve_host')
    def evidence(self):
        self.log.write_text('\ufeff'+json.dumps(self.event)+'\n',encoding='utf-8')
        return guard_evidence(self.entry,1010)
    def test_guard_is_precise_commit_evidence(self):
        e=self.evidence();self.assertEqual(e['failure_code'],'memory_pressure');self.assertIn('提交余量 15.932',e['failure_detail'])
        self.assertFalse(e['failure_evidence']['process_creation_time_recorded'])
    def test_wrong_condition_duration_and_environment_are_rejected(self):
        original=self.event['command']
        for old,new in [('weak','strong_bursts'),('5000','600'),('H01_L6_R05','H01_L6_R050')]:
            self.event['command']=original.replace(old,new);self.assertIsNone(self.evidence())
    def test_old_generation_and_future_events_are_rejected(self):
        for stamp in [999,1200]:
            self.event['time']=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat();self.assertIsNone(self.evidence())
    def test_active_unknown_and_new_progress_never_receive_old_failure(self):
        e=self.evidence();snap=dict(received_at=2000,items=[e])
        for state,process in [('running','running'),('unknown','unknown')]:
            row=dict(id=e['id'],name=e['name'],completed=1437,total=5000,lifecycle=state,process_state=process,source_age_seconds=10)
            t=dict(sampled_at=2000,simulations=[row]);merge_terminal_evidence(t,snap,2020)
            self.assertNotIn('failure_code',row)
        row=dict(id=e['id'],name=e['name'],completed=1437,total=5000,lifecycle='failed',process_state='stopped',source_age_seconds=1)
        t=dict(sampled_at=2100,simulations=[row]);merge_terminal_evidence(t,snap,2100);self.assertNotIn('failure_code',row)
    def test_receipt_ages_handle_node_clock_skew_and_preserve_worker_time(self):
        e=self.evidence();row=dict(id=e['id'],name=e['name'],completed=1437,total=5000,lifecycle='failed',process_state='stopped',source_age_seconds=30)
        t=dict(sampled_at=2020,simulations=[row]);merge_terminal_evidence(t,dict(received_at=2000,items=[e]),2020)
        self.assertEqual(row['failure_code'],'memory_pressure');self.assertEqual(t['sampled_at'],2020)
    def test_stale_evidence_cannot_classify_new_observation(self):
        e=self.evidence();row=dict(id=e['id'],name=e['name'],completed=1437,total=5000,lifecycle='failed',process_state='stopped',source_age_seconds=210)
        t=dict(sampled_at=2200,simulations=[row]);merge_terminal_evidence(t,dict(received_at=2000,items=[e]),2200)
        self.assertNotIn('failure_code',row)

    def test_runtime_guard_receipt_binds_generation_and_stays_center_owned(self):
        entry=dict(self.entry,execution_receipt_dir=str(self.progress.parent))
        receipt=dict(status='failed',root_pid=42,root_created=900,started=900,finished=1010,
                     remaining_processes=[],command=self.event['command'].split(),failure_code='resource_limit',
                     exit_code=15,private_limit_gb=32)
        path=self.progress.parent/'center_weak_execution.json';path.write_text(json.dumps(receipt))
        evidence=guard_evidence(entry,1015)
        self.assertEqual(evidence['failure_code'],'resource_limit');self.assertIn('32 GiB',evidence['failure_detail'])
        self.assertTrue(evidence['failure_evidence']['process_creation_time_recorded'])
        receipt['remaining_processes']=[42];path.write_text(json.dumps(receipt));self.assertIsNone(guard_evidence(entry,1015))
        receipt['remaining_processes']=[];receipt['started']=1005;path.write_text(json.dumps(receipt));self.assertIsNone(guard_evidence(entry,1015))


if __name__=='__main__':unittest.main()
