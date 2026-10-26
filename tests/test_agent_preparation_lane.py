import tempfile,threading,unittest,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from agent import Worker
class PreparationLaneTest(unittest.TestCase):
 def test_download_does_not_block_delivery_or_create_an_unbounded_claim_queue(self):
  with tempfile.TemporaryDirectory()as d:
   w=Worker.__new__(Worker);w.root=Path(d);w.state_path=w.root/'active_jobs.json';w.state=None;w._states_lock=threading.RLock();w.states={};w.completed_ids=set();w.clock_offset=0
   w._preparation_pool=ThreadPoolExecutor(1);w._preparation_future=None;w._preparing_id=None;w._delivery_pool=ThreadPoolExecutor(1);w._archive_pool=ThreadPoolExecutor(1);w._deliveries={};w._archive_future=None
   w.background_controls=True;w.last_response={};w.cfg={'worker':True,'max_parallel':8};w.archive_one=lambda:None
   entered=threading.Event();release=threading.Event();delivered=threading.Event();claims=[]
   def prepare(job):entered.set();release.wait(3)
   w.prepare=prepare
   def api(*a,**kw):claims.append(1);return {'job':dict(id='big',lease='big-lease',spec={})}
   w.api=api;w.reconcile=lambda *a:delivered.set()
   try:
    w.once();self.assertTrue(entered.wait(1));self.assertEqual(len(claims),1)
    j=dict(id='small',lease='small-lease',spec={});w.remember(j);p=w.root/'small';p.mkdir();(p/'exit.json').write_text('{}')
    w.once();self.assertTrue(delivered.wait(1));self.assertEqual(len(claims),1);self.assertEqual(set(json.loads(w.state_path.read_text())),{'big','small'})
   finally:
    release.set();w._preparation_pool.shutdown();w._delivery_pool.shutdown();w._archive_pool.shutdown()
