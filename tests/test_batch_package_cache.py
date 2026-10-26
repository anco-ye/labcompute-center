import importlib.util,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_external_linux
rpc=test_external_linux.rpc
from external.ssh_bridge import Bridge

class PackageCacheTest(unittest.TestCase):
 def test_cache_copy_is_private_and_does_not_require_hashing(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)):
   key='a'*32+'-123-4';cache=rpc.package_cache(key);cache.write_bytes(b'data')
   folder=Path(tmp)/'job';folder.mkdir();rpc.attach_package(folder,key,4)
   (folder/'package.zip').write_bytes(b'evil');self.assertEqual(cache.read_bytes(),b'data')
   self.assertNotEqual(cache.stat().st_ino,(folder/'package.zip').stat().st_ino)
 def test_invalid_identity_and_symlink_are_rejected(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)):
   for key in ['../escape','a'*32,'a'*32+'-123-4/../x']:
    with self.assertRaises(ValueError):rpc.package_cache(key)
   parent=Path(tmp)/'package-cache';parent.symlink_to(Path(tmp)/'elsewhere')
   with self.assertRaises(ValueError):rpc.package_cache('a'*32+'-123-4')
 def test_incomplete_input_not_attached_and_interrupted_copy_is_recoverable(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)):
   key='a'*32+'-123-4';cache=rpc.package_cache(key);cache.write_bytes(b'bad');folder=Path(tmp)/'job';folder.mkdir()
   with self.assertRaises(ValueError):rpc.attach_package(folder,key,4)
   self.assertFalse((folder/'package.zip').exists());cache.write_bytes(b'data');(folder/'package.zip.cache-copy').write_bytes(b'x');rpc.attach_package(folder,key,4)
   self.assertEqual((folder/'package.zip').read_bytes(),b'data')
 def test_same_batch_downloaded_once_and_new_revision_does_not_reuse_cache(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge.__new__(Bridge);b.root=Path(tmp);b.cfg={'default_work_volume':'/Dataset2','workspace':'/own'}
   b.remote_folder=lambda job:'/own/jobs/'+job['id'];b.upload_slice=Mock(return_value=True);sftp=Mock();client=Mock();client.open_sftp.return_value=sftp
   def call(client,req):return dict(folder='/own/jobs/'+req['id'],package_cache='/own/cache/'+req.get('package_ref','')+'/package.zip')
   b.rpc=Mock(side_effect=call)
   def download(path,download=None,**kw):Path(download).write_bytes(b'data')
   b.api=Mock(side_effect=download)
   for i,key in enumerate(['a'*32+'-123-4','a'*32+'-123-4','a'*32+'-124-4']):
    job=dict(id=str(i),lease='lease',spec={},package_ref=key);self.assertEqual(b.stage(client,job),4)
   self.assertEqual(b.api.call_count,2)
   self.assertEqual(sum(c.args[1]['action']=='attach_package' for c in b.rpc.call_args_list),3)
 def test_completed_remote_file_with_wrong_size_is_not_overwritten(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'file';path.write_bytes(b'123');sftp=Mock();sftp.stat.return_value.st_size=2
   with self.assertRaises(IOError):Bridge.__new__(Bridge).upload_slice(sftp,path,'dest')
   sftp.open.assert_not_called()

if __name__=='__main__':unittest.main()
