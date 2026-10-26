import io,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_center as fixtures
from agent import upload_result,DownloadInterrupted
class UploadIdentity(unittest.TestCase):
 setUp=fixtures.CenterTest.setUp
 tearDown=fixtures.CenterTest.tearDown
 submit=fixtures.CenterTest.submit
 package=fixtures.CenterTest.package
 heartbeat=fixtures.CenterTest.heartbeat
 claim=fixtures.CenterTest.claim
 def job(self):
  jid=self.submit().json['ids'][0];self.heartbeat();j=self.claim();return jid,'/agent/win2/jobs/'+jid,{**self.auth,'X-Lease':j['lease']}
 def test_same_length_changed_version_preserves_old_prefix(self):
  jid,url,h=self.job();q=url+'/finish-upload';h.update({'X-Upload-Size':'100','X-Upload-Mtime-Ns':'1000'})
  self.client.post(q,data=b'old-prefix',headers={**h,'X-Upload-Offset':'0'})
  self.assertEqual(self.client.get(q,headers=h).json['offset'],10)
  self.assertEqual(self.client.get(q,headers={**h,'X-Upload-Mtime-Ns':'2000'}).json['offset'],0)
  old=[p for p in (self.root/'data/results').glob('*.retained-*') if not p.name.endswith('.json')];self.assertEqual(len(old),1);self.assertEqual(old[0].read_bytes(),b'old-prefix')
 def test_invalid_version_is_rejected(self):
  _,url,h=self.job()
  self.assertEqual(self.client.get(url+'/finish-upload',headers={**h,'X-Upload-Size':'100','X-Upload-Mtime-Ns':'nan'}).status_code,400)
if __name__=='__main__':unittest.main()
