import ast,json,os,time,zipfile,tempfile,unittest,types,sys
from pathlib import Path
from unittest.mock import patch
P=Path(__file__).resolve().parents[1]/'external/linux_rpc.py'
s=P.read_text();node=next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='finalize');code=ast.get_source_segment(s,node)
ns=dict(Path=Path,json=json,os=os,time=time,zipfile=zipfile,atomic=lambda p,v:p.write_text(json.dumps(v)));exec(code,ns);finalize=ns['finalize']
class Tests(unittest.TestCase):
 def run_case(self,cancel=False,live=False,extra=None):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'work').mkdir();(p/'start.json').write_text(json.dumps({'at':time.time()}))
   if cancel:(p/'cancelled.json').write_text('{}')
   if live:(p/'usage.json').write_text(json.dumps({'processes':[{'pid':123,'created':1}]}))
   fake=types.SimpleNamespace(Process=lambda pid:types.SimpleNamespace(create_time=lambda:1),NoSuchProcess=ProcessLookupError)
   with patch.dict(sys.modules,{'psutil':fake}):finalize(p,'nonexistent-test-unit.service',{'LoadState':'not-found',**(extra or {})})
   return json.loads((p/'exit.json').read_text()) if (p/'exit.json').exists() else None
 def test_unknown_missing_unit_stays_unresolved(self):self.assertIsNone(self.run_case())
 def test_cancelled_empty_unit_releases_as_cancelled(self):
  r=self.run_case(True);self.assertTrue(r['cleanup_verified']);self.assertTrue(r['cancelled']);self.assertNotEqual(r['exit_code'],0)
 def test_live_identity_prevents_release(self):self.assertIsNone(self.run_case(True,True))
 def test_missing_unit_with_active_pid_is_not_clean(self):self.assertIsNone(self.run_case(True,extra={'MainPID':'123'}))
 def test_missing_unit_with_tasks_is_not_clean(self):self.assertIsNone(self.run_case(True,extra={'TasksCurrent':'1'}))
if __name__=='__main__':unittest.main()
