import json,tempfile,unittest,threading,ast
from pathlib import Path
from unittest.mock import Mock
from learning_cache import LearningCache

class LearningCacheTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.package=self.root/'input.zip';self.package.write_bytes(b'input')
  self.result=self.root/'result.zip';self.result.write_bytes(b'receipt')
  self.cache=LearningCache();self.final=Mock(return_value={'peak_gb':2});self.current=Mock(return_value={'family':'family'});self.reservation=Mock(return_value={'family':'family'})
 def row(self,i='one',status='succeeded'):
  return dict(id=i,owner='owner',status=status,spec=json.dumps(dict(runtime_revision='r1',resource_signature='input1')),package=str(self.package),result=str(self.result))
 def build(self,rows):return self.cache.build(rows,self.final,self.current,self.reservation)
 def test_status_churn_does_not_reopen_historical_archives_beyond_old_lru_capacity(self):
  rows=[self.row(str(i))for i in range(2200)];self.build(rows);self.final.reset_mock();self.reservation.reset_mock()
  self.build(rows+[self.row('new','running')]);self.final.assert_not_called();self.assertEqual(self.reservation.call_count,1)
 def test_changed_receipt_and_package_invalidate_even_without_database_change(self):
  self.build([self.row()]);self.result.write_bytes(b'new receipt');self.build([self.row()]);self.assertEqual(self.final.call_count,2)
  self.package.write_bytes(b'new input');self.build([self.row()]);self.assertEqual(self.reservation.call_count,3)
 def test_queue_to_running_uses_reservation_adapter(self):
  self.build([self.row(status='queued')]);self.build([self.row(status='running')]);self.current.assert_called_once();self.reservation.assert_called_once();self.final.assert_not_called()
 def test_owner_and_runtime_identity_preserved(self):
  r=self.row();x=self.build([r]);self.assertIn(('owner','input1','r1'),x['observations']);r['owner']='other';x=self.build([r]);self.assertNotIn(('owner','input1','r1'),x['observations']);self.assertIn(('other','input1','r1'),x['observations'])
 def test_adapter_failure_does_not_publish_partial_cache(self):
  self.build([self.row()]);old=self.cache.rows;self.reservation.side_effect=RuntimeError('bad metadata')
  with self.assertRaises(RuntimeError):self.build([self.row('new','running')])
  self.assertIs(self.cache.rows,old)
 def test_removed_rows_are_pruned_and_none_results_are_cached(self):
  self.reservation.return_value=None;self.build([self.row()]);self.build([self.row()]);self.assertEqual(self.reservation.call_count,1)
  self.build([]);self.assertFalse(self.cache.rows)
 def test_concurrent_refresh_does_not_block_or_publish_partial_snapshot(self):
  import server
  tree=ast.parse(Path(server.__file__).read_text(encoding="utf-8"));parent=next(x for x in ast.walk(tree)if isinstance(x,ast.FunctionDef)and x.name=='create_app');fn=next(x for x in parent.body if isinstance(x,ast.FunctionDef)and x.name=='refresh_learning')
  # Execute the actual nested function in a factory so its nonlocal publication
  # and nonblocking lock behavior are tested, not a duplicate implementation.
  source='def factory(learning_lock):\n    learning_snapshot={"old": True}\n'+''.join('    '+line+'\n'for line in ast.unparse(fn).splitlines())+'    return refresh_learning, lambda: learning_snapshot\n'
  scope={};exec(source,scope);lock=threading.Lock();refresh,snapshot=scope['factory'](lock)
  with lock:refresh()
  self.assertEqual(snapshot(),{'old':True})

 def test_slow_refresh_keeps_concurrent_readers_on_complete_snapshot(self):
  import server,time
  tree=ast.parse(Path(server.__file__).read_text(encoding="utf-8"));fn=next(x for x in ast.walk(tree)if isinstance(x,ast.FunctionDef)and x.name=='refresh_learning')
  source='def factory(learning_lock, db, learning_cache, resource_policy, resource_metadata):\n    learning_snapshot={"old": True}\n'+''.join('    '+line+'\n'for line in ast.unparse(fn).splitlines())+'    return refresh_learning, lambda: learning_snapshot\n'
  class DB:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def execute(self,*a):return []
  entered=threading.Event();release=threading.Event()
  def build(*args):entered.set();release.wait(2);return dict(models={'new':True},observations={},families={})
  scope={};exec(source,scope);refresh,snapshot=scope['factory'](threading.Lock(),DB,Mock(build=build),Mock(),Mock())
  t=threading.Thread(target=refresh);t.start()
  try:
   self.assertTrue(entered.wait(1));before=time.monotonic();refresh();self.assertLess(time.monotonic()-before,.1);self.assertEqual(snapshot(),{'old':True})
  finally:release.set();t.join(2)
  self.assertEqual(snapshot()['models'],{'new':True})
