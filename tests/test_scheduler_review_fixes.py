"""Regression scenarios from the independent scheduler reviews."""
import copy,hashlib,io,json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import resource_metadata,resource_policy,scheduler,storage_layout,test_center,test_scheduler

class ReviewResourceTest(unittest.TestCase):
 def test_scale_reduction_preserves_fixed_workspace(self):
  m=resource_policy.workload_model(dict(family='loader',features=dict(rows=1)),'bundle')
  s=dict(resource_mode='estimated',restart_policy='from_start',memory_gb=16,commit_gb=20,resource_model=m)
  rows=[dict(id=str(i),model=dict(m,features=dict(rows=10)),observation=dict(cleanup_verified=True,peak_gb=8,peak_private_gb=8)) for i in range(3)]
  r=resource_policy.fit_completed(s,rows);self.assertGreaterEqual(r['memory_gb'],8*1.6+.75);self.assertEqual(s['memory_gb'],16)
 def test_changed_binary_changes_family_for_existing_specs(self):
  for ext in ('.pyd','.so','.sh','.jar','.class','.so.1'):
   with tempfile.TemporaryDirectory() as d:
    files=[]
    for n in (1,2):
     p=Path(d)/str(n);files.append(p)
     with zipfile.ZipFile(p,'w') as z:z.writestr('main.py','import helper');z.writestr('helper'+ext,bytes([n]))
    spec=dict(kind='python',runtime='python',entry='main.py',requirements=None,cores=1,resource_model=dict(label='data',family='old',features=dict(rows=1)))
    self.assertNotEqual(resource_metadata.current_model(files[0],spec)['family'],resource_metadata.current_model(files[1],spec)['family'],ext)
 def test_fit_cache_does_not_share_mutable_results(self):
  s=dict(memory_gb=16,commit_gb=20);a=resource_policy.fit_completed(s,[]);a['memory_gb']=1;self.assertEqual(resource_policy.fit_completed(s,[])['memory_gb'],16)
 def test_disk_reserves_only_unwritten_growth_and_stale_fails_closed(self):
  f=test_scheduler.SchedulingTest();now=time.time();n=f.node(work_volumes=[dict(id='D',free_gb=60,reserve_gb=0)],default_work_volume='D');j=f.job('active',disk_gb=100);j.update(status='running',node='a')
  n['telemetry']['storage_observation']=dict(at=now,disk_usage={'active':dict(at=now,verified=True,allocated_gb=80)})
  self.assertEqual(scheduler.disk_capacities(n,[j],now),[('D',40)])
  self.assertEqual(scheduler.disk_capacities(n,[j],now+61),[('D',-40)])
 def test_manual_retry_age_is_independent_of_automatic_failure_count(self):
  f=test_scheduler.SchedulingTest();old=f.job('old',1,task_key='unit');old.update(batch_id='batch',status='failed');retry=f.job('retry',100,task_key='unit',execution_retries=2,retry_origin='manual');retry['batch_id']='batch'
  self.assertEqual(scheduler.dependency_jobs([old,retry])[-1]['queue_created'],100)
  retry['spec'].pop('retry_origin');self.assertEqual(scheduler.dependency_jobs([old,retry])[-1]['queue_created'],1)
  retry['spec'].update(retry_origin='automatic',queue_origin_created=50);self.assertEqual(scheduler.dependency_jobs([old,retry])[-1]['queue_created'],50)
 def test_time_backfill_inherits_absolute_resource_deadline(self):
  f=test_scheduler.SchedulingTest();now=time.time();n=f.node();busy=f.job('busy',cores=4,wall_minutes=60);busy.update(status='running',node='a',compute_started=now)
  head=f.job('head',1,cores=7);short=f.job('short',2,cores=1,wall_minutes=1)
  # Fixture has seven schedulable CPUs after service reservation.
  p,_=scheduler.plan([n],[busy,head,short],now);self.assertNotIn('short',p);self.assertIsNone(scheduler.finish_bound(busy,now))

class AbsoluteBackfillTest(unittest.TestCase):
 def test_pause_cannot_extend_backfill_resource_occupancy(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'work').mkdir();(p/'pause').touch()
   spec=dict(kind='probe',runtime='fixture',cores=1,memory_gb=1,max_minutes=60,deadline=time.time()+1)
   (p/'launch.json').write_text(json.dumps(dict(spec=spec,argv=[sys.executable,'-c',"raise RuntimeError('must not start while paused')"])))
   process=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'runner.py'),str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   try:
    time.sleep(1.5);self.assertIsNone(process.poll());self.assertFalse((p/'child.json').exists())
    (p/'cancel').touch();process.wait(timeout=10)
    report=json.loads((p/'exit.json').read_text());self.assertEqual(report['status'],'cancelled');self.assertFalse((p/'child.json').exists())
   finally:
    if process.poll() is None:process.kill();process.wait()


