"""Independent regression audit for truthful legacy-task lifecycle reporting."""
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import apply_science_control, simulation_lifecycle
from simulation_monitor import SimulationMonitor, alive
from science_control import ProcessInventory, ScienceControl, matched, process_inventory, research_processes
import task_health
import health_watchdog


class ProgressAudit(unittest.TestCase):
    def network_fixture(self, root, completed=315, age=600):
        folder = root / 'environment_networks_fixed_active_v3' / 'H01_L6_R05'
        folder.mkdir(parents=True)
        progress = folder / 'progress.json'
        progress.write_text(json.dumps(dict(
            time_ms=completed, duration_ms=600, input_condition='strong_bursts',
            elapsed_s=1200, output=str(folder / 'pilot_600ms.h5'),
        )))
        stamp = time.time() - age
        os.utime(progress, (stamp, stamp))
        obs = [dict(pid=123, start=stamp-1200,
                    args=['python', str(root / 'scripts/full_window_network_fixed_active_v3.py'),
                          '--environment', 'H01_L6_R05'])]
        monitor = SimulationMonitor({'network_roots': [str(root)]}, root / 'cache.json')
        return monitor, progress, obs

    def test_permission_failure_is_not_confirmed_process_exit(self):
        with patch('simulation_monitor.psutil.Process', side_effect=psutil.AccessDenied(123)):
            self.assertIsNone(alive(123, time.time()))

    def test_absent_pid_is_confirmed_process_exit(self):
        with patch('simulation_monitor.psutil.Process', side_effect=psutil.NoSuchProcess(123)):
            self.assertIs(alive(123, time.time()), False)

    def test_stale_batch_has_no_live_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'audit_manifest.json').write_text(json.dumps({'queue': [{}, {}]}))
            state = root / 'audit_runner_state.json'
            stamp = time.time() - 600
            state.write_text(json.dumps(dict(
                phase='running', pid=123, completed=1, total=2, failed=0,
                start_utc=dt.datetime.fromtimestamp(stamp - 600, dt.timezone.utc).isoformat(),
                last_update_utc=dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(),
            )))
            os.utime(state, (stamp, stamp))
            obs = [dict(pid=123, start=stamp-600,
                        args=['python', str(root / 'windows_observed_batch_runner.py'),
                              '--batch-name', 'audit'])]
            row = SimulationMonitor({'batch_dirs': [tmp]}, root / 'cache.json').collect(obs)[0]
            self.assertIsNone(row['remaining_seconds'])
            self.assertEqual(simulation_lifecycle(row), 'running')
            self.assertNotEqual(row['status'], '运行中')

    def test_started_waiting_batch_leaves_queue(self):
        row = dict(status='等待前序批次', completed=0, total=2)
        apply_science_control(row, dict(status='running'))
        self.assertEqual(simulation_lifecycle(row), 'running')
        self.assertIn(row['status'], ('初始化中', '运行中'))

    def test_pause_keeps_progress_and_suppresses_eta(self):
        row = dict(status='进程运行中，进度暂未更新', completed=315, total=600,
                   remaining_seconds=123, source_age_seconds=900)
        apply_science_control(row, dict(status='paused'))
        self.assertEqual(simulation_lifecycle(row), 'paused')
        self.assertEqual(row['completed'], 315)
        self.assertIsNone(row['remaining_seconds'])

    def test_completed_result_remains_success_after_process_exit(self):
        row = dict(status='计算完成', completed=600, total=600,
                   source_age_seconds=900)
        apply_science_control(row, dict(status='stopped'))
        self.assertEqual(simulation_lifecycle(row), 'succeeded')
        self.assertEqual(row['actions'], [])

    def test_completed_result_rejects_late_control_state(self):
        for control in ('paused', 'cancelled', 'starting'):
            with self.subTest(control=control):
                row = dict(status='计算完成', completed=600, total=600)
                apply_science_control(row, dict(status=control))
                self.assertEqual(simulation_lifecycle(row), 'succeeded')
                self.assertEqual(row['actions'], [])

    def test_observation_unavailable_does_not_confirm_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, _, _ = self.network_fixture(Path(tmp))
            row = monitor.collect([], observation_verified=False)[0]
            self.assertEqual(row['process_state'], 'unknown')
            self.assertEqual(simulation_lifecycle(row), 'unknown')
            self.assertIsNone(row['remaining_seconds'])

    def test_complete_observation_can_confirm_missing_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, _, _ = self.network_fixture(Path(tmp))
            row = monitor.collect([], observation_verified=True)[0]
            self.assertEqual(row['process_state'], 'stopped')
            self.assertEqual(simulation_lifecycle(row), 'failed')

    def test_valid_result_wins_over_stale_progress_while_process_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, progress, obs = self.network_fixture(Path(tmp), completed=600)
            (progress.parent / 'result_600ms.json').write_text(json.dumps(dict(
                duration_ms=600, input_condition='strong_bursts', finite=True,
            )))
            row = monitor.collect(obs)[0]
            self.assertEqual(simulation_lifecycle(row), 'succeeded')
            self.assertIsNone(row['remaining_seconds'])

    def test_nearly_complete_steps_wait_for_result_without_tiny_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, _, obs = self.network_fixture(Path(tmp), completed=600-1e-9, age=5)
            row = monitor.collect(obs)[0]
            self.assertEqual(row['progress_state'], 'finalizing')
            self.assertEqual(simulation_lifecycle(row), 'running')
            self.assertIsNone(row['remaining_seconds'])

    def test_corrupt_progress_does_not_erase_live_process_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, progress, obs = self.network_fixture(Path(tmp))
            progress.write_text('{')
            row = monitor.collect(obs)[0]
            self.assertEqual(row['progress_state'], 'unreadable')
            self.assertEqual(row['process_state'], 'running')
            self.assertEqual(simulation_lifecycle(row), 'running')
            self.assertIsNone(row['remaining_seconds'])


