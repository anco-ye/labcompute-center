import json,os,subprocess,sys,tempfile,unittest,zipfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class RunnerTest(unittest.TestCase):
 def test_zero_exit_native_failure_is_not_success(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'work').mkdir()
   cfg=dict(argv=[sys.executable,'-c',"print('java.security.AccessControlException: access denied user.dir')"],spec=dict(kind='probe',runtime='fixture',cores=1,memory_gb=1,max_minutes=1))
   (root/'launch.json').write_text(json.dumps(cfg))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=15)
   result=json.loads((root/'exit.json').read_text());self.assertEqual(result['status'],'failed');self.assertEqual(result['exit_code'],0)
   self.assertEqual(result['failure_code'],'application_error')
 def test_published_native_runtime_environment_reaches_child(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir();native=root/'native';native.mkdir()
   (native/'native_runtime_fixture.py').write_text('VALUE=42')
   source="import os,native_runtime_fixture;assert native_runtime_fixture.VALUE==42;assert os.environ['NEURONHOME']=='fixture-home';assert os.environ['PYTHONPATH'].split(os.pathsep)[0].endswith('vendor')"
   cfg=dict(argv=[sys.executable,'-c',source],spec=dict(kind='probe',runtime='fixture',cores=1,memory_gb=1,max_minutes=1),runtime=dict(python_paths=[str(native)],path_entries=[str(native)],neuron_home='fixture-home'))
   (root/'launch.json').write_text(json.dumps(cfg))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=15)
   self.assertEqual(json.loads((root/'exit.json').read_text())['status'],'succeeded')
 def test_natural_failure_and_resource_limits_keep_terminal_outcomes(self):
  scenarios=[('ok','print(42)',{},'succeeded',None),('error',"raise RuntimeError('fixture failure')",{},'failed','fixture failure'),('memory','import time;time.sleep(10)',{'memory_gb':.001},'failed','内存上限'),('legacy_deadline','import time;time.sleep(.6)',{'deadline':time.time()-1,'max_minutes':.00001},'succeeded',None)]
  for name,source,override,expected,reason in scenarios:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);(root/'work').mkdir();(root/'work/partial.txt').write_text('preserved')
    spec=dict(kind='probe',runtime='fixture',cores=1,memory_gb=1,max_minutes=1);spec.update(override)
    (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-c',source],spec=spec)))
    subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=15)
    result=json.loads((root/'exit.json').read_text());self.assertEqual(result['status'],expected)
    if reason:self.assertIn(reason,result['detail'])
    self.assertEqual((root/'work/partial.txt').read_text(),'preserved')
 def test_entry_exit_with_live_child_is_failure_and_cleans_child(self):
  import psutil
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir()
   source="import subprocess,sys,time,json\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\nPath('child-pid').write_text(str(p.pid))\nPath('partial.txt').write_text('preserved')\ntime.sleep(0.01)\n"
   script=work/'main.py';script.write_text(source)
   spec=dict(kind='probe',runtime='fixture',cores=1,memory_gb=1,max_minutes=1)
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,str(script)],spec=spec)))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=20)
   result=json.loads((root/'exit.json').read_text());self.assertEqual(result['status'],'failed');self.assertIn('子进程未完成',result['detail'])
   self.assertEqual((work/'partial.txt').read_text(),'preserved')
   try:self.assertEqual(psutil.Process(int((work/'child-pid').read_text())).status(),psutil.STATUS_ZOMBIE)
   except psutil.NoSuchProcess:pass
 def isolated(self,auto=False,nested=False):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir();wheels=root/'wheels';wheels.mkdir()
   with zipfile.ZipFile(wheels/'lab_fixture-1.0-py3-none-any.whl','w') as z:
    z.writestr('lab_fixture.py',('import lab_extra1\nimport lab_extra2\n' if nested else '')+'VALUE=42\n')
    z.writestr('lab_fixture-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: lab-fixture\nVersion: 1.0\n')
    z.writestr('lab_fixture-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    z.writestr('lab_fixture-1.0.dist-info/RECORD','')
   if nested:
    for name in ('lab_extra1','lab_extra2'):
     with zipfile.ZipFile(wheels/(name+'-1.0-py3-none-any.whl'),'w') as z:
      z.writestr(name+'.py','VALUE=1\n')
      z.writestr(name+'-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: '+name.replace('_','-')+'\nVersion: 1.0\n')
      z.writestr(name+'-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
      z.writestr(name+'-1.0.dist-info/RECORD','')
   (work/'requirements.txt').write_text('--no-index\nlab-fixture==1.0\n')
   (work/'main.py').write_text("from pathlib import Path\nassert not Path('once').exists()\nPath('once').touch()\nimport lab_fixture,importlib.util,json,sys\nassert lab_fixture.VALUE==42\nassert importlib.util.find_spec('flask') is None\nopen('result.json','w').write(json.dumps({'answer':lab_fixture.VALUE,'python':sys.executable}))\n")
   spec=dict(kind='python',runtime='python',cores=1,memory_gb=2,max_minutes=3,requirements=None if auto else 'requirements.txt')
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-u',str(work/'main.py')],spec=spec,runtime={'executable':sys.executable,'revision':'test'},wheelhouse=str(wheels))))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=90,env=dict(os.environ,PIP_NO_INDEX='1'))
   result=json.loads((root/'exit.json').read_text());self.assertEqual(result['status'],'succeeded',(root/'task.log').read_text())
   self.assertEqual(json.loads((work/'result.json').read_text())['answer'],42)
   self.assertIn('lab-fixture==1.0',(root/'environment-lock.txt').read_text())
   self.assertTrue((root/'execution.json').exists())
   if auto:self.assertEqual(json.loads((root/'dependency-status.json').read_text())['status'],'installed')
 def test_published_runtime_pin_rejects_project_override(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir();wheels=root/'wheels';wheels.mkdir()
   for version in ('1.0','2.0'):
    dist='lab_fixture-'+version+'.dist-info/'
    with zipfile.ZipFile(wheels/('lab_fixture-'+version+'-py3-none-any.whl'),'w') as z:
     z.writestr('lab_fixture.py','VALUE=42\n')
     z.writestr(dist+'METADATA','Metadata-Version: 2.1\nName: lab-fixture\nVersion: '+version+'\n')
     z.writestr(dist+'WHEEL','Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
     z.writestr(dist+'RECORD','')
   (work/'requirements.txt').write_text('lab-fixture==2.0\n')
   (work/'main.py').write_text("from pathlib import Path;Path('entry-started').touch()")
   spec=dict(kind='python',runtime='pinned',entry='main.py',cores=1,memory_gb=2,max_minutes=2,requirements='requirements.txt')
   runtime=dict(executable=sys.executable,packages=['lab-fixture==1.0'],protected_packages=['lab-fixture==1.0'])
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,str(work/'main.py')],spec=spec,runtime=runtime,wheelhouse=str(wheels))))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=60,env=dict(os.environ,PIP_NO_INDEX='1'))
   result=json.loads((root/'exit.json').read_text())
   self.assertEqual(result['status'],'failed');self.assertEqual(result['failure_code'],'environment_setup')
   self.assertFalse((work/'entry-started').exists())
   self.assertIn('ResolutionImpossible',(root/'task.log').read_text())
 def test_missing_cached_template_downloads_from_published_index(self):
  import http.server,threading,functools
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir();wheels=root/'empty-cache';wheels.mkdir()
   index=root/'index';package=index/'simple/lab-fixture';package.mkdir(parents=True)
   name='lab_fixture-1.0-py3-none-any.whl'
   with zipfile.ZipFile(package/name,'w') as z:
    z.writestr('lab_fixture.py','VALUE=42\n')
    z.writestr('lab_fixture-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: lab-fixture\nVersion: 1.0\n')
    z.writestr('lab_fixture-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    z.writestr('lab_fixture-1.0.dist-info/RECORD','')
   (package/'index.html').write_text('<a href="'+name+'">fixture</a>')
   handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=str(index))
   server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    (work/'main.py').write_text("import lab_fixture;assert lab_fixture.VALUE==42")
    spec=dict(kind='python',runtime='pinned',entry='main.py',cores=1,memory_gb=2,max_minutes=2)
    runtime=dict(executable=sys.executable,packages=['lab-fixture==1.0'],protected_packages=['lab-fixture==1.0'],pip_index_url='http://127.0.0.1:'+str(server.server_port)+'/simple')
    (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,str(work/'main.py')],spec=spec,runtime=runtime,wheelhouse=str(wheels))))
    env=dict(os.environ,PIP_CONFIG_FILE=os.devnull);env.pop('PIP_NO_INDEX',None)
    subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=60,env=env)
    self.assertEqual(json.loads((root/'exit.json').read_text())['status'],'succeeded',(root/'task.log').read_text())
    self.assertIn('lab-fixture==1.0',(root/'environment-lock.txt').read_text())
   finally:server.shutdown();server.server_close();thread.join()
 def test_cancel_before_start(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'work').mkdir();(root/'cancel').touch()
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-c','raise Exception()'],spec=dict(kind='python',runtime='python',cores=1,memory_gb=1,max_minutes=1))))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=10)
   self.assertEqual(json.loads((root/'exit.json').read_text())['status'],'cancelled')

 def test_isolated_dependency_and_result(self):self.isolated()
 def test_automatic_dependency_without_replaying_entry(self):self.isolated(auto=True)
 def test_multiple_missing_transitive_dependencies(self):self.isolated(auto=True,nested=True)
 def test_missing_dependency_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir()
   (work/'main.py').write_text('import lab_nonexistent_dependency_437692\n')
   spec=dict(kind='python',runtime='python',entry='main.py',args=[],cores=1,memory_gb=2,max_minutes=3)
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-u',str(work/'main.py')],spec=spec,runtime={'executable':sys.executable})))
   subprocess.run([sys.executable,str(ROOT/'runner.py'),str(root)],check=True,timeout=90,env=dict(os.environ,PIP_NO_INDEX='1'))
   result=json.loads((root/'exit.json').read_text());self.assertEqual(result['status'],'failed')
   self.assertIn('lab_nonexistent_dependency_437692',result['detail']);self.assertIn('自动安装失败',result['detail'])
 def test_pause_resume_cancel_running(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);work=root/'work';work.mkdir()
   (work/'main.py').write_text("import time,json\nfrom pathlib import Path\nfor i in range(200):\n Path('ticks').write_text(str(i))\n Path('progress.json').write_text(json.dumps(dict(completed=i,total=200)))\n time.sleep(.1)\n")
   spec=dict(kind='python',runtime='python',entry='main.py',args=[],cores=1,memory_gb=2,max_minutes=3)
   (root/'launch.json').write_text(json.dumps(dict(argv=[sys.executable,'-u',str(work/'main.py')],spec=spec,runtime={'executable':sys.executable})))
   proc=subprocess.Popen([sys.executable,str(ROOT/'runner.py'),str(root)])
   def wait_for(fn):
    deadline=time.time()+60
    while time.time()<deadline:
     try:
      if fn():return
     except (OSError,ValueError):pass
     time.sleep(.1)
    self.fail('runner transition timed out')
   try:
    wait_for(lambda:json.loads((root/'usage.json').read_text()).get('progress',{}).get('total')==200);(root/'pause').touch()
    wait_for(lambda:json.loads((root/'usage.json').read_text())['phase']=='paused')
    self.assertEqual(json.loads((root/'usage.json').read_text())['progress']['total'],200)
    before=(work/'ticks').read_text();time.sleep(.6);self.assertEqual(before,(work/'ticks').read_text())
    (root/'pause').unlink();wait_for(lambda:(work/'ticks').read_text()!=before)
    (root/'pause').touch();wait_for(lambda:json.loads((root/'usage.json').read_text())['phase']=='paused')
    (root/'cancel').touch();proc.wait(timeout=15)
    self.assertEqual(json.loads((root/'exit.json').read_text())['status'],'cancelled')
   finally:
    (root/'cancel').touch()
    if proc.poll() is None:proc.wait(timeout=15)
