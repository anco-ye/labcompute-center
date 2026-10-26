import json,os,tempfile,time,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from health_watchdog import controller_row
from task_health import merge_controller_observations
class ControllerObserverTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'state.json'
  self.entry=dict(script='C:/research/run_5s.py',status_file=str(self.path),environment='H01_L5_R03')
  self.procs=[dict(pid=12,created=time.time()-10,args=['python',self.entry['script']])]
  self.write('waiting_for_prior_win4_network_batch',prior_batch='H01_L3_R05')
 def write(self,phase,**extra):self.path.write_text(json.dumps(dict(phase=phase,completed=[],**extra)))
 def row(self):return controller_row('five',self.entry,self.procs)
 def test_live_waiter_is_queued_not_solver(self):
  r=self.row();self.assertEqual(r['lifecycle'],'queued');self.assertFalse(r['covered_by_solver']);self.assertIn('H01_L3_R05',r['status_detail']);self.assertGreaterEqual(r['state_observed_at'],self.procs[0]['created']-2);self.assertLess(r['state_age_seconds'],300)
 def test_missing_waiter_is_problem_not_disappeared_queue(self):
  self.procs=[];self.assertEqual(self.row()['lifecycle'],'failed')
 def test_permission_unknown_does_not_assert_exit(self):
  self.assertEqual(controller_row('five',self.entry,[],False)['lifecycle'],'unknown')
 def test_old_generation_or_stale_wait_is_unknown(self):
  stamp=time.time()-1000;os.utime(self.path,(stamp,stamp));self.assertEqual(self.row()['lifecycle'],'unknown')
 def test_active_solver_is_recognized_without_guessing_from_controller(self):
  self.write('condition_running_pending_mac_review')
  self.assertEqual(self.row()['lifecycle'],'unknown')
  self.procs.append(dict(pid=13,created=time.time()-1,args=['python','C:/research/full_window_network_fixed_active_v3.py','--environment','H01_L5_R03','--duration-ms','5000']))
  r=self.row();self.assertEqual(r['lifecycle'],'running');self.assertTrue(r['covered_by_solver'])
 def test_completion_waits_for_controller_exit(self):
  self.write('three_conditions_computed_pending_mac_review');self.assertEqual(self.row()['lifecycle'],'running')
  self.procs=[];self.assertEqual(self.row()['lifecycle'],'succeeded')
 def test_snapshot_staleness_preserves_visible_unknown(self):
  t={};merge_controller_observations(t,dict(received_at=1,items=[self.row()]),200)
  self.assertEqual(t['simulations'][0]['lifecycle'],'unknown')
 def test_wait_file_expires_even_when_snapshot_is_recent(self):
  row=dict(self.row(),state_age_seconds=290)
  t={};merge_controller_observations(t,dict(received_at=100,items=[row]),120)
  self.assertEqual(t['simulations'][0]['lifecycle'],'unknown')
 def test_live_solver_is_not_rendered_twice(self):
  row=dict(self.row(),covered_by_solver=True,lifecycle='running')
  t={'simulations':[dict(kind='network',lifecycle='running',total=5000,name='H01_L5_R03 · weak')]}
  merge_controller_observations(t,dict(received_at=100,items=[row]),101)
  self.assertEqual(len(t['simulations']),1)
if __name__=='__main__':unittest.main()
