"""Platform-only regressions for the 2026-10-22 system audit."""
import io,json,sys,tempfile,time,zipfile,threading,unittest,importlib.util
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_center import CenterTest
from common import atomic_json
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
linux=module('audit_linux','external/linux_rpc.py');bridge=module('audit_bridge','external/ssh_bridge.py')

class ReceiptTests(unittest.TestCase):
 setUp=CenterTest.setUp
 tearDown=CenterTest.tearDown
 submit=CenterTest.submit
 package=CenterTest.package
 heartbeat=CenterTest.heartbeat
 claim=CenterTest.claim
 def job(self):
  jid=self.submit().json['ids'][0];self.heartbeat();j=self.claim();return jid,'/agent/win2/jobs/'+jid,{**self.auth,'X-Lease':j['lease']}
 def result(self,report=None):
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:
   z.writestr('work/output.txt','preserved')
   if report is not None:z.writestr('exit.json',json.dumps(report))
  return b.getvalue()
 def test_missing_receipt_after_stopped_is_saved_but_never_success(self):
  jid,url,h=self.job();self.client.post(url+'/stopped',json={'cleanup_verified':True},headers=h)
  r=self.client.post(url+'/finish',data=self.result(),headers={**h,'X-Result-Status':'succeeded'})
  self.assertEqual(r.status_code,200)
  with self.app.center_db() as c:
   row=c.execute('SELECT status,result,delivery_state FROM jobs WHERE id=?',(jid,)).fetchone()
   self.assertEqual(row['status'],'failed');self.assertEqual(row['delivery_state'],'ready')
   with zipfile.ZipFile(row['result']) as z:self.assertEqual(z.read('work/output.txt'),b'preserved')
 def test_boolean_exit_code_cannot_be_success(self):
  jid,url,h=self.job();self.client.post(url+'/stopped',json={'cleanup_verified':True},headers=h)
  self.client.post(url+'/finish',data=self.result({'exit_code':False,'cleanup_verified':True}),headers={**h,'X-Result-Status':'succeeded'})
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'failed')
 def test_native_linux_receipt_without_status_is_accepted(self):
  jid,url,h=self.job();r=self.client.post(url+'/finish',data=self.result({'exit_code':0,'cleanup_verified':True,'platform':'linux'}),headers={**h,'X-Result-Status':'succeeded'})
  self.assertEqual(r.status_code,200)
  with self.app.center_db() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'succeeded')
 def test_stale_lease_partial_cannot_be_resumed_or_committed(self):
  jid,url,h=self.job();p=self.root/'data/results';f=p/(jid+'.resumable.upload');meta=p/(jid+'.resumable.upload.json')
  body=self.result({'exit_code':0,'cleanup_verified':True});f.write_bytes(body);atomic_json(meta,{'size':len(body),'lease':'old'})
  headers={**h,'X-Upload-Size':str(len(body)),'X-Result-Status':'succeeded','X-Upload-Commit':'1'}
  self.assertEqual(self.client.post(url+'/finish',data=b'',headers=headers).status_code,409)
  self.assertEqual(self.client.get(url+'/finish-upload',headers=headers).json['offset'],0)
 def test_old_extra_fields_keep_same_lease_resume_without_file_hash(self):
  jid,url,h=self.job();p=self.root/'data/results';(p/(jid+'.resumable.upload')).write_bytes(b'123')
  atomic_json(p/(jid+'.resumable.upload.json'),{'size':100,'lease':h['X-Lease'],'sha256':'unused-legacy-field'})
  self.assertEqual(self.client.get(url+'/finish-upload',headers={**h,'X-Upload-Size':'100'}).json['offset'],3)
 def test_concurrent_commits_are_serialized_and_idempotent(self):
  import server
  jid,url,h=self.job();body=self.result({'exit_code':0,'cleanup_verified':True});headers={**h,'X-Upload-Size':str(len(body)),'X-Result-Status':'succeeded'}
  self.client.post(url+'/finish-upload',data=body,headers={**headers,'X-Upload-Offset':'0'})
  entered=threading.Event();release=threading.Event();original=server.inspect_zip
  def inspect(*a,**k):entered.set();self.assertTrue(release.wait(3));return original(*a,**k)
  def commit():return self.app.test_client().post(url+'/finish',data=b'',headers={**headers,'X-Upload-Commit':'1'}).status_code
  with patch('server.inspect_zip',side_effect=inspect),ThreadPoolExecutor(2) as pool:
   one=pool.submit(commit);self.assertTrue(entered.wait(1));two=pool.submit(commit);self.assertFalse(two.done());release.set();self.assertEqual([one.result(3),two.result(3)],[200,200])

