import hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch,Mock
from collaboration_service import create_app

class CollaborationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=self.root/'app';self.source.mkdir();(self.source/'static').mkdir();(self.source/'static/a.js').write_text('const x=1;');(self.source/'runner.py').write_text('print(1)')
  self.users=self.root/'users.json';self.users.write_text(json.dumps([dict(id='one',username='one',role='user',password_hash='h1',enabled=True),dict(id='two',username='two',role='user',password_hash='h2',enabled=True),dict(id='disabled',username='disabled',role='user',password_hash='h3',enabled=False)]))
  self.cfg=self.root/'server.json';self.cfg.write_text(json.dumps(dict(session_secret='secret',password_hash='legacy',users_file=str(self.users),data_dir=str(self.root))))
  with closing(sqlite3.connect(self.root/'center.sqlite3'))as c,c:c.execute('CREATE TABLE jobs(id TEXT,execution_stopped INTEGER,status TEXT)')
  self.service=self.root/'collaboration.json';self.service.write_text(json.dumps(dict(source_root=str(self.source),data_root=str(self.root/'collaboration'),full_edit=True,ca_file='unused',platform_url='https://127.0.0.1:8443')))
  self.app=create_app(self.cfg,self.service);self.client=self.app.test_client();self.login('one','h1')
 def tearDown(self):self.tmp.cleanup()
 def login(self,uid,password):
  with self.client.session_transaction()as s:s.clear();s.update(signed_in=True,user_id=uid,auth_version=hashlib.sha256(password.encode()).hexdigest(),csrf='csrf')
 def post(self,path,body):return self.client.post(path,json=body,headers={'X-CSRF-Token':'csrf'})
 def feedback(self):return self.post('/api/feedback',dict(title='download problem',message='node route timeout',request_id='f1'))
 def change(self,path='static/a.js',content='const x=2;'):
  rev=self.client.get('/api/files',query_string={'path':path}).json['revision'];return self.post('/api/changes',dict(path=path,base_revision=rev,content=content)).json['id']
 def test_authenticated_accounts_all_promoted_but_disabled_stays_disabled(self):
  self.assertEqual(self.client.get('/api/me').status_code,200);users=json.loads(self.users.read_text());self.assertEqual([x['role']for x in users],['admin','admin','user']);self.login('disabled','h3');self.assertEqual(self.client.get('/api/me').status_code,401)
 def test_anonymous_and_csrf_denied(self):
  self.assertEqual(self.client.post('/api/feedback',json={}).status_code,403)
  with self.client.session_transaction()as s:s.clear()
  for path in ['/','/api/feedback','/api/files','/api/changes','/api/events']:self.assertEqual(self.client.get(path).status_code,401)
 def test_password_change_invalidates_session_and_agent_token(self):
  token=self.post('/api/tokens',{'name':'agent'}).json['token'];rows=json.loads(self.users.read_text());rows[0]['password_hash']='new';self.users.write_text(json.dumps(rows));self.assertEqual(self.client.get('/api/me').status_code,401);self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+token}).status_code,401)
 def test_feedback_idempotency_and_cross_user_edit(self):
  r=self.feedback();self.assertEqual(r.status_code,201);fid=r.json['feedback']['id'];self.assertEqual(self.feedback().json['feedback']['id'],fid)
  self.assertEqual(self.post('/api/feedback',dict(title='changed',message='x',request_id='f1')).status_code,409)
  self.login('two','h2');r=self.client.patch('/api/feedback/'+str(fid),json={'version':1,'status':'working','title':'updated','message':'retained'},headers={'X-CSRF-Token':'csrf'});self.assertEqual(r.status_code,200);self.assertEqual(r.json['feedback']['version'],2)
  self.assertEqual(self.client.patch('/api/feedback/'+str(fid),json={'version':1},headers={'X-CSRF-Token':'csrf'}).status_code,409)
 def test_comments_changes_cursor_and_duplicate_guard(self):
  fid=self.feedback().json['feedback']['id'];r=self.post('/api/feedback/'+str(fid)+'/comments',dict(message='repairing',request_id='comment'));self.assertEqual(r.status_code,201);self.assertEqual(self.post('/api/feedback/'+str(fid)+'/comments',dict(message='different',request_id='comment')).status_code,409)
  events=self.client.get('/api/events').json;self.assertEqual(len(events['events']),2);self.assertEqual(self.client.get('/api/events?after='+str(events['next_cursor'])).json['events'],[])
 def test_token_is_once_hashed_revocable_and_cannot_mint_more(self):
  v=self.post('/api/tokens',{'name':'agent'}).json;header={'Authorization':'Bearer '+v['token']};self.assertEqual(self.client.get('/api/me',headers=header).status_code,200)
  self.assertEqual(self.client.post('/api/tokens',json={'name':'nested'},headers=header).status_code,403)
  with self.app.collaboration_db()as c:self.assertNotIn(v['token'],str([dict(x)for x in c.execute('SELECT * FROM tokens')]))
  self.assertEqual(self.client.delete('/api/tokens/'+v['id'],headers={'X-CSRF-Token':'csrf'}).status_code,200);self.assertEqual(self.client.get('/api/me',headers=header).status_code,401)
 def test_source_traversal_symlink_and_nontext_are_denied(self):
  for path in ['../server.json','/server.json','static/../../server.json','static\\a.js','data.sqlite']:
   self.assertEqual(self.client.get('/api/files',query_string={'path':path}).status_code,400,path)
  (self.source/'static/link.js').symlink_to(self.cfg);self.assertEqual(self.client.get('/api/files?path=static/link.js').status_code,400)
 def test_source_conflict_preserves_live_source(self):
  cid=self.change();(self.source/'static/a.js').write_text('concurrent change');r=self.post('/api/changes/'+cid+'/deploy',{});self.assertEqual(r.status_code,409);self.assertEqual((self.source/'static/a.js').read_text(),'concurrent change')
 def test_static_publish_backup_and_idempotency(self):
  cid=self.change();r=self.post('/api/changes/'+cid+'/deploy',{});self.assertEqual(r.json['status'],'live');self.assertEqual((self.source/'static/a.js').read_text(),'const x=2;');self.assertEqual((self.root/'collaboration/backups'/cid/'before').read_text(),'const x=1;');self.assertTrue(self.post('/api/changes/'+cid+'/deploy',{}).json['already_applied'])
 def test_backend_deployment_waits_without_mutating_busy_work(self):
  with closing(sqlite3.connect(self.root/'center.sqlite3'))as c,c:c.execute("INSERT INTO jobs VALUES('protected',0,'running')")
  cid=self.change('runner.py','print(2)');r=self.post('/api/changes/'+cid+'/deploy',{});self.assertEqual(r.json['status'],'queued');self.assertEqual((self.source/'runner.py').read_text(),'print(1)')
  with closing(sqlite3.connect(self.root/'center.sqlite3'))as c,c:c.execute('UPDATE jobs SET execution_stopped=1')
  r=self.post('/api/changes/'+cid+'/deploy',{});self.assertEqual(r.json['status'],'published_pending_activation');self.assertFalse(r.json['effective']);self.assertEqual((self.source/'runner.py').read_text(),'print(2)')
 def test_invalid_python_never_staged(self):
  rev=self.client.get('/api/files?path=runner.py').json['revision'];r=self.post('/api/changes',dict(path='runner.py',base_revision=rev,content='def invalid('));self.assertEqual(r.status_code,400);self.assertEqual(self.client.get('/api/changes').json['changes'],[])
 def test_prefixed_endpoints_work(self):self.assertEqual(self.client.get('/collaboration/api/me').status_code,200)
 def test_platform_proxy_stays_on_loopback_and_uses_bound_identity(self):
  response=Mock();response.status=200;response.read.return_value=b'{"ok":true}';response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);op=Mock();op.open.return_value=response
  with patch('collaboration_service.ssl.create_default_context'),patch('collaboration_service.urllib.request.build_opener',return_value=op):
   self.assertEqual(self.client.get('/api/platform?path=/api/state').status_code,200)
   req=op.open.call_args.args[0];self.assertEqual(req.full_url,'https://127.0.0.1:8443/api/state');self.assertTrue(req.get_header('Cookie').startswith('session='))
  self.assertEqual(self.client.get('/api/platform?path=https://evil/api/state').status_code,400)

 def test_deploy_requires_consuming_valid_json_before_mutation(self):
  cid=self.change();r=self.client.post('/api/changes/'+cid+'/deploy',data='not-json',content_type='text/plain',headers={'X-CSRF-Token':'csrf'});self.assertEqual(r.status_code,400);self.assertEqual((self.source/'static/a.js').read_text(),'const x=1;')
  self.assertEqual(self.post('/api/changes/'+cid+'/deploy',{}).json['status'],'live')
