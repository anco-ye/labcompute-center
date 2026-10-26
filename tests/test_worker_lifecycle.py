"""Control delivery and autonomous recovery; no scientific workload is started."""
import json,sys,tempfile,time,unittest,zipfile,os,psutil,urllib.error,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import Worker,apply_science_control,simulation_lifecycle,external_reservation,observed_affinity,observed_process
from common import atomic_json,worker_release,WORKER_FILES

class WorkerLifecycleTest(unittest.TestCase):
 def test_cross_user_child_is_counted_as_managed(self):
  from science_control import affinity_snapshot
  process=Mock();process.info={'name':'python.exe'};process.pid=42
  process.create_time.return_value=123;process.cpu_affinity.return_value=[0]
  process.username.return_value='HOST\\LCJ'+('a'*17)
  self.assertTrue(affinity_snapshot([(process,['python','-c','pass'])])['42']['managed'])
 def test_worker_release_changes_when_monitor_dependency_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for name in WORKER_FILES:
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
   original=worker_release(root);self.assertEqual(original,worker_release(root))
   (root/'simulation_monitor.py').write_text('changed monitor')
   self.assertNotEqual(original,worker_release(root))

 def test_privileged_affinity_observation_rejects_stale_snapshot_and_reused_pid(self):
  s={'at':time.time(),'affinities':{'42':{'created':123,'cpu_ids':[0,1]}}}
  self.assertEqual(observed_affinity(s,42,123),[0,1])
  s['affinities']['42']['managed']=True
  self.assertTrue(observed_process(s,42,123)['managed'])
  self.assertIsNone(observed_affinity(s,42,124))
  s['at']-=16;self.assertIsNone(observed_affinity(s,42,123))
 def test_external_affinity_union_avoids_duplicate_process_reservations(self):
  self.assertEqual(external_reservation([{'cpu_ids':[0,1]},{'cpu_ids':[0,1]},{'cpu_ids':[3,7]}],list(range(7))),([0,1,3],3))
  self.assertEqual(external_reservation([{'cpu_ids':[0]},{'cpu_ids':None}],list(range(7))),([],7))
  self.assertEqual(external_reservation([{'cpu_ids':list(range(8))}],list(range(7))), (list(range(7)),7))
  self.assertEqual(external_reservation([],list(range(7))),([],0))
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'jobs';self.root.mkdir()
  self.w=Worker.__new__(Worker);self.w.root=self.root;self.w.cfg={};self.w.background_controls=False;self.w.completed_ids=set();self.w.clock_offset=0
  self.job=dict(id='a'*32,lease='lease',spec={});self.w.state=self.job;self.w.states={self.job['id']:self.job};self.w.state_path=self.root/'active_jobs.json'
  self.w._states_lock=threading.RLock();self.w._delivery_pool=ThreadPoolExecutor(1);self.w._deliveries={};self.w._archive_future=None;self.w.last_response={}
  self.folder=self.root/self.job['id'];self.folder.mkdir();(self.folder/'work').mkdir();self.w.api=Mock(return_value={})
 def tearDown(self):self.w._delivery_pool.shutdown(wait=True);self.tmp.cleanup()
 def test_finished_isolated_job_does_not_return_to_preparing(self):
  self.job['spec']['requires_isolation']=True
  (self.folder/'launching.json').touch()
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  atomic_json(self.folder/'usage.json',dict(phase='finished'))
  self.w.reconcile([])
  update=self.w.api.call_args_list[-1]
  self.assertEqual(update.args[1]['status'],'running')
  self.assertIn('清理',update.args[1]['detail'])
  self.assertIn(self.job['id'],self.w.states)
 def test_dead_supervisor_settles_without_relaunch(self):
  with patch('agent.subprocess.Popen') as spawn:
   self.w.reconcile([])
   self.assertEqual(json.loads((self.folder/'exit.json').read_text())['status'],'failed');spawn.assert_not_called()
   self.w.reconcile([])
   self.assertEqual(self.w.states,{})
 def test_cancel_retains_partial_results_and_releases_local_slot(self):
  (self.folder/'work'/'partial.csv').write_text('preserved')
  (self.folder/'work'/'result.zip').write_bytes(b'user output, not the supervisor cache')
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  self.w.reconcile([self.job['id']])
  with zipfile.ZipFile(self.folder/'cancelled-result.zip') as z:
   self.assertEqual(z.read('work/partial.csv'),b'preserved')
   self.assertEqual(z.read('work/result.zip'),b'user output, not the supervisor cache')
  self.assertEqual(self.w.states,{})
  finish=next(c for c in self.w.api.call_args_list if c.args[0].endswith('/finish'))
  self.assertEqual(finish.kwargs['extra']['X-Result-Status'],'cancelled')
 def test_cancel_after_completion_reuses_original_archive_bytes(self):
  archive=self.folder/'result.zip'
  with zipfile.ZipFile(archive,'w') as z:z.writestr('work/output.bin',b'original')
  original=archive.read_bytes()
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done',cleanup_verified=True))
  self.w.reconcile([self.job['id']])
  finish=next(c for c in self.w.api.call_args_list if c.args[0].endswith('/finish'))
  self.assertEqual(finish.kwargs['upload'],archive)
  self.assertEqual(archive.read_bytes(),original)
  self.assertFalse((self.folder/'cancelled-result.zip').exists())
 def test_rejected_archive_is_retained_for_diagnosis(self):
  (self.folder/'result.zip').write_bytes(b'original archive')
  self.w.result_error(self.job,'invalid_archive','bad')
  retained=list(self.folder.glob('result.zip.rejected-*'))
  self.assertEqual(len(retained),1)
  self.assertEqual(retained[0].read_bytes(),b'original archive')
 def test_controls_written_without_reconcile_or_transfer_completion(self):
  self.w.telemetry=Mock(return_value={})
  self.w.api.return_value=dict(cancel=[self.job['id']],controls={self.job['id']:'pause'})
  self.w.sync_controls()
  self.assertTrue((self.folder/'cancel').exists());self.assertTrue((self.folder/'pause').exists())
 def test_native_completion_releases_compute_before_delivery_lane(self):
  self.w.telemetry=Mock(return_value={});self.w.api.return_value={}
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,cleanup_verified=True))
  self.w.sync_controls()
  stopped=[c for c in self.w.api.call_args_list if c.args[0].endswith('/stopped')]
  self.assertEqual(len(stopped),1);self.assertEqual(stopped[0].kwargs['extra']['X-Lease'],'lease')
  self.assertFalse((self.folder/'cancel').exists())
 def test_background_reconcile_does_not_undo_newer_control(self):
  self.w.background_controls=True;(self.folder/'pause').touch()
  atomic_json(self.folder/'launching.json',dict(at=time.time()))
  self.w.reconcile([],controls={self.job['id']:'run'})
  self.assertTrue((self.folder/'pause').exists());self.assertFalse((self.folder/'exit.json').exists())
 def test_cancel_during_package_download_never_launches_runner(self):
  self.w.runtimes={}
  def api(path,**kw):
   if path.endswith('/package'):
    with zipfile.ZipFile(kw['download'],'w') as z:z.writestr('main.py','raise Exception()')
    (self.folder/'cancel').touch()
  self.w.api=api
  with patch('agent.subprocess.Popen') as spawn:
   self.w.prepare(self.job);spawn.assert_not_called()
  self.assertEqual(json.loads((self.folder/'exit.json').read_text())['status'],'cancelled')
 def test_clock_offset_applies_only_once(self):
  self.w.clock_offset=120;self.job['spec']['deadline']=1000
  self.w.local_deadline(self.job);self.w.local_deadline(self.job)
  self.assertEqual(self.job['spec']['deadline'],1120)
 def test_service_restart_attaches_live_supervisor_without_relaunch(self):
  atomic_json(self.folder/'runner.json',dict(pid=os.getpid(),create_time=psutil.Process().create_time()))
  atomic_json(self.folder/'usage.json',dict(phase='running'))
  with patch('agent.subprocess.Popen') as spawn:
   self.w.reconcile([]);spawn.assert_not_called()
  self.assertFalse((self.folder/'exit.json').exists())
  self.assertEqual(self.w.api.call_args.args[1]['status'],'running')
 def test_recovery_never_kills_reused_pid(self):
  atomic_json(self.folder/'child.json',dict(pid=os.getpid(),create_time=psutil.Process().create_time()-100))
  with patch('agent.kill_tree') as kill:
   self.w.reconcile([]);kill.assert_not_called()
 def test_missing_usage_does_not_regress_running_to_preparing(self):
  atomic_json(self.folder/'runner.json',dict(pid=os.getpid(),create_time=psutil.Process().create_time()))
  atomic_json(self.folder/'usage.json',dict(phase='running'))
  self.w.reconcile([]);self.w.api.reset_mock()
  from common import read_json_shared
  original=read_json_shared
  def transient(path,*args,**kw):
   if path.name=='usage.json':raise PermissionError('replacement in progress')
   return original(path,*args,**kw)
  with patch('agent.read_json_shared',side_effect=transient):self.w.reconcile([])
  self.w.api.assert_not_called()
  atomic_json(self.folder/'usage.json',dict(phase='paused'))
  self.w.reconcile([]);self.assertEqual(self.w.api.call_args.args[1]['status'],'paused')
 def test_secondary_archive_failure_does_not_hold_compute_slot(self):
  self.w.cfg['archive_dir']=str(self.root/'secondary')
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  self.w.reconcile([])
  self.assertEqual(self.w.states,{})
  with patch('agent.shutil.copy2',side_effect=OSError('disk full')):self.w.archive_one()
  pending=list((self.root/'pending-archive').glob('*.json'));self.assertEqual(len(pending),1)
  self.assertGreater(json.loads(pending[0].read_text())['next_try'],time.time())
 def test_lost_completion_ack_does_not_reupload_result(self):
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  self.w.api.return_value={'finished':True,'delivery_ready':True};self.w.reconcile([])
  self.assertFalse(any(c.args[0].endswith('/finish') for c in self.w.api.call_args_list))
  self.assertEqual(self.w.states,{})
 def test_legacy_process_confirmation_preserves_completed_batches(self):
  done=dict(status='已完成');apply_science_control(done,dict(status='stopped'))
  self.assertEqual(done['status'],'已完成')
  waiting=dict(status='等待前序批次');apply_science_control(waiting,dict(status='running'))
  self.assertEqual(waiting['status'],'初始化中')
  stale=dict(status='进程状态待确认');apply_science_control(stale,dict(status='running'))
  self.assertEqual(stale['status'],'运行中')
  apply_science_control(stale,dict(status='stopped'))
  self.assertEqual(stale['status'],'已中断');self.assertIn('未完成',stale['failure_detail'])
  apply_science_control(stale,dict(status='stopped',actions=['start','cancel']))
  self.assertEqual(stale['status'],'已中断');self.assertEqual(simulation_lifecycle(stale),'failed')
  for label in ('初始化中','进程运行中，进度暂未更新'):
   sim=dict(status=label);apply_science_control(sim,dict(status='running'));self.assertEqual(sim['status'],label)
 def test_external_lifecycle_outcomes_do_not_depend_on_restart_button(self):
  for actions in ([],['start','cancel']):
   for status in ('运行中','初始化中','进程状态待确认','进程运行中，进度暂未更新'):
    sim=dict(status=status);apply_science_control(sim,dict(status='stopped',actions=actions))
    self.assertEqual(simulation_lifecycle(sim),'failed')
  for label,expected in [('已完成','succeeded'),('计算完成','succeeded'),('已取消','cancelled'),('已暂停','paused'),('等待前序批次','queued'),('状态文件暂不可读','unknown')]:
   self.assertEqual(simulation_lifecycle(dict(status=label)),expected)
 def test_utf8_results_are_read_independently_of_windows_locale(self):
  atomic_json(self.folder/'exit.json',dict(status='failed',exit_code=1,detail='内存不足，输出已保留'))
  read=Path.read_text
  def local_read(path,*args,**kw):return read(path,*args,**dict(encoding='gbk',**kw)) if 'encoding' not in kw else read(path,*args,**kw)
  with patch.object(Path,'read_text',local_read):self.w.reconcile([])
  self.assertEqual(self.w.states,{})
 def test_transfer_error_retains_durable_retry_and_outputs(self):
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  (self.folder/'work'/'answer.txt').write_text('42')
  def api(path,*a,**kw):
   if path.endswith('/finish'):raise urllib.error.HTTPError(path,413,'too large',{},None)
   return {}
  self.w.api=Mock(side_effect=api);self.w.reconcile([])
  self.assertIn(self.job['id'],self.w.states)
  self.assertTrue((self.folder/'delivery-retry.json').exists())
  self.assertEqual((self.folder/'work'/'answer.txt').read_text(),'42')
  self.assertNotIn(self.job['id'],self.w.completed_ids)
  self.assertTrue(any(c.args[0].endswith('/result-error') for c in self.w.api.call_args_list))
 def test_transient_network_error_keeps_automatic_retry(self):
  atomic_json(self.folder/'exit.json',dict(status='succeeded',exit_code=0,detail='done'))
  def api(path,*a,**kw):
   if path.endswith('/finish'):raise urllib.error.URLError('network unavailable')
   return {}
  self.w.api=Mock(side_effect=api)
  with self.assertRaises(urllib.error.URLError):self.w.reconcile([])
  self.assertIn(self.job['id'],self.w.states)
  self.assertFalse(any(c.args[0].endswith('/result-error') for c in self.w.api.call_args_list))
 def test_transfer_retry_repackages_without_launching_entry(self):
  atomic_json(self.folder/'exit.json',dict(status='cancelled',execution_status='succeeded',exit_code=0,detail='cancelled transfer'))
  (self.folder/'work'/'entry_once').write_text('original execution')
  (self.folder/'cancel').touch();(self.folder/'result.zip').write_bytes(b'broken cached archive')
  self.job['spec']['result_transfer_error']=True;self.job['control']='run'
  self.w.states={};self.w.last_response={'recover':[self.job]};self.w.background_controls=True
  with patch('agent.subprocess.Popen') as spawn:
   self.w.once()
   for future in self.w._deliveries.values():future.result(timeout=3)
   spawn.assert_not_called()
  self.assertEqual(self.w.states,{})
  with zipfile.ZipFile(self.folder/'result.zip') as z:
   self.assertEqual(z.read('work/entry_once'),b'original execution')
   self.assertEqual(json.loads(z.read('exit.json'))['status'],'succeeded')

