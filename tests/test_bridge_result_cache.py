import tempfile,unittest
from pathlib import Path
from external.ssh_bridge import Bridge

class BridgeResultCacheTest(unittest.TestCase):
 def test_new_artifacts_use_configured_volume_and_inputs_stay_in_control_root(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge.__new__(Bridge);b.root=Path(tmp)/'C';b.cfg={'result_cache_dir':str(Path(tmp)/'E')};j={'id':'job'}
   self.assertEqual(b.result_folder(j),Path(tmp)/'E/job')
   self.assertEqual(b.root/j['id'],Path(tmp)/'C/job')
 def test_existing_prefix_and_complete_archive_are_never_discarded(self):
  for name in ['result.zip.partial','result.zip']:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
    b=Bridge.__new__(Bridge);b.root=Path(tmp)/'C';b.cfg={'result_cache_dir':str(Path(tmp)/'E')};legacy=b.root/'job';legacy.mkdir(parents=True);(legacy/name).write_bytes(b'original')
    self.assertEqual(b.result_folder({'id':'job'}),legacy);self.assertEqual((legacy/name).read_bytes(),b'original')
 def test_online_alias_and_legacy_no_policy_are_supported(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge.__new__(Bridge);b.root=Path(tmp)/'C';b.cfg={};j={'id':'job'};self.assertEqual(b.result_folder(j),b.root/'job')
   legacy=b.root/'job';legacy.mkdir(parents=True);data=Path(tmp)/'E.partial';data.write_bytes(b'prefix');(legacy/'result.zip.partial').symlink_to(data);b.cfg={'result_cache_dir':str(Path(tmp)/'E')}
   self.assertEqual(b.result_folder(j),legacy)

class BridgeCachePriorityTest(unittest.TestCase):
 def test_delivery_priority_uses_configured_cache_and_complete_archives(self):
  import threading,time
  from unittest.mock import Mock
  for filename in ['result.zip.partial','result.zip']:
   with self.subTest(filename=filename),tempfile.TemporaryDirectory()as tmp:
    b=Bridge.__new__(Bridge);b.root=Path(tmp)/'C';b.root.mkdir();b.cfg={'result_cache_dir':str(Path(tmp)/'E')};b.jobs={x:{'id':x}for x in ['almost','small']};b.observation={'almost':{'result_size':1000},'small':{'result_size':200}};b.recovered=[];b.transfers={};b.state_lock=threading.RLock();b.inventory={'at':time.time()};b.refresh_inventory=Mock();b.tick_job=Mock();b.api=Mock(return_value={'job':None});b.connect=Mock()
    folder=Path(tmp)/'E/almost';folder.mkdir(parents=True);(folder/filename).write_bytes(b'x'*(1000 if filename=='result.zip'else 900))
    b.tick();self.assertEqual([x.args[1]['id']for x in b.tick_job.call_args_list],['small','almost'])
