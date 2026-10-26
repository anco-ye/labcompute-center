import json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import execution_feedback as feedback

class ArchivedErrorReceipts(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close);self.db.row_factory=sqlite3.Row
  self.db.executescript('CREATE TABLE jobs(id TEXT,status TEXT,execution_stopped INTEGER,result TEXT,finished REAL DEFAULT 0.5);CREATE TABLE job_feedback(id INTEGER PRIMARY KEY,job TEXT,request_id TEXT,message TEXT,created REAL,reply TEXT,replied_at REAL,UNIQUE(job,request_id));')
 def archive(self,report,**extra):
  p=self.root/'result.zip'
  with zipfile.ZipFile(p,'w')as z:
   z.writestr('exit.json',json.dumps(report));z.writestr('work/science-result.json','not read')
   for n,v in extra.items():z.writestr(n,json.dumps(v))
  self.db.execute('INSERT INTO jobs(id,status,execution_stopped,result) VALUES(?,?,?,?)',('job','failed',1,str(p)))
 def test_original_error_is_automatic_deduplicated_and_only_platform_members_read(self):
  self.archive(dict(exit_code=1,cleanup_verified=True,detail='TimeoutError: The read operation timed out'),**{'execution-outcome.json':{'native_exit_code':1,'evidence':[{'source':'task.log','context':'original timeout'}]}})
  original=zipfile.ZipFile.read;names=[]
  def read(z,name,*a,**kw):
   names.append(name.filename if hasattr(name,'filename')else name);return original(z,name,*a,**kw)
  with patch.object(zipfile.ZipFile,'read',read):
   self.assertTrue(feedback.ensure_archived_receipt(self.db,'job',1));self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',2))
  self.assertEqual(names,['exit.json','execution-outcome.json']);data=feedback.read([dict(x)for x in self.db.execute('SELECT * FROM job_feedback')]);self.assertEqual(data['evidence'][0]['context'],'original timeout');self.assertFalse(data['ai_generated'])
 def test_active_or_unverified_exit_never_publishes(self):
  self.archive(dict(exit_code=1,cleanup_verified=False));self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',1));self.db.execute("UPDATE jobs SET status='running'");self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',1))
 def test_oversized_corrupt_and_success_reports_do_not_manufacture_failures(self):
  for report in [dict(exit_code=0,cleanup_verified=True),dict(exit_code=1,cleanup_verified=True,detail='x'*20000)]:
   self.db.execute('DELETE FROM jobs');self.archive(report);self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',1))
 def test_legacy_windows_failure_evidence_retained(self):
  self.archive(dict(exit_code=1,cleanup_verified=True,failure_evidence={'native_exit_code':1,'evidence':[{'source':'task.log','message':'mechanism missing'}]}));self.assertTrue(feedback.ensure_archived_receipt(self.db,'job',1));data=feedback.read([dict(x)for x in self.db.execute('SELECT * FROM job_feedback')]);self.assertEqual(data['evidence'][0]['message'],'mechanism missing')

 def test_new_execution_keeps_old_report_and_displays_latest(self):
  self.archive(dict(exit_code=1,cleanup_verified=True,started=1,detail='first failure'));self.assertTrue(feedback.ensure_archived_receipt(self.db,'job',2))
  p=self.root/'result.zip'
  with zipfile.ZipFile(p,'w')as z:z.writestr('exit.json',json.dumps(dict(exit_code=1,cleanup_verified=True,started=3,detail='second failure')))
  self.db.execute('UPDATE jobs SET finished=4');self.assertTrue(feedback.ensure_archived_receipt(self.db,'job',5))
  rows=[dict(x)for x in self.db.execute('SELECT * FROM job_feedback ORDER BY id')];self.assertEqual(len(rows),2);self.assertIn('first failure',rows[0]['reply']);self.assertEqual(feedback.read(rows)['detail'],'second failure')

 def test_live_windows_failure_evidence_is_preserved(self):
  report=dict(exit_code=1,cleanup_verified=True,failure_evidence={'native_exit_code':7,'evidence':[{'source':'execution-result.json','message':'ConnectionResetError'}]})
  data=feedback.native_report('job',report)
  self.assertEqual(data['native_exit_code'],7);self.assertEqual(data['evidence'][0]['message'],'ConnectionResetError')
 def test_empty_published_receipt_enriched_from_same_execution(self):
  report=dict(exit_code=1,cleanup_verified=True,started=1)
  self.archive(dict(report,failure_evidence={'evidence':[{'source':'task.log','message':'original error'}]}))
  feedback.publish(self.db,'job',report,2)
  self.assertTrue(feedback.ensure_archived_receipt(self.db,'job',3))
  self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',4))
  rows=[dict(x)for x in self.db.execute('SELECT * FROM job_feedback ORDER BY id')]
  self.assertEqual(len(rows),2);self.assertEqual(feedback.read(rows)['evidence'][0]['message'],'original error')
 def test_empty_new_execution_not_replaced_by_old_archive(self):
  self.archive(dict(exit_code=1,cleanup_verified=True,started=1,failure_evidence={'evidence':[{'message':'old error'}]}))
  feedback.publish(self.db,'job',dict(exit_code=1,cleanup_verified=True,started=3),5)
  self.assertFalse(feedback.ensure_archived_receipt(self.db,'job',6))
