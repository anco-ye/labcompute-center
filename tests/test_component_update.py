import importlib.util,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('worker_update',Path(__file__).parents[1]/'deploy/finish_worker_update.py')
update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)

class UpdateDecisionTests(unittest.TestCase):
    def test_stale_target_is_distinct_from_busy_worker(self):
        self.assertEqual(update.decision('old','new',0,{}),'target_mismatch')
        self.assertEqual(update.decision('new','new',1,{}),'draining')
        self.assertEqual(update.decision('new','new',0,{'job':{}}),'draining')
        self.assertEqual(update.decision('new','new',0,{}),'ready')
        self.assertEqual(update.decision(None,'new',0,{}),'no_update')
