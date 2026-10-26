"""Issue actions use real center authentication and persistent SQLite tables."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_app
from issue_actions import register_routes


class IssueActionsAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        path = root/'config.json'
        path.write_text(json.dumps(dict(data_dir=str(root/'data'), session_secret='test',
            password_hash='unused', secure_cookie=False, nodes=[dict(id='win2', label='Win2',
                token_hash=hashlib.sha256(b'node-token').hexdigest(), enabled=True)])))
        self.app = create_app(path)
        # Parent integrates the module in create_app; allow the test to run both
        # before and after that integration without registering routes twice.
        if 'issue_detail' not in self.app.view_functions:
            def audit(c, action, subject):
                c.execute('INSERT INTO audit VALUES(?,?,?)', (1, action, subject))
            register_routes(self.app, self.app.center_db, audit)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(signed_in=True, csrf='csrf')
        self.report_id = hashlib.sha256(b'win2|task').hexdigest()[:24]
        self.payload = dict(owner='center', code='execution_failed', detail='unknown exit',
            error_report=dict(report_id=self.report_id, evidence=dict(pid=42), summary='unknown exit'))
        with self.app.center_db() as c:
            c.execute('INSERT INTO task_issues VALUES(?,?,?,?,?,?,?)',
                      ('win2', 'task', 'open', 10, 20, None, json.dumps(self.payload)))
        self.url = '/api/issues/'+self.report_id

    def tearDown(self):
        self.temp.cleanup()

    def post(self, **changes):
        data = dict(first_seen=10, request_id='feedback-1', message='我已检查输入，请中心核查进程。')
        data.update(changes)
        return self.client.post(self.url+'/feedback', json=data, headers={'X-CSRF-Token': 'csrf'})

    def test_feedback_reaches_the_durable_responsibility_inbox(self):
        self.assertEqual(self.post().status_code,201)
        data=self.client.get('/api/issues/reports?owner=center').json
        self.assertEqual(data['reports'][0]['feedback'][0]['message'],'我已检查输入，请中心核查进程。')

    def test_detail_and_report_include_evidence_with_safe_download_name(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['issue']['data'], self.payload)
        self.assertEqual(response.json['feedback'], [])
        report = self.client.get(self.url+'/report')
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.mimetype, 'application/json')
        self.assertIn('attachment;', report.headers['Content-Disposition'])
        self.assertIn('issue-'+self.report_id+'.json', report.headers['Content-Disposition'])
        self.assertEqual(report.json['report']['evidence'], dict(pid=42))

    def test_authentication_csrf_and_report_id_are_enforced(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(self.url).status_code, 401)
        self.assertEqual(anonymous.get(self.url+'/report').status_code, 401)
        self.assertEqual(self.client.post(self.url+'/feedback', json={}).status_code, 403)
        for value in ('not-a-report', 'a'*24, '..%2Fconfig.json'):
            self.assertEqual(self.client.get('/api/issues/'+value+'/report').status_code, 404)

    def test_feedback_is_persistent_idempotent_and_has_no_task_effect(self):
        first = self.post()
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json['recorded'])
        self.assertFalse(first.json['duplicate'])
        again = self.post()
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json['duplicate'])
        self.assertEqual(first.json['feedback'], again.json['feedback'])
        self.assertEqual(self.post(message='different').status_code, 409)
        with self.app.center_db() as c:
            row = c.execute('SELECT * FROM task_issues').fetchone()
            self.assertEqual(row['state'], 'open')
            self.assertEqual(json.loads(row['data']), self.payload)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM issue_feedback').fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit WHERE action='issue_feedback_recorded'").fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM science_commands').fetchone()[0], 0)

    def test_wrong_instance_rejected_and_old_feedback_not_mixed_into_new_incident(self):
        self.assertEqual(self.post(first_seen=9).status_code, 409)
        self.assertEqual(self.post().status_code, 201)
        with self.app.center_db() as c:
            c.execute('UPDATE task_issues SET first_seen=30')
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.client.get(self.url).json['feedback'], [])
        self.assertEqual(self.post(first_seen=30).status_code, 201)
        with self.app.center_db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM issue_feedback').fetchone()[0], 2)

    def test_closed_issue_accepts_note_without_reopening_or_claiming_repair(self):
        with self.app.center_db() as c:
            c.execute("UPDATE task_issues SET state='resolved',resolved_at=25")
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['issue_state'], 'resolved')
        detail = self.client.get(self.url).json
        self.assertEqual(detail['issue']['state'], 'resolved')
        self.assertEqual(detail['issue']['resolved_at'], 25)
        self.assertEqual(len(detail['feedback']), 1)

    def test_heartbeat_updates_do_not_erase_feedback(self):
        self.assertEqual(self.post().status_code, 201)
        heartbeat = self.client.post('/agent/win2/heartbeat',
            json=dict(telemetry=dict(simulations=[dict(id='task', lifecycle='failed', process_state='stopped')])),
            headers={'Authorization': 'Bearer node-token'})
        self.assertEqual(heartbeat.status_code, 200)
        detail = self.client.get(self.url).json
        self.assertEqual(detail['issue']['state'], 'open')
        self.assertEqual(len(detail['feedback']), 1)

    def test_message_and_instance_validation(self):
        for changes in (dict(message=''), dict(message='  '), dict(message='x'*2001),
                        dict(message=3), dict(message='a\x00b'), dict(first_seen=True),
                        dict(first_seen=float('nan')), dict(request_id='../x'), dict(request_id='')):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(**changes).status_code, 400)
        self.assertEqual(self.post(message='字'*2000).status_code, 201)


if __name__ == '__main__':
    unittest.main()