class ObservationAudit(unittest.TestCase):
    def test_denied_scientific_candidate_invalidates_observation(self):
        process = Mock(pid=123, info={'name': 'python.exe'})
        process.cmdline.side_effect = psutil.AccessDenied(123)
        with patch('science_control.psutil.process_iter', return_value=[process]):
            inventory = process_inventory()
        self.assertFalse(inventory.verified)
        self.assertEqual(inventory, [])

    def test_unrelated_protected_process_does_not_invalidate_observation(self):
        process = Mock(pid=123, info={'name': 'lsass.exe'})
        process.cmdline.side_effect = psutil.AccessDenied(123)
        with patch('science_control.psutil.process_iter', return_value=[process]):
            inventory = process_inventory()
        self.assertTrue(inventory.verified)
        process.cmdline.assert_not_called()

    def test_research_identity_denial_invalidates_observation(self):
        process = Mock(pid=123, info={'name': 'python.exe'})
        process.create_time.side_effect = psutil.AccessDenied(123)
        inventory = ProcessInventory([(process, ['python', '/research/task.py'])])
        self.assertEqual(research_processes(['/research'], inventory), [])
        self.assertFalse(inventory.verified)

    def test_direct_restart_match_refuses_incomplete_observation(self):
        inventory = ProcessInventory()
        inventory.verified = False
        with patch('science_control.process_inventory', return_value=inventory):
            with self.assertRaisesRegex(RuntimeError, '进程监测不可用'):
                matched({'matches': [['/research/task.py']]})

    def test_incomplete_snapshot_never_offers_or_executes_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps(dict(spool=str(root / 'spool'), recipes={
                'research': dict(name='research', matches=[['/research/task.py']],
                                 restart_task='LabCompute-Science-research')
            })))
            manager = ScienceControl(cfg)
            requests = manager.root / 'requests'
            requests.mkdir()
            (requests / 'cmd.json').write_text(json.dumps(dict(
                simulation='research', action='start', generation='prior',
            )))
            inventory = ProcessInventory()
            inventory.verified = False
            with patch('science_control.process_inventory', return_value=inventory), \
                 patch.object(manager, 'perform') as perform:
                manager.once()
            perform.assert_not_called()
            snapshot = json.loads((manager.root / 'snapshot.json').read_text())
            self.assertFalse(snapshot['observation_verified'])
            self.assertEqual(snapshot['items']['research']['status'], 'unknown')
            self.assertEqual(snapshot['items']['research']['actions'], [])
            response = json.loads((manager.root / 'responses' / 'cmd.json').read_text())
            self.assertEqual(response['status'], 'failed')
            self.assertIn('进程监测不可用', response['detail'])
            with patch('science_control.process_inventory', return_value=ProcessInventory()):
                manager.once()
            recovered = json.loads((manager.root / 'snapshot.json').read_text())
            self.assertTrue(recovered['observation_verified'])
            self.assertEqual(recovered['items']['research']['status'], 'stopped')
            self.assertIn('start', recovered['items']['research']['actions'])


