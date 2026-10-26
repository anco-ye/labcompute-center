import io,json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import common,platform_contract,scheduler
import test_scheduler,test_center

class PlatformContractTest(unittest.TestCase):
 def package(self,**changes):
  raw=dict(kind='python',entry='main.py',runtime='python',platforms=dict(windows=dict(runtime='python-science'),linux=dict(entry='linux.py',runtime='python-science-linux',requirements='linux-requirements.txt')),resources=dict(cpus=2,memory_gb=2,commit_gb=3,disk_gb=4,wall_minutes=10))
  raw.update(changes)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'p.zip'
   with zipfile.ZipFile(p,'w') as z:
    z.writestr('manifest.json',json.dumps(dict(schema_version=1,tasks=[raw])));z.writestr('main.py','print(1)');z.writestr('linux.py','print(1)');z.writestr('linux-requirements.txt','numpy==2.3.5')
   return common.package_specs(p,common.inspect_zip(p))[0]
 def test_variants_are_validated_and_resources_cannot_be_overridden(self):
  s=self.package();self.assertEqual(s['platforms']['linux']['entry'],'linux.py')
  for variants in [[],{},dict(mac={}),dict(linux=dict(runtime='python-linux',memory_gb=1)),dict(linux=dict(runtime='python-linux',entry='../main.py')),dict(linux=dict(runtime='python-linux',requirements='missing.txt')),dict(linux=dict(runtime='python-linux',min_version='unknown'))]:
   with self.subTest(variants=variants),self.assertRaises(ValueError):self.package(platforms=variants)
 def test_comsol_variants_require_a_model_version(self):
  base=dict(kind='comsol',entry='model.mph',args=[],requirements=None)
  raw=dict(platforms=dict(linux=dict(runtime='comsol-linux')))
  with self.assertRaises(ValueError):platform_contract.normalize(raw,base,{'model.mph'},None)
  raw['platforms']['linux']['min_version']='6.4'
  self.assertEqual(platform_contract.normalize(raw,base,{'model.mph'},None)['linux']['min_application_version'],'6.4')
 def test_missing_platform_is_not_silently_enabled(self):
  s=self.package(platforms=dict(linux=dict(runtime='python-linux')))
  self.assertIsNone(platform_contract.resolve(s,'windows'))
 def test_version_gate_and_wrong_app_kind_are_not_bypassed(self):
  r=dict(min_application_version='6.4')
  self.assertFalse(platform_contract.version_ready(r,dict(version='6.2.0.290')))
  self.assertFalse(platform_contract.version_ready(r,{}));self.assertTrue(platform_contract.version_ready(r,dict(version='6.4.0.293')))
 def nodes(self):
  fixture=test_scheduler.SchedulingTest()
  return [fixture.node('win1',os='windows',runtimes={'python-science':dict(kind='python',status='ready')}),fixture.node('dell-r940',os='linux',runtimes={'python-science-linux':dict(kind='python',status='ready')}),fixture.node('a800-ssh',os='linux',runtimes={'python-science-linux':dict(kind='python',status='ready')})]
 def test_preference_and_fallback_resolve_one_lease_with_frozen_resources(self):
  original=self.package();job=dict(id='task',created=1,status='queued',spec=original);nodes=self.nodes()
  for expected in ['dell-r940','a800-ssh','win1']:
   placement,_=scheduler.plan(nodes,[job]);p=placement['task'];self.assertEqual(p['node'],expected)
   self.assertEqual(p['spec']['entry'],'main.py' if expected=='win1' else 'linux.py')
   self.assertEqual(p['spec']['runtime'],'python-science' if expected=='win1' else 'python-science-linux')
   self.assertEqual(p['spec']['memory_gb'],original['memory_gb']);self.assertEqual(job['spec'],original)
   next(n for n in nodes if n['id']==expected)['enabled']=False
 def test_optional_version_constraint_does_not_leak_between_variants(self):
  s=self.package(platforms=dict(windows=dict(runtime='python',min_version='3.12'),linux=dict(runtime='python-linux')))
  w=platform_contract.resolve(s,'windows');l=platform_contract.resolve(w,'linux')
  self.assertNotIn('min_application_version',l)
 def test_single_platform_task_is_never_silently_relabeled(self):
  request=dict(os='windows',runtime='python-science',kind='python',cores=1,memory_gb=1,disk_gb=2)
  self.assertIn('操作系统',scheduler.readiness(self.nodes()[-1],request,time.time()))
 def test_profiles_remain_platform_specific_and_repair_can_move_unleased_work(self):
  s=self.package();a=platform_contract.resolve(s,'linux');b=platform_contract.resolve(a,'windows');c=platform_contract.resolve(s,'windows')
  self.assertNotEqual(a['profile'],b['profile']);self.assertEqual(b['profile'],c['profile'])
  self.assertEqual(b['portable_profile'],s['profile'])
 def test_parallelism_is_bounded_by_resources_without_fixed_one_job_cap(self):
  f=test_scheduler.SchedulingTest();n=f.node('a800-ssh',cpu_pool=list(range(16)),max_parallel=16,memory_total_gb=128,memory_available_gb=120,runtimes={'python':dict(kind='python',status='ready',max_parallel=0)})
  jobs=[f.job(str(i),i,cores=2,memory_gb=1) for i in range(12)];p,reasons=scheduler.plan([n],jobs)
  self.assertEqual(len(p),8);self.assertEqual(len({cpu for v in p.values() for cpu in v['spec']['cpu_ids']}),16)
 def test_active_execution_is_never_relocated(self):
  s=self.package();active=dict(id='existing',created=0,status='running',node='win1',spec=platform_contract.resolve(s,'windows'))
  p,_=scheduler.plan(self.nodes(),[active,dict(id='new',created=1,status='queued',spec=s)])
  self.assertNotIn('existing',p);self.assertEqual(p['new']['node'],'dell-r940')
 def test_upload_to_claim_uses_selected_variant(self):
  f=test_center.CenterTest();f.setUp()
  try:
   f.heartbeat(os='linux',runtimes={'python-linux':dict(kind='python',status='ready')})
   raw=dict(entry='main.py',kind='python',runtime='python',platforms=dict(windows=dict(runtime='python'),linux=dict(runtime='python-linux')))
   response=f.client.post('/api/jobs',data={'package':(f.package(spec=raw),'portable.zip')},headers=f.csrf)
   self.assertEqual(response.status_code,201);job=f.claim();self.assertEqual(job['spec']['runtime'],'python-linux');self.assertEqual(job['spec']['selected_platform'],'linux')
   self.assertEqual(f.claim()['id'],job['id'])
  finally:f.tearDown()


