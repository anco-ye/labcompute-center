import contextlib,json,tempfile,unittest
from pathlib import Path
import artifact_spool

class ArtifactSpoolTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.source=self.root/'C/job/result.zip.partial';self.source.parent.mkdir(parents=True);self.dest=self.root/'E/job/result.zip.partial';self.identity={'node':'node','job':'job','lease_digest':'lease'}
 def test_future_artifact_uses_new_volume_and_legacy_rename_still_works(self):
  self.assertEqual(artifact_spool.redirect(self.source,self.dest,self.identity)['state'],'future_artifact_routed')
  with self.source.open('ab') as f:f.write(b'archive')
  final=self.source.with_name('result.zip');self.source.replace(final)
  self.assertTrue(final.is_symlink());self.assertEqual(final.read_bytes(),b'archive');self.assertEqual(self.dest.read_bytes(),b'archive')
 def test_prefix_and_backup_survive_append_and_result_cache_rename(self):
  self.source.write_bytes(b'contiguous-prefix');result=artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100)
  backup=Path(result['backup']);self.assertEqual(backup.read_bytes(),b'contiguous-prefix');self.assertTrue(self.source.is_symlink())
  with self.source.open('ab') as f:f.write(b'-suffix')
  self.assertEqual(self.dest.read_bytes(),b'contiguous-prefix-suffix');self.assertEqual(backup.read_bytes(),b'contiguous-prefix')
  self.assertEqual(artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100)['state'],'already_routed')
 def test_active_writer_is_not_relocated(self):
  self.source.write_bytes(b'prefix')
  @contextlib.contextmanager
  def busy(path):raise artifact_spool.BusyFile('busy');yield
  with self.assertRaises(artifact_spool.BusyFile):artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100,reader=busy)
  self.assertFalse(self.source.is_symlink());self.assertEqual(self.source.read_bytes(),b'prefix');self.assertFalse(self.dest.exists())
 def test_changed_binding_and_newer_prefix_are_preserved(self):
  self.source.write_bytes(b'prefix');self.dest.parent.mkdir(parents=True);self.dest.write_bytes(b'newer-prefix')
  with self.assertRaises(ValueError):artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100)
  self.assertEqual(self.dest.read_bytes(),b'newer-prefix');self.assertEqual(self.source.read_bytes(),b'prefix')
  with self.assertRaises(ValueError):artifact_spool.redirect(self.source,self.dest,dict(self.identity,lease_digest='other'),archive_size=100)
 def test_complete_archive_is_retained_and_unknown_size_does_not_move_prefix(self):
  self.source.write_bytes(b'prefix')
  with self.assertRaises(ValueError):artifact_spool.redirect(self.source,self.dest,self.identity)
  with self.assertRaises(ValueError):artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=6)
  self.source.with_name('result.zip').write_bytes(b'complete')
  self.assertEqual(artifact_spool.redirect(self.source,self.dest,self.identity)['state'],'complete_cache_retained')

 @unittest.skipUnless(__import__('os').name=='nt','Windows deny-writer handle')
 def test_windows_writer_lock_and_atomic_alias_cutover(self):
  self.source.write_bytes(b'prefix')
  with self.source.open('ab'):
   with self.assertRaises(artifact_spool.BusyFile):artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100)
  result=artifact_spool.redirect(self.source,self.dest,self.identity,archive_size=100)
  self.assertEqual(result['bytes'],6);self.assertTrue(self.source.is_symlink());self.assertEqual(self.source.read_bytes(),b'prefix')