class ActivityAudit(unittest.TestCase):
    def controller(self):
        manager=ScienceControl.__new__(ScienceControl)
        manager.activity_samples={}
        return manager

    def proc(self,pid,created=100,cpu=0):
        process=Mock(pid=pid)
        process.create_time.return_value=created
        process.cpu_times.return_value=SimpleNamespace(user=cpu,system=0)
        process.children.return_value=[]
        return process

    def test_first_sample_is_unknown_then_deduplicated_tree_activity(self):
        manager=self.controller()
        parent=self.proc(1,cpu=2);child=self.proc(2,cpu=3)
        parent.children.return_value=[child]
        first=manager.activity('task',[parent,child],now=10)
        self.assertFalse(first['verified']);self.assertIsNone(first['cpu_seconds'])
        parent.cpu_times.return_value=SimpleNamespace(user=2,system=.5)
        child.cpu_times.return_value=SimpleNamespace(user=5,system=.25)
        second=manager.activity('task',[parent,child],now=12)
        self.assertTrue(second['verified']);self.assertEqual(second['sample_seconds'],2)
        self.assertEqual(second['cpu_seconds'],2.75);self.assertEqual(second['working_processes'],2)
        self.assertEqual(second['process_count'],2)
        self.assertEqual(second['child_processes'],1)
        self.assertEqual(child.cpu_times.call_count,2)

    def test_child_count_and_real_zero_require_complete_samples(self):
        manager=self.controller()
        parent=self.proc(1,cpu=2);child=self.proc(2,cpu=3)
        parent.children.return_value=[child]
        manager.activity('task',[parent],now=10)
        row=manager.activity('task',[parent],now=12)
        self.assertTrue(row['verified']);self.assertEqual(row['cpu_seconds'],0)
        self.assertEqual(row['working_processes'],0);self.assertEqual(row['child_processes'],1)

    def test_denied_cpu_read_never_becomes_zero(self):
        manager=self.controller();process=self.proc(1)
        manager.activity('task',[process],now=10)
        process.cpu_times.side_effect=psutil.AccessDenied(1)
        row=manager.activity('task',[process],now=12)
        self.assertFalse(row['verified']);self.assertIsNone(row['cpu_seconds'])
        self.assertIsNone(row['working_processes'])

    def test_new_pid_generation_cannot_inherit_previous_cpu_delta(self):
        manager=self.controller();process=self.proc(1,created=100,cpu=20)
        manager.activity('task',[process],now=10)
        process.create_time.return_value=200
        process.cpu_times.return_value=SimpleNamespace(user=1,system=0)
        row=manager.activity('task',[process],now=12)
        self.assertFalse(row['verified']);self.assertIsNone(row['cpu_seconds'])

    def test_missing_child_or_stale_sample_is_unverified(self):
        manager=self.controller();parent=self.proc(1);child=self.proc(2)
        parent.children.return_value=[child]
        manager.activity('task',[parent],now=10)
        parent.children.return_value=[]
        row=manager.activity('task',[parent],now=12)
        self.assertFalse(row['verified']);self.assertIsNone(row['cpu_seconds'])
        row=manager.activity('task',[parent],now=50)
        self.assertFalse(row['verified']);self.assertIsNone(row['cpu_seconds'])


