import sys,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import gpu_inventory
class GpuInventoryTest(unittest.TestCase):
 def test_desktop_memory_does_not_block_idle_card(self):
  with patch('agent.subprocess.check_output',side_effect=['0, GPU-a, RTX, 8192, 6000, 2\n','']):
   self.assertTrue(gpu_inventory()[0]['available'])
 def test_idle_compute_context_still_blocks_card(self):
  with patch('agent.subprocess.check_output',side_effect=['0, GPU-a, RTX, 8192, 6000, 0\n','GPU-a, 123, python.exe\n']):
   self.assertFalse(gpu_inventory()[0]['available'])
 def test_windows_desktop_context_does_not_block(self):
  from types import SimpleNamespace
  with patch('agent.os',SimpleNamespace(name='nt')),patch('agent.subprocess.CREATE_NO_WINDOW',0,create=True),patch('agent.subprocess.check_output',side_effect=['0, GPU-a, RTX, 8192, 6000, 2\n','GPU-a, 123, C:/Windows/System32/dwm.exe\n']):
   self.assertTrue(gpu_inventory()[0]['available'])
 def test_failed_process_inventory_keeps_specs_but_blocks_admission(self):
  with patch('agent.subprocess.check_output',side_effect=['0, GPU-a, RTX, 8192, 6000, 0\n',subprocess.TimeoutExpired('nvidia-smi',4)]):
   g=gpu_inventory()[0];self.assertEqual(g['name'],'RTX');self.assertFalse(g['available']);self.assertFalse(g['compute_verified'])
