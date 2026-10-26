import copy,time,unittest
import resource_policy,scheduler
from test_scheduler import SchedulingTest
class ActiveReservationsTest(unittest.TestCase):
 def fixture(self):
  now=time.time();model=resource_policy.workload_model({'family':'verified-buffer','features':{'cells':30,'segments':1000}},'code');model.update(adapter_version=3,additional_memory_gb=1)
  spec=dict(memory_gb=16,commit_gb=20,kernel_memory_required=True,resource_mode='estimated',restart_policy='from_start',cores=1,cpu_ids=[0],runtime='python',kind='python')
  usage=dict(at=now,phase='running',elapsed_seconds=120,rss_gb=1,private_gb=1.2,peak_gb=1.1,peak_private_gb=1.3)
  samples=[dict(id=str(i),model=model,observation=dict(cleanup_verified=True,peak_gb=1,peak_private_gb=1.2)) for i in range(3)]
  return now,model,spec,usage,samples
 def test_complete_evidence_changes_only_transient_reservation(self):
  now,model,spec,usage,samples=self.fixture();original=copy.deepcopy(spec);hold=resource_policy.running_reservation(spec,model,samples,usage,now)
  self.assertEqual(spec,original);self.assertEqual(hold['samples'],3);self.assertLess(hold['memory_gb'],spec['memory_gb']);self.assertGreaterEqual(hold['memory_gb'],3.5)
 def test_unknown_insufficient_stale_retried_or_no_kernel_limit_are_strict(self):
  now,model,spec,u,samples=self.fixture()
  self.assertIsNone(resource_policy.running_reservation(spec,None,samples,u,now));self.assertIsNone(resource_policy.running_reservation(spec,model,samples[:2],u,now));self.assertIsNone(resource_policy.running_reservation(spec,model,samples,dict(u,at=now-21),now))
  for change in [dict(resource_retries=1),dict(kernel_memory_required=False),dict(resource_mode='fixed'),dict(restart_policy='never')]:self.assertIsNone(resource_policy.running_reservation(dict(spec,**change),model,samples,u,now))
  stale=dict(spec,resource_calibration=dict(method='completed_scale_envelope',samples=10,sample_ids=['old']))
  self.assertIsNone(resource_policy.running_reservation(stale,model,[],u,now))
 def test_generic_numeric_model_needs_no_program_specific_adapter(self):
  now,_,s,u,_=self.fixture();m=resource_policy.workload_model({'family':'image-tiles','features':{'pixels':4096,'channels':3}},'unrelated-image-program');samples=[dict(id=str(i),model=m,observation=dict(cleanup_verified=True,peak_gb=1,peak_private_gb=1.2)) for i in range(3)];self.assertIsNotNone(resource_policy.running_reservation(s,m,samples,u,now))
 def test_observed_high_peak_cannot_be_reclaimed(self):
  now,m,s,u,samples=self.fixture();h=resource_policy.running_reservation(s,m,samples,dict(u,peak_gb=14,peak_private_gb=18),now);self.assertIsNone(h)
 def scheduler_fixture(self):
  now,m,s,u,samples=self.fixture();hold=resource_policy.running_reservation(s,m,samples,u,now);j=dict(id='active',node='a',status='running',spec=s,admission_reservation=hold)
  n=SchedulingTest().node(memory_total_gb=32,memory_available_gb=20,memory_commit_limit_gb=80,memory_committed_gb=50,memory_commit_verified=True,resource_observation=dict(at=now,verified=True,state='normal'),storage_observation=dict(at=now,managed=[dict(job='active',pid=10,created=1)]),usages={'active':u})
  return now,n,j
 def test_real_allocator_and_capacity_share_hold_but_keep_limits_and_cpu(self):
  now,n,j=self.scheduler_fixture();original=copy.deepcopy(j);old=scheduler.budget(n,[dict(j,admission_reservation=None)],now);new=scheduler.budget(n,[j],now)
  self.assertGreater(new['memory_gb'],old['memory_gb']);self.assertGreater(new['commit_gb'],old['commit_gb']);self.assertEqual(new['execution_limit_memory_gb'],16);self.assertEqual(new['learned_reservation_jobs'],1)
  q=SchedulingTest().job(memory_gb=8,commit_gb=9);p,_=scheduler.allocate(n,q['spec'],[j],now);self.assertIsNotNone(p);self.assertNotIn(0,p['cpu_ids']);self.assertEqual(j,original)
 def test_pressure_external_or_stale_identity_revokes_new_admission_only(self):
  for mode in ['pressure','stale','external','missing_process','unknown_commit']:
   now,n,j=self.scheduler_fixture();t=n['telemetry']
   if mode=='pressure':t['resource_observation']['state']='pressured'
   if mode=='stale':j['admission_reservation']['at']=now-20
   if mode=='external':t['external_busy']=True
   if mode=='missing_process':t['storage_observation']['managed']=[]
   if mode=='unknown_commit':t['memory_commit_verified']=False
   self.assertEqual(scheduler.reservation(j,n,now),j['spec']);self.assertEqual(j['status'],'running')

