import importlib.util,tempfile,unittest,time,types
from unittest.mock import Mock,patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('bridge',str(Path(__file__).resolve().parents[1]/'external/ssh_bridge.py'));mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
class Tests(unittest.TestCase):
 def bridge(self):
  b=mod.Bridge.__new__(mod.Bridge);b.cfg={'id':'a800-ssh'};b.controls={};b.inventory={'at':time.time()};b.observation={};b.remote_offset=0;b.persist=lambda:None;return b
 def test_stopped_before_large_transfer(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=self.bridge();b.root=Path(tmp);calls=[];b.api=lambda *a,**k:calls.append(a[0]);b.remote_folder=lambda j:'/remote';j={'id':'test','lease':'secret'}
   client=types.SimpleNamespace(open_sftp=lambda:(_ for _ in ()).throw(OSError('offline')))
   with self.assertRaises(OSError):b.deliver(client,j,{'result_size':999,'report':{'cleanup_verified':True}})
   self.assertEqual(calls,['/jobs/test/update','/jobs/test/stopped']);self.assertTrue(j['_compute_released'])
 def test_cancel_during_stage_never_starts(self):
  b=self.bridge();actions=[];b.api=lambda *a,**k:None;b.deliver=lambda *a:actions.append('deliver');b.heartbeat=lambda:None
  def rpc(c,r):actions.append(r['action']);return {'state':'finished' if r['action']=='cancel' else 'staged'}
  b.rpc=rpc
  def stage(c,j):b.controls['test']='cancel';return 10
  b.stage=stage;b.connect=lambda:types.SimpleNamespace(close=lambda:None);b.queue_artifact=lambda job,result=None:b.artifact_pass(job,result);b.tick_job(None,{'id':'test','lease':'x','spec':{}});self.assertEqual(actions,['status','cancel'])
 def test_inventory_refresh_independent(self):
  b=self.bridge();b.inventory={'at':0};events=[];waits=iter([False,True]);b.stop=types.SimpleNamespace(wait=lambda _:next(waits));client=types.SimpleNamespace(close=lambda:events.append('close'));b.connect=lambda:client;b.refresh_inventory=lambda c:events.append('fresh');b.deliver_controls=lambda:None;b.pulse();self.assertEqual(events,['fresh','close'])
 def test_finished_releases_before_busy_delivery_lane_and_is_idempotent(self):
  b=self.bridge();calls=[];queued=[];report={'cleanup_verified':True,'exit_code':0}
  b.rpc=lambda c,r:dict(state='finished',report=report,result_size=100)
  b.api=lambda path,body,**kw:calls.append((path,body,kw))
  b.queue_artifact=lambda *args:queued.append(args)
  job={'id':'done','lease':'original','spec':{}}
  b.tick_job(None,job);b.tick_job(None,job)
  self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],'/jobs/done/stopped')
  self.assertEqual(calls[0][2]['lease'],'original');self.assertIs(calls[0][1]['execution_report'],report)
  self.assertEqual(len(queued),2);self.assertTrue(job['_compute_released'])
 def test_finished_without_cleanup_keeps_compute_reservation(self):
  b=self.bridge();b.rpc=lambda c,r:dict(state='finished',report={'cleanup_verified':False})
  b.api=lambda *a,**kw:self.fail('unverified release');b.queue_artifact=lambda *a:self.fail('unverified transfer')
  with self.assertRaises(RuntimeError):b.tick_job(None,{'id':'unsafe','lease':'x','spec':{}})
 def test_live_connection_bounds_new_channels_and_enables_keepalive(self):
  b=self.bridge();b.cfg.update(host='node',port=22,user='worker',known_hosts='hosts',password_dpapi='encrypted')
  client=Mock();module=types.SimpleNamespace(SSHClient=lambda:client,RejectPolicy=Mock)
  with patch.dict('sys.modules',{'paramiko':module}),patch.object(mod,'unprotect',return_value='credential'):
   self.assertIs(b.connect(),client)
  self.assertEqual(client.get_transport().channel_timeout,20)
  client.get_transport().set_keepalive.assert_called_once_with(15)
 def test_upload_bounded_and_resumes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);b=self.bridge();b.root=root;b.cfg.update(default_work_volume='/Dataset2',workspace='/remote');folder=root/'test';folder.mkdir();data=b'z'*(9*1024**2);(folder/'package.zip').write_bytes(data);remote=root/'remote';remote.mkdir()
   class SFTP:
    def stat(self,p):return (remote/Path(p).name).stat()
    def mkdir(self,*a,**k):pass
    def open(self,p,mode):return (remote/Path(p).name).open(mode)
    def posix_rename(self,a,c):(remote/Path(a).name).replace(remote/Path(c).name)
    def close(self):pass
   b.remote_folder=lambda j:'/remote';b.rpc=lambda c,r:{'folder':'/remote'};cl=types.SimpleNamespace(open_sftp=lambda:SFTP());j={'id':'test','lease':'x','spec':{}}
   self.assertIsNone(b.stage(cl,j));self.assertEqual((remote/'package.zip.upload').stat().st_size,4*1024**2)
   self.assertIsNone(b.stage(cl,j));self.assertEqual(b.stage(cl,j),len(data));self.assertEqual((remote/'package.zip').read_bytes(),data)
class DellTests(Tests):
 def bridge(self):
  b=super().bridge();b.cfg['id']='dell-r940';return b
 def test_no_cleanup_no_release(self):
  b=self.bridge();calls=[];b.api=lambda *a,**k:calls.append(a)
  with self.assertRaises(RuntimeError):b.deliver(None,{'id':'unsafe','lease':'x'},{'report':{'cleanup_verified':False}})
  self.assertEqual(calls,[])
if __name__=='__main__':unittest.main()
