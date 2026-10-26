from test_center import finish_clean
import io,json,sys,time,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_center import CenterTest
from werkzeug.security import generate_password_hash

class ProductClosureTest(unittest.TestCase):
 setUp=CenterTest.setUp
 tearDown=CenterTest.tearDown
 package=CenterTest.package
 batch_package=CenterTest.batch_package
 submit=CenterTest.submit
 heartbeat=CenterTest.heartbeat
 claim=CenterTest.claim
 def accounts(self):
  rows=[dict(id=x,username=x,password_hash=generate_password_hash('test'),role='user',enabled=True) for x in ('a','b')]
  (self.root/'users.json').write_text(json.dumps(rows))
  with self.client.session_transaction() as s:s.update(user_id='a')
  self.client.get('/api/submission-document/download')
  other=self.app.test_client()
  with other.session_transaction() as s:s.update(signed_in=True,user_id='b',csrf='csrf')
  return other
 def completed_result(self,peak_gb,peak_private_gb):
  path=self.root/'completed-resource-result.zip'
  report=dict(exit_code=0,status='succeeded',cleanup_verified=True,peak_complete=True,
              peak_gb=peak_gb,peak_private_gb=peak_private_gb)
  with zipfile.ZipFile(path,'w') as archive:
   archive.writestr('exit.json',json.dumps(report))
  return str(path)
 def test_owner_routes_and_isolation_admission(self):
  other=self.accounts();jid=self.submit().json['ids'][0]
  self.assertEqual(other.get('/api/state').json['jobs'],[])
  self.assertEqual(self.client.get('/api/state').json['jobs'][0]['actions'],['cancel'])
  for suffix in ('result','log','reviews'):
   self.assertIn(other.get('/api/jobs/'+jid+'/'+suffix).status_code,(403,404))
  self.assertEqual(other.post('/api/jobs/'+jid+'/cancel',headers=self.csrf).status_code,404)
  self.assertEqual(self.client.post('/api/jobs/'+jid+'/retry',headers=self.csrf).status_code,403)
  self.assertEqual(self.client.get('/operations').status_code,403)
  self.heartbeat();self.assertIsNone(self.claim())
  self.heartbeat(isolation_verified=True);self.assertEqual(self.claim()['id'],jid)
 def test_other_user_sees_real_load_without_task_details(self):
  other=self.accounts();jid=self.submit().json['ids'][0]
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='running',node='win2' WHERE id=?",(jid,))
  state=other.get('/api/state').json
  self.assertEqual(state['jobs'],[]);self.assertEqual(state['batches'],[])
  self.assertEqual(state['workload'],dict(active_units=1,queued_units=0))
  self.assertEqual(state['nodes'][0]['active_unit_count'],1)
  self.assertEqual(state['nodes'][0]['current_jobs'],[])
  self.assertEqual(state['account']['username'],'b')
  self.assertEqual(other.get('/api/jobs/'+jid+'/log').status_code,404)
  self.assertIn('当前账号：b',other.get('/').get_data(as_text=True))

 def test_cancel_ack_before_result_transfer_is_idempotent(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth=dict(self.auth,**{'X-Lease':job['lease']});base='/agent/win2/jobs/'+jid
  self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf)
  self.assertEqual(self.client.post(base+'/stopped',json={'cleanup_verified':False},headers=auth).status_code,409)
  for _ in range(2):self.assertEqual(self.client.post(base+'/stopped',json={'cleanup_verified':True},headers=auth).status_code,200)
  j=self.client.get('/api/state').json['jobs'][0];self.assertEqual(j['status'],'cancelled');self.assertFalse(j['has_result'])
  recover=self.heartbeat(active_ids=[]).json['recover'];self.assertEqual(recover[0]['id'],jid)
  self.assertFalse(self.client.post(base+'/update',json={'status':'uploading'},headers=auth).json['delivery_ready'])
  self.assertEqual(finish_clean(self.client,base+'/finish',data=self.package().read(),headers=dict(auth,**{'X-Result-Status':'succeeded'})).status_code,200)
  j=self.client.get('/api/state').json['jobs'][0];self.assertEqual(j['status'],'cancelled');self.assertTrue(j['has_result'])
  self.assertTrue(self.client.post(base+'/update',json={'status':'uploading'},headers=auth).json['delivery_ready'])
 def test_duplicate_retry_returns_same_attempt(self):
  jid=self.submit().json['ids'][0]
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='failed',finished=? WHERE id=?",(time.time(),jid))
  a=self.client.post('/api/jobs/'+jid+'/retry',headers=self.csrf).json
  b=self.client.post('/api/jobs/'+jid+'/retry',headers=self.csrf).json
  self.assertEqual(a['id'],b['id'])
 def test_uploads_isolated_and_idempotency_scoped(self):
  other=self.accounts();uid=self.client.post('/api/uploads',json={'name':'x.zip'},headers=self.csrf).json['id']
  self.assertEqual(other.post('/api/uploads/'+uid+'/cancel',headers=self.csrf).status_code,404)
  self.assertEqual(other.get('/api/state').json['uploads'],[])

 def test_batch_download_authorization_and_contents(self):
  other=self.accounts();jid=self.submit().json['ids'][0]
  target=self.root/'result.zip';target.write_bytes(self.package().read())
  with self.app.center_db() as c:c.execute("UPDATE jobs SET result=?,status='succeeded',finished=1 WHERE id=?",(str(target),jid))
  bid=self.client.get('/api/state').json['batches'][0]['id'];url='/api/batches/'+bid+'/result'
  self.assertEqual(other.get(url).status_code,404)
  raw_bid=self.client.get('/api/state').json['jobs'][0]['batch_id']
  self.assertEqual(self.client.get('/api/batches/'+raw_bid+'/result').status_code,200)
  self.assertEqual(other.get('/api/batches/'+raw_bid+'/result').status_code,404)
  response=self.client.get(url);self.assertEqual(response.status_code,200)
  with zipfile.ZipFile(io.BytesIO(response.data)) as z:self.assertIn(jid+'.zip',z.namelist())
  response.close()

 def test_batch_resume_and_head_preserve_owner_and_snapshot(self):
  other=self.accounts();jid=self.submit().json['ids'][0]
  target=self.root/'result.zip';target.write_bytes(self.package().read())
  with self.app.center_db() as c:c.execute("UPDATE jobs SET result=?,status='succeeded',finished=1 WHERE id=?",(str(target),jid))
  bid=self.client.get('/api/state').json['batches'][0]['id'];url='/api/batches/'+bid+'/result'
  response=self.client.get(url);content=response.data;etag=response.headers['ETag'];modified=response.headers['Last-Modified'];response.close()
  head=self.client.head(url);self.assertEqual(head.status_code,200);self.assertEqual(int(head.headers['Content-Length']),len(content));self.assertEqual(head.data,b'');head.close()
  for validator in (etag,modified):
   resumed=self.client.get(url,headers={'Range':'bytes=100-','If-Range':validator})
   self.assertEqual(resumed.status_code,206);self.assertEqual(resumed.data,content[100:]);self.assertEqual(resumed.headers['Content-Range'],f'bytes 100-{len(content)-1}/{len(content)}');resumed.close()
  self.assertEqual(other.head(url).status_code,404)
  self.assertEqual(other.get(url,headers={'Range':'bytes=0-10'}).status_code,404)
  bad=self.client.get(url,headers={'Range':f'bytes={len(content)}-'});self.assertEqual(bad.status_code,416);bad.close()
  # A new result changes the immutable snapshot; an old validator gets the complete new ZIP.
  target.write_bytes(self.package(text='new output').read())
  changed=self.client.get(url,headers={'Range':'bytes=100-','If-Range':etag})
  self.assertEqual(changed.status_code,200);self.assertNotEqual(changed.headers['ETag'],etag);changed.close()

 def test_scientific_review_disabled_by_default(self):
  from server import create_app
  config=json.loads((self.root/'config.json').read_text());config.pop('legacy_result_reviews',None)
  (self.root/'config.json').write_text(json.dumps(config));client=create_app(self.root/'config.json').test_client()
  with client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
  self.assertEqual(client.post('/api/jobs/any/review',json={},headers=self.csrf).status_code,410)

 def test_result_budget_is_separate_from_upload_limit(self):
  jid=self.submit(resources={'disk_gb':16}).json['ids'][0];self.heartbeat();job=self.claim()
  self.app.config['MAX_CONTENT_LENGTH']=128
  response=finish_clean(self.client,'/agent/win2/jobs/'+jid+'/finish',data=self.package().read(),headers={**self.auth,'X-Lease':job['lease'],'X-Result-Status':'succeeded'})
  self.assertEqual(response.status_code,200)

 def test_failed_dependency_has_terminal_response(self):
  tasks=[dict(id='parent',entry='main.py'),dict(id='child',entry='main.py',depends_on=['parent'])]
  ids=self.client.post('/api/jobs',data={'package':(self.batch_package(tasks),'batch.zip')},headers=self.csrf).json['ids']
  self.heartbeat();job=self.claim()
  finish_clean(self.client,'/agent/win2/jobs/'+job['id']+'/finish',data=self.package().read(),headers={**self.auth,'X-Lease':job['lease'],'X-Result-Status':'failed'})
  state=self.client.get('/api/state').json
  self.assertEqual(state['batches'][0]['queued'],0)
  self.assertEqual(next(j for j in state['jobs'] if j['id']==ids[1])['status'],'failed')

 def test_feedback_is_owned_idempotent_and_reply_visible(self):
  other=self.accounts();jid=self.submit().json['ids'][0];url='/api/jobs/'+jid+'/feedback'
  body=dict(request_id='same-request',message='结果下载遇到问题')
  self.assertEqual(other.get(url).status_code,404)
  for _ in range(2):response=self.client.post(url,json=body,headers=self.csrf);self.assertEqual(response.status_code,200)
  rows=response.json['feedback'];self.assertEqual(len(rows),1)
  self.assertEqual(self.client.get('/api/feedback').status_code,403)
  registry=json.loads((self.root/'users.json').read_text());registry.append(dict(id='admin',username='admin',role='admin',enabled=True,password_hash=registry[0]['password_hash']));(self.root/'users.json').write_text(json.dumps(registry))
  admin=self.app.test_client()
  with admin.session_transaction() as session:session.update(signed_in=True,user_id='admin',csrf='csrf')
  self.assertEqual(len(admin.get('/api/feedback').json['feedback']),1)
  reply=url+'/'+str(rows[0]['id'])+'/reply'
  self.assertEqual(self.client.post(reply,json={'reply':'伪造回复'},headers=self.csrf).status_code,403)
  self.assertEqual(admin.post(reply,json={'reply':'下载服务已修复'},headers=self.csrf).status_code,200)
  self.assertEqual(self.client.get(url).json['feedback'][0]['reply'],'下载服务已修复')

 def test_budget_calibration_is_bound_to_exact_input_owner_and_runtime(self):
  self.heartbeat(runtimes={'python':{'kind':'python','revision':'r1'}})
  jid=self.submit(resources={'mode':'estimated','memory_gb':32,'commit_gb':40},restart_policy='from_start').json['ids'][0]
  with self.app.center_db() as c:
   row=dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone());raw=Path(row['package']).read_bytes();spec=json.loads(row['spec'])
   spec.update(runtime_revision='r1',resource_observation=dict(cleanup_verified=True,peak_gb=2,peak_private_gb=3))
   c.execute("UPDATE jobs SET status='succeeded',finished=1,spec=? WHERE id=?",(json.dumps(spec),jid))
   c.execute("INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,finished,owner,result) VALUES(?,?,?,?,?,?,?,?,?,?)",('0'*32,row['batch_id'],json.dumps(spec),row['package'],'succeeded',2,2,2,row['owner'],self.completed_result(2,3)))
  def again():
   response=self.client.post('/api/jobs',data={'package':(io.BytesIO(raw),'same.zip')},headers=self.csrf)
   with self.app.center_db() as c:return json.loads(c.execute('SELECT spec FROM jobs WHERE id=?',(response.json['ids'][0],)).fetchone()[0])
  self.assertEqual(again()['memory_gb'],3.0)
  self.heartbeat(runtimes={'python':{'kind':'python','revision':'r2'}})
  self.assertGreater(again()['memory_gb'],32)
  self.accounts();self.heartbeat(runtimes={'python':{'kind':'python','revision':'r1'}})
  self.assertGreater(again()['memory_gb'],32)


 def test_pending_budget_adjustment_updates_existing_queue_only(self):
  from server import create_app
  self.heartbeat(runtimes={'python':{'kind':'python','status':'ready','revision':'r1'}})
  resource={'mode':'estimated','memory_gb':16,'commit_gb':24,'workload':{'family':'arrays-v1','features':{'items':100}}}
  first=self.submit(resources=resource,restart_policy='from_start').json['ids'][0]
  second=self.submit(resources=resource,restart_policy='from_start').json['ids'][0]
  with self.app.center_db() as c:
   c.execute("UPDATE jobs SET status='running',node='win2' WHERE id=?",(first,))
   row=dict(c.execute('SELECT * FROM jobs WHERE id=?',(first,)).fetchone());original=json.loads(row['spec'])
   source=dict(original,runtime_revision='r1',resource_observation=dict(cleanup_verified=True,peak_gb=1,peak_private_gb=1.5))
   c.execute("INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,finished,owner,result) VALUES(?,?,?,?,?,?,?,?,?,?)",('1'*32,row['batch_id'],json.dumps(source),row['package'],'succeeded',1,1,1,row['owner'],self.completed_result(1,1.5)))
  p=self.root/'config.json';cfg=json.loads(p.read_text());cfg['pending_resource_adjustment']={'enabled':True};cfg['kernel_memory_limits']=True;p.write_text(json.dumps(cfg))
  client=create_app(p).test_client()
  with client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
  s=client.get('/api/state').json;by_id={j['id']:j for j in s['jobs']}
  self.assertEqual(by_id[first]['spec']['memory_gb'],original['memory_gb'])
  self.assertLess(by_id[second]['spec']['memory_gb'],original['memory_gb'])
  self.assertEqual(by_id[second]['spec']['resource_calibration']['method'],'completed_scale_envelope')
  self.assertTrue(by_id[second]['spec']['kernel_memory_required'])
  self.assertEqual(by_id[second]['spec'],next(j for j in client.get('/api/state').json['jobs'] if j['id']==second)['spec'])

 def test_lab_admin_default_view_includes_other_owners(self):
  other=self.accounts();jid=self.submit().json['ids'][0]
  registry=json.loads((self.root/'users.json').read_text());registry.append(dict(id='legacy',username='lab',role='admin',enabled=True,password_hash=registry[0]['password_hash']));(self.root/'users.json').write_text(json.dumps(registry))
  admin=self.app.test_client()
  with admin.session_transaction() as session:session.update(signed_in=True,user_id='legacy',csrf='csrf')
  self.assertIn(jid,[j['id'] for j in admin.get('/api/state').json['jobs']])
  self.assertEqual(admin.get('/api/state?scope=mine').json['jobs'],[])
  self.assertEqual(other.get('/api/state?scope=all').json['jobs'],[])
  self.assertIn('全部计算任务',admin.get('/').get_data(as_text=True))

 def test_claim_rebalances_without_any_user_state_poll(self):
  from server import create_app
  self.heartbeat(runtimes={'python':{'kind':'python','status':'ready','revision':'r1'}},isolation_verified=True)
  resource={'mode':'estimated','memory_gb':16,'commit_gb':24,'workload':{'family':'generic-array','features':{'live_elements':100}}}
  jid=self.submit(resources=resource,restart_policy='from_start').json['ids'][0]
  with self.app.center_db() as c:
   row=dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone());original=json.loads(row['spec'])
   source=dict(original,runtime_revision='r1',resource_observation=dict(cleanup_verified=True,peak_gb=1,peak_private_gb=1.5))
   c.execute("INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,finished,owner,result) VALUES(?,?,?,?,?,?,?,?,?,?)",('2'*32,row['batch_id'],json.dumps(source),row['package'],'succeeded',1,1,1,row['owner'],self.completed_result(1,1.5)))
  p=self.root/'config.json';cfg=json.loads(p.read_text());cfg['pending_resource_adjustment']={'enabled':True};cfg['kernel_memory_limits']=True;p.write_text(json.dumps(cfg))
  client=create_app(p).test_client()
  response=client.post('/agent/win2/claim',json={},headers=self.auth)
  self.assertEqual(response.status_code,200);job=response.json['job']
  self.assertIsNotNone(job);self.assertEqual(job['id'],jid)
  self.assertEqual(job['spec']['resource_calibration']['method'],'completed_scale_envelope')
  self.assertLess(job['spec']['memory_gb'],original['memory_gb']);self.assertTrue(job['spec']['kernel_memory_required'])

 def test_delayed_phase_reports_cannot_regress_running_or_uploading(self):
  jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
  auth={**self.auth,'X-Lease':job['lease']};url='/agent/win2/jobs/'+jid+'/update'
  for status,expected in [('running','running'),('preparing','running'),('uploading','uploading'),('running','uploading'),('preparing','uploading')]:
   self.assertEqual(self.client.post(url,json=dict(status=status,detail=status),headers=auth).status_code,200)
   with self.app.center_db() as c:
    row=c.execute('SELECT status,detail FROM jobs WHERE id=?',(jid,)).fetchone()
   self.assertEqual(row['status'],expected);self.assertEqual(row['detail'],expected)
  self.assertEqual(row['status'],'uploading');self.assertEqual(row['detail'],'uploading')
  state=self.client.get('/api/state').json;events=next(j for j in state['jobs'] if j['id']==jid)['events']
  self.assertEqual([e['status'] for e in events if e['status'] in ('running','preparing','uploading')],['running','uploading'])

 def test_storage_handshake_precedes_legacy_worker_claim(self):
  self.heartbeat(isolation_verified=True)
  volumes=[dict(id='C',free_gb=1,total_gb=32,reserve_gb=1),dict(id='D',free_gb=100,total_gb=200,reserve_gb=4)]
  self.assertEqual(self.client.post('/agent/win2/storage',json=dict(volumes=volumes,default='C'),headers=self.auth).status_code,200)
  jid=self.submit().json['ids'][0]
  self.assertIsNone(self.claim())
  pending=self.client.post('/agent/win2/storage',json=dict(volumes=volumes,default='C'),headers=self.auth).json['jobs'];self.assertEqual(len(pending),1)
  job=pending[0];self.assertEqual(job['volume'],'D')
  self.assertEqual(self.client.post('/agent/win2/jobs/'+jid+'/storage-ready',json={'ready':True},headers={**self.auth,'X-Lease':'wrong'}).status_code,403)
  self.assertEqual(self.client.post('/agent/win2/jobs/'+jid+'/storage-ready',json={'ready':True},headers={**self.auth,'X-Lease':job['lease']}).status_code,200)
  self.assertEqual(self.claim()['id'],jid)

 def test_storage_creation_failure_requeues_without_starting_process(self):
  self.heartbeat(isolation_verified=True)
  volumes=[dict(id='C',free_gb=100,total_gb=200,reserve_gb=4),dict(id='D',free_gb=100,total_gb=200,reserve_gb=4)]
  body=dict(volumes=volumes,default='C');self.client.post('/agent/win2/storage',json=body,headers=self.auth)
  jid=self.submit().json['ids'][0];self.assertIsNone(self.claim())
  job=self.client.post('/agent/win2/storage',json=body,headers=self.auth).json['jobs'][0]
  self.client.post('/agent/win2/jobs/'+jid+'/storage-ready',json=dict(ready=False,detail='test unavailable'),headers={**self.auth,'X-Lease':job['lease']})
  with self.app.center_db() as c:row=c.execute('SELECT status,started,spec FROM jobs WHERE id=?',(jid,)).fetchone()
  self.assertEqual(row['status'],'queued');self.assertIsNone(row['started'])
  self.assertIsNone(self.claim());job=self.client.post('/agent/win2/storage',json=body,headers=self.auth).json['jobs'][0]
  self.assertEqual(job['volume'],'D')

 def test_verified_prelaunch_transfer_retry_is_bounded_and_idempotent(self):
  self.heartbeat(isolation_verified=True);jid=self.submit().json['ids'][0];job=self.claim()
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='failed',compute_started=0,execution_stopped=0,detail='File is not a zip file' WHERE id=?",(jid,))
  body=dict(volumes=[dict(id='C',free_gb=100,total_gb=200,reserve_gb=4)],default='C',prelaunch_failures=[jid])
  for _ in range(2):self.assertEqual(self.client.post('/agent/win2/storage',json=body,headers=self.auth).status_code,200)
  with self.app.center_db() as c:rows=c.execute('SELECT * FROM jobs').fetchall()
  self.assertEqual(next(r for r in rows if r['id']==jid)['execution_stopped'],1)
  self.assertEqual(len(rows),2);retry=next(r for r in rows if r['id']!=jid);self.assertEqual(retry['status'],'queued');self.assertEqual(json.loads(retry['spec'])['execution_retries'],1)
 def test_prelaunch_recovery_cannot_retry_a_scientific_failure(self):
  self.heartbeat(isolation_verified=True);jid=self.submit().json['ids'][0];self.claim()
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='failed',compute_started=1,execution_stopped=1,detail='File is not a zip file' WHERE id=?",(jid,))
  body=dict(volumes=[dict(id='C',free_gb=100,total_gb=200,reserve_gb=4)],default='C',prelaunch_failures=[jid])
  self.assertEqual(self.client.post('/agent/win2/storage',json=body,headers=self.auth).status_code,200)
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

del CenterTest
