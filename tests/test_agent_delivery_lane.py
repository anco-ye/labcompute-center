import sys,tempfile,threading,time,unittest,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import Worker,UploadYielded,delivery_paused
class DeliveryLaneTest(unittest.TestCase):
 def test_timed_maintenance_pause_expires_without_operator(self):
  with tempfile.TemporaryDirectory()as d:
   p=Path(d)/'delivery.pause';p.write_text(json.dumps({'until':time.time()-1}))
   self.assertFalse(delivery_paused(p))
   p.write_text(json.dumps({'until':time.time()+60}));self.assertTrue(delivery_paused(p))
 def test_small_result_is_delivered_before_large_result_and_pause_keeps_controls(self):
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as d:
   w=Worker.__new__(Worker);w.root=Path(d);w.state_path=w.root/'active_jobs.json';w.state=None
   w._states_lock=threading.RLock();w._delivery_pool=ThreadPoolExecutor(1);w._deliveries={};w._archive_future=None
   w.states={j:dict(id=j,lease=j,spec={})for j in ['large','small']};w.completed_ids=set();w.background_controls=True;w.last_response={};w.cfg={'worker':False};w.archive_one=lambda:None;calls=[]
   for jid,size in [('large',10000),('small',100)]:
    p=w.root/jid;p.mkdir();(p/'exit.json').write_text('{}');(p/'result.zip').write_bytes(b'x'*size)
   (w.root/'large/delivery.pause').touch()
   w.reconcile=lambda *args:calls.append(w.state['id'])
   try:
    w.once()
    for f in list(w._deliveries.values()):f.result(2)
    self.assertEqual(calls,['small']);self.assertIn('large',w.states)
   finally:w._delivery_pool.shutdown(wait=True)
 def test_blocked_delivery_does_not_block_claim_or_duplicate_delivery(self):
  with tempfile.TemporaryDirectory() as d:
   w=Worker.__new__(Worker);w.root=Path(d);w.state_path=w.root/'active_jobs.json';w.state=None
   w._states_lock=threading.RLock();w._delivery_pool=ThreadPoolExecutor(1);w._deliveries={};w._archive_future=None
   old={'id':'old','lease':'old-lease','spec':{}};new={'id':'new','lease':'new-lease','spec':{}}
   w.states={'old':old};w.completed_ids=set();w.background_controls=True;w.last_response={};w.cfg={'worker':True,'max_parallel':2}
   (w.root/'old').mkdir();(w.root/'old/exit.json').write_text('{}')
   entered=threading.Event();release=threading.Event();calls=[];claims=iter([new,None,None,None]);w.archive_one=lambda:None
   def reconcile(*args):
    if w.state['id']=='old':
     calls.append(w.state['lease']);entered.set();release.wait(3);self.assertEqual(w.state['lease'],'old-lease');w.forget('old')
   w.reconcile=reconcile
   def api(path,*a,**kw):return {'job':next(claims,None)}
   w.api=api
   def prepare(job):w.state=job;w.remember(job)
   w.prepare=prepare
   try:
    w.once();self.assertTrue(entered.wait(1));self.assertIn('new',w.states)
    w.once();self.assertEqual(calls,['old-lease'])
    release.set();w._delivery_pool.shutdown(wait=True)
    self.assertEqual(set(json.loads(w.state_path.read_text())),{'new'})
   finally:release.set();w._delivery_pool.shutdown(wait=True)
 def test_task_context_is_thread_local(self):
  w=Worker.__new__(Worker);w.state={'lease':'main'}
  def other():
   self.assertIsNone(w.state);w.state={'lease':'delivery'};return w.state['lease']
  with ThreadPoolExecutor(1) as pool:self.assertEqual(pool.submit(other).result(),'delivery')
  self.assertEqual(w.state['lease'],'main')
if __name__=='__main__':unittest.main()
