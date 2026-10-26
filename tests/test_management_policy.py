import json,time,unittest
from test_center import CenterTest
from management_policy import archived,filter_legacy

class ManagementPolicyTest(unittest.TestCase):
 setUp=CenterTest.setUp
 tearDown=CenterTest.tearDown
 heartbeat=CenterTest.heartbeat
 package=CenterTest.package
 submit=CenterTest.submit
 def test_retirement_keeps_unconfirmed_execution_visible_and_blocks_old_start(self):
  policy={'schema_version':1,'center_only':True,'retired_nodes':['win2'],'archive_before':time.time()}
  (self.root/'management-policy.json').write_text(json.dumps(policy))
  self.heartbeat(simulations=[dict(id='old',name='old',lifecycle='failed',process_state='stopped'),dict(id='unexpected',name='unexpected',lifecycle='running',process_state='running',observed_running=True)])
  sims=self.client.get('/api/state').json['nodes'][0]['telemetry']['simulations']
  self.assertEqual([s['id'] for s in sims],['unexpected'])
  r=self.client.post('/api/nodes/win2/simulations/control',json={'simulation':'old','action':'start'},headers=self.csrf)
  self.assertEqual(r.status_code,409)
 def test_archive_never_hides_unfinished_jobs_and_keeps_results(self):
  jid=self.submit().json['ids'][0]
  (self.root/'management-policy.json').write_text(json.dumps({'schema_version':1,'archive_before':time.time()}))
  self.assertIn(jid,[j['id'] for j in self.client.get('/api/state').json['jobs']])
  self.client.post('/api/jobs/'+jid+'/cancel',headers=self.csrf)
  self.assertNotIn(jid,[j['id'] for j in self.client.get('/api/state').json['jobs']])
  self.assertIn(jid,[j['id'] for j in self.client.get('/api/state?include_archived=1').json['jobs']])
  with self.app.center_db() as c:self.assertIsNotNone(c.execute('SELECT package FROM jobs WHERE id=?',(jid,)).fetchone())
 def test_offline_unretired_node_is_not_hidden(self):
  t={'simulations':[{'id':'pending','process_state':'unknown'}]}
  filter_legacy(t,'win2',{'retired_nodes':['win1']})
  self.assertEqual(len(t['simulations']),1)
  self.assertFalse(archived({'created':1,'status':'cancel_requested'},{'archive_before':2}))

 def test_cancelled_upload_is_retained_but_not_active(self):
  uid=self.client.post('/api/uploads',json={'name':'old.zip'},headers=self.csrf).json['id']
  self.client.post('/api/uploads/'+uid+'/cancel',headers=self.csrf)
  self.assertEqual(self.client.get('/api/state').json['uploads'],[])
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT status FROM uploads WHERE id=?',(uid,)).fetchone()[0],'cancelled')

 def test_explicit_archive_keeps_live_jobs_and_other_owners_visible(self):
  policy={'archived_job_ids':['old']}
  self.assertTrue(archived({'id':'old','created':1,'status':'failed'},policy))
  self.assertTrue(archived({'id':'old','created':1,'status':'uploading','execution_stopped':True},policy))
  for status in ('running','leased','queued','uploading'):
   self.assertFalse(archived({'id':'old','created':1,'status':status},policy))
  self.assertFalse(archived({'id':'new','created':1,'status':'succeeded'},policy))
 def test_public_storage_without_internal_inventory(self):
  from unittest.mock import patch
  with self.app.center_db() as c:
   t=json.loads(c.execute("SELECT telemetry FROM nodes WHERE id='win2'").fetchone()[0])
   t.update(storage_volumes=[{'id':'D','total_gb':100,'free_gb':40,'secret':'hidden'}],host_inventory={'home_storage':{'path':'/home/kxy','total_gb':600,'free_gb':200,'secret':'hidden'},'internal':'hidden'})
   c.execute("UPDATE nodes SET telemetry=? WHERE id='win2'",(json.dumps(t),))
  with patch('access_control.is_admin',return_value=False):
   data=self.client.get('/api/state').json
  t=data['nodes'][0]['telemetry']
  self.assertEqual(t['storage_volumes'],[{'id':'D','total_gb':100,'free_gb':40}])
  self.assertEqual(t['host_inventory'],{'home_storage':{'path':'/home/kxy','total_gb':600,'free_gb':200}})