class ReviewClosureTest(unittest.TestCase):
 setUp=test_center.CenterTest.setUp
 tearDown=test_center.CenterTest.tearDown
 package=test_center.CenterTest.package
 submit=test_center.CenterTest.submit
 heartbeat=test_center.CenterTest.heartbeat
 claim=test_center.CenterTest.claim
 def test_result_without_cleanup_cannot_free_resources_or_settle_dependencies(self):
  self.submit();self.heartbeat();job=self.claim();h={**self.auth,'X-Lease':job['lease'],'X-Result-Status':'succeeded'};route='/agent/win2/jobs/'+job['id']
  for report in (None,dict(exit_code=0,cleanup_verified=False)):
   data=self.package().getvalue() if report is None else self.package('exit.json',json.dumps(report)).getvalue()
   self.assertEqual(self.client.post(route+'/finish',data=data,headers=h).status_code,409)
   with self.app.center_db() as c:
    r=c.execute('SELECT status,execution_stopped,result FROM jobs WHERE id=?',(job['id'],)).fetchone();self.assertEqual(r['status'],'leased');self.assertFalse(r['execution_stopped']);self.assertFalse(r['result'])
  self.assertEqual(self.client.post(route+'/stopped',json=dict(cleanup_verified=True),headers=h).status_code,200)
  self.assertEqual(self.client.post(route+'/finish',data=self.package().getvalue(),headers=h).status_code,200)
 def test_expired_observer_falls_back_to_fresh_worker_progress(self):
  self.submit();self.heartbeat();job=self.claim();now=time.time();h={**self.auth,'X-Lease':job['lease']};self.client.post('/agent/win2/jobs/'+job['id']+'/update',json=dict(status='running'),headers=h)
  with self.app.center_db() as c:
   t=json.loads(c.execute("SELECT telemetry FROM nodes WHERE id='win2'").fetchone()[0]);t['usages']={job['id']:dict(at=now,phase='running',progress=dict(completed=7,total=10))};t['progress_observation']=dict(items={job['id']:dict(received_at=now-60,lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),progress=dict(completed=1,total=10))});c.execute("UPDATE nodes SET telemetry=? WHERE id='win2'",(json.dumps(t),))
  j=self.client.get('/api/state').json['jobs'][0];self.assertEqual(j['progress']['completed'],7)
 def test_learning_ignores_result_contents_and_parses_packages_outside_write_transaction(self):
  self.submit();self.heartbeat();job=self.claim()
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='succeeded',finished=1 WHERE id=?",(job['id'],))
  model=resource_metadata.current_model
  self.assertFalse(hasattr(resource_metadata,'register_completed'))
  def checked_model(*args):
   import sqlite3
   c=sqlite3.connect(self.root/'data/center.sqlite3',timeout=.01)
   try:c.execute('BEGIN IMMEDIATE');c.rollback()
   finally:c.close()
   return model(*args)
  self.submit()
  with patch('resource_metadata.current_model',side_effect=checked_model):
   self.client.get('/api/state');self.client.get('/api/state');self.claim()


 def test_lease_converts_backfill_marker_to_clock_adjusted_standard_deadline(self):
  self.submit(resources=dict(cpus=4,memory_gb=1,wall_minutes=60));self.heartbeat(max_parallel=10,cpu_pool=list(range(7)),memory_total_gb=128,memory_available_gb=120);busy=self.claim()
  h={**self.auth,'X-Lease':busy['lease']};self.client.post('/agent/win2/jobs/'+busy['id']+'/update',json=dict(status='running'),headers=h)
  self.submit(resources=dict(cpus=7,memory_gb=1));sid=self.submit(resources=dict(cpus=1,memory_gb=1,wall_minutes=1)).json['ids'][0]
  self.heartbeat(max_parallel=10,cpu_pool=list(range(7)),memory_total_gb=128,memory_available_gb=120,active_ids=[busy['id']]);self.assertIsNone(self.claim())
  # Once resources actually release, the next ordinary lease carries no deadline.
  with self.app.center_db() as c:c.execute("UPDATE jobs SET execution_stopped=1,status='succeeded',finished=? WHERE id=?",(time.time(),busy['id']))
  job=self.claim();self.assertIsNotNone(job);self.assertNotIn('deadline',job['spec']);self.assertNotIn('backfill_deadline',job['spec']);self.assertEqual(job['spec']['max_minutes'],0)
 def test_retry_drops_old_backfill_deadline_and_preserves_manual_queue_origin(self):
  s=dict(restart_policy='from_start',execution_retries=0,backfill_deadline=1,deadline=1,queue_origin_created=123)
  r=resource_policy.recovery(s,dict(failure_code='runner_interrupted',cleanup_verified=True));self.assertNotIn('deadline',r);self.assertNotIn('backfill_deadline',r);self.assertEqual(r['queue_origin_created'],123);self.assertEqual(r['retry_origin'],'automatic')


