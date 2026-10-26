import json,sqlite3,tempfile,unittest
from pathlib import Path
import importlib.util
path=Path(__file__).resolve().parents[1]/'deploy/activate_native_errors.py'
spec=importlib.util.spec_from_file_location('activation',path);activation=importlib.util.module_from_spec(spec);spec.loader.exec_module(activation)
class ActivationGuardTests(unittest.TestCase):
    def test_resumable_delivery_and_native_compute_do_not_block_web_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);results=root/'data/results';results.mkdir(parents=True)
            with sqlite3.connect(root/'data/center.sqlite3') as db:db.execute('CREATE TABLE jobs(status TEXT,delivery_state TEXT)')
            for node in ['a800-ssh','dell-r940']:
                p=root/'data/external-ssh'/node;p.mkdir(parents=True);(p/'active.json').write_text('{"jobs":[]}')
            self.assertEqual(activation.blockers(root),[])
            upload=results/'original.upload';upload.write_bytes(b'partial')
            self.assertEqual(activation.blockers(root),[]);upload.unlink()
            with sqlite3.connect(root/'data/center.sqlite3') as db:db.execute("INSERT INTO jobs VALUES('uploading','pending')")
            self.assertEqual(activation.blockers(root),[])
            with sqlite3.connect(root/'data/center.sqlite3') as db:db.execute("UPDATE jobs SET delivery_state='ready'")
            p=root/'data/external-ssh/a800-ssh/active.json';p.write_text('{"jobs":[{"id":"original"}]}')
            self.assertEqual(activation.blockers(root),[]);p.write_text('{"jobs":[]}')
            self.assertEqual(activation.blockers(root),[])
    def test_actual_legacy_writer_requires_prefix_handoff(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);results=root/'data/results';results.mkdir(parents=True)
            (results/'legacy.upload').write_bytes(b'prefix')
            with patch.object(activation,'idle_reader',side_effect=activation.BusyFile('writer')):
                self.assertEqual(activation.blockers(root),['legacy result upload writer active'])
            (results/'legacy.upload').rename(results/'job.resumable.upload')
            with patch.object(activation,'idle_reader',side_effect=activation.BusyFile('writer')):
                self.assertEqual(activation.blockers(root),[])
