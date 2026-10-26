import copy,io,json,sys,tempfile,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import resource_policy,resource_metadata
class AllocationContractTest(unittest.TestCase):
 def result(self,root,**extra):
  path=root/'result.zip';report=dict(exit_code=0,cleanup_verified=True,peak_gb=2,peak_private_gb=3,**extra)
  with zipfile.ZipFile(path,'w') as z:z.writestr('exit.json',json.dumps(report))
  return path
 def test_partial_or_unverified_peaks_never_train_even_when_exit_zero(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder)
   p=self.result(root,peak_complete=False,kernel_memory=dict(verified=True));self.assertIsNone(resource_policy.final_observation({},p))
   p=self.result(root);self.assertIsNone(resource_policy.final_observation({},p))
 def test_explicit_full_and_native_legacy_final_records_are_accepted(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder)
   for extra in [dict(peak_complete=True),dict(kernel_memory=dict(verified=True)),dict(status='succeeded',started=10,finished=30,elapsed_seconds=19)]:
    ob=resource_policy.final_observation({},self.result(root,**extra));self.assertTrue(ob['peak_complete']);self.assertEqual(ob['peak_gb'],2)
 def test_one_exact_complete_sample_replaces_oversized_estimate_without_mutation(self):
  spec=dict(resource_mode='estimated',restart_policy='from_start',memory_gb=28,commit_gb=37)
  before=copy.deepcopy(spec);out=resource_policy.calibrate(spec,[dict(cleanup_verified=True,peak_complete=True,peak_gb=2,peak_private_gb=3)])
  self.assertEqual(spec,before);self.assertEqual((out['memory_gb'],out['commit_gb']),(3,4.5))
 def test_partial_sample_cannot_enlarge_or_reduce_exact_peak_cap(self):
  m=dict(family='same',features={'x':1},input_binding_sha256='same')
  rows=[dict(id='good',model=m,observation=dict(cleanup_verified=True,peak_complete=True,peak_gb=2,peak_private_gb=3)),dict(id='partial',model=m,observation=dict(cleanup_verified=True,peak_complete=False,peak_gb=200,peak_private_gb=300))]
  cap=resource_policy.completed_peak_cap(m,rows,dict(rss_gb=0,peak_gb=0,private_gb=0,peak_private_gb=0))
  self.assertEqual(cap['memory_gb'],3);self.assertEqual(cap['sample_ids'],['good'])
import test_center
class MemoryAllocationApiTest(unittest.TestCase):
 setUp=test_center.CenterTest.setUp
 tearDown=test_center.CenterTest.tearDown
 package=test_center.CenterTest.package
 submit=test_center.CenterTest.submit
 def test_queue_api_labels_request_limit_and_unconfirmed_estimate_separately(self):
  response=self.submit(resources=dict(mode='estimated',memory_basis='budget',memory_gb=2,commit_gb=3),restart_policy='never')
  self.assertEqual(response.status_code,201)
  a=self.client.get('/api/state').json['jobs'][0]['memory_allocation']
  self.assertEqual(a['reservation_memory_gb'],2);self.assertEqual(a['execution_limit_memory_gb'],2)
  self.assertFalse(a['peak_complete']);self.assertEqual(a['state'],'estimated_or_unconfirmed')

 def test_terminal_api_keeps_native_metrics_after_hiding_private_archive_path(self):
  jid=self.submit().json['ids'][0]
  path=self.root/'native.zip'
  with zipfile.ZipFile(path,'w') as z:z.writestr('exit.json',json.dumps(dict(exit_code=0,cleanup_verified=True,peak_complete=True,peak_gb=4,peak_private_gb=6)))
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='succeeded',finished=20,result=? WHERE id=?",(str(path),jid))
  j=self.client.get('/api/state').json['jobs'][0];a=j['memory_allocation']
  self.assertNotIn('result',j);self.assertIsNone(a['reservation_memory_gb']);self.assertTrue(a['peak_complete']);self.assertEqual(a['final_peak_gb'],4)
  spec=j['spec'];spec['execution_validation']=dict(failure_code='application_error')
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='failed',spec=? WHERE id=?",(json.dumps(spec),jid))
  a=self.client.get('/api/state').json['jobs'][0]['memory_allocation']
  self.assertFalse(a['peak_complete']);self.assertIsNone(a['final_peak_gb']);self.assertEqual(a['observed_peak_gb'],4);self.assertIsNone(a['reservation_memory_gb'])