class LinuxLaunchTests(unittest.TestCase):
 def test_explicit_launch_rejection_finalizes_without_restarting(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);jid='a'*32;f=root/'jobs'/jid;f.mkdir(parents=True)
   with zipfile.ZipFile(f/'package.zip','w') as z:z.writestr('main.py','print(1)')
   s=dict(os='linux',kind='python',runtime='python-linux',cores=1,cpu_ids=[4],memory_gb=1,commit_gb=1,disk_gb=4,max_minutes=10,entry='main.py')
   req=dict(action='start',id=jid,lease_digest='b'*64,spec=s,package_size=(f/'package.zip').stat().st_size)
   with patch.object(linux,'ROOT',root),patch.object(linux.os,'sched_getaffinity',return_value=set(range(8)),create=True),patch('psutil.virtual_memory',return_value=SimpleNamespace(total=256*2**30)),patch.object(linux,'ctl',return_value={'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'}),patch.object(linux.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr='permission denied')) as launch:
    self.assertEqual(linux.rpc(req)['state'],'finished');self.assertEqual(linux.rpc(dict(req,action='status'))['state'],'finished');self.assertEqual(launch.call_count,1)
   self.assertEqual(json.loads((f/'exit.json').read_text())['failure_code'],'environment_setup')
 def test_namespace_hides_siblings_credentials_and_manager_sockets(self):
  with tempfile.TemporaryDirectory() as d,patch.object(linux.subprocess,'check_output',return_value='/usr\n'),patch.object(linux.shutil,'which',return_value='/usr/bin/bwrap'),patch.object(Path,'is_dir',return_value=True):
   f=Path(d)/'jobs'/'one';f.mkdir(parents=True);(f/'work').mkdir()
   cmd=linux.sandbox_command(f,{'runtime':'python-linux'})
   self.assertIn('--unshare-pid',cmd);self.assertIn('--unshare-user',cmd)
   self.assertEqual(cmd[cmd.index('--bind')+1:cmd.index('--bind')+3],[str(f),str(f)])
   self.assertIn('/home',cmd);self.assertIn('/run',cmd)
 def test_isolation_claim_requires_host_proof(self):
  with tempfile.TemporaryDirectory() as d,patch.object(linux,'ROOT',Path(d)):
   self.assertFalse(linux.sandbox_ready())

class BridgeControlTests(unittest.TestCase):
 def test_blocked_upload_does_not_block_other_unit_cancel(self):
  with tempfile.TemporaryDirectory() as d:
   b=bridge.Bridge.__new__(bridge.Bridge);b.root=Path(d);b.cfg={'max_parallel':2};b.controls={};b.inventory={'at':time.time()};b.recovered=[];b.remote_offset=0;b.observation={};b.state_lock=threading.RLock();b.artifact_pool=ThreadPoolExecutor(1);b.transfers={};b.cancelled_leases=set();b.persist=lambda:None;b.heartbeat=lambda:None;b.refresh_inventory=lambda c:None;b.connect=lambda:SimpleNamespace(close=lambda:None)
   jobs=[{'id':'finished','lease':'x','spec':{}},{'id':'active','lease':'y','spec':{}}];b.jobs={j['id']:j for j in jobs};(b.root/'finished').mkdir();(b.root/'finished/result.zip').write_bytes(b'result');entered=threading.Event();release=threading.Event();actions=[]
   def rpc(c,r):
    actions.append((r['id'],r['action']))
    if r['id']=='finished':return {'state':'finished','result_size':6,'report':{'cleanup_verified':True,'exit_code':0}}
    return {'state':'running'}
   def api(path,*a,**kw):
    if kw.get('upload'):entered.set();release.wait(3)
    return {'job':None}
   b.rpc=rpc;b.api=api
   try:
    b.tick();self.assertTrue(entered.wait(1));b.controls['active']='cancel';b.deliver_controls();self.assertIn(('active','cancel'),actions);self.assertFalse(release.is_set())
   finally:release.set();b.artifact_pool.shutdown(wait=True)

 def test_delivered_results_are_not_downloaded_or_uploaded_again(self):
  b=bridge.Bridge.__new__(bridge.Bridge);b.api=lambda *a,**k:{'delivery_ready':True}
  calls=[];b.forget_delivered=lambda c,j:calls.append(j['id'])
  client=SimpleNamespace(open_sftp=lambda:(_ for _ in ()).throw(AssertionError('duplicate transfer')))
  b.deliver(client,{'id':'done','lease':'lease'},{'report':{'cleanup_verified':True}})
  self.assertEqual(calls,['done'])


class StandaloneBridgeTest(unittest.TestCase):
 def test_direct_entrypoint_resolves_its_own_shared_modules(self):
  import subprocess,os
  env=dict(os.environ);env.pop('PYTHONPATH',None)
  with tempfile.TemporaryDirectory() as d:
   p=subprocess.run([sys.executable,str(ROOT/'external/ssh_bridge.py'),'--help'],cwd=d,env=env,capture_output=True,text=True,timeout=10)
  self.assertEqual(p.returncode,0,p.stderr)
  self.assertIn('--config',p.stdout)

class BridgeEncodingTest(unittest.TestCase):
 def test_utf8_persistent_jobs_reopen_under_windows_locale(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);cfg=root/'bridge.json';cfg.write_text(json.dumps({'ca_file':'unused'}),encoding='utf-8')
   atomic_json(root/'active.json',{'jobs':[{'id':'one','spec':{'name':'计算任务'}}]})
   read=Path.read_text
   def locale_read(path,*args,**kwargs):
    kwargs.setdefault('encoding','gbk');return read(path,*args,**kwargs)
   with patch.object(Path,'read_text',locale_read),patch.object(bridge.ssl,'create_default_context',return_value=None):
    b=bridge.Bridge(cfg)
   try:self.assertEqual(b.jobs['one']['spec']['name'],'计算任务')
   finally:b.artifact_pool.shutdown(wait=True)

if __name__=='__main__':unittest.main()
