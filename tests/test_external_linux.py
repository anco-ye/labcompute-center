import importlib.util,json,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import common,scheduler
spec=importlib.util.spec_from_file_location('linux_rpc',Path(__file__).parents[1]/'external/linux_rpc.py');rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)

class LinuxContractsTest(unittest.TestCase):
    def test_concurrent_atomic_writers_keep_complete_json(self):
        import concurrent.futures,threading
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'host-inventory.json';barrier=threading.Barrier(8)
            replace=Path.replace;staging=[]
            def synchronized_replace(source,destination):
                staging.append(source);barrier.wait(timeout=10)
                return replace(source,destination)
            with patch.object(Path,'replace',synchronized_replace):
                with concurrent.futures.ThreadPoolExecutor(8) as pool:
                    list(pool.map(lambda i:rpc.atomic(target,dict(writer=i,payload='x'*10000)),range(8)))
            self.assertEqual(len(set(staging)),8)
            self.assertEqual(json.loads(target.read_text())['payload'],'x'*10000)
            self.assertEqual(list(root.iterdir()),[target])
    def test_atomic_failure_preserves_previous_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'exit.json';target.write_text('{"previous":true}')
            with patch.object(Path,'replace',side_effect=OSError('unavailable')):
                with self.assertRaises(OSError):rpc.atomic(target,dict(next=True))
            self.assertEqual(json.loads(target.read_text()),dict(previous=True))
            self.assertEqual(list(root.iterdir()),[target])
    def test_explicit_os_and_legacy_default(self):
        self.assertEqual(common.task_spec(dict(entry='main.py'),{'main.py'})['os'],'windows')
        self.assertEqual(common.task_spec(dict(entry='main.py',os='linux'),{'main.py'})['os'],'linux')
        with self.assertRaises(ValueError):common.task_spec(dict(entry='main.py',os='guess'),{'main.py'})
    def test_os_guard_precedes_capacity(self):
        n=dict(id='a800',enabled=True,last_seen=100,telemetry=dict(os='linux',runtimes={}))
        self.assertIn('操作系统',scheduler.readiness(n,dict(runtime='python'),100))
        n['telemetry']['os']='windows'
        self.assertIn('操作系统',scheduler.readiness(n,dict(runtime='python',os='linux'),100))
    def test_remote_archive_traversal_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);z=root/'p.zip'
            for name in ('../escape','/absolute','bad\\path'):
                with zipfile.ZipFile(z,'w') as a:a.writestr(name,'bad')
                with self.assertRaises(ValueError):rpc.extract(z,root/'work')
    def test_durable_intent_never_launches_twice_or_guesses_completion(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'ctl',return_value=dict(ActiveState='inactive',LoadState='not-found')):
            req=dict(action='status',id='a'*32,lease_digest='b'*64)
            self.assertEqual(rpc.rpc(req)['state'],'absent')
            folder=Path(tmp)/'jobs'/req['id'];folder.mkdir(parents=True)
            rpc.atomic(folder/'identity.json',dict(lease_digest=req['lease_digest']))
            rpc.atomic(folder/'start.json',dict(at=1))
            self.assertEqual(rpc.rpc(req)['state'],'uncertain')
            with patch.object(rpc.subprocess,'run') as run:
                self.assertEqual(rpc.rpc(dict(req,action='start'))['state'],'uncertain')
                run.assert_not_called()
            with self.assertRaises(ValueError):rpc.rpc(dict(req,lease_digest='c'*64))
    def test_live_unit_never_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);rpc.finalize(folder,'unit',dict(ActiveState='active'))
            self.assertFalse((folder/'exit.json').exists())

class BridgeClockTest(unittest.TestCase):
    def test_remote_clock_skew_cannot_disable_fresh_inventory(self):
        from unittest.mock import Mock
        spec=importlib.util.spec_from_file_location('ssh_bridge',Path(__file__).parents[1]/'external/ssh_bridge.py')
        bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
        b=bridge.Bridge.__new__(bridge.Bridge);b.jobs={};b.cfg={};b.recovered=[];b.state_lock=__import__('threading').RLock();b.transfers={};b.connect=Mock(return_value=Mock());b.rpc=Mock(return_value=dict(at=105));b.heartbeat=Mock();b.api=Mock(return_value=dict(job=None))
        with patch.object(bridge.time,'time',return_value=100):b.tick()
        self.assertEqual(b.inventory['at'],100);self.assertEqual(b.inventory['remote_at'],105);self.assertEqual(b.remote_offset,-5)