class HealthAudit(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE task_issues(node TEXT,task TEXT,state TEXT,first_seen REAL,last_seen REAL,resolved_at REAL,data TEXT,PRIMARY KEY(node,task))')

    def tearDown(self):self.db.close()

    def state(self):return self.db.execute('SELECT state,first_seen,last_seen FROM task_issues').fetchone()

    def test_heartbeat_opens_issue_without_dashboard_and_missing_task_retains_it(self):
        stale=dict(id='C:/Research/progress.json',name='science',lifecycle='running',
                   progress_state='stale',process_state='running',source_age_seconds=600)
        task_health.reconcile(self.db,'win1',dict(simulations=[stale]),100)
        self.assertEqual(self.state(),('open',100,100))
        self.assertIn('health',stale)
        task_health.reconcile(self.db,'win1',dict(simulations=[]),110)
        self.assertEqual(self.state(),('open',100,100))

    def test_only_positive_running_evidence_resolves_issue(self):
        sim=dict(id='task',lifecycle='unknown')
        task_health.reconcile(self.db,'win1',dict(simulations=[sim]),100)
        task_health.reconcile(self.db,'win1',dict(simulations=[dict(id='task',lifecycle='running')]),110)
        self.assertEqual(self.state()[0],'open')
        task_health.reconcile(self.db,'win1',dict(simulations=[dict(id='task',lifecycle='running',
                              process_state='running',progress_state='fresh')]),120)
        self.assertEqual(self.state()[0],'resolved')

    def test_activity_is_not_claimed_to_prove_scientific_progress(self):
        row=task_health.diagnose(dict(lifecycle='running',progress_state='stale',source_age_seconds=600,
            activity=dict(verified=True,sample_seconds=2,cpu_seconds=1,working_processes=1)))
        self.assertIn('1 个进程消耗 CPU',row['detail'])
        self.assertIn('不等于科学进度',row['detail'])
        row=task_health.diagnose(dict(lifecycle='running',progress_state='stale',source_age_seconds=600,
            activity=dict(verified=False,sample_seconds=2,cpu_seconds=None,working_processes=None)))
        self.assertNotIn('未测到 CPU',row['detail'])

    def test_issue_upsert_keeps_original_age_until_positive_recovery(self):
        sim=dict(id='task',lifecycle='failed')
        task_health.reconcile(self.db,'win1',dict(simulations=[sim]),100)
        task_health.reconcile(self.db,'win1',dict(simulations=[sim]),110)
        self.assertEqual(self.state(),('open',100,110))
        task_health.reconcile(self.db,'win1',dict(simulations=[dict(id='task',lifecycle='succeeded')]),120)
        self.assertEqual(self.state()[0],'resolved')
        task_health.reconcile(self.db,'win1',dict(simulations=[sim]),130)
        self.assertEqual(self.state(),('open',130,130))


class WatchdogAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.spool=self.root/'science-control'
        self.spool.mkdir()
        self.restart=Mock()

    def tearDown(self):self.tmp.cleanup()

    def snapshot(self,at,verified=True):
        (self.spool/'snapshot.json').write_text(json.dumps(dict(at=at,observation_verified=verified)))

    def test_fresh_healthy_or_permission_failure_does_not_restart(self):
        for verified in (True,False):
            with self.subTest(verified=verified):
                self.snapshot(999,verified)
                row=health_watchdog.recover(self.root,task_state=lambda:'Running',restart=self.restart,now=1000)
                self.assertEqual(row['status'],'healthy' if verified else 'needs_admin')
        self.restart.assert_not_called()

    def test_missing_snapshot_starts_stopped_observer(self):
        row=health_watchdog.recover(self.root,task_state=lambda:'Ready',restart=self.restart,now=1000)
        self.restart.assert_called_once_with(False)
        self.assertEqual(row['status'],'recovering')
        self.assertEqual(row['attempts'],[1000])

    def test_stale_running_observer_recovers_then_fresh_healthy(self):
        self.snapshot(500)
        row=health_watchdog.recover(self.root,task_state=lambda:'Running',restart=self.restart,now=1000)
        self.restart.assert_called_once_with(True)
        self.assertEqual(row['status'],'recovering')
        self.snapshot(1001)
        row=health_watchdog.recover(self.root,task_state=lambda:'Running',restart=self.restart,now=1002)
        self.assertEqual(row['status'],'healthy')
        self.assertEqual(self.restart.call_count,1)

    def test_pending_control_does_not_kill_running_observer(self):
        (self.spool/'requests').mkdir()
        (self.spool/'requests'/'pending.json').write_text('{}')
        row=health_watchdog.recover(self.root,task_state=lambda:'Running',restart=self.restart,now=1000)
        self.restart.assert_not_called()
        self.assertEqual(row['status'],'needs_admin')
        self.assertIn('未确认的控制操作',row['detail'])

    def test_recovery_is_backed_off_and_limited_to_three_attempts(self):
        times=[1000,1001,1060,1061,1180,1181,1420,1500]
        for now in times:
            row=health_watchdog.recover(self.root,task_state=lambda:'Running',restart=self.restart,now=now)
        self.assertEqual(self.restart.call_count,3)
        self.assertEqual(row['attempts'],[1000,1060,1180])
        self.assertEqual(row['status'],'needs_admin')

    def test_default_restart_only_targets_monitor_task(self):
        with patch('health_watchdog.subprocess.run') as run:
            health_watchdog.recover(self.root,task_state=lambda:'Running',now=1000)
        run.assert_called_once()
        argv=run.call_args.args[0]
        self.assertIn('Stop-ScheduledTask -TaskName LabCompute-ScienceControl',argv[-1])
        self.assertIn('Start-ScheduledTask -TaskName LabCompute-ScienceControl',argv[-1])
        self.assertNotIn('Stop-Process',argv[-1])
        self.assertNotIn('LabCompute-Agent',argv[-1])


if __name__ == '__main__':
    unittest.main()
