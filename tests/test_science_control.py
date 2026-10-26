import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from science_control import ScienceControl,matched,generation
from science_restart import restart
class ScienceTest(unittest.TestCase):
 def test_exact_process_pause_resume_cancel_and_stale_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);script=root/'experiment.py'
   script.write_text("import time\nfrom pathlib import Path\nfor i in range(500):\n Path('ticks').write_text(str(i));time.sleep(.05)\n")
   cfg={'spool':str(root/'control'),'recipes':{'test':{'matches':[[str(script)]]}}}
   path=root/'config.json';path.write_text(json.dumps(cfg));manager=ScienceControl(path)
   proc=subprocess.Popen([sys.executable,str(script)],cwd=root)
   try:
    deadline=time.time()+10
    while not (root/'ticks').exists() and time.time()<deadline:time.sleep(.05)
    recipe=cfg['recipes']['test'];record={};token=generation(matched(recipe))
    with self.assertRaises(ValueError):manager.perform(recipe,record,{'action':'cancel','generation':'stale'})
    self.assertIsNone(proc.poll())
    manager.perform(recipe,record,{'action':'pause','generation':token})
    tick=(root/'ticks').read_text();time.sleep(.2);self.assertEqual(tick,(root/'ticks').read_text())
    manager.perform(recipe,record,{'action':'resume','generation':token});time.sleep(.2)
    self.assertNotEqual(tick,(root/'ticks').read_text())
    manager.perform(recipe,record,{'action':'cancel','generation':token});proc.wait(timeout=5)
    self.assertTrue((root/'ticks').exists());self.assertEqual(record['status'],'cancelled')
   finally:
    if proc.poll() is None:proc.kill();proc.wait()
 def test_restart_backs_up_before_running(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);result=root/'result.txt';result.write_text('old result')
   script=root/'restart.py';script.write_text("from pathlib import Path\nPath('result.txt').write_text('new result')\n")
   recipe=dict(matches=[[str(script)]],restart_task='LabCompute-Science-test',preserve_paths=[str(result)],argv=[sys.executable,str(script)],cwd=str(root))
   cfg=dict(spool=str(root/'spool'),backup_root=str(root/'backups'),recipes={'test':recipe})
   config=root/'config.json';config.write_text(json.dumps(cfg));restart(config,'test')
   self.assertEqual(result.read_text(),'new result')
   self.assertEqual(next((root/'backups').rglob('0_result.txt')).read_text(),'old result')
 def test_unknown_recipe_has_error_response(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);config=root/'cfg.json';config.write_text(json.dumps(dict(spool=str(root/'spool'),recipes={})))
   manager=ScienceControl(config);p=manager.root/'requests';p.mkdir();(p/'test.json').write_text(json.dumps(dict(simulation='not-registered',action='cancel')))
   manager.once();result=json.loads((manager.root/'responses/test.json').read_text());self.assertEqual(result['status'],'failed')

 def test_discovery_tracks_new_version_and_dedicated_parent_without_unsafe_restart(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);analysis=root/'experiment/analysis';script=str(analysis/'scripts/full_window_network_fixed_active_v4_buffered.py')
   key=str(analysis/'environment_networks_fixed_active_v3/H01_L6_R05/progress.json')
   cfg=dict(spool=str(root/'spool'),discovery_roots=[str(root)],recipes={key:dict(matches=[[script.replace('v4_buffered','v3')]],restart_task='LabCompute-Science-old')})
   config=root/'cfg.json';config.write_text(json.dumps(cfg));m=ScienceControl(config)
   args=['python',script,'--environment','H01_L6_R05'];parent=['python',str(root/'experiment/bootstrap/run_l6r05_600ms_v4_equivalence.py')]
   m.discover([dict(pid=1,ppid=0,start=1,args=parent),dict(pid=2,ppid=1,start=2,args=args)])
   recipe=m.discovered[key];self.assertNotIn('restart_task',recipe);self.assertEqual(len(recipe['matches']),2)
   self.assertIn(args[1:],recipe['matches']);self.assertIn(parent[1:],recipe['matches'])
   m.discover([dict(pid=3,ppid=0,start=3,args=['python','/outside/scripts/full_window_network_fixed_active_v4.py'])]);self.assertEqual(len(m.discovered),1)
