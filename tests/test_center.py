import hashlib,io,json,tempfile,unittest,zipfile,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import create_app
from common import inspect_zip
from werkzeug.security import generate_password_hash
def finish_clean(client,url,**kwargs):
 # Existing lifecycle fixtures represent a worker that finished cleanup.
 source=io.BytesIO(kwargs['data']);out=io.BytesIO()
 with zipfile.ZipFile(source) as before,zipfile.ZipFile(out,'w') as after:
  for entry in before.infolist():
   if entry.filename!='exit.json':after.writestr(entry,before.read(entry))
  report=json.loads(before.read('exit.json')) if 'exit.json' in before.namelist() else dict(exit_code=0)
  report.setdefault('exit_code',0);report.setdefault('status','succeeded');report['cleanup_verified']=True;after.writestr('exit.json',json.dumps(report))
 kwargs['data']=out.getvalue()
 return client.post(url,**kwargs)

class CenterTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  cfg=dict(legacy_result_reviews=True,data_dir=str(self.root/'data'),session_secret='testsecret',password_hash=generate_password_hash('test',method='pbkdf2:sha256:600000'),secure_cookie=False,nodes=[dict(id='win2',label='Win2',enabled=True,token_hash=hashlib.sha256(b'token').hexdigest())])
  (self.root/'config.json').write_text(json.dumps(cfg));self.app=create_app(self.root/'config.json');self.client=self.app.test_client()
  with self.client.session_transaction() as s:s.update(signed_in=True,csrf='csrf')
  self.auth={'Authorization':'Bearer token'};self.csrf={'X-CSRF-Token':'csrf'}
  self.client.get('/api/submission-document/download')
 def tearDown(self):self.tmp.cleanup()
 def package(self,name='main.py',text='print(42)',spec=None):
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w') as z:
   z.writestr(name,text)
   if spec is not None:z.writestr('manifest.json',json.dumps({'schema_version':1,'tasks':[spec]}))
  f.seek(0);return f
 def submit(self,**spec):
  s=dict(entry='main.py',kind='python',memory_gb=1,cores=1);s.update(spec)
  return self.client.post('/api/jobs',data={'package':(self.package(spec=s),'job.zip')},headers=self.csrf)
 def heartbeat(self,**kw):
  t=dict(cpu_threads=8,memory_total_gb=16,memory_available_gb=12,reserve_gb=4,runtimes={'python':{'kind':'python'}});t.update(kw)
  return self.client.post('/agent/win2/heartbeat',json={'telemetry':t},headers=self.auth)
 def claim(self):return self.client.post('/agent/win2/claim',headers=self.auth).json['job']
 def test_controller_sample_survives_worker_heartbeat_without_fake_liveness(self):
  self.heartbeat()
  before=self.client.get('/api/state').json['nodes'][0]['last_seen']
  row=dict(id='controller:batch',name='pending batch',lifecycle='queued',status='等待前序批次',process_state='running')
  self.assertEqual(self.client.post('/agent/win2/controller-observations',json={'items':[row]},headers=self.auth).status_code,200)
  node=self.client.get('/api/state').json['nodes'][0]
  self.assertEqual(node['last_seen'],before)
  self.heartbeat()
  node=self.client.get('/api/state').json['nodes'][0]
  self.assertEqual(node['telemetry']['simulations'][0]['lifecycle'],'queued')
  self.assertEqual(node['telemetry']['simulations'][0]['actions'],[])
 def test_controller_endpoint_requires_node_auth_and_valid_identity(self):
  self.assertEqual(self.client.post('/agent/win2/controller-observations',json={'items':[]}).status_code,401)
  self.assertEqual(self.client.post('/agent/win2/controller-observations',json={'items':[{'id':'arbitrary'}]},headers=self.auth).status_code,400)
 def test_terminal_evidence_is_authenticated_persistent_and_not_liveness(self):
  row=dict(id='C:/research/progress.json',name='test · weak',kind='network',lifecycle='failed',process_state='stopped',completed=100,total=5000,source_age_seconds=20)
  self.heartbeat(sampled_at=time.time(),simulations=[row])
  before=self.client.get('/api/state').json['nodes'][0]['last_seen']
  url='/agent/win2/terminal-evidence'
  proof=dict(id=row['id'],name=row['name'],completed=100,total=5000,source_age_seconds=20,failure_code='memory_pressure',failure_detail='guard stopped solver',failure_evidence={'pid':123})
  self.assertEqual(self.client.post(url,json={'items':[proof]}).status_code,401)
  self.assertEqual(self.client.post(url,json={'items':[{}]},headers=self.auth).status_code,400)
  self.assertEqual(self.client.post(url,json={'items':[proof]},headers=self.auth).status_code,200)
  node=self.client.get('/api/state').json['nodes'][0];self.assertEqual(node['last_seen'],before)
  self.assertEqual(node['telemetry']['simulations'][0]['failure_code'],'memory_pressure')
  self.heartbeat(sampled_at=time.time(),simulations=[row])
  issue=self.client.get('/api/issues?owner=center').json['issues'][0]
  self.assertEqual(issue['data']['code'],'memory_pressure')
  self.assertEqual(issue['data']['error_report']['evidence']['failure_evidence']['pid'],123)
  # A failed observer upload may omit the proof; the already verified incident
  # stays attributed until positive recovery, rather than reverting to unknown.
  self.client.post(url,json={'items':[]},headers=self.auth)
  self.heartbeat(sampled_at=time.time(),simulations=[row])
  issue=self.client.get('/api/issues?owner=center').json['issues'][0]
  self.assertEqual(issue['data']['code'],'memory_pressure')
  self.assertEqual(issue['data']['error_report']['evidence']['failure_evidence']['pid'],123)
 def test_resource_samples_use_age_not_remote_clock(self):
  for skew in (-3600,3600):
   now=time.time();sampled=now+skew
   response=self.heartbeat(sampled_at=sampled,scheduling_policy={'require_observation':True},
    resource_observation=dict(at=sampled-2,verified=True,state='normal'),
    usages={'fresh':dict(at=sampled-3),'stale':dict(at=sampled-90)})
   self.assertEqual(response.status_code,200)
   node=next(n for n in self.client.get('/api/state').json['nodes'] if n['id']=='win2')
   self.assertIsNone(node['capacity']['pressure_reason'])
   t=node['telemetry'];self.assertAlmostEqual(t['sampled_at']-t['resource_observation']['at'],2,places=2)
   self.assertAlmostEqual(t['sampled_at']-t['usages']['stale']['at'],90,places=2)
  self.heartbeat(sampled_at=sampled,resource_observation=dict(at=sampled-60,verified=True,state='normal'))
  node=next(n for n in self.client.get('/api/state').json['nodes'] if n['id']=='win2')
  self.assertIsNotNone(node['capacity']['pressure_reason'])
 def test_upload_lifecycle_in_single_queue(self):
  uid=self.client.post('/api/uploads',json={'name':'batch.zip'},headers=self.csrf).json['id']
  self.assertEqual(self.client.get('/api/state').json['uploads'][0]['status'],'receiving')
  spec=dict(entry='main.py',kind='python',resources={'cpus':1,'memory_gb':1})
  r=self.client.post('/api/uploads/'+uid+'/content',data={'package':(self.package(spec=spec),'batch.zip')},headers=self.csrf)
  self.assertEqual(r.status_code,201)
  state=self.client.get('/api/state').json
  self.assertEqual(state['uploads'],[]);self.assertEqual(len(state['jobs']),1);self.assertEqual(state['jobs'][0]['status'],'queued')
  self.assertEqual(self.client.post('/api/uploads/'+uid+'/content',data={'package':(self.package(spec=spec),'batch.zip')},headers=self.csrf).status_code,409)
 def test_cancelled_upload_cannot_enqueue(self):
  uid=self.client.post('/api/uploads',json={'name':'batch.zip'},headers=self.csrf).json['id']
  self.client.post('/api/uploads/'+uid+'/cancel',headers=self.csrf)
  self.assertEqual(self.client.post('/api/uploads/'+uid+'/content',headers=self.csrf).status_code,409)
  self.assertEqual(self.client.get('/api/state').json['jobs'],[])
 def test_batch_upload_control_and_retry_preserve_completed(self):
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w') as z:
   z.writestr('main.py','print(42)')
   z.writestr('manifest.json',json.dumps(dict(schema_version=1,name='完整实验批次',tasks=[dict(name='项'+str(i),entry='main.py',args=[str(i)]) for i in range(3)])))
  f.seek(0)
  ids=self.client.post('/api/jobs',data={'package':(f,'experiment.zip')},headers=self.csrf).json['ids']
  batch=self.client.get('/api/state').json['batches'][0]
  self.assertEqual((batch['name'],batch['total'],batch['queued']),('完整实验批次',3,3))
  base='/api/batches/'+batch['id']
  self.assertEqual(len(self.client.post(base+'/pause',headers=self.csrf).json['ids']),3)
  self.heartbeat();self.assertIsNone(self.claim())
  self.client.post(base+'/resume',headers=self.csrf)
  with self.app.center_db() as c:
   c.execute("UPDATE jobs SET status='succeeded',finished=1,result='preserved.zip' WHERE id=?",(ids[0],))
   c.execute("UPDATE jobs SET status='failed',finished=1 WHERE id=?",(ids[1],))
  retry=self.client.post(base+'/retry',headers=self.csrf).json['ids']
  self.assertEqual(len(retry),1)
  self.assertEqual(self.client.post(base+'/retry',headers=self.csrf).json['ids'],[])
  batch=self.client.get('/api/state').json['batches'][0]
  self.assertEqual((batch['total'],batch['completed'],batch['queued'],len(batch['attempts'])),(3,1,2,4))
  self.client.post(base+'/cancel',headers=self.csrf)
  with self.app.center_db() as c:
   row=c.execute('SELECT status,result FROM jobs WHERE id=?',(ids[0],)).fetchone()
   self.assertEqual(tuple(row),('succeeded','preserved.zip'))
 def test_migrated_science_groups_original_batches_and_attempts(self):
  import batches
  rows=[]
  for i,(batch,identity,status) in enumerate([('l2_r04_win2_pairfill_v7','a','failed'),('l2_r04_win2_pairfill_v7','a','running'),('l2_r04_win2_pairfill_v7','b','queued'),('l2_r04_win2_pairfill_v8','c','queued')]):
   rows.append(dict(id=str(i),batch_id='combined-zip',created=i,node='win4',status=status,spec=dict(name=identity,science_batch=batch,science_identity=identity)))
  groups=batches.summarize(rows)
  self.assertEqual(len(groups),2)
  self.assertEqual((groups[0]['total'],groups[0]['running'],groups[0]['queued'],groups[0]['failed']),(2,1,1,0))
  self.assertEqual(groups[0]['group'],'running');self.assertEqual(groups[1]['group'],'queued')
 def test_access(self):
  self.assertEqual(self.app.test_client().get('/api/state').status_code,401)
  self.assertEqual(self.client.post('/api/jobs').status_code,403)
  self.assertEqual(self.client.post('/agent/win2/claim').status_code,401)
 def test_cancel_wins_result_upload_race_and_events(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth=dict(self.auth,**{'X-Lease':job['lease']})
  self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'running'},headers=auth)
  self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf)
  finish_clean(self.client,'/agent/win2/jobs/'+jid+'/finish',data=self.package().read(),headers=dict(auth,**{'X-Result-Status':'succeeded'}))
  j=self.client.get('/api/state').json['jobs'][0]
  self.assertEqual(j['status'],'cancelled');self.assertTrue(j['has_result'])
  self.assertEqual([e['status'] for e in j['events']],['queued','leased','running','cancel_requested','cancelled'])
 def test_restart_reconciles_only_started_jobs(self):
  jid=self.submit().json['ids'][0];self.heartbeat(active_ids=[]);job=self.claim()
  self.client.post('/api/jobs/'+jid+'/pause',headers=self.csrf)
  self.assertEqual(self.heartbeat(active_ids=[]).json['recover'],[])
  self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'preparing'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
  recovery=self.heartbeat(active_ids=[]).json['recover']
  self.assertEqual(recovery[0]['id'],jid);self.assertEqual(recovery[0]['lease'],job['lease'])
 def test_worker_update_drains_then_automatically_restores(self):
  self.heartbeat(active_ids=[])
  release='a'*64
  self.client.post('/api/nodes/win2/update-worker',json={'release':release},headers=self.csrf)
  self.submit();self.assertIsNone(self.claim())
  self.heartbeat(active_ids=[],agent_release='old');self.assertIsNone(self.claim())
  self.heartbeat(active_ids=[],agent_release=release);self.assertIsNotNone(self.claim())
 def test_upload_failure_and_progress_are_not_cancellation(self):
  uid=self.client.post('/api/uploads',json={'name':'test.zip'},headers=self.csrf).json['id']
  self.client.post('/api/uploads/'+uid+'/progress',json={'percent':42},headers=self.csrf)
  self.assertIn('42%',self.client.get('/api/state').json['uploads'][0]['detail'])
  self.client.post('/api/uploads/'+uid+'/failed',headers=self.csrf)
  self.assertEqual(self.client.get('/api/state').json['uploads'][0]['status'],'failed')
 def test_installation_stage_remains_controllable(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'running'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
  self.heartbeat(active_ids=[jid],usages={jid:dict(stage='installing',package='example')})
  b=self.client.get('/api/state').json['batches'][0]
  self.assertEqual(b['phase'],'installing');self.assertEqual(b['phases'],{'installing':1})
  self.assertIn('pause',b['actions']);self.assertIn('cancel',b['actions'])
 def test_result_error_retry_keeps_job_identity_and_never_requeues_computation(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth=dict(self.auth,**{'X-Lease':job['lease']});base='/agent/win2/jobs/'+jid
  self.client.post(base+'/update',json={'status':'uploading'},headers=auth)
  self.assertEqual(self.client.post(base+'/result-error',json={'kind':'too_large'},headers=auth).status_code,200)
  s=self.client.get('/api/state').json;j=s['jobs'][0]
  self.assertEqual(j['status'],'uploading');self.assertTrue(j['spec']['result_transfer_error']);self.assertEqual(j['delivery_state'],'retrying')
  self.assertIsNone(j.get('result'));self.assertFalse(j['has_result'])
  s=self.client.get('/api/state').json;self.assertEqual(len(s['jobs']),1);self.assertEqual(s['jobs'][0]['status'],'uploading')
  recover=self.heartbeat(active_ids=[]).json['recover'];self.assertEqual(recover[0]['lease'],job['lease'])
  finish_clean(self.client,base+'/finish',data=self.package().read(),headers=dict(auth,**{'X-Result-Status':'succeeded'}))
  j=self.client.get('/api/state').json['jobs'][0]
  self.assertEqual(j['status'],'succeeded');self.assertNotIn('result_transfer_error',j['spec'])
 def test_cancel_rejected_result_can_still_finish(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf)
  r=self.client.post('/agent/win2/jobs/'+jid+'/result-error',json={'kind':'invalid_archive'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
  self.assertEqual(r.status_code,200)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'cancel_requested')
  # Transfer errors alone cannot prove that execution has stopped.
  self.client.post('/agent/win2/jobs/'+jid+'/stopped',json={'cleanup_verified':True},headers=dict(self.auth,**{'X-Lease':job['lease']}))
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'cancelled')
 def test_upload_and_lease(self):
  jid=self.submit().json['ids'][0];self.assertIsNone(self.claim());self.heartbeat(external_busy=True);self.assertIsNone(self.claim())
  self.heartbeat(memory_available_gb=4);self.assertIsNone(self.claim())
  self.heartbeat();job=self.claim();self.assertEqual(job['id'],jid);self.assertEqual(self.claim()['lease'],job['lease']);self.heartbeat(worker_busy=True);self.assertIsNone(self.claim())
  wrong=dict(self.auth,**{'X-Lease':'bad'});self.assertEqual(self.client.get('/agent/win2/jobs/'+jid+'/package',headers=wrong).status_code,403)
  auth=dict(self.auth,**{'X-Lease':job['lease']});path='/agent/win2/jobs/'+jid
  self.assertEqual(self.client.post(path+'/update',json={'status':'running'},headers=auth).status_code,200)
  self.assertEqual(finish_clean(self.client,path+'/finish',data=self.package().read(),headers=dict(auth,**{'X-Result-Status':'succeeded'})).status_code,200)
  self.assertTrue(self.client.post(path+'/update',json={'status':'uploading'},headers=auth).json['finished'])
  self.assertTrue(self.client.get('/api/state').json['jobs'][0]['has_result'])
  restored=create_app(self.root/'config.json')
  with restored.center_db() as db:self.assertEqual(db.execute('select status from jobs').fetchone()[0],'succeeded')
 def test_invalid_and_cancel(self):
  r=self.client.post('/api/jobs',data={'package':(self.package('../bad.py'),'job.zip'),'tasks':'[]'},headers=self.csrf);self.assertEqual(r.status_code,400)
  r=self.client.post('/api/jobs',data={'package':(io.BytesIO(b'bad'),'job.zip'),'tasks':'[]'},headers=self.csrf);self.assertEqual(r.status_code,400)
  jid=self.submit().json['ids'][0];self.assertEqual(self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf).status_code,200)
  self.heartbeat();self.assertIsNone(self.claim())
 def test_runtime_and_pause(self):
  self.submit(runtime='matlab');self.heartbeat();self.assertIsNone(self.claim())
  self.submit();self.client.post('/api/nodes/win2/enabled',json={'enabled':False},headers=self.csrf);self.assertIsNone(self.claim())
 def test_auto_resources_and_history(self):
  jid=self.submit(memory_gb=999,cores=256,estimated_minutes=999).json['ids'][0]
  self.heartbeat();job=self.claim();self.assertEqual(job['spec']['memory_gb'],4);self.assertEqual(job['spec']['cores'],1)
  self.assertIsNone(job['spec']['estimated_minutes'])
  self.heartbeat(worker_busy=True,usage={'elapsed_seconds':20,'progress':{'completed':2,'total':10}})
  self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'running'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
  state=self.client.get('/api/state').json['jobs'][0];self.assertEqual(state['estimate_source'],'实时进度');self.assertEqual(state['estimated_seconds'],100)
  result=self.package('exit.json',json.dumps({'elapsed_seconds':100,'peak_gb':.3}))
  finish_clean(self.client,'/agent/win2/jobs/'+jid+'/finish',data=result.read(),headers=dict(self.auth,**{'X-Lease':job['lease'],'X-Result-Status':'succeeded'}))
  self.heartbeat();self.submit();state=self.client.get('/api/state').json['jobs'][0]
  self.assertEqual(state['estimated_seconds'],100);self.assertEqual(state['estimate_source'],'历史实测')
 def test_parallel_slots_and_no_overlap(self):
  ids=[self.submit(resources={'cpus':2,'memory_gb':4}).json['ids'][0] for _ in range(3)]
  self.heartbeat(max_parallel=4,active_ids=[])
  a=self.claim()
  self.heartbeat(max_parallel=4,active_ids=[a['id']])
  b=self.claim()
  self.assertEqual({a['id'],b['id']},set(ids[:2]))
  self.assertFalse(set(a['spec']['cpu_ids'])&set(b['spec']['cpu_ids']))
  self.heartbeat(max_parallel=4,active_ids=[a['id'],b['id']],memory_available_gb=6)
  self.assertIsNone(self.claim())
 def test_runtime_verification_and_resources(self):
  self.submit(resources={'cpus':3,'memory_gb':6})
  self.heartbeat(runtimes={'python':{'kind':'python','status':'unverified'}})
  self.assertIsNone(self.claim())
  self.heartbeat(disk_free_gb=2);self.assertIsNone(self.claim())
  self.heartbeat(disk_free_gb=100);self.assertEqual(self.claim()['spec']['cores'],3)
  self.assertEqual(self.submit(resources={'cpus':-1}).status_code,400)
 def test_requirements_validation(self):
  self.assertEqual(self.submit(requirements='missing.txt').status_code,400)
  self.assertEqual(self.submit(requirements='../requirements.txt').status_code,400)
 def test_cancel_preparation(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth=dict(self.auth,**{'X-Lease':job['lease']})
  self.assertEqual(self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'preparing'},headers=auth).status_code,200)
  self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf)
  self.assertIn(jid,self.heartbeat().json['cancel'])
 def test_missing_manifest(self):
  r=self.client.post('/api/jobs',data={'package':(self.package(),'job.zip')},headers=self.csrf)
  self.assertEqual(r.status_code,400)
 def test_pages(self):
  self.assertEqual(self.client.get('/').status_code,200)
  self.assertEqual(self.app.test_client().get('/login').status_code,200)
 def test_queue_controls_priority_and_retry(self):
  first=self.submit(name='first').json['ids'][0];second=self.submit(name='second').json['ids'][0]
  action=lambda jid,a:self.client.post('/api/jobs/'+jid+'/'+a,headers=self.csrf)
  self.assertEqual(action(second,'pause').status_code,200)
  self.heartbeat();self.assertEqual(self.claim()['id'],first)
  self.assertEqual(action(second,'start').status_code,200)
  self.assertEqual(action(second,'cancel').status_code,200)
  new=action(second,'retry').json['id'];self.assertNotEqual(new,second)
  jobs={j['id']:j for j in self.client.get('/api/state').json['jobs']}
  self.assertEqual(jobs[second]['status'],'cancelled');self.assertEqual(jobs[new]['status'],'queued')
  self.assertEqual(action(first,'retry').status_code,409)
 def test_pause_resume_ack_and_cancel_redelivery(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth=dict(self.auth,**{'X-Lease':job['lease']});path='/agent/win2/jobs/'+jid+'/update'
  action=lambda a:self.client.post('/api/jobs/'+jid+'/'+a,headers=self.csrf)
  action('pause');self.assertEqual(self.heartbeat().json['controls'][jid],'pause')
  self.client.post(path,json={'status':'running'},headers=auth)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'pause_requested')
  self.client.post(path,json={'status':'paused'},headers=auth)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'paused')
  action('resume');self.client.post(path,json={'status':'paused'},headers=auth)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'resume_requested')
  self.client.post(path,json={'status':'running'},headers=auth)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'running')
  action('cancel');self.assertEqual(self.claim()['control'],'cancel')
  self.client.post(path,json={'status':'paused'},headers=auth)
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['status'],'cancel_requested')
 def test_science_command_acknowledgement(self):
  sim=dict(id='s',control_id='s',generation='g',actions=['pause','cancel'])
  self.heartbeat(simulations=[sim])
  url='/api/nodes/win2/simulations/control'
  r=self.client.post(url,json={'simulation':'s','action':'pause'},headers=self.csrf)
  self.assertEqual(r.status_code,200);cid=r.json['id']
  self.assertEqual(self.client.post(url,json={'simulation':'s','action':'cancel'},headers=self.csrf).status_code,409)
  self.assertEqual(self.heartbeat().json['science_commands'][0]['id'],cid)
  r=self.client.post('/agent/win2/heartbeat',json={'telemetry':{'simulations':[sim]},'science_results':[{'id':cid,'status':'failed','detail':'stale process'}]},headers=self.auth)
  self.assertIn(cid,r.json['science_ack']);self.assertEqual(r.json['science_commands'],[])
  self.assertEqual(self.client.get('/api/state').json['nodes'][0]['telemetry']['simulations'][0]['command']['detail'],'stale process')
 def batch_package(self,tasks):
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w') as z:z.writestr('main.py','print(42)');z.writestr('manifest.json',json.dumps(dict(schema_version=1,tasks=tasks)))
  f.seek(0);return f
 def test_publisher_retry_is_idempotent_and_rejects_changed_input(self):
  payload=self.batch_package([dict(id='a',entry='main.py')]).read();headers=dict(self.csrf,**{'Idempotency-Key':'publisher-run-1'})
  def send(data):return self.client.post('/api/jobs',data={'package':(io.BytesIO(data),'batch.zip')},headers=headers)
  first=send(payload);second=send(payload)
  self.assertEqual(first.status_code,201);self.assertEqual(second.status_code,200)
  self.assertEqual(first.json['ids'],second.json['ids']);self.assertTrue(second.json['duplicate'])
  changed=self.batch_package([dict(id='b',entry='main.py')]).read();self.assertEqual(send(changed).status_code,400)
  self.assertEqual(len(self.client.get('/api/state').json['jobs']),1)
 def test_dag_requires_success_and_freezes_result_attempt(self):
  tasks=[dict(id='prepare',entry='main.py'),dict(id='analyze',entry='main.py',depends_on=['prepare'])]
  ids=self.client.post('/api/jobs',data={'package':(self.batch_package(tasks),'batch.zip')},headers=self.csrf).json['ids']
  self.heartbeat(max_parallel=4);first=self.claim();self.assertEqual(first['id'],ids[0])
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='failed',finished=1 WHERE id=?",(ids[0],))
  self.assertIsNone(self.claim())
  queued=next(j for j in self.client.get('/api/state').json['jobs'] if j['id']==ids[1]);self.assertIn('前序任务失败',queued['waiting_reason'])
  retry=self.client.post('/api/jobs/'+ids[0]+'/retry',headers=self.csrf).json['id'];self.assertEqual(self.claim()['id'],retry)
  path=self.root/'upstream.zip';path.write_bytes(self.package(text='upstream output').read())
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='succeeded',finished=2,result=? WHERE id=?",(str(path),retry))
  child=self.claim();self.assertEqual(child['id'],ids[1]);self.assertEqual(child['spec']['dependency_jobs'],{'prepare':retry})
  auth=dict(self.auth,**{'X-Lease':child['lease']})
  response=self.client.get('/agent/win2/jobs/'+ids[1]+'/dependency/prepare',headers=auth)
  self.assertEqual(response.status_code,200);self.assertEqual(response.data,path.read_bytes());response.close()
  self.assertEqual(self.client.get('/agent/win2/jobs/'+ids[1]+'/dependency/arbitrary',headers=auth).status_code,404)
 def test_invalid_dependency_graph_rejected_before_enqueue(self):
  for tasks in ([dict(id='a',entry='main.py',depends_on=['missing'])],
                [dict(id='a',entry='main.py',depends_on=['b']),dict(id='b',entry='main.py',depends_on=['a'])],
                [dict(id='a',entry='main.py'),dict(id='a',entry='main.py')]):
   response=self.client.post('/api/jobs',data={'package':(self.batch_package(tasks),'batch.zip')},headers=self.csrf)
   self.assertEqual(response.status_code,400)
  self.assertEqual(self.client.get('/api/state').json['jobs'],[])

if __name__=='__main__':unittest.main()