class StorageObserverLatencyTest(unittest.TestCase):
 def test_slow_disk_scan_does_not_block_fresh_pid_inventory(self):
  import storage_daemon,threading
  gate=threading.Event();storage_daemon._disk_future=None;storage_daemon._disk_started=0
  def slow(*args):gate.wait(2);return {}
  try:
   with patch('storage_layout.active_disk_usage',side_effect=slow):
    started=time.monotonic();storage_daemon.disk_usage_snapshot(Path('/unused'),{});self.assertLess(time.monotonic()-started,.2)
    gate.set();storage_daemon._disk_future.result(timeout=3)
  finally:gate.set();storage_daemon._disk_future=None


class IdentityObservationTest(unittest.TestCase):
 def test_recorded_identities_avoid_global_scans_and_reject_reused_pids(self):
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);jid='b'*32;folder=p/jid;folder.mkdir();(p/'active_jobs.json').write_text(json.dumps({jid:{}}))
   for name,data in [('runner.json',dict(pid=1,create_time=10)),('child.json',dict(pid=2,create_time=20)),('process_tree.json',[dict(pid=3,create_time=30)])]: (folder/name).write_text(json.dumps(data))
   def process(pid):
    m=Mock();m.pid=pid;m.create_time.return_value=99 if pid==3 else pid*10;m.children.side_effect=AssertionError('global scan forbidden');return m
   with patch('storage_layout.psutil.Process',side_effect=process):rows=storage_layout.managed_processes(p,{})
   self.assertEqual({r['pid'] for r in rows},{1,2})


class HousekeepingIsolationTest(unittest.TestCase):
 def test_slow_retired_acl_cleanup_does_not_block_directory_inventory(self):
  import storage_daemon,threading
  gate=threading.Event();storage_daemon._acl_future=None;storage_daemon._acl_started=0;storage_daemon._disk_future=None;storage_daemon._disk_started=time.time()
  def slow(*args):gate.wait(2)
  try:
   with patch('storage_layout.cleanup_volume_acls',side_effect=slow):
    started=time.monotonic();storage_daemon.disk_usage_snapshot(Path('/unused'),{});self.assertLess(time.monotonic()-started,.2)
    gate.set();storage_daemon._acl_future.result(timeout=3)
  finally:gate.set();storage_daemon._acl_future=None


class CleanupOwnershipTest(unittest.TestCase):
 def test_observer_never_kills_a_live_process_named_in_task_metadata(self):
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);jid='c'*32;folder=p/jid;folder.mkdir()
   for name,data in [('exit.json',dict(cleanup_verified=False)),('runner.json',dict(pid=1,create_time=10)),('child.json',dict(pid=2,create_time=20)),('process_tree.json',[])]: (folder/name).write_text(json.dumps(data))
   def process(pid):
    if pid==1:raise storage_layout.psutil.NoSuchProcess(pid)
    m=Mock();m.create_time.return_value=20;m.status.return_value='running';m.kill.side_effect=AssertionError('observer must not signal');return m
   with patch('storage_layout.psutil.Process',side_effect=process):self.assertFalse(storage_layout.confirm_finished_cleanup(p,jid,{}))
   self.assertFalse(json.loads((folder/'exit.json').read_text())['cleanup_verified'])


class SharedObservationReadTest(unittest.TestCase):
 def test_observation_handle_is_closed_before_json_parsing(self):
  import common
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'usage.json';common.atomic_json(p,dict(version=1));real=common.json.loads
   def read_while_replacing(data):
    common.atomic_json(p,dict(version=2));return real(data)
   with patch('common.json.loads',side_effect=read_while_replacing):
    self.assertEqual(common.read_json_shared(p),dict(version=1))
   # The new observation sees the replacement, never a partially written document.
   self.assertEqual(common.read_json_shared(p),dict(version=2))
