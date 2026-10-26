import io,json,unittest,zipfile
from pathlib import Path
import test_center as fixture
from common import inspect_zip
from server import create_app


class ResultFileCountTest(unittest.TestCase):
 setUp=fixture.CenterTest.setUp
 tearDown=fixture.CenterTest.tearDown
 package=fixture.CenterTest.package
 submit=fixture.CenterTest.submit
 heartbeat=fixture.CenterTest.heartbeat
 claim=fixture.CenterTest.claim
 def archive(self,count=20001):
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w')as z:
   for i in range(count):z.writestr('work/frame-'+str(i)+'.txt','x')
   z.writestr('exit.json',json.dumps(dict(exit_code=0,cleanup_verified=True)))
  return out.getvalue()
 def prepare(self):
  jid=self.submit().json['ids'][0];self.heartbeat();j=self.claim()
  return jid,'/agent/win2/jobs/'+jid+'/finish',{**self.auth,'X-Lease':j['lease'],'X-Result-Status':'succeeded'}
 def test_many_small_result_files_accepted_without_loosening_input_policy(self):
  data=self.archive();p=self.root/'many.zip';p.write_bytes(data)
  with self.assertRaisesRegex(ValueError,'20000'):inspect_zip(p)
  jid,url,headers=self.prepare();self.assertEqual(self.client.post(url,data=data,headers=headers).status_code,200)
  with self.app.center_db()as c:
   row=c.execute('SELECT delivery_state,result FROM jobs WHERE id=?',(jid,)).fetchone();self.assertEqual(row['delivery_state'],'ready')
   with zipfile.ZipFile(row['result'])as z:self.assertEqual(len(z.infolist()),20002)
 def test_configured_result_entry_bound_still_rejects(self):
  jid,url,headers=self.prepare();p=self.root/'config.json';cfg=json.loads(p.read_text());cfg['max_result_files']=3;p.write_text(json.dumps(cfg));app=create_app(p)
  self.assertEqual(app.test_client().post(url,data=self.archive(4),headers=headers).status_code,400)
  with app.center_db()as c:self.assertNotEqual(c.execute('SELECT delivery_state FROM jobs WHERE id=?',(jid,)).fetchone()[0],'ready')
 def test_expanded_count_keeps_path_and_byte_limits(self):
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w')as z:z.writestr('../escape','x')
  p=self.root/'unsafe.zip';p.write_bytes(out.getvalue())
  with self.assertRaises(ValueError):inspect_zip(p,max_files=200000)
  p.write_bytes(self.archive(1))
  with self.assertRaisesRegex(ValueError,'解压大小'):inspect_zip(p,max_files=200000,max_bytes=0)
 def test_linux_case_distinct_results_retained_but_exact_duplicates_rejected(self):
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w')as z:
   z.writestr('work/terminfo/e/eterm','lower');z.writestr('work/terminfo/E/Eterm','upper')
   z.writestr('exit.json',json.dumps(dict(exit_code=0,cleanup_verified=True)))
  p=self.root/'case.zip';p.write_bytes(out.getvalue())
  with self.assertRaisesRegex(ValueError,'重名'):inspect_zip(p)
  jid,url,headers=self.prepare();self.assertEqual(self.client.post(url,data=out.getvalue(),headers=headers).status_code,200)
  import warnings
  with warnings.catch_warnings():
   warnings.simplefilter('ignore',UserWarning)
   with zipfile.ZipFile(p,'w')as z:z.writestr('same','a');z.writestr('same','b')
  with self.assertRaisesRegex(ValueError,'重名'):inspect_zip(p,max_files=200000,case_sensitive=True)

if __name__=='__main__':unittest.main()
