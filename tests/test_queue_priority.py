import copy,json,time,unittest
from pathlib import Path
from werkzeug.security import generate_password_hash
import scheduler,resource_metadata,test_center,test_scheduler

class PriorityPlannerTest(unittest.TestCase):
 def test_priority_then_fifo_for_unrelated_programs(self):
  factory=test_scheduler.SchedulingTest();node=factory.node(cpu_threads=2,max_parallel=1,cpu_pool=[0]);a=factory.job(id='older',created=1);b=factory.job(id='newer',created=2)
  a['spec'].update(name='image-analysis');b['spec'].update(name='fluid-solver');b['priority']=10
  self.assertEqual(set(scheduler.plan([node],[a,b])[0]),{'newer'})
  a['priority']=10;self.assertEqual(set(scheduler.plan([node],[b,a])[0]),{'older'})
  a['priority']=0;b['priority']=0;b['spec']['priority']=100
  self.assertEqual(set(scheduler.plan([node],[b,a])[0]),{'older'})
 def test_priority_does_not_preempt_running_or_bypass_dependencies(self):
  f=test_scheduler.SchedulingTest();n=f.node(cpu_threads=2,max_parallel=1,cpu_pool=[0]);active=f.job(id='live',cpu_ids=[0]);active.update(status='running',node='a');q=f.job(id='urgent');q['priority']=20
  original=copy.deepcopy(active);self.assertEqual(scheduler.plan([n],[active,q])[0],{});self.assertEqual(active,original)
  q['spec'].update(task_key='child',depends_on=['missing']);other=f.job(id='ready',created=2)
  placed,reasons=scheduler.plan([n],[q,other])
  self.assertEqual(placed,{})
  self.assertIn('最高优先级',reasons['ready'])
 def test_batch_display_uses_pending_priority_queue_age_not_upload_age(self):
  import batches
  jobs=[dict(id='old',batch_id='batch',created=1,status='queued',spec={'name':'old'},priority=0),dict(id='high',batch_id='batch',created=9,status='queued',spec={'name':'high'},priority=10)]
  b=batches.summarize(jobs)[0];self.assertEqual(b['created'],1);self.assertEqual(b['queue_created'],9);self.assertEqual(b['priority'],10)
 def test_generic_identical_input_fallback_never_merges_different_inputs(self):
  import tempfile
  with tempfile.TemporaryDirectory() as root:
   package=Path(root)/'program.zip';package.write_bytes(b'invalid adapter package')
   a=resource_metadata.reservation_model(package,{'resource_signature':'aaa'});b=resource_metadata.reservation_model(package,{'resource_signature':'bbb'})
   self.assertNotEqual(a['family'],b['family']);self.assertEqual(a['features'],{'identical_inputs':1})
   self.assertIsNone(resource_metadata.reservation_model(package,{}))

