import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from test_external_linux import rpc
import common,scheduler

class LinuxComsolTest(unittest.TestCase):
 def test_mph_manifest_requires_explicit_linux_runtime(self):
  s=common.task_spec(dict(kind='comsol',entry='model.mph',os='linux',runtime='comsol-linux'),{'model.mph'})
  self.assertEqual((s['kind'],s['os'],s['runtime']),('comsol','linux','comsol-linux'))
 def test_comsol_command_pins_threads_and_task_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);exe=root/'comsol';exe.touch()
   with patch.object(rpc,'HOST_CFG',dict(comsol_executable=str(exe),runtimes=['comsol-linux'])):
    s=dict(kind='comsol',runtime='comsol-linux',entry='input.mph',cores=2)
    self.assertTrue(rpc.supported_runtime(s))
    cmd=rpc.application_command(root,s,'python')
    self.assertEqual(cmd[1:3],['-np','2']);self.assertIn('batch',cmd)
    self.assertEqual(cmd[cmd.index('-outputfile')+1],str(root/'work/computed_model.mph'))
    for delta in [dict(entry='input.class'),dict(args=['-np','8']),dict(requirements='requirements.txt')]:
     with self.assertRaises(ValueError):rpc.application_command(root,dict(s,**delta),'python')
    self.assertFalse(rpc.supported_runtime(dict(kind='python',runtime='comsol-linux')))
    self.assertFalse(rpc.supported_runtime(dict(kind='comsol',runtime='python-linux')))
 def test_unpublished_or_missing_executable_is_not_accepted(self):
  with patch.object(rpc,'HOST_CFG',{}):
   self.assertFalse(rpc.supported_runtime(dict(kind='comsol',runtime='comsol-linux')))
   with self.assertRaises(ValueError):rpc.application_command(Path('/tmp'),dict(kind='comsol',runtime='comsol-linux'),'/python')
 def test_existing_python_command_unchanged(self):
  s=dict(kind='python',runtime='python-linux',entry='main.py',args=['--flag'])
  self.assertEqual(rpc.application_command(Path('/job'),s,'/python'),['/python','-B','/job/work/main.py','--flag'])
 def test_comsol_install_is_readonly_in_sandbox(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);exe=r/'comsol/bin/comsol';exe.parent.mkdir(parents=True);exe.touch()
   with patch.object(rpc,'HOST_CFG',dict(comsol_executable=str(exe))),patch.object(rpc.shutil,'which',return_value='/usr/bin/bwrap'),patch.object(rpc.subprocess,'check_output',return_value='/usr\n'):
    cmd=rpc.sandbox_command(r/'job',dict(runtime='comsol-linux'))
   i=cmd.index(str(exe.resolve().parents[1]));self.assertEqual(cmd[i-1],'--ro-bind')

if __name__=='__main__':unittest.main()
