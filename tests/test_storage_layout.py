import json,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import storage_layout,resource_metadata,resource_policy
import test_scheduler

class StorageLayoutTest(unittest.TestCase):
 def test_overflow_isolated_job_keeps_stable_path(self):
  with tempfile.TemporaryDirectory() as d:
   canonical=Path(d).resolve()/'canonical';alternate=Path(d).resolve()/'other';canonical.mkdir();alternate.mkdir();cfg={'work_roots':[str(alternate)]};jid='a'*32
   if sys.platform=='win32':self.skipTest('Windows junction and ACL covered by live qualification')
   link=storage_layout.job_folder(canonical,jid,cfg,str(alternate))
   (link/'value').write_text('kept');self.assertEqual((alternate/jid/'value').read_text(),'kept')
   self.assertEqual(storage_layout.trusted_folder(canonical,jid,cfg),alternate/jid)
   self.assertEqual(storage_layout.job_folder(canonical,jid,cfg,str(alternate)),link)
   with self.assertRaises(ValueError):storage_layout.job_folder(canonical,jid,cfg,str(canonical))
 def test_unconfigured_volume_and_escaping_link_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'root';root.mkdir();outside=Path(d)/'other';outside.mkdir();jid='b'*32
   with self.assertRaises(ValueError):storage_layout.job_folder(root,jid,{},'unknown')
   if sys.platform!='win32':
    (root/jid).symlink_to(outside,target_is_directory=True)
    with self.assertRaises(ValueError):storage_layout.trusted_folder(root,jid,{})
 def test_allocator_accounts_each_work_volume_and_retains_old_jobs(self):
  f=test_scheduler.SchedulingTest();n=f.node(work_volumes=[dict(id='C',free_gb=20,reserve_gb=4),dict(id='D',free_gb=100,reserve_gb=10)],default_work_volume='C')
  old=f.job('old',disk_gb=16,cpu_ids=[0]);old.update(status='running',node='a')
  p,_=__import__('scheduler').allocate(n,f.job(disk_gb=20)['spec'],[old]);self.assertEqual(p['work_volume'],'D')
  full=f.job('full',disk_gb=70,cpu_ids=[1],work_volume='D');full.update(status='running',node='a')
  p,reason=__import__('scheduler').allocate(n,f.job(disk_gb=20)['spec'],[old,full]);self.assertIsNone(p);self.assertIn('工作盘',reason)
 def test_static_retained_allowance_is_added_and_retries_not_shrunk(self):
  model=resource_policy.workload_model({'family':'buffered-v1','features':{'cells':20}},'digest');model['additional_memory_gb']=2
  spec=dict(resource_mode='estimated',restart_policy='from_start',resource_model=model,memory_gb=32,commit_gb=40)
  samples=[dict(id='a',model=model,observation=dict(cleanup_verified=True,peak_gb=1,peak_private_gb=1))]
  revised=resource_policy.fit_completed(spec,samples);self.assertEqual(revised['memory_gb'],4.5)
  self.assertEqual(resource_policy.fit_completed(dict(spec,resource_retries=1),samples)['memory_gb'],32)


class BufferedMetadataTest(unittest.TestCase):
 def test_only_verified_buffered_program_uses_duration_independent_state(self):
  import zipfile
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'pkg.zip';table={k:dict(id=k,environment='e',stage=stage,condition='strong_bursts',duration_ms=duration,input_scale=dict(modeled_cells=20,recorded_segments=1000)) for k,stage,duration in [('short','short_network',600),('long','long_network',5000)]}
   main='"""Self-contained H01 batch entry. All writes are confined to this task workdir."""\n'+"argv=['--no-savecurrents','--buffer-ms','1']"
   with zipfile.ZipFile(p,'w') as z:
    z.writestr('main.py',main);z.writestr('task_table.json',json.dumps(table));z.writestr('source_manifests.json','{}');z.writestr('scripts/full_window_network_fixed_active_v3.py','\n'.join(['for vector in im+[t]+vv:vector.resize(0)','np.arange(args.buffer_ms,duration,args.buffer_ms)','raw.append(ima@coeff.T)','volts[n:]=va','max(100,int(duration*4))']))
   spec=dict(kind='python',runtime='python-neuron',entry='main.py',cores=1)
   short=resource_metadata.legacy_model(p,dict(spec,task_key='short'));long=resource_metadata.legacy_model(p,dict(spec,task_key='long'))
   self.assertEqual(short['features'],long['features']);self.assertGreater(long['additional_memory_gb'],short['additional_memory_gb']);self.assertEqual(short['family'],long['family'])
   p2=Path(d)/'unverified.zip'
   with zipfile.ZipFile(p2,'w') as z:
    z.writestr('main.py',main);z.writestr('task_table.json',json.dumps(table));z.writestr('source_manifests.json','{}')
   unverified=resource_metadata.legacy_model(p2,dict(spec,task_key='long'));self.assertIn('duration_ms',unverified['features']);self.assertNotIn('additional_memory_gb',unverified)

