import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from resource_monitor import PressureWindow
class PressureTest(unittest.TestCase):
 def setUp(self):self.p=PressureWindow()
 def sample(self,t,**kw):
  data=dict(cpu_percent=10,total_gb=128,available_gb=70,reserve_gb=8,cpu_threads=20,counters=dict(pages_in=0,pages_out=0,read_latency=.001));data.update(kw)
  return self.p.observe(t,**data)
 def test_transient_io_not_pressure_and_warmup_required(self):
  for t in range(0,25,5):r=self.sample(t)
  self.assertEqual(r['state'],'normal')
  r=self.sample(25,counters=dict(pages_in=2000,pages_out=2000,read_latency=.1));self.assertEqual(r['state'],'normal')
 def test_sustained_paging_and_recovery_hysteresis(self):
  for t in range(0,25,5):r=self.sample(t,counters=dict(pages_in=2000,pages_out=2000,read_latency=.004))
  self.assertEqual(r['state'],'pressured')
  for t in range(25,65,5):r=self.sample(t)
  self.assertEqual(r['state'],'pressured')
  r=self.sample(65);self.assertEqual(r['state'],'normal')
 def test_invalid_counter_and_sample_gap_do_not_look_idle(self):
  for t in range(0,25,5):self.sample(t)
  r=self.sample(25,counters=None);self.assertEqual(r['state'],'unknown');self.assertFalse(r['verified'])
  r=self.sample(100);self.assertEqual(r['state'],'warming');self.assertFalse(r['verified'])
 def test_cpu_high_percentile_subtracts_managed_cpu(self):
  for t in range(0,25,5):r=self.sample(t,cpu_percent=50,managed_cores=8)
  self.assertEqual(r['external_cpu_cores'],2)
  r=self.sample(25,cpu_percent=0);self.assertEqual(r['external_cpu_cores'],2)
