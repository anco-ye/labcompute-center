import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import Worker


class SupervisorFailureTest(unittest.TestCase):
    def test_supervisor_exception_is_reported_without_blind_restart_code(self):
        with tempfile.TemporaryDirectory() as temp:
            worker=Worker.__new__(Worker)
            worker.root=Path(temp)
            worker.background_controls=False
            job=dict(id='a'*32,spec=dict(requires_isolation=False))
            folder=worker.root/job['id'];folder.mkdir()
            (folder/'supervisor-error.json').write_text(json.dumps(dict(
                type='PermissionError',detail='cannot write usage.json')))
            worker.state=job
            worker.reconcile([],{})
            report=json.loads((folder/'exit.json').read_text())
            self.assertEqual(report['failure_code'],'supervisor_error')
            self.assertEqual(report['failure_evidence']['type'],'PermissionError')
            self.assertTrue(report['cleanup_verified'])
            self.assertIn('cannot write usage.json',report['detail'])


if __name__=='__main__':unittest.main()
