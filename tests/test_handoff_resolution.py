"""Independent regressions: retiring an observer is not scientific acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handoff_resolution


class HandoffResolutionAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'legacy-handoffs.json'
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE task_issues(node TEXT,task TEXT,state TEXT,first_seen REAL,last_seen REAL,resolved_at REAL,data TEXT,PRIMARY KEY(node,task))')
        self.prior = dict(owner='center', code='monitor_unavailable', error_report=dict(report_id='original'))
        self.db.execute('INSERT INTO task_issues VALUES(?,?,?,?,?,?,?)',
                        ('win2', 'c:/old/state.json', 'open', 10, 20, None, json.dumps(self.prior)))
        manifest = json.dumps(dict(tasks=[dict(id='a'), dict(id='b')]))
        self.entry = dict(node='win2', task='C:\\OLD\\state.json', issue_first_seen=10, batch_id='batch', science_batch='old',
            expected_identities=['a', 'b'], authorization=dict(authority='TRANSFERRED_TO_COMPUTE_CENTER',
                granted_at=30, manifest_utf8=manifest, manifest_sha256=hashlib.sha256(manifest.encode()).hexdigest()),
            retirement=dict(node='win2', task='c:/old/state.json', observed_at=40,
                entry='scheduled task', entry_disabled=True, generation_stopped=True, evidence='verified old waiter and child tree stopped'))
        self.nodes = [dict(id='win2', last_seen=95, telemetry=dict(simulations=[]))]
        self.jobs = [dict(id=x, batch_id='batch', status='succeeded', has_result=True,
                         spec=dict(science_identity=x, science_batch='old', source='existing_science_queue')) for x in ('a', 'b')]

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def run_reconcile(self):
        self.path.write_text(json.dumps(dict(schema_version=1, handoffs=[self.entry])), encoding='utf-8')
        return handoff_resolution.reconcile(self.db, self.path, self.jobs, self.nodes, 100)

    def row(self):
        state, data = self.db.execute('SELECT state,data FROM task_issues').fetchone()
        return state, json.loads(data)

    def test_exact_retirement_and_results_resolve_preserving_original_report(self):
        self.jobs.append(dict(self.jobs[0], id='prior-failure', status='failed', has_result=False))
        result = self.run_reconcile()
        self.assertEqual(len(result['resolved']), 1)
        state, data = self.row()
        self.assertEqual(state, 'resolved')
        self.assertEqual(data['error_report'], self.prior['error_report'])
        self.assertEqual(data['resolution']['scientific_review'], 'not_asserted')
        self.assertIn('prior-failure', data['resolution']['mapped_jobs'][0]['attempt_ids'])
        self.assertEqual(self.run_reconcile()['resolved'], [])

    def test_reappeared_old_task_in_any_state_blocks_resolution(self):
        for lifecycle in ('running', 'unknown', 'succeeded'):
            self.nodes[0]['telemetry']['simulations'] = [dict(id='C:\\OLD\\state.json', lifecycle=lifecycle)]
            self.assertFalse(self.run_reconcile()['resolved'])
            self.assertEqual(self.row()[0], 'open')

    def placeholder(self):
        return dict(id='C:\\OLD\\state.json', kind='batch', name='old', lifecycle='queued',
                    process_state='unknown', status='等待前序批次', completed=0, total=2,
                    percent=0, running=[], source_age_seconds=None)

    def test_only_explicit_registered_retired_placeholder_is_allowed(self):
        self.nodes[0]['telemetry']['simulations'] = [self.placeholder()]
        self.assertFalse(self.run_reconcile()['resolved'])
        self.entry['retired_wait_placeholder'] = True
        self.assertFalse(self.run_reconcile()['resolved'])
        self.entry['retirement']['evidence'] = dict(old_runner_state_exists=False,
                                                  observation='old waiter disabled and tree absent')
        self.assertTrue(self.run_reconcile()['resolved'])
        self.assertEqual(self.row()[1]['resolution']['retired_wait_placeholder'], self.placeholder())

    def test_placeholder_exception_rejects_live_state_or_progress_evidence(self):
        self.entry['retired_wait_placeholder'] = True
        self.entry['retirement']['evidence'] = dict(old_runner_state_exists=False)
        for key, value in (('kind', 'network'), ('name', 'another'), ('lifecycle', 'unknown'),
                           ('process_state', 'running'), ('status', '等待资源'), ('completed', 1),
                           ('completed', False), ('total', 3), ('percent', 1), ('running', [42]),
                           ('pid', 42), ('pid', 0), ('process_refs', [dict(pid=42, created=95)]),
                           ('observed_running', True), ('source_age_seconds', 0),
                           ('source_mtime', 95), ('mtime', 95), ('state_observed_at', 95),
                           ('generation', 'new'), ('control_id', 'old'), ('actions', ['start'])):
            with self.subTest(key=key, value=value):
                row = self.placeholder(); row[key] = value
                self.nodes[0]['telemetry']['simulations'] = [row]
                self.assertFalse(self.run_reconcile()['resolved'])
                self.assertEqual(self.row()[0], 'open')
        row = self.placeholder(); del row['source_age_seconds']
        self.nodes[0]['telemetry']['simulations'] = [row]
        self.assertFalse(self.run_reconcile()['resolved'])
        self.nodes[0]['telemetry']['simulations'] = [self.placeholder(), self.placeholder()]
        self.assertFalse(self.run_reconcile()['resolved'])

    def test_missing_or_stale_observations_are_not_absence_evidence(self):
        for nodes in ([], [dict(id='win2', last_seen=-30, telemetry=dict(simulations=[]))],
                      [dict(id='win2', last_seen=95, telemetry={})]):
            self.nodes = nodes
            self.assertFalse(self.run_reconcile()['resolved'])

    def test_manifest_hash_scope_or_authority_mismatch_blocks(self):
        original = copy.deepcopy(self.entry)
        for field, value in (('manifest_sha256', '0'*64), ('authority', 'NOT_TRANSFERRED')):
            self.entry = copy.deepcopy(original)
            self.entry['authorization'][field] = value
            self.assertFalse(self.run_reconcile()['resolved'])
        self.entry = original
        self.entry['expected_identities'] = ['a']
        self.assertFalse(self.run_reconcile()['resolved'])

    def test_retirement_evidence_must_be_exact_and_after_grant(self):
        original = copy.deepcopy(self.entry)
        for field, value in (('entry_disabled', False), ('generation_stopped', False), ('task', 'other'),
                             ('node', 'win3'), ('observed_at', 29), ('evidence', '')):
            self.entry = copy.deepcopy(original); self.entry['retirement'][field] = value
            self.assertFalse(self.run_reconcile()['resolved'])

    def test_missing_result_running_attempt_or_ambiguous_success_blocks(self):
        original = copy.deepcopy(self.jobs)
        self.jobs[0]['has_result'] = False
        self.assertFalse(self.run_reconcile()['resolved'])
        for status in ('running', 'queued', 'succeeded'):
            self.jobs = copy.deepcopy(original) + [dict(original[0], id='duplicate', status=status)]
            self.assertFalse(self.run_reconcile()['resolved'])
        self.jobs = original[:1]
        self.assertFalse(self.run_reconcile()['resolved'])

    def test_other_batch_owner_or_extra_identity_blocks(self):
        original = copy.deepcopy(self.jobs)
        self.jobs.append(dict(original[0], id='other-owner', batch_id='other'))
        self.assertFalse(self.run_reconcile()['resolved'])
        self.jobs = original + [dict(original[0], id='extra', spec=dict(original[0]['spec'], science_identity='c'))]
        self.assertFalse(self.run_reconcile()['resolved'])

    def test_execution_and_publisher_failures_remain_open(self):
        for owner, code in (('center', 'execution_failed'), ('center', 'memory_pressure'), ('publisher', 'missing_input')):
            self.db.execute('UPDATE task_issues SET data=?', (json.dumps(dict(owner=owner, code=code)),))
            self.assertFalse(self.run_reconcile()['resolved'])
            self.assertEqual(self.row()[0], 'open')

    def test_duplicate_or_malformed_registration_fails_before_any_resolution(self):
        for config in (dict(schema_version=1, handoffs=[self.entry, self.entry]),
                       dict(schema_version=1, handoffs=[self.entry, None]), []):
            self.path.write_text(json.dumps(config))
            result = handoff_resolution.reconcile(self.db, self.path, self.jobs, self.nodes, 100)
            self.assertFalse(result['resolved'])
            self.assertTrue(result['skipped'])
            self.assertEqual(self.row()[0], 'open')

    def test_later_incident_cannot_reuse_old_handoff_authorization(self):
        self.assertTrue(self.run_reconcile()['resolved'])
        self.db.execute("UPDATE task_issues SET state='open',first_seen=90,resolved_at=NULL")
        self.assertFalse(self.run_reconcile()['resolved'])
        self.assertEqual(self.row()[0], 'open')

    def test_server_heartbeat_resolves_without_page_and_requires_stored_result(self):
        from server import create_app
        root = Path(self.temp.name)
        config = root/'config.json'
        config.write_text(json.dumps(dict(data_dir=str(root/'center-data'), session_secret='test',
            password_hash='unused', secure_cookie=False, nodes=[dict(id='win2', label='Win2',
            enabled=True, token_hash=hashlib.sha256(b'node-token').hexdigest())])))
        self.path.write_text(json.dumps(dict(schema_version=1, handoffs=[self.entry])))
        app = create_app(config)
        result_path = root/'stored-result.zip'
        with app.center_db() as c:
            c.execute('INSERT INTO task_issues VALUES(?,?,?,?,?,?,?)',
                      ('win2', 'c:/old/state.json', 'open', 10, 20, None, json.dumps(self.prior)))
            for job in self.jobs:
                c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,result) VALUES(?,?,?,?,?,?,?,?)',
                          (job['id'], job['batch_id'], json.dumps(job['spec']), 'fixture.zip',
                           job['status'], 40, 50, str(result_path)))
        client = app.test_client()
        def heartbeat():
            response = client.post('/agent/win2/heartbeat',
                json=dict(telemetry=dict(simulations=[])),
                headers={'Authorization': 'Bearer node-token'})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        heartbeat()
        with app.center_db() as c:
            self.assertEqual(c.execute('SELECT state FROM task_issues').fetchone()[0], 'open')
        result_path.write_bytes(b'fixture stored result')
        heartbeat()
        with app.center_db() as c:
            state, data = c.execute('SELECT state,data FROM task_issues').fetchone()
            self.assertEqual(state, 'resolved')
            self.assertEqual(json.loads(data)['error_report'], self.prior['error_report'])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit WHERE action='legacy_handoff_resolved'").fetchone()[0], 1)
            c.execute("UPDATE task_issues SET state='open',first_seen=90,resolved_at=NULL")
        heartbeat()
        with app.center_db() as c:
            self.assertEqual(c.execute('SELECT state FROM task_issues').fetchone()[0], 'open')


if __name__ == '__main__':
    unittest.main()
