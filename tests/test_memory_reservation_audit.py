import copy
import unittest
import resource_policy
import task_health
from maintenance.memory_reservation.monitor import inspect
from test_active_reservations import ActiveReservationsTest


class MemoryReservationAuditTest(unittest.TestCase):
    def test_complete_matching_peak_caps_hold_at_one_point_five_without_changing_limits(self):
        now,m,s,u,rows=ActiveReservationsTest().fixture();m.update(state_binding_sha256='inputs',coverage_duration_ms=5000)
        rows=[dict(r,model=copy.deepcopy(m)) for r in rows];original=copy.deepcopy(s)
        hold=resource_policy.running_reservation(s,m,rows,u,now)
        self.assertLessEqual(hold['memory_gb'],max(1,u['peak_gb'])*1.5)
        self.assertLessEqual(hold['commit_gb'],max(1.2,u['peak_private_gb'])*1.5)
        self.assertEqual(s,original)
        self.assertEqual(hold['peak_headroom_cap']['ratio'],1.5)

    def test_small_excess_still_obeys_cap_with_one_complete_matching_run(self):
        now,m,s,u,rows=ActiveReservationsTest().fixture();m.update(state_binding_sha256='inputs',coverage_duration_ms=5000)
        rows=[dict(rows[0],model=copy.deepcopy(m))];s.update(memory_gb=2,commit_gb=2.1)
        hold=resource_policy.running_reservation(s,m,rows,u,now)
        self.assertIsNotNone(hold);self.assertLessEqual(hold['memory_gb'],1.5*u['peak_gb'])

    def test_early_or_shorter_record_cannot_cap_future_memory(self):
        _,m,_,u,rows=ActiveReservationsTest().fixture();m.update(state_binding_sha256='inputs',coverage_duration_ms=5000)
        rows=[dict(r,model=dict(m,coverage_duration_ms=600)) for r in rows]
        self.assertIsNone(resource_policy.completed_peak_cap(m,rows,u))
        for r in rows:r['model']['coverage_duration_ms']=5000;r['observation']['cleanup_verified']=False
        self.assertIsNone(resource_policy.completed_peak_cap(m,rows,u))

    def test_audit_distinguishes_reservation_from_execution_limit_and_incomplete_peak(self):
        j=dict(id='x',status='running',node='n',spec=dict(name='compute',memory_gb=20,commit_gb=25),scheduling_reservation=dict(memory_gb=3,commit_gb=4))
        state=dict(now=100,jobs=[j],nodes=[dict(id='n',telemetry=dict(usages={'x':dict(peak_gb=1,peak_private_gb=1.2)}))])
        row=inspect(state)['running'][0]
        self.assertEqual(row['observed_ratio'],3);self.assertEqual(row['execution_limit_memory_gb'],20)
        self.assertEqual(row['verdict'],'provisional_over_ratio')
        j['scheduling_reservation']['peak_headroom_cap']=dict(reference_peak_gb=2,sample_ids=['finished'])
        self.assertEqual(inspect(state)['running'][0]['verdict'],'within_completed_peak_limit')

    def test_pending_exact_complete_reference_also_caps_before_start(self):
        _,model,spec,_,rows=ActiveReservationsTest().fixture()
        model.update(state_binding_sha256='same-complete-inputs',coverage_duration_ms=5000)
        rows=[dict(rows[0],model=copy.deepcopy(model))]
        spec['resource_model']=model;before=copy.deepcopy(spec)
        revised=resource_policy.fit_completed(spec,rows)
        self.assertAlmostEqual(revised['memory_gb'],1.5)
        self.assertAlmostEqual(revised['commit_gb'],1.8)
        self.assertEqual(revised['resource_calibration']['method'],'completed_peak_cap')
        self.assertEqual(spec,before)
        for values in [dict(resource_retries=1),dict(resource_mode='fixed'),dict(restart_policy='never')]:
            protected=dict(spec,**values)
            self.assertEqual(resource_policy.fit_completed(protected,rows),protected)

    def test_short_failed_or_different_state_never_gives_pending_peak_cap(self):
        _,model,spec,_,rows=ActiveReservationsTest().fixture()
        model.update(state_binding_sha256='same',coverage_duration_ms=5000)
        spec['resource_model']=model
        for other,ob in [(dict(model,coverage_duration_ms=600),rows[0]['observation']),
                         (dict(model,state_binding_sha256='different'),rows[0]['observation']),
                         (model,dict(rows[0]['observation'],cleanup_verified=False))]:
            revised=resource_policy.fit_completed(spec,[dict(id='old',model=other,observation=ob)])
            self.assertNotEqual(revised.get('resource_calibration',{}).get('method'),'completed_peak_cap')

    def test_native_oom_and_structured_limit_feedback_include_increasing_budgets(self):
        for code,text in [('resource_limit','超过任务内存上限'),('model_error','java.lang.OutOfMemoryError: Java heap space'),('execution_failed','MemoryError')]:
            health=task_health.diagnose(dict(lifecycle='failed',failure_code=code,failure_detail=text))
            self.assertIn('提高',health['next_action']);self.assertIn('memory_gb',health['next_action']);self.assertIn('commit_gb',health['next_action'])


if __name__=='__main__':unittest.main()


class LinuxIncompletePeakTest(unittest.TestCase):
    def test_sampled_linux_peak_never_becomes_full_run_memory_reference(self):
        job=dict(id='linux',status='succeeded',finished=100,spec=dict(name='linux',memory_gb=16,resource_observation=dict(cleanup_verified=True,peak_gb=.05,peak_complete=False)))
        result=inspect(dict(now=101,jobs=[job],nodes=[]))
        self.assertEqual(result['completed'][0]['verdict'],'awaiting_complete_peak')
        self.assertEqual(result['issues'],[])
    def test_linux_memory_limit_feedback_includes_budget_increase(self):
        health=task_health.diagnose(dict(lifecycle='failed',failure_code='memory_limit',failure_detail='cgroup cap'))
        self.assertEqual(health['code'],'resource_limit')
        self.assertIn('memory_gb',health['next_action']);self.assertIn('commit_gb',health['next_action'])
