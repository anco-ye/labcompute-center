import json,tempfile,time,unittest,sys,os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulation_monitor import SimulationMonitor,read_json
class MonitorTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def test_rate_reset_and_stale(self):
  m=SimulationMonitor({},self.root/'cache.json')
  self.assertEqual(m.estimate('a','run1',20,100,1000,200,True)[0],800)
  self.assertEqual(m.estimate('a','run1',30,100,1100,300,True)[0],700)
  self.assertEqual(m.estimate('a','run2',2,100,1200,20,True)[0],980)
  self.assertIsNone(m.estimate('a','run2',3,100,1300,30,False)[0])
 def test_batch_and_waiting(self):
  folder=self.root/'batches';folder.mkdir()
  for name in ['active','waiting']:(folder/(name+'_manifest.json')).write_text(json.dumps({'queue':[{}, {}, {}, {}]}))
  stamp=time.time()
  (folder/'active_runner_state.json').write_text(json.dumps(dict(phase='running',pid=os.getpid(),total=4,completed=2,failed=0,running={'env/pair':3},start_utc='2026-01-01T00:00:00+00:00')))
  m=SimulationMonitor({'batch_dirs':[str(folder)],'waiting_batches':['waiting']},self.root/'cache.json')
  rows=m.collect();self.assertEqual(len(rows),2)
  active=next(r for r in rows if r['name']=='active');self.assertEqual(active['percent'],50);self.assertGreater(active['remaining_seconds'],0)
  self.assertEqual(next(r for r in rows if r['name']=='waiting')['status'],'等待前序批次')
  self.assertTrue((self.root/'cache.json').exists())
 def test_network_progress_and_corrupt_file(self):
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L5_R01';folder.mkdir(parents=True)
  (folder/'progress.json').write_text(json.dumps(dict(time_ms=250,duration_ms=5000,input_condition='weak',elapsed_s=100,output=str(folder/'pilot_5000ms_weak_b1.h5'))))
  status=self.root/'status.json';status.write_text(json.dumps(dict(pid=os.getpid(),condition='weak',partial_backup=str(folder/'a'),completed=[])))
  m=SimulationMonitor({'network_roots':[str(self.root)],'status_files':[str(status)]},self.root/'cache.json')
  with patch('simulation_monitor.psutil.process_iter',return_value=[]):
   row=m.collect()[0]
  self.assertEqual(row['percent'],5);self.assertEqual(row['remaining_seconds'],1900);self.assertEqual(row['queue_completed'],0)
  status.write_text(json.dumps(dict(pid=os.getpid(),command=['python','script.py','--environment','H01_L5_R01','--condition','weak','--duration-ms','5000'])))
  m.last=0
  with patch('simulation_monitor.psutil.process_iter',return_value=[]):row=m.collect()[0]
  self.assertEqual(row['status'],'运行中');self.assertGreater(row['remaining_seconds'],0);self.assertIsNone(row['queue_total'])
  (folder/'progress.json').write_text('{');m.last=0
  with patch('simulation_monitor.psutil.process_iter',return_value=[]):self.assertEqual(m.collect()[0]['status'],'状态文件暂不可读')
 def test_discovered_v4_ignores_result_from_previous_run(self):
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L6_R05';folder.mkdir(parents=True)
  result=folder/'result_600ms_b1.json';result.write_text(json.dumps(dict(duration_ms=600,input_condition='strong_bursts',finite=True)))
  os.utime(result,(time.time()-100,time.time()-100))
  (folder/'progress.json').write_text(json.dumps(dict(time_ms=52,duration_ms=600,input_condition='strong_bursts',elapsed_s=100,output=str(folder/'pilot_600ms_b1.h5'))))
  obs=[dict(pid=123,start=time.time()-110,args=['python',str(self.root/'scripts/full_window_network_fixed_active_v4_buffered.py'),'--environment','H01_L6_R05'])]
  m=SimulationMonitor({'discovery_roots':[str(self.root)]},self.root/'cache.json')
  row=m.collect(obs)[0];self.assertEqual(row['status'],'运行中');self.assertAlmostEqual(row['percent'],52/6)
 def test_same_environment_in_different_workspaces_is_not_deduplicated(self):
  obs=[dict(pid=i+100,start=time.time()-10,args=['python',str(self.root/f'run{i}/scripts/full_window_network_fixed_active_v3.py'),'--environment','H01_L4_R02']) for i in range(2)]
  m=SimulationMonitor({'discovery_roots':[str(self.root)]},self.root/'cache.json')
  rows=m.collect(obs);self.assertEqual(len(rows),2);self.assertEqual(len({r['id'] for r in rows}),2)
 def test_probe_completion_is_terminal_and_old_progress_not_reused(self):
  (self.root/'probe_manifest.json').write_text(json.dumps({'queue':[{}]}))
  (self.root/'probe_runner_state.json').write_text(json.dumps(dict(phase='probe_finished_pending_mac_review',total=1,completed=1,pid=-1)))
  m=SimulationMonitor({'batch_dirs':[str(self.root)]},self.root/'cache.json')
  row=m.collect([])[0];self.assertEqual(row['status'],'已完成');self.assertIsNone(row['elapsed_seconds'])
 def test_new_run_does_not_inherit_old_completed_progress(self):
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L4_R02';folder.mkdir(parents=True)
  f=folder/'progress.json';f.write_text(json.dumps(dict(time_ms=600,duration_ms=600,input_condition='strong_bursts',elapsed_s=100)))
  os.utime(f,(time.time()-100,time.time()-100))
  obs=[dict(pid=123,start=time.time()-5,args=['python',str(self.root/'scripts/full_window_network_fixed_active_v3.py'),'--environment','H01_L4_R02'])]
  m=SimulationMonitor({'discovery_roots':[str(self.root)]},self.root/'cache.json')
  row=m.collect(obs)[0];self.assertEqual(row['status'],'初始化中');self.assertEqual(row['percent'],0)
 def test_batch_restart_does_not_inherit_finished_previous_run(self):
  (self.root/'batch_manifest.json').write_text(json.dumps({'queue':[{}]}))
  f=self.root/'batch_runner_state.json';f.write_text(json.dumps(dict(phase='finished',completed=1,total=1,pid=-1)))
  os.utime(f,(time.time()-100,time.time()-100))
  obs=[dict(pid=123,start=time.time()-5,args=['python',str(self.root/'windows_observed_batch_runner.py'),'--batch-name','batch'])]
  m=SimulationMonitor({'batch_dirs':[str(self.root)]},self.root/'cache.json');row=m.collect(obs)[0]
  self.assertEqual(row['status'],'初始化中');self.assertEqual(row['completed'],0)
 def test_process_identity_change_bypasses_progress_cache(self):
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L6_R05';folder.mkdir(parents=True)
  f=folder/'progress.json';f.write_text(json.dumps(dict(time_ms=315,duration_ms=600,input_condition='strong_bursts')))
  old=time.time()-100;os.utime(f,(old,old))
  args=['python',str(self.root/'scripts/full_window_network_fixed_active_v4_buffered.py'),'--environment','H01_L6_R05']
  m=SimulationMonitor({'network_roots':[str(self.root)]},self.root/'cache.json')
  row=m.collect([dict(pid=123,start=old-10,args=args)])[0];self.assertEqual(row['completed'],315)
  row=m.collect([dict(pid=124,start=time.time()-1,args=args)])[0]
  self.assertEqual(row['status'],'初始化中');self.assertEqual(row['completed'],0)
 def test_completed_discovered_task_survives_process_exit_and_service_restart(self):
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L6_R05';folder.mkdir(parents=True)
  progress=folder/'progress.json';progress.write_text(json.dumps(dict(time_ms=100,duration_ms=600,input_condition='strong_bursts',output=str(folder/'pilot_600ms.h5'))))
  config={'discovery_roots':[str(self.root)]};cache=self.root/'cache.json'
  obs=[dict(pid=123,start=time.time()-20,args=['python',str(self.root/'scripts/full_window_network_fixed_active_v3.py'),'--environment','H01_L6_R05'])]
  SimulationMonitor(config,cache).collect(obs)
  progress.write_text(json.dumps(dict(time_ms=600,duration_ms=600,input_condition='strong_bursts',output=str(folder/'pilot_600ms.h5'))))
  (folder/'result_600ms.json').write_text(json.dumps(dict(duration_ms=600,input_condition='strong_bursts',finite=True)))
  row=SimulationMonitor(config,cache).collect([])[0]
  self.assertEqual(row['status'],'计算完成');self.assertEqual(row['percent'],100)
 def test_memory_guard_exit_and_old_report_after_restart(self):
  import datetime as dt
  folder=self.root/'environment_networks_fixed_active_v3'/'H01_L6_R05';folder.mkdir(parents=True)
  progress=folder/'progress.json';progress.write_text(json.dumps(dict(time_ms=315,duration_ms=600,input_condition='strong_bursts')))
  report=self.root/'terminal.json';report.write_text(json.dumps(dict(phase='v4_terminated_to_preserve_existing_long_run',updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),memory=dict(free_physical_gib=2.065))))
  cfg=dict(discovery_roots=[str(self.root)],network_roots=[str(self.root)],terminal_reports={str(progress):str(report)})
  m=SimulationMonitor(cfg,self.root/'cache.json');row=m.collect([])[0]
  self.assertEqual(row['status'],'已中断');self.assertIn('2.1 GiB',row['failure_detail']);self.assertEqual(row['completed'],315)
  obs=[dict(pid=123,start=time.time()-5,args=['python',str(self.root/'scripts/full_window_network_fixed_active_v4_buffered.py'),'--environment','H01_L6_R05'])]
  m.last=0;row=m.collect(obs)[0];self.assertEqual(row['status'],'运行中');self.assertNotIn('failure_detail',row)
  newer=time.time()+1;os.utime(progress,(newer,newer));m.last=0;row=m.collect([])[0];self.assertNotIn('内存保护',row.get('failure_detail',''))
if __name__=='__main__':unittest.main()
