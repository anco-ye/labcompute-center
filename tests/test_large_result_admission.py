"""Large declared outputs use configured admission without allocating test payloads."""
import json
import unittest
import test_center as fixture
from server import create_app


class LargeResultAdmissionTests(unittest.TestCase):
    setUp = fixture.CenterTest.setUp
    tearDown = fixture.CenterTest.tearDown
    submit = fixture.CenterTest.submit
    package = fixture.CenterTest.package
    heartbeat = fixture.CenterTest.heartbeat
    claim = fixture.CenterTest.claim

    def prepare(self, cap_mb):
        jid = self.submit().json['ids'][0]
        self.heartbeat()
        job = self.claim()
        with self.app.center_db() as db:
            row = db.execute('SELECT spec FROM jobs WHERE id=?', (jid,)).fetchone()
            spec = json.loads(row['spec'])
            spec['disk_gb'] = 600
            db.execute('UPDATE jobs SET spec=? WHERE id=?', (json.dumps(spec), jid))
        config_path = self.root / 'config.json'
        config = json.loads(config_path.read_text())
        config['max_result_mb'] = cap_mb
        config_path.write_text(json.dumps(config))
        self.app = create_app(config_path)
        self.client = self.app.test_client()
        return '/agent/win2/jobs/' + jid + '/finish-upload', {**self.auth, 'X-Lease': job['lease']}

    def test_accepts_over_64_gib_under_declared_budget(self):
        url, headers = self.prepare(601 * 1024)
        response = self.client.get(url, headers={**headers, 'X-Upload-Size': str(80 * 2**30)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['offset'], 0)
        self.assertEqual(response.json['size'], 80 * 2**30)

    def test_task_budget_still_limits_admission(self):
        url, headers = self.prepare(601 * 1024)
        self.assertEqual(self.client.get(url, headers={**headers, 'X-Upload-Size': str(602 * 2**30)}).status_code, 400)

    def test_default_sized_config_and_lease_remain_enforced(self):
        url, headers = self.prepare(64 * 1024)
        self.assertEqual(self.client.get(url, headers={**headers, 'X-Upload-Size': str(80 * 2**30)}).status_code, 400)
        self.assertEqual(self.client.get(url, headers={**headers, 'X-Lease': 'wrong', 'X-Upload-Size': '100'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