class PrelaunchStorageRecoveryTest(unittest.TestCase):
 def test_only_empty_never_launched_directory_is_retired(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'jobs';root.mkdir();jid='c'*32;(root/jid).mkdir()
   self.assertTrue(storage_layout.retire_empty_isolated_setup(root,jid,{}))
   report=json.loads((root/jid/'exit.json').read_text());self.assertTrue(report['cleanup_verified']);self.assertEqual(report['failure_code'],'environment_setup')
   self.assertFalse(storage_layout.retire_empty_isolated_setup(root,jid,{}))
 def test_live_or_pending_execution_is_never_retired(self):
  for marker in ('runner.json','launching.json','package.zip'):
   with tempfile.TemporaryDirectory() as d:
    root=Path(d)/'jobs';root.mkdir();jid='d'*32;p=root/jid;p.mkdir();(p/marker).write_text('{}')
    self.assertFalse(storage_layout.retire_empty_isolated_setup(root,jid,{}));self.assertFalse((p/'exit.json').exists())
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'jobs';root.mkdir();jid='e'*32;(root/jid).mkdir();requests=root.parent/'broker/requests';requests.mkdir(parents=True);(requests/(jid+'.json')).write_text('{}')
   self.assertFalse(storage_layout.retire_empty_isolated_setup(root,jid,{}))

class ManagedWorkVolumeTest(unittest.TestCase):
 def test_central_process_is_not_double_counted_as_external(self):
  t=dict(external_jobs=[dict(pid=10,elapsed_minutes=1,cpu_ids=[0]),dict(pid=11,elapsed_minutes=2,cpu_ids=[1])],external_busy=True,external_reserved_cores=2,cpu_pool=[0,1,2])
  storage_layout.normalize_external(t,dict(at=100,managed=[dict(pid=10,created=40,job='a'*32)]),101,100)
  self.assertEqual([p['pid'] for p in t['external_jobs']],[11]);self.assertEqual(t['external_reserved_cores'],1)
 def test_legacy_tenth_minute_age_rounding_is_supported(self):
  t=dict(external_jobs=[dict(pid=10,elapsed_minutes=1,cpu_ids=[0])],external_busy=True,external_reserved_cores=1,cpu_pool=[0,1])
  storage_layout.normalize_external(t,dict(at=100,managed=[dict(pid=10,created=37,job='a'*32)]),101,100)
  self.assertFalse(t['external_busy']);self.assertEqual(t['external_reserved_cores'],0)
 def test_stale_or_reused_pid_is_not_excluded(self):
  for observed,created in [(80,40),(100,90)]:
   t=dict(external_jobs=[dict(pid=10,elapsed_minutes=1,cpu_ids=[0])],external_busy=True,external_reserved_cores=1,cpu_pool=[0,1])
   storage_layout.normalize_external(t,dict(at=observed,managed=[dict(pid=10,created=created)]),101,100)
   self.assertTrue(t['external_busy']);self.assertEqual(t['external_reserved_cores'],1)
 def test_platform_retry_keeps_original_queue_age(self):
  import scheduler
  f=test_scheduler.SchedulingTest();n=f.node(max_parallel=1)
  old=f.job('old',1,task_key='same');old.update(status='failed',batch_id='batch')
  retry=f.job('retry',3,task_key='same',execution_retries=1);retry['batch_id']='batch'
  other=f.job('other',2)
  p,_=scheduler.plan([n],[old,other,retry]);self.assertEqual(list(p),['retry'])

class IsolatedVolumeAclTest(unittest.TestCase):
 def test_launch_hook_grants_physical_folder_and_link_separately(self):
  import provision_job_storage
  if sys.platform=='win32':self.skipTest('Qualified on live Windows node')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d).resolve();jobs=root/'jobs';jobs.mkdir();other=root/'other';other.mkdir();(root/'config').mkdir();jid='f'*32
   cfg=dict(work_roots=[str(other)]);(root/'config/agent.json').write_text(json.dumps(cfg));requests=root/'broker/requests';requests.mkdir(parents=True);(requests/(jid+'.json')).write_text('{}')
   storage_layout.job_folder(jobs,jid,cfg,str(other))
   with patch('provision_job_storage.subprocess.run') as call:
    provision_job_storage.grant(root,jid[:17],'S-1-5-21-1-2-3-4')
    self.assertEqual(call.call_args_list[0].args[0][1],str(other/jid));self.assertIn('/L',call.call_args_list[1].args[0])
   receipts=root/'broker/receipts';receipts.mkdir();(receipts/(jid+'.json')).write_text(json.dumps(dict(status='retired')))
   with patch('storage_layout.remove_retired_sid') as call:
    storage_layout.cleanup_volume_acls(jobs,cfg);self.assertEqual(call.call_count,2)
   self.assertTrue(json.loads((root/'broker/volume-acls'/(jid+'.json')).read_text())['cleaned'])

class PrelaunchTransferEvidenceTest(unittest.TestCase):
 def test_only_incomplete_never_launched_packages_are_recovered(self):
  import zipfile
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'jobs';root.mkdir();jid='1'*32;folder=root/jid;folder.mkdir();p=folder/'package.zip';p.write_bytes(b'PK partial')
   self.assertTrue(storage_layout.verify_incomplete_prelaunch(root,jid,{}))
   (folder/'launching.json').write_text('{}');self.assertFalse(storage_layout.verify_incomplete_prelaunch(root,jid,{}));(folder/'launching.json').unlink()
   with zipfile.ZipFile(p,'w') as z:z.writestr('main.py','pass')
   self.assertFalse(storage_layout.verify_incomplete_prelaunch(root,jid,{}))
 def test_truncated_download_cannot_replace_valid_package(self):
  import agent,io
  from unittest.mock import MagicMock
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'package.zip';p.write_bytes(b'previous verified package')
   worker=agent.Worker.__new__(agent.Worker);worker.cfg={'token':'test'};worker.state=None;worker.base='https://example.test';worker.opener=MagicMock()
   def response(request,**kwargs):
    r=io.BytesIO(b'' if request.get_method()=='HEAD' else b'PK partial');r.headers={'Content-Length':'100','Content-Range':'bytes 0-99/100'};r.status=206
    return r
   worker.opener.open.side_effect=response
   with patch('agent.time.sleep'),self.assertRaises(ConnectionError):worker.api('/package',method='GET',download=p)
   self.assertEqual(p.read_bytes(),b'previous verified package')
