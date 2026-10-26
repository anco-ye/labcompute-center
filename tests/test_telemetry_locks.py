import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import runner,resource_policy
class TelemetryLocksTest(unittest.TestCase):
 def test_transient_telemetry_lock_is_nonfatal_and_next_sample_recovers(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d);error=PermissionError('reader lock');error.winerror=32;real=runner.atomic_json
   def locked(p,data):
    if p.name=='usage.json':raise error
    return real(p,data)
   with patch('runner.atomic_json',side_effect=locked):self.assertFalse(runner.publish_usage(folder,{'phase':'running'}))
   self.assertEqual(json.loads((folder/'telemetry-write-health.json').read_text())['status'],'retrying')
   self.assertTrue(runner.publish_usage(folder,{'phase':'running'}));self.assertEqual(json.loads((folder/'telemetry-write-health.json').read_text())['status'],'recovered')
 def test_disk_full_or_other_errors_are_not_hidden(self):
  with tempfile.TemporaryDirectory() as d,patch('runner.atomic_json',side_effect=OSError('disk full')):
   with self.assertRaises(OSError):runner.publish_usage(Path(d),{})
 def test_only_supervisor_usage_replace_errors_recover_with_cleanup_and_restart(self):
  detail="[WinError 5] Access denied: 'D:\\LabComputeWork\\jobs\\"+'a'*32+"\\usage.json."+'b'*32+".tmp' -> 'D:\\LabComputeWork\\jobs\\"+'a'*32+"\\usage.json'"
  report=dict(exit_code=-1,cleanup_verified=True,detail=detail);spec=dict(restart_policy='from_start')
  self.assertTrue(resource_policy.telemetry_write_failure(report));self.assertEqual(resource_policy.recovery(spec,report)['execution_retries'],1)
  for changes in [dict(exit_code=1),dict(cleanup_verified=False),dict(detail=detail.replace('usage.json','science.json')),dict(detail='exit=1 · '+detail)]:self.assertFalse(resource_policy.telemetry_write_failure(dict(report,**changes)))
  self.assertIsNone(resource_policy.recovery(dict(spec,restart_policy='never'),report));self.assertIsNone(resource_policy.recovery(dict(spec,execution_retries=2),report))

 def test_process_tree_reader_lock_is_nonfatal_but_not_false_recovered_by_usage(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d);error=PermissionError('reader lock');error.winerror=5;real=runner.atomic_json
   def locked(p,data):
    if p.name=='process_tree.json':raise error
    return real(p,data)
   with patch('runner.atomic_json',side_effect=locked):self.assertFalse(runner.publish_observation(folder,'process_tree.json',[dict(pid=123,create_time=456)]))
   self.assertTrue(runner.publish_usage(folder,{'phase':'running'}))
   self.assertEqual(json.loads((folder/'telemetry-write-health.json').read_text())['status'],'retrying')
   self.assertTrue(runner.publish_observation(folder,'process_tree.json',[dict(pid=123,create_time=456)]))
   self.assertEqual(json.loads((folder/'telemetry-write-health.json').read_text())['status'],'recovered')
 def test_process_tree_disk_full_remains_fatal(self):
  with tempfile.TemporaryDirectory() as d,patch('runner.atomic_json',side_effect=OSError('disk full')):
   with self.assertRaises(OSError):runner.publish_observation(Path(d),'process_tree.json',[])
