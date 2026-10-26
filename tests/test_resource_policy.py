from test_center import finish_clean
import copy,io,json,sys,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import resource_policy
import test_center

class ResourcePolicyTest(unittest.TestCase):
 def test_initial_headroom_is_applied_once_and_existing_budget_is_explicit(self):
  resources=dict(mode='estimated',memory_gb=48,commit_gb=64)
  spec=resource_policy.configure(dict(memory_gb=48,commit_gb=64),resources,'never')
  self.assertEqual((spec['memory_gb'],spec['commit_gb']),(63,83.75))
  self.assertEqual(resource_policy.configure(copy.deepcopy(spec),resources,'never'),spec)
  self.assertEqual(resource_policy.configure(copy.deepcopy(spec),dict(mode='estimated'),'never'),spec)
  budget=resource_policy.configure(dict(memory_gb=12,commit_gb=16),dict(mode='estimated',memory_basis='budget'),'never')
  self.assertEqual((budget['memory_gb'],budget['commit_gb']),(12,16))
  self.assertEqual(budget['resource_budget']['factor'],1)
  self.assertEqual(resource_policy.configure(copy.deepcopy(budget),dict(mode='estimated',memory_basis='budget'),'never'),budget)
  with self.assertRaises(ValueError):resource_policy.configure(dict(memory_gb=1,commit_gb=1),dict(mode='estimated',memory_basis='guess'),'never')
 def test_setup_retry_is_bounded_and_does_not_require_entry_replay(self):
  report=dict(failure_code='environment_setup',failure_evidence={'phase':'preparing'},cleanup_verified=True)
  original=dict(restart_policy='never')
  first=resource_policy.recovery(original,report);self.assertEqual(first['execution_retries'],1)
  second=resource_policy.recovery(first,report);self.assertIsNone(resource_policy.recovery(second,report))
  self.assertIsNone(resource_policy.recovery(original,dict(report,cleanup_verified=False)))
 def test_interruption_restarts_only_when_declared_safe(self):
  report=dict(failure_code='runner_interrupted',cleanup_verified=True)
  self.assertIsNone(resource_policy.recovery(dict(restart_policy='never'),report))
  self.assertEqual(resource_policy.recovery(dict(restart_policy='from_start'),report)['execution_retries'],1)
 def test_complete_identical_history_can_reduce_estimates(self):
  spec=dict(resource_mode='estimated',restart_policy='from_start',memory_gb=32,commit_gb=40)
  observation=dict(peak_gb=2,peak_private_gb=3,cleanup_verified=True)
  self.assertEqual(resource_policy.calibrate(spec,[dict(observation,peak_complete=False)]),spec)
  self.assertEqual(resource_policy.calibrate(spec,[observation])['memory_gb'],3)
  revised=resource_policy.calibrate(spec,[observation,observation]);self.assertEqual(revised['memory_gb'],3);self.assertEqual(revised['commit_gb'],4.5)
  report=dict(failure_code='resource_limit',failure_evidence=dict(limit='超过任务内存上限',phase='running',cleanup_verified=True,peak_gb=4.5,peak_private_gb=5))
  self.assertEqual(resource_policy.recovery(revised,report)['memory_gb'],6.5)
  self.assertEqual(resource_policy.calibrate(dict(spec,restart_policy='never'),[observation]*2)['memory_gb'],32)
 def test_estimate_and_bounded_recovery(self):
  s=resource_policy.configure(dict(memory_gb=1,commit_gb=2),{'mode':'estimated'},'from_start')
  self.assertEqual(s['resource_estimate'],dict(memory_gb=1,commit_gb=2));self.assertEqual(s['memory_gb'],2)
  report=dict(failure_code='resource_limit',failure_evidence=dict(limit='超过任务内存上限',phase='running',cleanup_verified=True,peak_gb=2.1,peak_private_gb=2.3))
  a=resource_policy.recovery(s,report);self.assertEqual(a['memory_gb'],4)
  b=resource_policy.recovery(a,report);self.assertEqual(b['memory_gb'],8);self.assertIsNone(resource_policy.recovery(b,report))
  for field,value in [('phase','preparing'),('cleanup_verified',False),('limit','超过最长运行时间'),('peak_private_gb',float('nan'))]:
   bad=copy.deepcopy(report);bad['failure_evidence'][field]=value;self.assertIsNone(resource_policy.recovery(s,bad))
  self.assertIsNone(resource_policy.recovery(dict(s,restart_policy='never'),report))
  self.assertIsNone(resource_policy.recovery(s,report,dict(max_memory_gb=3)))

