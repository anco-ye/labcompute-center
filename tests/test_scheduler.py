import sys,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scheduler
class SchedulingTest(unittest.TestCase):
 def node(self,id='a',**kw):
  t=dict(cpu_threads=8,cpu_pool=list(range(7)),memory_total_gb=32,memory_available_gb=28,reserve_gb=4,max_parallel=7,runtimes={'python':{'kind':'python','status':'ready'}});t.update(kw)
  return dict(id=id,enabled=True,last_seen=time.time(),telemetry=t)
 def job(self,id='j',created=1,**kw):
  s=dict(runtime='python',kind='python',cores=1,memory_gb=4,disk_gb=4);s.update(kw)
  return dict(id=id,created=created,status='queued',spec=s)
 def test_runtime_parallel_override_preserves_resource_limits(self):
  n=self.node(runtimes={'python':{'kind':'python','status':'ready','max_parallel':1}})
  active=self.job('active',cpu_ids=[0]);active.update(status='running',node='a')
  req=self.job()['spec']
  self.assertIsNone(scheduler.allocate(n,req,[active])[0])
  n['telemetry']['scheduling_policy']={'runtime_max_parallel':{'python':0}}
  self.assertIsNotNone(scheduler.allocate(n,req,[active])[0])
  self.assertTrue(all(x<=0 for x in scheduler.reservation_deficits(n,req,[active],time.time())))
  self.assertIsNone(scheduler.allocate(n,dict(req,cores=7),[active])[0])
  self.assertIsNone(scheduler.allocate(n,dict(req,memory_gb=30),[active])[0])
 def test_shared_admission_respects_legacy_guard_and_configured_exposure(self):
  n=self.node(memory_total_gb=128,memory_available_gb=100,memory_committed_gb=80,memory_commit_limit_gb=160,
    external_busy=True,external_reserved_cores=2,resource_observation={'external_cpu_cores':2,'at':time.time(),'verified':True,'state':'normal'},scheduling_policy={
      'external_mode':'shared','shared_memory_gb':48,'shared_memory_fraction':.5,
      'shared_reserve_gb':32,'shared_commit_reserve_gb':36})
  c=scheduler.budget(n);self.assertEqual(c['memory_gb'],48);self.assertEqual(c['commit_gb'],44)
  p,reason=scheduler.allocate(n,self.job(commit_gb=48,memory_gb=40)['spec'])
  self.assertIsNone(p);self.assertIn('提交额度',reason)
  n['telemetry']['memory_committed_gb']=60
  p,reason=scheduler.allocate(n,self.job(commit_gb=48,memory_gb=40)['spec'])
  self.assertIsNotNone(p)
  running=self.job('active',memory_gb=40,commit_gb=48);running.update(status='running',node='a')
  p,reason=scheduler.allocate(n,self.job(memory_gb=30,commit_gb=36)['spec'],[running])
  self.assertIsNone(p);self.assertIn('共享额度',reason)
 def test_shared_fraction_default_unchanged_and_never_exceeds_half_ram(self):
  n=self.node(memory_total_gb=128,memory_available_gb=120,external_busy=True,
    resource_observation={'external_cpu_cores':0},scheduling_policy={'external_mode':'shared','shared_memory_gb':100})
  self.assertEqual(scheduler.budget(n)['memory_gb'],32)
  n['telemetry']['scheduling_policy']['shared_memory_fraction']=.9
  self.assertEqual(scheduler.budget(n)['memory_gb'],64)
 def test_preferred_nodes_and_capacity_fallback(self):
  nodes=[self.node('win3'),self.node('dell-r940'),self.node('a800-ssh')]
  p,_=scheduler.plan(nodes,[self.job()],estimate=lambda req,nid:1 if nid=='win3' else 100)
  self.assertEqual(p['j']['node'],'dell-r940')
  nodes[1]['telemetry']['memory_available_gb']=1
  p,_=scheduler.plan(nodes,[self.job()]);self.assertEqual(p['j']['node'],'a800-ssh')
  nodes[-1]['telemetry']['memory_available_gb']=1
  p,_=scheduler.plan(nodes,[self.job()]);self.assertEqual(p['j']['node'],'win3')
 def test_preference_does_not_move_active_execution(self):
  active=self.job('active');active.update(status='running',node='win3')
  p,_=scheduler.plan([self.node('win3'),self.node('a800-ssh')],[active,self.job()])
  self.assertNotIn('active',p);self.assertEqual(active['node'],'win3');self.assertEqual(p['j']['node'],'a800-ssh')
 def test_fifo_fills_both_nodes(self):
  nodes=[self.node('a'),self.node('b')];jobs=[self.job(str(i),i) for i in range(10)]
  placements,reasons=scheduler.plan(nodes,jobs)
  self.assertEqual(list(placements),[str(i) for i in range(10)])
  self.assertEqual({v['node'] for v in placements.values()},{'a','b'})
 def test_memory_commitment(self):
  n=self.node(memory_available_gb=12,usages={'active':{'rss_gb':2}})
  j=self.job('active',memory_gb=10);j.update(status='running',node='a')
  p,r=scheduler.plan([n],[j,self.job()]);self.assertFalse(p)
  self.assertIn('内存',r['j'])
 def test_head_reservation_other_node_work(self):
  a=self.node('a');b=self.node('b',cpu_pool=[0,1]);running=self.job('run',cores=4);running.update(status='running',node='a')
  p,r=scheduler.plan([a,b],[running,self.job('big',1,cores=6),self.job('small',2)])
  self.assertNotIn('big',p);self.assertEqual(p['small']['node'],'b')
 def test_bounded_backfill(self):
  n=self.node();j=self.job('run',cores=4,wall_minutes=40);j.update(status='running',node='a',started=time.time())
  jobs=[j,self.job('big',1,cores=6),self.job('short',2,wall_minutes=2),self.job('unbounded',3)]
  p,r=scheduler.plan([n],jobs);self.assertNotIn('short',p);self.assertNotIn('unbounded',p)  # A declared duration cannot promise a forced release.
 def test_application_and_gpu(self):
  a=self.node('a');b=self.node('b',gpus=[dict(index=0,available=True,memory_free_gb=20)])
  p,r=scheduler.plan([a,b],[self.job(gpus=1,gpu_memory_gb=10)])
  self.assertEqual(p['j']['node'],'b');self.assertEqual(p['j']['spec']['gpu_ids'],[0])
 def test_gpu_headroom_and_runtime_disk_are_reserved(self):
  n=self.node(gpus=[dict(index=0,available=True,memory_free_gb=4)],disk_free_gb=30)
  req=self.job(gpus=1,gpu_memory_gb=4)['spec']
  self.assertIsNone(scheduler.allocate(n,req)[0])
  n['telemetry']['gpus'][0]['memory_free_gb']=5
  n['telemetry']['runtimes']['python']['disk_overhead_gb']=8
  placed,_=scheduler.allocate(n,req);self.assertEqual(placed['runtime_disk_gb'],8)
  j=self.job('busy');j.update(status='running',spec=placed)
  n['telemetry']['disk_free_gb']=20
  self.assertIsNone(scheduler.allocate(n,self.job()['spec'],[j])[0])
 def test_gpu_wait_does_not_idle_unneeded_cpu_capacity(self):
  now=time.time();n=self.node(gpus=[dict(index=0,available=False,memory_free_gb=5)])
  running=self.job('gpu-running',cores=2,gpus=1,gpu_ids=[0],cpu_ids=[0,1],wall_minutes=1)
  running.update(status='running',node='a',started=now)
  head=self.job('gpu-next',2,cores=2,gpus=1,gpu_memory_gb=1)
  late=self.job('cpu-long',3,cores=3,wall_minutes=60)
  placed,_=scheduler.plan([n],[running,head,late],now)
  self.assertIn('cpu-long',placed);self.assertNotIn('gpu-next',placed)
  # More CPU needed by the older GPU task must still be protected.
  head['spec']['cores']=4
  placed,_=scheduler.plan([n],[running,head,late],now)
  self.assertNotIn('cpu-long',placed)
 def test_external_capacity_accounted(self):
  n=self.node(external_busy=True,external_reserved_cores=5)
  p,r=scheduler.plan([n],[self.job('first',1,cores=2),self.job('next',2)])
  self.assertEqual(list(p),['first'])
 def test_history_selects_faster_eligible(self):
  p,r=scheduler.plan([self.node('a'),self.node('b')],[self.job()],estimate=lambda s,n:100 if n=='a' else 50)
  self.assertEqual(p['j']['node'],'b')
 def test_external_affinity_is_excluded_and_remaining_pool_fills(self):
  n=self.node(external_busy=True,external_reserved_cores=2,external_cpu_ids=[0,1])
  p,r=scheduler.plan([n],[self.job('j'+str(i),i,memory_gb=.5) for i in range(6)])
  self.assertEqual(len(p),5)
  ids=[cpu for placement in p.values() for cpu in placement['spec']['cpu_ids']]
  self.assertEqual(sorted(ids),[2,3,4,5,6])
 def test_generic_job_preserves_only_gpu_node_for_next_job(self):
  a=self.node('a',gpus=[dict(index=0,available=True,memory_free_gb=20)],max_parallel=1)
  b=self.node('b',max_parallel=1)
  p,_=scheduler.plan([a,b],[self.job('cpu',1),self.job('gpu',2,gpus=1,gpu_memory_gb=8)])
  self.assertEqual(list(p),['cpu','gpu']);self.assertEqual(p['cpu']['node'],'b');self.assertEqual(p['gpu']['node'],'a')
 def test_large_memory_job_not_stranded_by_generic_placement(self):
  a=self.node('a',memory_total_gb=128,memory_available_gb=124,max_parallel=1)
  b=self.node('b',max_parallel=1)
  p,_=scheduler.plan([a,b],[self.job('small',1),self.job('large',2,memory_gb=64)])
  self.assertEqual(p['small']['node'],'b');self.assertEqual(p['large']['node'],'a')
 def test_lookahead_does_not_delay_head_or_reserve_for_dependencies(self):
  a=self.node('a',max_parallel=1);b=self.node('b',max_parallel=1)
  p,_=scheduler.plan([a,b],[self.job('first',1,target='a'),self.job('later',2,target='a')])
  self.assertEqual(list(p),['first'])
  p,_=scheduler.plan([a,b],[self.job('first',1),self.job('gated',2,target='a',dependency_wait='review')])
  self.assertEqual(p['first']['node'],'a');self.assertNotIn('gated',p)
 def test_reservation_chooses_known_release_not_unbounded_external(self):
  a=self.node('a',external_busy=True,external_reserved_cores=7)
  b=self.node('b');run=self.job('run',cores=4,wall_minutes=20);run.update(status='running',node='b',started=time.time())
  p,r=scheduler.plan([a,b],[run,self.job('big',1,cores=6),self.job('unbounded',2)])
  self.assertEqual(p['unbounded']['node'],'b')
  p,r=scheduler.plan([a,b],[run,self.job('big',1,cores=6),self.job('unbounded',2,cores=2)])
  self.assertIn('unbounded',p);self.assertEqual(p['unbounded']['node'],'b')
 def observed(self,**extra):
  return dict(at=time.time(),verified=True,state='normal',external_cpu_cores=1,**extra)
 def test_high_commit_low_residency_does_not_block_win2(self):
  n=self.node(memory_total_gb=64,memory_available_gb=52,memory_committed_gb=98,memory_commit_limit_gb=136,memory_commit_verified=True,resource_observation=self.observed())
  p,_=scheduler.plan([n],[self.job(memory_gb=1)]);self.assertIn('j',p)
 def test_commit_limit_and_physical_limit_are_independent(self):
  n=self.node(memory_committed_gb=39,memory_commit_limit_gb=40)
  p,r=scheduler.plan([n],[self.job(memory_gb=1)]);self.assertFalse(p);self.assertIn('提交额度',r['j'])
  n=self.node(memory_available_gb=4,memory_committed_gb=8,memory_commit_limit_gb=100)
  p,r=scheduler.plan([n],[self.job(memory_gb=1)]);self.assertFalse(p);self.assertIn('物理内存',r['j'])
 def test_sustained_pressure_blocks_then_auto_recovers(self):
  n=self.node(resource_observation={**self.observed(),'state':'pressured'})
  p,r=scheduler.plan([n],[self.job()]);self.assertFalse(p);self.assertIn('换页',r['j'])
  n['telemetry']['resource_observation']['state']='normal';p,_=scheduler.plan([n],[self.job()]);self.assertIn('j',p)
 def test_shared_legacy_capacity_is_bounded_without_pinning_legacy(self):
  n=self.node(external_busy=True,external_reserved_cores=7,external_cpu_ids=list(range(7)),resource_observation=self.observed(),scheduling_policy={'external_mode':'shared','shared_memory_gb':2,'shared_cpu_fraction':.5})
  p,_=scheduler.plan([n],[self.job(str(i),i,memory_gb=.5) for i in range(10)])
  self.assertEqual(len(p),3);self.assertTrue(all(x['spec']['shared_node'] for x in p.values()))
  n['telemetry']['resource_observation']['external_cpu_cores']=7
  p,_=scheduler.plan([n],[self.job(memory_gb=.5)]);self.assertFalse(p)
 def test_stale_required_observation_blocks_new_lease(self):
  n=self.node(scheduling_policy={'require_observation':True})
  p,_=scheduler.plan([n],[self.job()]);self.assertFalse(p)
  n['telemetry']['resource_observation']=self.observed();n['telemetry']['resource_observation']['at']-=30
  p,_=scheduler.plan([n],[self.job()]);self.assertFalse(p)
 def test_managed_private_allocation_not_double_counted(self):
  n=self.node(memory_committed_gb=30,memory_commit_limit_gb=40,usages={'active':{'at':time.time(),'rss_gb':4,'private_gb':10}})
  run=self.job('active',memory_gb=4,commit_gb=10);run.update(status='running',node='a')
  p,_=scheduler.plan([n],[run,self.job(memory_gb=4,commit_gb=4)]);self.assertIn('j',p)
 def test_reservation_uses_first_sufficient_release_with_unknown_job_left(self):
  n=self.node();now=time.time()
  short=self.job('short',cores=4,memory_gb=2,wall_minutes=10);short.update(status='running',node='a',started=now)
  unknown=self.job('unknown',cores=1,memory_gb=2);unknown.update(status='running',node='a')
  head=self.job('head',cores=6,memory_gb=2)
  bound=scheduler.reservation_release(n,head['spec'],[short,unknown],now)
  self.assertIsNone(bound)  # No execution now has a guaranteed wall-time release.

 def elastic_fixture(self):
  now=time.time();n=self.node(memory_total_gb=32,memory_available_gb=25,reserve_gb=4,memory_commit_limit_gb=100,memory_committed_gb=20,memory_commit_verified=True,resource_observation=dict(at=now,verified=True,state='normal'),scheduling_policy={'elastic_memory':{'enabled':True,'fraction':.1}},usages={'active':dict(at=now,phase='running',elapsed_seconds=120,rss_gb=2,private_gb=2)})
  j=self.job('active',memory_gb=20,commit_gb=24,resource_mode='estimated',restart_policy='from_start');j.update(status='running',node='a')
  return now,n,j
 def test_light_ram_overcommit_keeps_cpu_and_commit_strict(self):
  now,n,j=self.elastic_fixture();q=self.job(memory_gb=4,commit_gb=6)
  self.assertEqual(scheduler.budget(n,[j],now)['elastic_memory_credit_gb'],2)
  self.assertIsNotNone(scheduler.allocate(n,q['spec'],[j],now)[0])
  q['spec']['cores']=7;self.assertIn('CPU',scheduler.allocate(n,q['spec'],[j],now)[1])
  q['spec']['cores']=1;q['spec']['commit_gb']=90;self.assertIn('提交',scheduler.allocate(n,q['spec'],[j],now)[1])
 def test_elastic_credit_revoked_on_pressure_stale_fixed_or_retry(self):
  now,n,j=self.elastic_fixture()
  for change in ('pressure','stale','fixed','retry','external'):
   now,n,j=self.elastic_fixture()
   if change=='pressure':n['telemetry']['resource_observation']['state']='pressured'
   if change=='stale':n['telemetry']['usages']['active']['at']=now-25
   if change=='fixed':j['spec']['resource_mode']='fixed'
   if change=='retry':j['spec']['resource_retries']=1
   if change=='external':n['telemetry']['external_busy']=True
   self.assertEqual(scheduler.budget(n,[j],now)['elastic_memory_credit_gb'],0)
 def test_elastic_credit_never_borrows_immediate_system_reserve(self):
  now,n,j=self.elastic_fixture();n['telemetry']['memory_available_gb']=4.5
  self.assertLessEqual(scheduler.budget(n,[j],now)['memory_gb'],.5)
  n['telemetry']['scheduling_policy']['elastic_memory']['fraction']=5
  self.assertLessEqual(scheduler.budget(n,[j],now)['elastic_memory_credit_gb'],3.2)

 def test_repeated_large_heads_do_not_reserve_all_hosts(self):
  now=time.time();nodes=[self.node(x,cpu_pool=[0,1,2,3],memory_total_gb=32,memory_available_gb=25) for x in ('a','b','c')]
  running=[]
  for x in ('a','b','c'):
   j=self.job('running-'+x,cores=2,memory_gb=16,wall_minutes=20);j.update(node=x,status='running',started=now);running.append(j)
  heads=[self.job('big-'+str(i),i,cores=3,memory_gb=24,wall_minutes=60) for i in range(3)]
  later=self.job('cpu-backfill',10,cores=1,memory_gb=2,wall_minutes=120)
  placements,_=scheduler.plan(nodes,running+heads+[later],now)
  self.assertIn('cpu-backfill',placements)
  self.assertNotEqual(placements['cpu-backfill']['node'],'a')

 def test_resource_summary_reports_assignments_without_job_identity(self):
  n=self.node(gpus=[dict(index=0,memory_total_gb=8,memory_free_gb=6,available=True)])
  j=self.job('private-job',cores=2,cpu_ids=[0,1],gpus=1,gpu_ids=[0],gpu_memory_gb=4);j.update(status='running',node='a')
  c=scheduler.budget(n,[j]);self.assertEqual(c['allocated_cpu_threads'],2);self.assertEqual(c['total_cpu_threads'],7)
  self.assertEqual(c['gpu_allocations'],[dict(index=0,allocated=True,reserved_memory_gb=4)])
  self.assertNotIn('private-job',str(c))
  j['execution_stopped']=1;c=scheduler.budget(n,[j]);self.assertEqual(c['allocated_cpu_threads'],0);self.assertFalse(c['gpu_allocations'][0]['allocated'])

 def test_kernel_supervisor_and_preparation_commit_are_reserved(self):
  n=self.node(memory_commit_limit_gb=10,memory_committed_gb=6,memory_commit_verified=True)
  q=self.job(memory_gb=1,commit_gb=1,kernel_memory_required=True)
  self.assertIn('提交',scheduler.allocate(n,q['spec'])[1])
  n['telemetry']['memory_committed_gb']=5
  self.assertIsNotNone(scheduler.allocate(n,q['spec'])[0])

 def test_unknown_gpu_release_backfills_only_surplus_resources(self):
  n=self.node(gpus=[dict(index=0,available=False,memory_free_gb=6)])
  busy=self.job('busy',cores=2,cpu_ids=[0,1],memory_gb=2,gpus=1,gpu_ids=[0]);busy.update(status='running',node='a')
  head=self.job('head',1,cores=2,memory_gb=4,gpus=1,gpu_memory_gb=4)
  tail=self.job('tail',2,cores=3,memory_gb=2)
  p,_=scheduler.plan([n],[busy,head,tail]);self.assertIn('tail',p)
  # No finite duration is needed, but the waiting head's CPUs remain protected.
  tail['spec']['cores']=4
  p,r=scheduler.plan([n],[busy,head,tail]);self.assertNotIn('tail',p)
  self.assertIn('更早排队',r['tail'])

 def test_preparation_time_counts_against_backfill_window(self):
  now=time.time();n=self.node()
  busy=self.job('busy',cores=4,wall_minutes=20);busy.update(status='running',node='a',started=now)
  head=self.job('head',1,cores=7)
  short=self.job('short',2,wall_minutes=1)
  p,_=scheduler.plan([n],[busy,head,short],now);self.assertNotIn('short',p)
  busy['spec']['wall_minutes']=40
  p,_=scheduler.plan([n],[busy,head,short],now);self.assertNotIn('short',p)

 def test_unknown_reservation_preserves_commit_and_application_seat(self):
  now=time.time();n=self.node(gpus=[dict(index=0,available=False,memory_free_gb=6)],memory_commit_limit_gb=20,memory_committed_gb=5,
    runtimes={'python':dict(kind='python',max_parallel=2)})
  busy=self.job('busy',cores=1,cpu_ids=[0],memory_gb=1,commit_gb=2);busy.update(status='running',node='a')
  head=self.job('head',1,cores=1,memory_gb=1,commit_gb=2,gpus=1)
  tail=self.job('tail',2,cores=1,memory_gb=1,commit_gb=2)
  p,_=scheduler.plan([n],[busy,head,tail],now);self.assertNotIn('tail',p)
  n['telemetry']['runtimes']['python']['max_parallel']=4;head['spec']['commit_gb']=10;tail['spec']['commit_gb']=5
  p,_=scheduler.plan([n],[busy,head,tail],now);self.assertNotIn('tail',p)

 def test_gpu_best_fit_preserves_large_device(self):
  n=self.node(gpus=[dict(index=0,available=True,memory_free_gb=24),dict(index=1,available=True,memory_free_gb=8)])
  p,_=scheduler.plan([n],[self.job('small',1,gpus=1,gpu_memory_gb=4),self.job('large',2,gpus=1,gpu_memory_gb=16)])
  self.assertEqual(p['small']['spec']['gpu_ids'],[1]);self.assertEqual(p['large']['spec']['gpu_ids'],[0])

 def test_shape_lookahead_sees_past_identical_job_array(self):
  a=self.node('a',max_parallel=1,gpus=[dict(index=0,available=True,memory_free_gb=8)])
  b=self.node('b',max_parallel=1)
  jobs=[self.job('cpu-0',0)]+[self.job('cpu-'+str(i),i,target='b') for i in range(1,40)]+[self.job('gpu',41,gpus=1,gpu_memory_gb=4)]
  p,_=scheduler.plan([a,b],jobs)
  self.assertEqual(p['cpu-0']['node'],'b');self.assertEqual(p['gpu']['node'],'a')

 def test_prelease_repair_admits_more_without_dropping_older_job(self):
  from unittest.mock import patch
  a=self.node('a',cpu_pool=[0,1,2,3]);b=self.node('b',cpu_pool=[0,1,2,3])
  jobs=[self.job('small',1),self.job('whole-a',2,cores=4,target='a')]
  # Exercise the fallback when bounded lookahead cannot see a scarce request.
  with patch.object(scheduler,'future_shapes',return_value=[]):p,_=scheduler.plan([a,b],jobs)
  self.assertEqual(set(p),{'small','whole-a'});self.assertEqual(p['small']['node'],'b');self.assertEqual(p['whole-a']['node'],'a')
  live=self.job('small',1,cpu_ids=[0]);live.update(status='leased',node='a')
  p,_=scheduler.plan([a,b],[live,jobs[1]]);self.assertNotIn('whole-a',p)

 def test_random_mixed_allocations_obey_all_resource_limits(self):
  import random
  for seed in range(30):
   rng=random.Random(seed);now=time.time()
   nodes=[self.node('a',cpu_pool=list(range(8)),memory_available_gb=28,gpus=[dict(index=0,available=True,memory_free_gb=24),dict(index=1,available=True,memory_free_gb=8)],memory_commit_limit_gb=80,memory_committed_gb=0),self.node('b',cpu_pool=list(range(8)),memory_available_gb=28,memory_commit_limit_gb=80,memory_committed_gb=0)]
   jobs=[self.job(str(i),i,cores=rng.choice([1,2,4]),memory_gb=rng.choice([1,2,8,16]),gpus=int(rng.random()<.2),gpu_memory_gb=rng.choice([2,6,16])) for i in range(25)]
   p,_=scheduler.plan(nodes,jobs,now)
   for n in nodes:
    residents=[dict(id=jid,status='leased',spec=v['spec']) for jid,v in p.items() if v['node']==n['id']]
    cpu=[cpu for j in residents for cpu in j['spec']['cpu_ids']];gpu=[gpu for j in residents for gpu in j['spec'].get('gpu_ids',[])]
    self.assertEqual(len(cpu),len(set(cpu)));self.assertEqual(len(gpu),len(set(gpu)))
    for j in residents:self.assertIsNotNone(scheduler.allocate(n,j['spec'],[x for x in residents if x['id']!=j['id']],now)[0])

 def test_impossible_host_commit_does_not_reserve_its_cpu(self):
  a=self.node('a',cpu_pool=[0,1],memory_commit_limit_gb=10,memory_committed_gb=0)
  b=self.node('b',cpu_pool=[0,1],memory_commit_limit_gb=100,memory_committed_gb=0)
  busy=self.job('busy',cores=2,cpu_ids=[0,1],memory_gb=2);busy.update(status='running',node='b')
  head=self.job('head',1,cores=2,memory_gb=1,commit_gb=20)
  tail=self.job('tail',2,cores=2,memory_gb=1,commit_gb=2)
  p,r=scheduler.plan([a,b],[busy,head,tail]);self.assertEqual(p['tail']['node'],'a')
  self.assertIn('单机可分配',r['head'])

 def test_impossible_vram_does_not_hold_small_gpu(self):
  n=self.node(gpus=[dict(index=0,available=True,memory_free_gb=8,memory_total_gb=8)])
  p,r=scheduler.plan([n],[self.job('large',1,gpus=1,gpu_memory_gb=16),self.job('small',2,gpus=1,gpu_memory_gb=4)])
  self.assertIn('small',p);self.assertIn('单机 GPU 容量',r['large'])