class ExternalAdmissionLimitsTest(unittest.TestCase):
    def test_backend_limits_reject_before_lease(self):
        import copy
        from test_scheduler import SchedulingTest
        fixture=SchedulingTest();node=fixture.node('linux')
        node['telemetry'].update(os='linux',runtimes={'python-linux':dict(kind='python',status='ready')},execution_limits=dict(max_cpu_threads=8,max_memory_gb=16,max_commit_gb=16,max_disk_gb=20,max_wall_minutes=10080))
        request=fixture.job('work')['spec'];request.update(os='linux',runtime='python-linux',cores=1,memory_gb=1,commit_gb=1,disk_gb=1,max_minutes=60)
        for field,value in [('cores',9),('memory_gb',17),('commit_gb',17),('disk_gb',21)]:
            with self.subTest(field=field):
                changed=dict(request,**{field:value});self.assertIn('后端',scheduler.readiness(node,changed,100))
        self.assertIsNone(scheduler.readiness(node,request,100))
        # User policy removes scientific wall-time caps, including admission
        # against an old backend max_wall_minutes advertisement.
        self.assertIsNone(scheduler.readiness(node,dict(request,max_minutes=10081),100))

class WholeHostA800Test(unittest.TestCase):
    def bridge(self):
        from unittest.mock import Mock
        import release_identity
        spec=importlib.util.spec_from_file_location('whole_bridge',Path(__file__).parents[1]/'external/ssh_bridge.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        b=m.Bridge.__new__(m.Bridge);b.loaded_release=release_identity.snapshot(Path(__file__).parents[1],'bridge');b.cfg=dict(runtimes={},workspace='/own',max_parallel=252,reserve_gb=64,execution_limits={});b.jobs={};b.observation={};b.inventory=dict(at=__import__('time').time(),cpu_ids=list(range(4,256)),host_available_gb=1970,disk_available_gb=950,host_inventory=dict(memory_total_gb=2004,cpu_threads=256,cpu_cores=128,gpu_observed=True,gpus=[dict(index=i,memory_free_gb=79,memory_total_gb=80,compute_occupied=False) for i in range(8)]));b.api=Mock(return_value={});return b
    def test_telemetry_advertises_whole_host_and_many_recoveries(self):
        b=self.bridge();b.jobs={'a':dict(id='a'),'b':dict(id='b')};b.observation={'a':dict(usage=dict(processes=[dict(pid=1,created=2)]))}
        b.api.return_value=dict(recover=[dict(id='c'),dict(id='d')]);b.heartbeat()
        t=b.api.call_args.args[1]['telemetry'];self.assertEqual(t['max_parallel'],252);self.assertEqual(len(t['cpu_pool']),252);self.assertEqual(t['memory_total_gb'],2004);self.assertEqual(len(t['gpus']),8);self.assertEqual(set(t['active_ids']),{'a','b'});self.assertEqual(t['storage_observation']['managed'][0]['job'],'a');self.assertEqual(len(b.recovered),2)
    def test_parallel_leases_keep_both_identities(self):
        from unittest.mock import Mock
        b=self.bridge();b.connect=Mock(return_value=Mock());b.rpc=Mock(return_value=b.inventory);b.heartbeat=Mock();b.recovered=[];b.state_lock=__import__('threading').RLock();b.transfers={};b.persist=Mock();b.tick_job=Mock()
        b.api.side_effect=[dict(job=dict(id='one')),dict(job=dict(id='two')),dict(job=None)];b.tick()
        self.assertEqual(set(b.jobs),{'one','two'});self.assertEqual(b.tick_job.call_count,2)
    def test_allocator_uses_disjoint_cpu_and_gpu_for_concurrent_jobs(self):
        import time
        b=self.bridge();b.heartbeat();t=b.api.call_args.args[1]['telemetry'];t['runtimes']={'python-gpu-linux':dict(status='ready',kind='python',gpu_supported=True)}
        n=dict(id='a800',enabled=True,last_seen=time.time(),telemetry=t)
        def j(i):return dict(id=str(i),created=i,status='queued',spec=dict(kind='python',runtime='python-gpu-linux',os='linux',cores=16,memory_gb=100,commit_gb=100,disk_gb=5,gpus=2,gpu_memory_gb=20))
        placed,_=scheduler.plan([n],[j(i) for i in range(4)])
        self.assertEqual(len(placed),4);self.assertEqual(len({x for v in placed.values() for x in v['spec']['gpu_ids']}),8);self.assertEqual(len({x for v in placed.values() for x in v['spec']['cpu_ids']}),64)

class WorkVolumeRoutingTest(unittest.TestCase):
    def test_placement_is_durable_and_cannot_move_or_change_lease(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)/'default'):
            rpc.ROOT.mkdir();alt=Path(tmp)/'alternate';alt.mkdir()
            with patch.object(rpc,'volume_root',side_effect=lambda v:rpc.ROOT if v=='/Dataset2' else alt if v=='/Dataset1' else (_ for _ in ()).throw(ValueError('unknown'))):
                req=dict(action='prepare',id='d'*32,lease_digest='e'*64,work_volume='/Dataset1')
                result=rpc.rpc(req);self.assertEqual(Path(result['folder']),alt/'jobs'/req['id'])
                self.assertTrue((rpc.ROOT/'locations'/(req['id']+'.json')).is_file())
                self.assertEqual(rpc.validate(dict(req,action='status'))[0],Path(result['folder']))
                with self.assertRaises(ValueError):rpc.validate(dict(req,work_volume='/Dataset2'))
                with self.assertRaises(ValueError):rpc.validate(dict(req,lease_digest='f'*64))
    def test_existing_default_job_cannot_get_alternate_copy(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)/'default'):
            rpc.ROOT.mkdir();jid='d'*32;(rpc.ROOT/'jobs'/jid).mkdir(parents=True)
            alt=Path(tmp)/'alternate';alt.mkdir()
            with patch.object(rpc,'volume_root',return_value=alt):
                with self.assertRaises(ValueError):rpc.validate(dict(action='prepare',id=jid,lease_digest='e'*64,work_volume='/Dataset1'))
    def test_allocator_uses_overflow_without_summing_disks(self):
        from test_scheduler import SchedulingTest
        fixture=SchedulingTest();n=fixture.node(work_volumes=[dict(id='/Dataset2',free_gb=65,reserve_gb=64),dict(id='/Dataset1',free_gb=120,reserve_gb=64)],default_work_volume='/Dataset2')
        request=fixture.job(disk_gb=20)['spec'];placed,_=scheduler.allocate(n,request);self.assertEqual(placed['work_volume'],'/Dataset1')
        request['disk_gb']=60;self.assertIsNone(scheduler.allocate(n,request)[0])

class HostConfigurationTest(unittest.TestCase):
    def test_custom_work_volume_keeps_allocation_under_own_root(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'HOST_CFG',{'volume_roots':{'/':tmp}}),patch.object(rpc,'VOLUME_MOUNTS',('/',)):
            self.assertEqual(rpc.volume_root('/'),Path(tmp))
            with self.assertRaises(ValueError):rpc.volume_root('/unregistered')
    def test_custom_default_retains_unique_placement_binding(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'DEFAULT_VOLUME','/'),patch.object(rpc,'VOLUME_MOUNTS',('/',)),patch.object(rpc,'HOST_CFG',{'volume_roots':{'/':tmp}}):
            req=dict(action='prepare',id='1'*32,lease_digest='2'*64)
            result=rpc.rpc(req)
            self.assertEqual(Path(result['folder']),Path(tmp)/'jobs'/('1'*32))
            saved=json.loads((Path(tmp)/'locations'/('1'*32+'.json')).read_text())
            self.assertEqual(saved['volume'],'/')
            with self.assertRaises(ValueError):rpc.rpc(dict(req,lease_digest='3'*64))
