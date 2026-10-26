import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from submission_tools.estimate_memory import estimate
class MemoryEstimatorTest(unittest.TestCase):
 def test_serial_phases_take_peak_and_parallel_private_copies_multiply(self):
  result=estimate(dict(schema_version=1,persistent=[dict(name='base',bytes=2**30)],stages=[dict(name='a',allocations=[dict(name='workers',bytes=2**30,processes=3)]),dict(name='b',allocations=[dict(name='array',shape=[2**27],dtype='float64')])]))
  self.assertEqual(result['predicted_peak_ram_gb'],4)
  self.assertEqual(result['resources']['memory_gb'],4)
  self.assertEqual(result['safety_factor_applied'],1)
 def test_shared_allocation_counted_once_without_assuming_free_commit(self):
  result=estimate(dict(schema_version=1,stages=[dict(name='a',allocations=[dict(name='shared',bytes=2**30,processes=8,shared_across_processes=True)])]))
  self.assertEqual(result['resources']['memory_gb'],1);self.assertEqual(result['resources']['commit_gb'],1)
 def test_unknown_workspace_cannot_be_presented_as_measured_peak(self):
  result=estimate(dict(schema_version=1,stages=[dict(name='solver',allocations=[])],unquantified=['native solver workspace']))
  self.assertEqual(result['status'],'provisional');self.assertEqual(result['resources']['memory_basis'],'peak')
 def test_invalid_negative_allocations_and_ambiguous_shapes_fail(self):
  for item in [dict(name='bad',bytes=-1),dict(name='bad',bytes=1,shape=[2]),dict(name='bad',shape=[-1],dtype='float64')]:
   with self.assertRaises(ValueError):estimate(dict(schema_version=1,stages=[dict(name='bad',allocations=[item])]))
