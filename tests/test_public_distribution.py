import json,tempfile,unittest,zipfile
from pathlib import Path
from deploy.build_bundle import build_app_archive
from deploy.init_config import initialize
from server import create_app

class PublicDistributionTest(unittest.TestCase):
 def test_bundle_contains_controller_worker_imports_and_excludes_private_files(self):
  with tempfile.TemporaryDirectory()as tmp:
   p=build_app_archive(Path(__file__).parents[1],Path(tmp)/'app.zip')
   with zipfile.ZipFile(p)as z:
    names=set(z.namelist())
    for name in ['server.py','agent.py','access_control.py','resource_sampling.py','external/linux_rpc.py','runtime_support/sitecustomize.py','templates/collaboration.html','submission_tools/estimate_memory.py']:self.assertIn(name,names)
    self.assertFalse(any(n.split('/')[0]in ['.private','config','data','jobs','tls','access','.git']for n in names))
   with self.assertRaises(FileExistsError):build_app_archive(Path(__file__).parents[1],p)
 def test_bootstrap_config_opens_authenticated_isolated_center_and_refuses_overwrite(self):
  with tempfile.TemporaryDirectory()as tmp:
   root=Path(tmp)/'new';config=initialize(root,'admin','fixture-password-123','test.crt','test.key');cfg=json.loads(config.read_text());self.assertGreaterEqual(len(cfg['session_secret']),64);self.assertEqual(cfg['nodes'],[])
   app=create_app(config);client=app.test_client();self.assertEqual(client.get('/health').status_code,200)
   with client.session_transaction()as s:s.update(signed_in=True,user_id=json.loads((config.parent/'users.json').read_text())[0]['id'],csrf='test')
   self.assertEqual(client.get('/api/state').status_code,200);before=config.read_bytes()
   with self.assertRaises(FileExistsError):initialize(root,'admin','fixture-password-123','x','y')
   self.assertEqual(config.read_bytes(),before)
 def test_weak_password_cannot_create_config(self):
  with tempfile.TemporaryDirectory()as tmp:
   root=Path(tmp)/'new'
   with self.assertRaises(ValueError):initialize(root,'admin','short','x','y')
   self.assertFalse(root.exists())