class EffectiveCatalogTest(unittest.TestCase):
 def test_catalog_reports_effective_uncapped_runtime_and_native_versions(self):
  import environment_snapshots
  nodes=[dict(id='win1',label='Win1',last_seen=time.time(),telemetry=dict(runtimes={'comsol':dict(kind='comsol',version='6.2.0.290',max_parallel=1)},scheduling_policy=dict(runtime_max_parallel={'comsol':0})))]
  row=environment_snapshots.catalog(nodes,time.time())[0]
  self.assertEqual(row['max_parallel'],0);self.assertEqual(row['version'],'6.2.0.290')
 def test_native_version_attestation_enriches_telemetry_without_private_executable(self):
  f=test_center.CenterTest();f.setUp()
  try:
   f.heartbeat(runtimes={'comsol':dict(kind='comsol',status='ready',max_parallel=1)})
   (f.root/'scheduling-overrides.json').write_text(json.dumps({'win2':dict(runtime_max_parallel={'comsol':0},verified_runtime_versions={'comsol':dict(version='6.4.0.293',executable='C:/COMSOL/comsolbatch.exe',at=time.time())})}))
   row=f.client.get('/api/environments').json['environments'][0]
   self.assertEqual(row['version'],'6.4.0.293');self.assertEqual(row['max_parallel'],0)
  finally:f.tearDown()

if __name__=='__main__':unittest.main()
