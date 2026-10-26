import copy,sys,unittest,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from legacy_terminal import reconcile_shared_admission_hold
import scheduler
class SharedIncidentHold(unittest.TestCase):
 def sample(self):
  return dict(simulations=[dict(id='failed',kind='network',lifecycle='failed',process_state='stopped',failure_code='memory_pressure',failure_evidence=dict(basis='registered_guard_command_condition_duration_and_progress_time',event=dict(pid=10,time='one'))),dict(id='live',kind='network',generation='same',lifecycle='running',process_state='running')])
 def test_new_memory_does_not_reopen_admission(self):
  t=self.sample();reconcile_shared_admission_hold(t)
  self.assertTrue(t['shared_admission_hold']['active'])
  fresh=self.sample();fresh.update(memory_available_gb=100,memory_committed_gb=10,resource_observation=dict(at=time.time(),verified=True,state='normal'))
  reconcile_shared_admission_hold(fresh,t)
  self.assertIsNotNone(scheduler.pressure_reason(fresh,time.time()))
 def test_unknown_and_missing_preserve_hold(self):
  t=self.sample();reconcile_shared_admission_hold(t)
  for rows in [[],[dict(id='live',kind='network',generation='unknown-new',lifecycle='unknown',process_state='unknown')]]:
   new=dict(simulations=rows);reconcile_shared_admission_hold(new,t);self.assertTrue(new['shared_admission_hold']['active'])
 def test_terminal_releases_and_old_incident_cannot_reopen(self):
  t=self.sample();reconcile_shared_admission_hold(t)
  new=self.sample();new['simulations'][1].update(lifecycle='succeeded',process_state='stopped')
  reconcile_shared_admission_hold(new,t);self.assertFalse(new['shared_admission_hold']['active'])
  later=self.sample();later['simulations'][1]['generation']='new'
  reconcile_shared_admission_hold(later,new);self.assertFalse(later['shared_admission_hold']['active'])
 def test_new_verified_generation_releases_old_cohort(self):
  t=self.sample();reconcile_shared_admission_hold(t)
  new=self.sample();new['simulations'][1]['generation']='new'
  reconcile_shared_admission_hold(new,t);self.assertFalse(new['shared_admission_hold']['active'])
 def test_unknown_cause_not_classified_as_resource(self):
  t=self.sample();t['simulations'][0]['failure_evidence']={};reconcile_shared_admission_hold(t)
  self.assertNotIn('shared_admission_hold',t)
 def test_late_historical_report_does_not_hold_later_workload(self):
  t=self.sample();t['simulations'][0]['source_age_seconds']=7200
  t['simulations'][0]['failure_evidence'].update(event_time=1005,progress_mtime=1000)
  t['simulations'][1]['elapsed_seconds']=600
  reconcile_shared_admission_hold(t);self.assertFalse(t['shared_admission_hold']['active'])
