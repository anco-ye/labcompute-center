import sys,tempfile,unittest,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from execution_contract import outcome,prepare
class Contract(unittest.TestCase):
 def test_native_error_even_exit_zero_utf16_and_large_log(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'work').mkdir()
   (p/'work/comsol_batch.log').write_text('java.security.AccessControlException: access denied user.dir\n'+'normal\n'*30000,encoding='utf-16')
   r=outcome(p,'comsol',0);self.assertEqual(r['status'],'failed');self.assertEqual(r['native_exit_code'],0)
 def test_benign_logs_and_preparation_excluded(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'task.log').write_bytes(b'java.lang.RuntimeException: preparation fallback\n')
   offset=(p/'task.log').stat().st_size
   with (p/'task.log').open('ab') as f:f.write(b'error tolerance=0.01\n0 errors\nexpected PermissionError handled\n')
   self.assertEqual(outcome(p,'python',0,offset)['status'],'succeeded')
 def test_cross_runtime_receipt_and_nonzero(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'work').mkdir()
   self.assertEqual(outcome(p,'matlab',2)['status'],'failed')
   (p/'work/execution-result.json').write_text(json.dumps(dict(status='failed',exit_code=3,detail='child failed')))
   self.assertEqual(outcome(p,'python',0)['status'],'failed')
 def test_job_scoped_permissions(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'work').mkdir()
   argv=prepare(p,'comsol',['comsol','-np','8','-prefsdir','old'])
   self.assertEqual(argv.count('-prefsdir'),1)
   self.assertIn('security.external.propertypermission=on',(p/'comsol-prefs/comsol.prefs').read_text())
   self.assertEqual(prepare(p,'matlab',['matlab']),['matlab'])
if __name__=='__main__':unittest.main()
