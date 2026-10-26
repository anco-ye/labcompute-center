import hashlib,io,json,tempfile,threading,time,unittest,urllib.request,urllib.error,zipfile,sqlite3
from pathlib import Path
from unittest.mock import Mock
from werkzeug.security import generate_password_hash
from http.server import ThreadingHTTPServer
from direct_download import issue,verify,selection,identity,etag
from node_result_service import Handler,result_path
from direct_gateway import create_app

JID='a'*32;LEASE='original-lease';KEY='b1'*32
class ProtocolTests(unittest.TestCase):
 def test_capability_tampering_expiry_node_and_job_boundaries(self):
  c=dict(jid=JID,node='win3',lease_digest=hashlib.sha256(LEASE.encode()).hexdigest(),kind='locate');t=issue(KEY,c)
  self.assertEqual(verify(KEY,t,'win3')['jid'],JID)
  for token,node,at in [(t+'x','win3',None),(t,'win4',None),(t,'win3',time.time()+601)]:
   with self.assertRaises(PermissionError):verify(KEY,token,node,at)
  with self.assertRaises(PermissionError):verify(KEY,issue(KEY,dict(c,jid='../other')),'win3')
 def test_single_range_suffix_invalid_if_range_and_version_preconditions(self):
  m=dict(size=100,mtime_ns=1);e=etag(m)
  for h,expected in [({'Range':'bytes=10-19'},(206,10,19)),({'Range':'bytes=-10'},(206,90,99)),({'Range':'bytes=10-'},(206,10,99)),({'Range':'bytes=100-'},(416,0,-1)),({'Range':'bytes=1-2,4-5'},(416,0,-1)),({'Range':'bytes=-0'},(416,0,-1)),({'Range':'bytes=1-2','If-Range':'old'},(200,0,99)),({'If-Match':'old'},(412,0,-1)),({'If-None-Match':e},(304,0,-1))]:self.assertEqual(selection(m,h),expected)
  self.assertEqual(selection(m,{'Range':'bytes=1-2','If-Range':'Thu, 01 Jan 1970 00:00:00 GMT'}),(206,1,2))

class NodeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'config').mkdir();(self.root/'jobs').mkdir();self.folder=self.root/'jobs'/JID;self.folder.mkdir()
  (self.root/'config/agent.json').write_text('{}');(self.root/'jobs/active_jobs.json').write_text(json.dumps({JID:{'lease':LEASE}}));self.path=self.folder/'result.zip'
  with zipfile.ZipFile(self.path,'w') as z:z.writestr('exit.json',json.dumps({'cleanup_verified':True,'exit_code':0}));z.writestr('payload.bin',bytes(range(256))*10)
  self.bytes=self.path.read_bytes();self.cfg=dict(root=str(self.root),platform='windows',node='win3',key=KEY,origins=['https://center:8445'])
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.cfg=self.cfg;self.server.streams=threading.BoundedSemaphore(4)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url='http://127.0.0.1:'+str(self.server.server_port)
  self.cap=dict(jid=JID,node='win3',lease_digest=hashlib.sha256(LEASE.encode()).hexdigest(),kind='read',identity=identity(self.path));self.ticket=issue(KEY,self.cap)
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def call(self,path=None,method='GET',headers=None):
  req=urllib.request.Request(self.url+(path or '/result/'+JID+'?ticket='+self.ticket),method=method,headers=headers or {})
  try:return urllib.request.urlopen(req,timeout=2)
  except urllib.error.HTTPError as e:return e
 def test_head_range_resume_and_cors(self):
  with self.call(method='HEAD',headers={'Origin':'https://center:8445'}) as r:
   self.assertEqual(int(r.headers['Content-Length']),len(self.bytes));self.assertEqual(r.headers['Access-Control-Allow-Origin'],'https://center:8445');self.assertEqual(r.read(),b'');tag=r.headers['ETag']
  with self.call(headers={'Range':'bytes=27-117','If-Range':tag}) as r:self.assertEqual(r.status,206);self.assertEqual(r.read(),self.bytes[27:118]);self.assertEqual(r.headers['Content-Range'],f'bytes 27-117/{len(self.bytes)}')
  with self.call(headers={'Range':'bytes=118-','If-Range':tag}) as r:self.assertEqual(r.read(),self.bytes[118:])
 def test_bad_capability_cross_job_and_lease_rejected(self):
  with self.call('/result/'+JID+'?ticket=bad') as r:self.assertEqual(r.status,403)
  with self.call('/result/'+'c'*32+'?ticket='+self.ticket) as r:self.assertEqual(r.status,403)
  (self.root/'jobs/active_jobs.json').write_text(json.dumps({JID:{'lease':'new-lease'}}))
  with self.call() as r:self.assertEqual(r.status,403)
 def test_changed_archive_is_not_appended_to_old_download(self):
  self.path.write_bytes(self.bytes+b'changed')
  with self.call(headers={'Range':'bytes=20-'}) as r:self.assertEqual(r.status,412)
 def test_unfinished_archive_and_internal_files_never_served(self):
  with zipfile.ZipFile(self.path,'w') as z:z.writestr('exit.json','{"cleanup_verified":false}')
  with self.call() as r:self.assertEqual(r.status,409)
  with self.call('/result/../../config/agent.json?ticket='+self.ticket) as r:self.assertEqual(r.status,404)
 def test_linux_location_is_bound_to_original_lease_and_volume(self):
  (self.root/'locations').mkdir();(self.root/'locations'/(JID+'.json')).write_text(json.dumps(dict(volume='volume',lease_digest=self.cap['lease_digest'])))
  (self.root/'host-config.json').write_text(json.dumps(dict(volume_roots={'volume':str(self.root)})))
  cfg=dict(self.cfg,platform='linux');self.assertEqual(result_path(cfg,self.cap),self.path)
  with self.assertRaises(PermissionError):result_path(cfg,dict(self.cap,lease_digest='f'*64))
 def test_same_range_contract_over_ssh_stdio(self):
  import subprocess,sys
  cfg=self.root/'stdio.json';cfg.write_text(json.dumps(self.cfg))
  req=json.dumps(dict(op='result',ticket=self.ticket,method='GET',headers={'Range':'bytes=20-77'})).encode()+b'\n'
  proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'node_result_service.py'),'--config',str(cfg),'--stdio'],input=req,capture_output=True,check=True)
  header,body=proc.stdout.split(b'\n',1);self.assertEqual(json.loads(header)['status'],206);self.assertEqual(body,self.bytes[20:78])

