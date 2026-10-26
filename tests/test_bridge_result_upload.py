import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from external.ssh_bridge import Bridge


class BridgeResultUploadTest(unittest.TestCase):
 def bridge(self):
  b=Bridge.__new__(Bridge);b.cfg=dict(server='https://center',id='a800-ssh',token='auth');b.opener=Mock();return b
 def test_bridge_uses_worker_lease_and_offset_protocol(self):
  b=self.bridge()
  with patch('agent.upload_result',return_value={'ok':True}) as send:
   self.assertEqual(b.api('/jobs/j/finish',upload='archive.zip',lease='lease',extra={'X-Result-Status':'succeeded'}),{'ok':True})
  args=send.call_args.args
  self.assertIs(args[0],b.opener);self.assertEqual(args[1:3],('https://center','/agent/a800-ssh/jobs/j/finish'))
  self.assertEqual(args[3],{'Authorization':'Bearer auth','X-Lease':'lease','X-Result-Status':'succeeded'})
  b.opener.open.assert_not_called()
 def test_interruption_never_falls_back_to_whole_archive(self):
  b=self.bridge()
  with patch('agent.upload_result',side_effect=TimeoutError('retained offset')):
   with self.assertRaises(TimeoutError):b.api('/jobs/j/finish',upload='archive.zip',lease='lease')
  b.opener.open.assert_not_called()
 def test_only_explicit_old_server_falls_back(self):
  b=self.bridge();response=io.BytesIO(b'{"ok": true}');b.opener.open.return_value=response
  with tempfile.TemporaryDirectory() as tmp,patch('agent.upload_result',return_value=False):
   path=Path(tmp)/'result.zip';path.write_bytes(b'old-server')
   self.assertEqual(b.api('/jobs/j/finish',upload=path,lease='lease'),{'ok':True})
  request=b.opener.open.call_args.args[0];self.assertEqual(request.get_header('Content-length'),'10');self.assertTrue(request.data.closed)

if __name__=='__main__':unittest.main()
