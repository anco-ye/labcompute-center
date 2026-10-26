import sys,tempfile,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import Worker
class Checked(Exception):pass
class Test(unittest.TestCase):
 def check_case(self,marker=None):
  with tempfile.TemporaryDirectory() as d:
   w=Worker.__new__(Worker);w.root=Path(d)/'jobs';f=w.root/'job';f.mkdir(parents=True)
   w.state={'id':'job','spec':{'requires_isolation':True}};w.background_controls=True
   (f/'exit.json').write_text(json.dumps({'status':'failed','exit_code':-1,'detail':'File is not a zip file'}))
   if marker:(f/marker).write_text('{}')
   calls=[]
   def api(path,*args,**kw):calls.append((path,args));raise Checked()
   w.api=api
   with self.assertRaises(Checked):w.reconcile([])
   r=json.loads((f/'exit.json').read_text())
   if marker:self.assertTrue(calls[0][0].endswith('/update'));self.assertNotIn('cleanup_verified',r)
   else:self.assertTrue(calls[0][0].endswith('/stopped'));self.assertTrue(r['cleanup_verified']);self.assertEqual(r['failure_code'],'environment_setup')
 def test_no_launch(self):self.check_case()
 def test_launch_in_flight(self):self.check_case('launching.json')
 def test_existing_child(self):self.check_case('child.json')
if __name__=='__main__':unittest.main()
