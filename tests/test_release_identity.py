import tempfile,unittest
from pathlib import Path
import release_identity

class RunningReleaseTests(unittest.TestCase):
    def test_disk_update_cannot_relabel_running_process(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'server.py';p.write_text('old')
            loaded=release_identity.snapshot(root,'web')
            p.write_text('new')
            reported=release_identity.status(root,loaded)
            self.assertEqual(reported['digest'],loaded['digest'])
            self.assertFalse(reported['disk_matches_loaded'])
            self.assertEqual(reported['changed_files'],['server.py'])
            self.assertNotEqual(release_identity.snapshot(root,'web')['digest'],loaded['digest'])

    def test_missing_source_is_visible_as_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'server.py';p.write_text('code')
            loaded=release_identity.snapshot(root,'web');p.unlink()
            self.assertFalse(release_identity.status(root,loaded)['disk_matches_loaded'])

class OnlineReleaseTests(unittest.TestCase):
    def test_health_and_admin_view_report_same_loaded_identity(self):
        from test_center import CenterTest
        fixture=CenterTest();fixture.setUp()
        try:
            public=fixture.client.get('/health').json['loaded_release']
            response=fixture.client.get('/api/components')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['web']['digest'],public['digest'])
            self.assertTrue(response.json['web']['disk_matches_loaded'])
            from unittest.mock import patch
            with patch('access_control.is_admin',return_value=False):
                self.assertEqual(fixture.client.get('/api/components').status_code,403)
        finally:fixture.tearDown()