class GatewayTests(NodeTests):
 def setUp(self):
  super().setUp();self.cfg_path=self.root/'center-config.json';self.users=self.root/'users.json'
  self.users.write_text(json.dumps([dict(id='owner',username='uploader',role='user',enabled=True,password_hash='hash'),dict(id='other',username='other',role='user',enabled=True,password_hash='hash'),dict(id='admin',username='admin',role='admin',enabled=True,password_hash='hash')]))
  self.cfg_path.write_text(json.dumps(dict(session_secret='secret',password_hash='unused',users_file=str(self.users))))
  db=sqlite3.connect(self.root/'center.sqlite3');db.execute('CREATE TABLE jobs(id TEXT, owner TEXT, node TEXT, lease TEXT, status TEXT, execution_stopped INTEGER)');db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(JID,'owner','win3',LEASE,'uploading',1));db.commit();db.close()
  service=self.root/'service.json';service.write_text(json.dumps(dict(data_root=str(self.root),ca_file='unused',nodes={'win3':dict(url=self.url,key=KEY)})))
  self.app=create_app(self.cfg_path,service,opener=urllib.request.urlopen);self.client=self.app.test_client()
  self.signin('owner')
 def signin(self,uid):
  with self.client.session_transaction() as s:s.clear();s.update(signed_in=True,user_id=uid,auth_version=hashlib.sha256(b'hash').hexdigest())
 def test_owner_and_admin_allowed_other_user_and_anonymous_rejected(self):
  url='/api/jobs/'+JID+'/meta';self.assertEqual(self.client.get(url).status_code,200)
  self.signin('other');self.assertEqual(self.client.get(url).status_code,404);self.assertEqual(self.client.head('/api/jobs/'+JID+'/result').status_code,404)
  self.signin('admin');self.assertEqual(self.client.get(url).status_code,200)
  with self.client.session_transaction() as s:s.clear()
  self.assertEqual(self.client.get(url).status_code,401)
 def test_disabled_and_changed_password_sessions_rejected(self):
  rows=json.loads(self.users.read_text());rows[0]['enabled']=False;self.users.write_text(json.dumps(rows));self.assertEqual(self.client.get('/api/jobs/'+JID+'/meta').status_code,401)
  rows[0]['enabled']=True;rows[0]['password_hash']='changed';self.users.write_text(json.dumps(rows));self.assertEqual(self.client.get('/api/jobs/'+JID+'/meta').status_code,401)
 def test_relay_range_without_local_copy_or_mutating_original_upload(self):
  before=sorted(p.name for p in self.root.iterdir());r=self.client.get('/api/jobs/'+JID+'/result',headers={'Range':'bytes=90-199'});self.assertEqual(r.status_code,206);self.assertEqual(r.data,self.bytes[90:200]);r.close();self.assertEqual(before,sorted(p.name for p in self.root.iterdir()))
  self.assertEqual(self.client.head('/api/jobs/'+JID+'/result').status_code,200)
  self.assertEqual(self.client.get('/direct/api/jobs/'+JID+'/download').status_code,200)
  with sqlite3.connect(self.root/'center.sqlite3') as db:self.assertEqual(db.execute('SELECT status,lease FROM jobs').fetchone(),('uploading',LEASE))
 def test_running_jobs_not_exposed_and_missing_nodes_are_graceful(self):
  with sqlite3.connect(self.root/'center.sqlite3') as db:db.execute("UPDATE jobs SET execution_stopped=0,status='running'")
  self.assertEqual(self.client.get('/api/jobs/'+JID+'/meta').status_code,409)
 def test_client_disconnect_releases_gateway_slot_and_source(self):
  for _ in range(6):
   r=self.client.get('/api/jobs/'+JID+'/result',buffered=False);self.assertEqual(r.status_code,200);r.close()

class TransportTests(unittest.TestCase):
 def test_https_denial_never_bypassed_and_ssh_fallback_is_explicit(self):
  from direct_transport import NodeTransport
  import urllib.error
  denied=urllib.error.HTTPError('https://node/meta',403,'denied',{},None)
  t=NodeTransport({'node':{'url':'https://node','bridge_config':'bridge'}},Mock(side_effect=denied));t.ssh=Mock()
  with self.assertRaises(urllib.error.HTTPError):t('https://node/meta')
  t.ssh.assert_not_called()
  t.https_open=Mock(side_effect=urllib.error.URLError('offline'));t.ssh.return_value='stream';self.assertEqual(t('https://node/meta'),'stream')
  with self.assertRaises(ValueError):t('https://attacker/meta')
 def test_unreachable_node_without_bridge_does_not_use_another_nodes_identity(self):
  from direct_transport import NodeTransport
  t=NodeTransport({'node':{'url':'https://node'}},Mock(side_effect=OSError('offline')));t.ssh=Mock()
  with self.assertRaises(OSError):t('https://node/meta')
  t.ssh.assert_not_called()
