import copy,threading,time,unittest
from unittest.mock import Mock
import resource_sampling as rs

class IndependentResourceSamplingTests(unittest.TestCase):
 def payload(self):
  return dict(sampled_at=100,sent_at=102,center_sent_at=1000,resource_observation=dict(at=100,verified=True,state='normal',window_seconds=25),
      values=dict(cpu_percent=20,memory_total_gb=64,memory_available_gb=40,memory_percent=30,memory_commit_limit_gb=90,memory_committed_gb=20),
      source=dict(release=rs.RELEASE,pid=2,created=50,sequence=1))
 def test_clock_translation_preserves_age(self):
  p=rs.receive(self.payload(),1000);self.assertEqual(p['at'],998);self.assertEqual(p['resource_observation']['at'],998)
 def test_transport_delay_is_counted_in_age(self):
  self.assertEqual(rs.receive(self.payload(),1005)['at'],998)
 def test_network_delayed_publication_cannot_renew_freshness(self):
  with self.assertRaises(ValueError):rs.receive(self.payload(),1020)
 def test_stale_and_future_sample_rejected(self):
  for sampled in [80,110]:
   p=self.payload();p['sampled_at']=sampled
   with self.assertRaises(ValueError):rs.receive(p,1000)
 def test_replay_and_old_generation_rejected(self):
  prior=rs.receive(self.payload(),1000)
  with self.assertRaises(ValueError):rs.receive(self.payload(),1001,prior)
  p=self.payload();p['source']['created']=49
  with self.assertRaises(ValueError):rs.receive(p,1001,prior)
  p=self.payload();p['source']['sequence']=2;self.assertEqual(rs.receive(p,1001,prior)['at'],998)
 def test_fresh_observation_preserved_across_legacy_heartbeat(self):
  sample=rs.receive(self.payload(),1000);t=dict(resource_observation={'state':'warming'},active_ids=['original'],sampled_at=995)
  self.assertTrue(rs.merge(t,sample,1002));self.assertEqual(t['resource_observation']['state'],'normal');self.assertEqual(t['active_ids'],['original']);self.assertEqual(t['sampled_at'],995)
 def test_expired_sampler_does_not_override_current_worker_observation(self):
  t=dict(resource_observation={'state':'unknown'});self.assertFalse(rs.merge(t,rs.receive(self.payload(),1000),1020));self.assertEqual(t['resource_observation']['state'],'unknown')
 def test_invalid_measurement_rejected(self):
  for value in [float('nan'),-1,True]:
   p=self.payload();p['values']['memory_total_gb']=value
   with self.assertRaises(ValueError):rs.receive(p,1000)
 def test_blocked_sender_cannot_block_fixed_cadence_collection(self):
  import tempfile
  from unittest.mock import patch
  blocked=threading.Event();release=threading.Event();collected=threading.Event()
  class FastStop:
   def __init__(self):self.event=threading.Event()
   def is_set(self):return self.event.is_set()
   def set(self):self.event.set()
   def wait(self,seconds):return self.event.wait(min(seconds,.01))
  class Response:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self):return b'{"server_time":'+str(time.time()).encode()+b'}'
  def send(req,**kwargs):
   if req.data!=b'{"probe":true}':blocked.set();release.wait(2)
   return Response()
  with tempfile.TemporaryDirectory()as temp:
   sampler=rs.Sampler.__new__(rs.Sampler);sampler.root=rs.Path(temp);sampler.stop=FastStop();sampler.lock=threading.Lock();sampler.latest=None
   sampler.cfg=dict(ca_file='unused',controller='https://controller',id='win2',token='fixture')
   rows=[]
   def collect():
    if rows:blocked.wait(1)
    rows.append(len(rows)+1)
    if len(rows)>=3:collected.set();sampler.stop.set()
    return dict(sampled_at=time.time(),source={'sequence':len(rows)})
   sampler.collect=collect
   with patch.object(rs.ssl,'create_default_context'),patch.object(rs.urllib.request,'build_opener',return_value=Mock(open=send)):
    thread=threading.Thread(target=sampler.run);thread.start()
    try:self.assertTrue(blocked.wait(1));self.assertTrue(collected.wait(1));self.assertGreaterEqual(len(rows),3)
    finally:release.set();sampler.stop.set();thread.join(2)
   self.assertFalse(thread.is_alive())


class ResourceSamplingEndpointTests(unittest.TestCase):
 def setUp(self):
  from test_center import CenterTest
  self.fixture=CenterTest();self.fixture.setUp();self.addCleanup(self.fixture.tearDown);self.client=self.fixture.client;self.app=self.fixture.app
 def test_authenticated_sample_does_not_make_dead_worker_online(self):
  payload=IndependentResourceSamplingTests().payload();payload['center_sent_at']=time.time()
  with self.app.center_db()as c:c.execute("UPDATE nodes SET last_seen=5 WHERE id='win2'")
  self.assertEqual(self.client.post('/agent/win2/resource-sample',json=payload).status_code,401)
  self.assertEqual(self.client.post('/agent/win2/resource-sample',json=payload,headers=self.fixture.auth).status_code,200)
  with self.app.center_db()as c:
   row=c.execute("SELECT last_seen,telemetry FROM nodes WHERE id='win2'").fetchone()
  self.assertEqual(row['last_seen'],5)

 def test_probe_is_authenticated_and_does_not_change_liveness(self):
  url='/agent/win2/resource-sample'
  self.assertEqual(self.client.post(url,json={'probe':True}).status_code,401)
  self.assertIn('server_time',self.client.post(url,json={'probe':True},headers=self.fixture.auth).json)

 def test_controller_observer_remains_compatible_with_resource_sampler(self):
  row=dict(id='controller:fixture',name='fixture',lifecycle='queued',process_state='running')
  self.assertEqual(self.client.post('/agent/win2/controller-observations',json={'items':[row]},headers=self.fixture.auth).status_code,200)

 def test_terminal_evidence_remains_compatible_with_resource_sampler(self):
  item=dict(id='fixture',name='fixture',failure_detail='guard',failure_code='memory_pressure',failure_evidence={},completed=1,total=2,source_age_seconds=0)
  self.assertEqual(self.client.post('/agent/win2/terminal-evidence',json={'items':[item]},headers=self.fixture.auth).status_code,200)