class ResourceRecoveryApiTest(unittest.TestCase):
 setUp=test_center.CenterTest.setUp
 tearDown=test_center.CenterTest.tearDown
 package=test_center.CenterTest.package
 submit=test_center.CenterTest.submit
 heartbeat=test_center.CenterTest.heartbeat
 claim=test_center.CenterTest.claim
 def test_finish_replay_creates_one_attempt_and_success_closes_original(self):
  response=self.submit(resources=dict(mode='estimated',memory_gb=.25,commit_gb=.25),restart_policy='from_start');self.assertEqual(response.status_code,201)
  self.heartbeat();job=self.claim();jid=job['id'];self.assertEqual(job['spec']['memory_gb'],1)
  report=dict(status='failed',exit_code=-1,detail='超过任务内存上限',failure_code='resource_limit',failure_evidence=dict(limit='超过任务内存上限',phase='running',cleanup_verified=True,peak_gb=1.1,peak_private_gb=1.2))
  def finish(j,report,status):
   f=io.BytesIO()
   with zipfile.ZipFile(f,'w') as z:z.writestr('exit.json',json.dumps(report));z.writestr('work/partial.txt','preserved')
   return finish_clean(self.client,'/agent/win2/jobs/'+j['id']+'/finish',data=f.getvalue(),headers={**self.auth,'X-Lease':j['lease'],'X-Result-Status':status})
  self.assertEqual(finish(job,report,'failed').status_code,200)
  self.assertEqual(finish(job,report,'failed').status_code,200)
  state=self.client.get('/api/state').json;self.assertEqual(len(state['jobs']),2)
  nextjob=self.claim();self.assertNotEqual(nextjob['id'],jid);self.assertEqual(nextjob['spec']['unit_id'],jid);self.assertEqual(nextjob['spec']['resource_retries'],1)
  self.assertEqual(finish(nextjob,dict(status='succeeded',exit_code=0),'succeeded').status_code,200)
  state=self.client.get('/api/state').json;self.assertEqual(state['batches'][0]['completed'],1);self.assertFalse(state['issues'])


class ScaleBudgetTest(unittest.TestCase):
 def spec(self):return dict(memory_gb=32,commit_gb=40,resource_mode='estimated',restart_policy='from_start',resource_model={'family':'f','features':{'items':100}})
 def sample(self,items=100,peak=1,private=2,**kw):return dict(id='done',model={'family':'f','features':{'items':items}},observation=dict(cleanup_verified=True,peak_gb=peak,peak_private_gb=private,**kw))
 def test_complete_data_reduces_each_resource_and_is_idempotent(self):
  s=self.spec();r=resource_policy.fit_completed(s,[self.sample()]);self.assertEqual(r['memory_gb'],2.5);self.assertEqual(r['commit_gb'],4)
  self.assertEqual(resource_policy.fit_completed(r,[self.sample()]),r)
 def test_no_sample_or_wrong_family_does_not_guess(self):
  s=self.spec();self.assertEqual(resource_policy.fit_completed(s,[]),s)
  sample=self.sample();sample['model']['family']='other';self.assertEqual(resource_policy.fit_completed(s,[sample]),s)
 def test_model_scales_with_work_size_without_far_extrapolation(self):
  s=self.spec();r=resource_policy.fit_completed(s,[self.sample(items=50)]);self.assertEqual(r['memory_gb'],4);self.assertEqual(r['commit_gb'],7.25)
  self.assertEqual(resource_policy.fit_completed(s,[self.sample(items=20)]),s)
 def test_fixed_retry_and_never_are_not_reduced(self):
  for k,v in [('resource_mode','fixed'),('restart_policy','never'),('resource_retries',1)]:
   s=self.spec();s[k]=v;self.assertEqual(resource_policy.fit_completed(s,[self.sample()]),s)
 def test_legacy_trial_can_still_restore_original_on_limit(self):
  s=dict(self.spec(),memory_gb=44.75,commit_gb=55.75,resource_calibration={'original_memory_gb':52.5,'original_commit_gb':65.5})
  report=dict(failure_code='resource_limit',failure_evidence=dict(phase='running',cleanup_verified=True,limit='超过任务内存上限',peak_gb=45,peak_private_gb=46))
  r=resource_policy.recovery(s,report);self.assertIsNotNone(r);self.assertGreaterEqual(r['memory_gb'],52.5);self.assertLessEqual(r['memory_gb'],64)

 def test_kernel_denied_peak_drives_clean_retry_without_polling_peak(self):
  s=dict(self.spec(),memory_gb=1.25,commit_gb=1.25)
  report=dict(failure_code='resource_limit',failure_evidence=dict(phase='running',cleanup_verified=True,limit='超过任务提交内存上限',peak_gb=.1,peak_private_gb=.1,kernel_peak_commit_gb=2.4))
  r=resource_policy.recovery(s,report);self.assertGreaterEqual(r['memory_gb'],3.5);self.assertGreaterEqual(r['commit_gb'],r['memory_gb'])
 def test_workload_rejects_free_text_and_nonfinite_values(self):
  for value in ['please guess',{'family':'x','features':{'n':float('nan')}},{'family':'x','features':{'n':1},'method':'eval'}]:
   with self.assertRaises(ValueError):resource_policy.workload_model(value,['code'])

 def test_learned_budget_grows_from_failure_without_reverting_to_inflated_estimate(self):
  s=resource_policy.fit_completed(self.spec(),[self.sample()])
  report=dict(failure_code='resource_limit',failure_evidence=dict(phase='running',cleanup_verified=True,limit='超过任务内存上限',peak_gb=2.6,peak_private_gb=2.7))
  r=resource_policy.recovery(s,report);self.assertGreater(r['memory_gb'],s['memory_gb']);self.assertLess(r['memory_gb'],10)
