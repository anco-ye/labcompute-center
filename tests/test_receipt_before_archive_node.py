import importlib.util,json,os,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch

path=Path(os.environ.get('LINUX_RPC_PATH',str(Path(__file__).parents[1]/'external/linux_rpc.py')))
spec=importlib.util.spec_from_file_location('receipt_rpc',path)
rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)


class Tests(unittest.TestCase):
 def create(self,root):
  request={'action':'status','id':'a'*32,'lease_digest':'b'*64,'receipt_first':True}
  folder=root/'jobs'/request['id'];(folder/'work').mkdir(parents=True)
  rpc.atomic(folder/'identity.json',{'lease_digest':request['lease_digest']})
  rpc.atomic(folder/'start.json',{'at':1})
  rpc.atomic(folder/'program-exit.json',{'exit_code':0,'elapsed_seconds':12})
  (folder/'work/output.txt').write_text('retained')
  return request,folder
 def state(self):return dict(ActiveState='active',SubState='exited',MainPID='0',TasksCurrent='0',Result='success')
 def test_receipt_is_immediate_without_opening_archive(self):
  with tempfile.TemporaryDirectory()as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'volume_root',return_value=Path(tmp)),patch.object(rpc,'ctl',return_value=self.state()):
   request,folder=self.create(Path(tmp))
   with patch.object(rpc.zipfile,'ZipFile',side_effect=AssertionError('archive must be deferred')):
    value=rpc.rpc(request)
   self.assertEqual(value['state'],'archiving');self.assertTrue(value['report']['cleanup_verified'])
   self.assertEqual(value['report']['exit_code'],0);self.assertFalse((folder/'result.zip').exists())
 def test_worker_builds_valid_archive_and_old_protocol_still_finishes(self):
  with tempfile.TemporaryDirectory()as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'volume_root',return_value=Path(tmp)),patch.object(rpc,'ctl',return_value=self.state()):
   request,folder=self.create(Path(tmp));rpc.rpc(request)
   value=rpc.rpc(dict(request,action='archive',receipt_first=False))
   self.assertEqual(value['state'],'finished')
   with zipfile.ZipFile(folder/'result.zip')as z:self.assertEqual(z.read('work/output.txt'),b'retained')
   self.assertEqual(rpc.rpc(dict(request,receipt_first=False))['state'],'finished')
 def test_live_process_keeps_reservation_and_creates_no_exit(self):
  with tempfile.TemporaryDirectory()as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'volume_root',return_value=Path(tmp)),patch.object(rpc,'ctl',return_value=dict(ActiveState='active',SubState='running',MainPID='99',TasksCurrent='3')):
   request,folder=self.create(Path(tmp));value=rpc.rpc(request)
   self.assertEqual(value['state'],'running');self.assertFalse((folder/'exit.json').exists())


if __name__=='__main__':unittest.main()
