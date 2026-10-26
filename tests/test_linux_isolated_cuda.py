import importlib.util,os,unittest
from pathlib import Path
path=Path(os.environ.get('LINUX_RPC_PATH',str(Path(__file__).parents[1]/'external/linux_rpc.py')))
spec=importlib.util.spec_from_file_location('cuda_rpc',path);rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)


class Tests(unittest.TestCase):
 def test_isolated_nonzero_host_device_is_logical_zero(self):
  self.assertEqual(rpc.cuda_visible_devices({'gpu_ids':[6],'requires_isolation':True}),'0')
 def test_isolated_multiple_devices_preserve_allocation_order(self):
  self.assertEqual(rpc.cuda_visible_devices({'gpu_ids':[2,6],'requires_isolation':True}),'0,1')
 def test_nonisolated_keeps_host_identifiers(self):
  self.assertEqual(rpc.cuda_visible_devices({'gpu_ids':[2,6],'requires_isolation':False}),'2,6')
 def test_cpu_task_hides_all_cuda_devices(self):
  self.assertEqual(rpc.cuda_visible_devices({'requires_isolation':True}),'')


if __name__=='__main__':unittest.main()