class WorkerDispatchBurstTest(unittest.TestCase):
 def worker(self):
  w=Worker.__new__(Worker);tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);w.root=Path(tmp.name);w._states_lock=threading.RLock();w._delivery_pool=ThreadPoolExecutor(1);self.addCleanup(w._delivery_pool.shutdown,wait=True);w._deliveries={};w._archive_future=None;w.state=None;w.states={};w.completed_ids=set();w.background_controls=True;w.last_response={};w.cfg={'max_parallel':4};w.archive_one=Mock();w.reconcile=Mock()
  w.prepare=Mock(side_effect=lambda j:w.states.update({j['id']:j}));return w
 def test_fills_all_slots_without_waiting_for_next_poll(self):
  w=self.worker();w.api=Mock(side_effect=[{'job':{'id':str(i)}} for i in range(4)])
  w.once();self.assertEqual(len(w.states),4);self.assertEqual(w.api.call_count,4)
 def test_duplicate_or_empty_lease_stops_burst(self):
  for reply in ({'job':{'id':'one'}},{'job':None}):
   w=self.worker();w.api=Mock(side_effect=[{'job':{'id':'one'}},reply]);w.once()
   self.assertEqual(w.prepare.call_count,1);self.assertEqual(w.api.call_count,2)
 def test_prepare_failure_stops_further_claims(self):
  w=self.worker();w.api=Mock(return_value={'job':{'id':'one'}});w.prepare=Mock(side_effect=OSError('transfer failed'))
  with self.assertRaises(OSError):w.once()
  self.assertEqual(w.api.call_count,1)
