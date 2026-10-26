import io,json,unittest,zipfile
import batches
from test_center import CenterTest


class CurrentBatchResultsTest(unittest.TestCase):
 def test_latest_pending_unit_never_falls_back_to_historical_failed_result(self):
  def row(jid,unit,created,status,result):
   return dict(id=jid,batch_id='b',created=created,status=status,result=result,spec=dict(name=unit,unit_id=unit))
  old_disk=row('old-disk','disk',1,'failed','old-disk.zip');old_nodes=row('old-nodes','nodes',2,'failed','old-nodes.zip')
  disk=row('disk','disk',3,'succeeded','disk.zip');pending=row('nodes','nodes',4,'queued',None)
  self.assertEqual(batches.current_results([disk,old_nodes,pending,old_disk]),[disk])
  pending['spec']['result_transfer_error']=True;pending['result']='transfer-error.zip'
  self.assertEqual(batches.current_results([old_nodes,pending]),[])


class CurrentBatchDownloadApiTest(unittest.TestCase):
 setUp=CenterTest.setUp
 tearDown=CenterTest.tearDown
 package=CenterTest.package
 submit=CenterTest.submit
 def test_batch_and_detail_downloads_use_same_latest_execution_and_keep_history(self):
  old=self.submit().json['ids'][0];oldfile=self.root/'old.zip';oldfile.write_bytes(self.package(text='old failed output').read())
  currentfile=self.root/'current.zip';currentfile.write_bytes(self.package(text='current result').read())
  with self.app.center_db() as c:
   j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(old,)).fetchone());spec=json.loads(j['spec']);spec['unit_id']=old
   c.execute("UPDATE jobs SET result=?,status='failed',finished=1 WHERE id=?",(str(oldfile),old))
   c.execute("INSERT INTO jobs(id,batch_id,created,updated,status,package,spec,owner,priority) VALUES(?,?,?,?,?,?,?,?,?)",('new',j['batch_id'],j['created']+1,j['created']+1,'queued',j['package'],json.dumps(spec),j['owner'],0))
  url='/api/batches/'+j['batch_id']+'/result'
  self.assertEqual(self.client.get(url).status_code,404)
  with self.app.center_db() as c:c.execute("UPDATE jobs SET status='succeeded',result=?,finished=2 WHERE id='new'",(str(currentfile),))
  response=self.client.get(url)
  with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
   self.assertEqual(set(archive.namelist()),{'new.zip','tasks.json'})
   self.assertEqual(archive.read('new.zip'),currentfile.read_bytes())
   self.assertEqual(json.loads(archive.read('tasks.json'))[0]['id'],'new')
  response.close()
  old_response=self.client.get('/api/jobs/'+old+'/result');self.assertEqual(old_response.data,oldfile.read_bytes());old_response.close()


if __name__=='__main__':unittest.main()
