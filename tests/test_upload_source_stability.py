import io,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from agent import upload_result
class UploadSource(unittest.TestCase):
 def test_local_source_replacement_stops_before_sending(self):
  with tempfile.TemporaryDirectory()as tmp:
   p=Path(tmp)/'result.zip';p.write_bytes(b'abcd');sent=[]
   def opener(req,**kw):
    if req.get_method()=='GET':
     self.assertEqual(req.get_header('X-upload-mtime-ns'),str(p.stat().st_mtime_ns));p.write_bytes(b'WXYZ');os.utime(p,ns=(1,2));return io.BytesIO(b'{"offset":0}')
    sent.append(req);raise AssertionError('Changed archive must not be sent')
   with self.assertRaisesRegex((OSError,RuntimeError),'changed|变化'):
    upload_result(SimpleNamespace(open=opener),'https://center','/jobs/id/finish',{},p)
   self.assertEqual(sent,[])
if __name__=='__main__':unittest.main()
