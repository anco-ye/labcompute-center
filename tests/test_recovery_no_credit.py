import sys,tempfile,os,unittest
from pathlib import Path
import recovery_storage as r
class ConservativeTests(unittest.TestCase):
 def test_no_credit_does_not_follow_environment_link(self):
  with tempfile.TemporaryDirectory()as f:
   root=Path(f);(root/'outside').symlink_to('/no/such/target')
   x=r.observe(root,job_id='a'*32,lease_digest='b'*64,volume='/',node='node',credit=False)
   self.assertEqual(x['allocated_bytes'],0);self.assertEqual(x['accounting_mode'],'full_budget_no_credit')
   with self.assertRaises(ValueError):r.observe(root,job_id='a'*32,lease_digest='b'*64,volume='/',node='node')
 def test_unsafe_root_still_rejected(self):
  with tempfile.TemporaryDirectory()as f:
   p=Path(f)/'link';p.symlink_to(f,target_is_directory=True)
   with self.assertRaises(ValueError):r.observe(p,job_id='a'*32,lease_digest='b'*64,volume='/',node='node',credit=False)
if __name__=='__main__':unittest.main()