class ActiveReservationApiTest(unittest.TestCase):
 from test_center import CenterTest as _Base
 setUp=_Base.setUp
 tearDown=_Base.tearDown
 package=_Base.package
 submit=_Base.submit
 def test_live_admission_uses_owner_bound_completed_samples_without_editing_active_spec(self):
  import json
  from unittest.mock import patch
  from server import create_app
  p=self.root/'config.json';cfg=json.loads(p.read_text());cfg['active_resource_reservations']={'enabled':True};p.write_text(json.dumps(cfg));self.app=create_app(p);self.client=self.app.test_client()
  with self.client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
  ids=[self.submit().json['ids'][0] for _ in range(5)]
  now,m,s,u,samples=ActiveReservationsTest().fixture();m.pop('adapter_version');s.update(runtime_revision='revision',entry='main.py')
  with self.app.center_db() as c:
   for i,jid in enumerate(ids):
    r=json.loads(c.execute("SELECT spec FROM jobs WHERE id=?",(jid,)).fetchone()[0]);r.update(s)
    if i<3:
     import zipfile
     r['resource_observation']=samples[i]['observation'];result=self.root/('complete-'+str(i)+'.zip')
     with zipfile.ZipFile(result,'w') as z:z.writestr('exit.json',json.dumps(dict(samples[i]['observation'],exit_code=0,peak_complete=True)))
     c.execute('UPDATE jobs SET result=? WHERE id=?',(str(result),jid))
    if i==4:r.update(memory_gb=8,commit_gb=9)
    c.execute('UPDATE jobs SET spec=?,status=?,node=?,owner=? WHERE id=?',(json.dumps(r),'succeeded' if i<3 else 'running' if i==3 else 'queued','win2' if i==3 else None,'lab' if i!=2 else 'other',jid))
   # A strict 16 GiB active request blocks another task; a proven 3.5 GiB hold permits it.
   t=dict(max_parallel=7,cpu_threads=8,memory_total_gb=24,memory_available_gb=22,reserve_gb=4,memory_commit_limit_gb=80,memory_committed_gb=10,memory_commit_verified=True,resource_observation=dict(at=now,verified=True,state='normal'),storage_observation=dict(at=now,volumes=[dict(id='D',free_gb=500,reserve_gb=10)],default='D',managed=[dict(job=ids[3],pid=10,created=1)]),usages={ids[3]:u},active_ids=[ids[3]],runtimes={'python':dict(kind='python',revision='revision')})
   c.execute('UPDATE nodes SET telemetry=?,last_seen=? WHERE id=?',(json.dumps(t),now,'win2'));before=c.execute('SELECT spec FROM jobs WHERE id=?',(ids[3],)).fetchone()[0]
  with patch('resource_metadata.current_model',return_value=m):
   # Another owner's successful run cannot supply the third required profile.
   self.assertEqual(self.client.get('/api/state').json['nodes'][0]['capacity']['learned_reservation_jobs'],0)
   self.assertIsNone(self.client.post('/agent/win2/claim',headers=self.auth).json['job'])
   with self.app.center_db() as c:
    record=json.loads(c.execute('SELECT spec FROM jobs WHERE id=?',(ids[2],)).fetchone()[0]);record['runtime_revision']='different-runtime';c.execute('UPDATE jobs SET owner=?,spec=? WHERE id=?',('lab',json.dumps(record),ids[2]))
   self.assertEqual(self.client.get('/api/state').json['nodes'][0]['capacity']['learned_reservation_jobs'],0)
   with self.app.center_db() as c:
    record['runtime_revision']='revision';c.execute('UPDATE jobs SET spec=? WHERE id=?',(json.dumps(record),ids[2]))
   self.assertEqual(self.client.get('/api/state').json['nodes'][0]['capacity']['learned_reservation_jobs'],1)
   self.client.post('/agent/win2/claim',headers=self.auth)
   with self.app.center_db() as c:
    row=c.execute('SELECT status,spec FROM jobs WHERE id=?',(ids[4],)).fetchone();self.assertEqual(row['status'],'leased');self.assertFalse(json.loads(row['spec'])['work_layout_ready'])
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT spec FROM jobs WHERE id=?',(ids[3],)).fetchone()[0],before)
