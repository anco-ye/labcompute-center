import copy,importlib.util,unittest
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'deploy/restrict_failed_platform.py'
s=importlib.util.spec_from_file_location('restriction',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class RestrictionTests(unittest.TestCase):
 def spec(self):return dict(os='windows',runtime='python-neuron',entry='run.py',args=['--stage','current'],cores=2,memory_gb=12,commit_gb=12,disk_gb=20,max_minutes=480,unit_id='original',selected_platform='windows',work_volume='C',cpu_ids=[0,2],platforms=dict(windows=dict(runtime='python-neuron'),linux=dict(runtime='python-neuron-linux')))
 def test_only_declared_alternative_is_selected_and_budget_identity_arguments_preserved(self):
  old=self.spec();before=copy.deepcopy(old);new=m.exclude_platform(old,'windows',{'native_error':'fcntl'})
  self.assertEqual(old,before);self.assertEqual(new['os'],'linux');self.assertEqual(new['runtime'],'python-neuron-linux');self.assertNotIn('windows',new['platforms'])
  for k in ('args','entry','cores','memory_gb','commit_gb','disk_gb','max_minutes','unit_id'):self.assertEqual(new[k],old[k])
  for k in ('selected_platform','work_volume','cpu_ids'):self.assertNotIn(k,new)
 def test_no_fabricated_adapter(self):
  old=self.spec();old['platforms'].pop('linux')
  with self.assertRaises(ValueError):m.exclude_platform(old,'windows',{})
 def test_unknown_platform_is_rejected(self):
  with self.assertRaises(ValueError):m.exclude_platform(self.spec(),'darwin',{})