class PriorityApiTest(unittest.TestCase):
 setUp=test_center.CenterTest.setUp
 tearDown=test_center.CenterTest.tearDown
 package=test_center.CenterTest.package
 submit=test_center.CenterTest.submit
 heartbeat=test_center.CenterTest.heartbeat
 claim=test_center.CenterTest.claim
 def test_lower_node_waits_for_actual_highest_priority_lease_on_other_node(self):
  from server import create_app
  config=self.root/'config.json';cfg=json.loads(config.read_text())
  cfg['nodes'].append(dict(cfg['nodes'][0],id='win3',label='Win3'))
  config.write_text(json.dumps(cfg));self.app=create_app(config);self.client=self.app.test_client()
  with self.client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
  ordinary=self.submit().json['ids'][0];highest=self.submit(runtime='special').json['ids'][0]
  self.client.post('/api/jobs/'+highest+'/priority',json={'priority':20},headers=self.csrf)
  self.heartbeat(cpu_pool=[0],max_parallel=1)
  self.client.post('/agent/win3/heartbeat',json={'telemetry':dict(cpu_threads=2,cpu_pool=[0],memory_total_gb=16,memory_available_gb=12,reserve_gb=4,max_parallel=1,runtimes={'special':{'kind':'python'}})},headers=self.auth)
  self.assertIsNone(self.claim())
  leased=self.client.post('/agent/win3/claim',headers=self.auth).json['job']
  self.assertEqual(leased['id'],highest)
  self.assertEqual(self.claim()['id'],ordinary)
 def test_adjustment_changes_real_dispatch_without_editing_specs(self):
  a=self.submit().json['ids'][0];b=self.submit().json['ids'][0]
  with self.app.center_db() as c:original={r['id']:r['spec'] for r in c.execute('SELECT id,spec FROM jobs')}
  response=self.client.post('/api/jobs/'+b+'/priority',json={'priority':10},headers=self.csrf)
  self.assertEqual(response.status_code,200);self.assertEqual(response.json['ids'],[b])
  self.heartbeat(max_parallel=1);self.assertEqual(self.claim()['id'],b)
  with self.app.center_db() as c:
   self.assertEqual({r['id']:r['spec'] for r in c.execute('SELECT id,spec FROM jobs WHERE id=?',(a,))},{a:original[a]})
   self.assertTrue(c.execute("SELECT 1 FROM audit WHERE action='queue_priority'").fetchone())
  # A committed lease is immutable even if its solver has not started yet.
  self.assertEqual(self.client.post('/api/jobs/'+b+'/priority',json={'priority':20},headers=self.csrf).status_code,409)
 def test_batch_only_changes_pending_and_pause_resume_retry_keep_priority(self):
  ids=[self.submit().json['ids'][0] for _ in range(3)]
  with self.app.center_db() as c:
   bid=c.execute('SELECT batch_id FROM jobs WHERE id=?',(ids[0],)).fetchone()[0]
   c.execute('UPDATE jobs SET batch_id=?',(bid,));c.execute("UPDATE jobs SET status='running',node='win2',priority=3 WHERE id=?",(ids[0],));running=c.execute('SELECT spec FROM jobs WHERE id=?',(ids[0],)).fetchone()[0]
  state=self.client.get('/api/state').json;batch=next(b for b in state['batches'] if len(b['units'])==3)
  response=self.client.post('/api/batches/'+batch['id']+'/priority',json={'priority':20},headers=self.csrf)
  self.assertEqual(set(response.json['ids']),set(ids[1:]));self.client.post('/api/jobs/'+ids[1]+'/pause',headers=self.csrf);self.client.post('/api/jobs/'+ids[1]+'/resume',headers=self.csrf)
  with self.app.center_db() as c:
   self.assertEqual(c.execute('SELECT priority FROM jobs WHERE id=?',(ids[1],)).fetchone()[0],20)
   self.assertEqual(c.execute('SELECT spec FROM jobs WHERE id=?',(ids[0],)).fetchone()[0],running)
   self.assertEqual(c.execute('SELECT priority FROM jobs WHERE id=?',(ids[0],)).fetchone()[0],3)
   c.execute("UPDATE jobs SET status='failed',finished=? WHERE id=?",(time.time(),ids[1]))
  retried=self.client.post('/api/jobs/'+ids[1]+'/retry',headers=self.csrf).json['id']
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT priority FROM jobs WHERE id=?',(retried,)).fetchone()[0],20)
  jobs=self.client.get('/api/state').json['jobs'];old=next(j for j in jobs if j['id']==ids[1]);new=next(j for j in jobs if j['id']==retried)
  self.assertEqual(old['submitted_at'],new['submitted_at']);self.assertEqual(new['submitted_by'],'lab')
 def test_validation_csrf_and_user_authorization(self):
  jid=self.submit().json['ids'][0];url='/api/jobs/'+jid+'/priority'
  for value in [True,1.2,'10',101,-101,None]:self.assertEqual(self.client.post(url,json={'priority':value},headers=self.csrf).status_code,400)
  self.assertEqual(self.client.post(url,json={'priority':10}).status_code,403)
  rows=[dict(id='person',username='Alice',password_hash=generate_password_hash('x'),role='user',enabled=True)]
  (self.root/'users.json').write_text(json.dumps(rows))
  with self.app.center_db() as c:c.execute("UPDATE jobs SET owner='person' WHERE id=?",(jid,))
  with self.client.session_transaction() as session:session.update(user_id='person')
  state=self.client.get('/api/state').json;batch=state['batches'][0];self.assertFalse(state['jobs'][0]['can_set_priority']);self.assertEqual(state['jobs'][0]['submitted_by'],'Alice')
  self.assertEqual(self.client.post(url,json={'priority':10},headers=self.csrf).status_code,403)
  self.assertEqual(self.client.post('/api/batches/'+batch['id']+'/priority',json={'priority':10},headers=self.csrf).status_code,403)
  self.assertIn('name="admin" content="false"',self.client.get('/').text)
 def test_large_state_compression_is_negotiated_and_lossless(self):
  import gzip
  for _ in range(3):self.submit()
  identity=self.client.get('/api/state');compressed=self.client.get('/api/state',headers={'Accept-Encoding':'gzip'})
  self.assertEqual(compressed.headers.get('Content-Encoding'),'gzip');decoded=json.loads(gzip.decompress(compressed.data))
  self.assertEqual([j['id'] for j in identity.json['jobs']],[j['id'] for j in decoded['jobs']]);self.assertLess(len(compressed.data),len(identity.data));self.assertEqual(int(compressed.headers['Content-Length']),len(compressed.data));self.assertIn('Accept-Encoding',compressed.headers['Vary'])
  declined=self.client.get('/api/state',headers={'Accept-Encoding':'gzip;q=0, identity'})
  self.assertNotIn('Content-Encoding',declined.headers);self.assertEqual(declined.status_code,200)

 def test_json_download_is_not_implicitly_buffered(self):
  import io
  from flask import send_file
  data=json.dumps({'items':list(range(1000))}).encode()
  with self.app.test_request_context('/api/fixture-download',headers={'Accept-Encoding':'gzip'}):
   response=self.app.process_response(send_file(io.BytesIO(data),mimetype='application/json',as_attachment=True,download_name='report.json'))
   self.assertTrue(response.direct_passthrough);self.assertNotIn('Content-Encoding',response.headers)
   response.direct_passthrough=False;self.assertEqual(response.data,data);response.close()
