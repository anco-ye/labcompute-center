import re
import shutil
import tempfile
import unittest
from pathlib import Path
import test_center
from server import ui_fingerprint

class UiReleaseTest(unittest.TestCase):
    setUp = test_center.CenterTest.setUp
    tearDown = test_center.CenterTest.tearDown

    def test_customer_page_excludes_operator_workflow(self):
        customer = self.client.get('/').get_data(as_text=True)
        operations = self.client.get('/operations').get_data(as_text=True)
        self.assertIn('name="view" content="tasks"', customer)
        self.assertNotIn('id="attention"', customer)
        self.assertIn('id="issue-report"', customer)
        self.assertIn('name="view" content="operations"', operations)
        for page in (customer, operations):
            self.assertIn('id="results-dialog"', page)
            self.assertIn('id="results-close"', page)
            self.assertIn('id="sync-warning"', page)
        self.assertIn('id="attention"', operations)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/operations').status_code, 302)

    def test_loaded_document_assets_and_api_share_release(self):
        release = self.client.get('/api/state').json['ui_version']
        self.assertEqual(release, self.client.get('/api/ui-version').json['ui_version'])
        for route in ('/', '/operations', '/upload'):
            response = self.client.get(route)
            text = response.get_data(as_text=True)
            self.assertIn('name="ui-version" content="'+release+'"', text)
            self.assertIn('/static/app.js?v='+release, text)
            self.assertIn('/static/style.css?v='+release, text)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/api/ui-version').status_code, 401)

    def test_each_ui_component_changes_release_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            names=('static/app.js','static/style.css','static/brand.css','templates/index.html','templates/upload.html','templates/auth_base.html','templates/login.html')
            for name in names:
                (root/name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(self.app.root_path)/name,root/name)
            previous=ui_fingerprint(root)
            for name in names:
                with (root/name).open('a') as f:f.write('\n ')
                current=ui_fingerprint(root)
                self.assertNotEqual(current,previous)
                previous=current
