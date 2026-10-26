import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
path=Path(os.environ.get('LINUX_RPC_PATH',str(Path(__file__).parents[1]/'external/linux_rpc.py')))
s=importlib.util.spec_from_file_location('liveness_rpc',path);rpc=importlib.util.module_from_spec(s);s.loader.exec_module(rpc)
class ReceiptLiveness(unittest.TestCase):
 def test_existing_receipt_cannot_override_live_os_processes(self):
  for state in [dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='42',TasksCurrent='3'),dict(LoadState='loaded',ActiveState='active',SubState='exited',MainPID='0',TasksCurrent='2')]:
   with self.subTest(state=state),tempfile.TemporaryDirectory()as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'volume_root',return_value=Path(tmp)),patch.object(rpc,'ctl',return_value=state):
    f=Path(tmp)/'jobs'/('a'*32);(f/'work').mkdir(parents=True)
    for name,value in [('identity.json',{'lease_digest':'b'*64}),('start.json',{'at':1}),('exit.json',{'exit_code':0,'cleanup_verified':True})]:rpc.atomic(f/name,value)
    (f/'result.zip').write_bytes(b'old or task-written artifact')
    result=rpc.rpc(dict(action='status',id='a'*32,lease_digest='b'*64,receipt_first=True))
    self.assertEqual(result['state'],'running');self.assertNotIn('report',result)
 def test_receipt_without_launch_intent_never_becomes_finished(self):
  with tempfile.TemporaryDirectory()as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'volume_root',return_value=Path(tmp)),patch.object(rpc,'ctl',return_value=dict(ActiveState='inactive',LoadState='loaded')):
   f=Path(tmp)/'jobs'/('a'*32);f.mkdir(parents=True);rpc.atomic(f/'identity.json',{'lease_digest':'b'*64});rpc.atomic(f/'exit.json',{'exit_code':0,'cleanup_verified':True})
   self.assertEqual(rpc.rpc(dict(action='status',id='a'*32,lease_digest='b'*64,receipt_first=True))['state'],'staged')
if __name__=='__main__':unittest.main()
