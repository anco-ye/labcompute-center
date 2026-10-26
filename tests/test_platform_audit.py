import copy,unittest
from platform_audit import evaluate,PRODUCTION_NODES

class AuditTest(unittest.TestCase):
 def snapshot(self,**changes):
  s=dict(at=10000,nodes=[dict(id=n,heartbeat_age_seconds=1,sample_age_seconds=1,cpu_percent=10,reserved_cpu_fraction=.9,unacknowledged_leases=0)for n in PRODUCTION_NODES],http=dict(status=200,seconds=.1));s.update(changes);return s
 def kinds(self,s):return {f['kind']for f in evaluate(s)['findings']}
 def test_ready_failed_archive_is_still_an_execution_failure(self):
  s=self.snapshot(recent_failed_jobs=[dict(id='abc',delivery_state='ready',detail='disk_limit')]);r=evaluate(s)
  self.assertIn('recent_execution_failure',self.kinds(s));self.assertTrue(r['findings'][0]['evidence']['native_verification_required'])
 def test_native_pending_and_failed_row_have_one_finding(self):
  s=self.snapshot(recent_failed_jobs=[dict(id='abc')],failed_native_receipts=[dict(job='abc')]);r=evaluate(s)
  self.assertEqual(len([x for x in r['findings']if x['key']=='native-failure:abc']),1)
 def test_low_cpu_alone_does_not_classify_long_calculation_as_fault(self):
  self.assertEqual(evaluate(self.snapshot())['findings'],[])
 def test_progress_elsewhere_does_not_hide_unreceived_leases(self):
  s=self.snapshot();s['nodes'][4].update(unacknowledged_leases=61,oldest_unacknowledged_seconds=34000,recent_successes=7)
  self.assertIn('lease_delivery_gap',self.kinds(s));self.assertIn('reservation_execution_gap',self.kinds(s))
 def test_native_verification_is_required_before_any_lease_action(self):
  s=self.snapshot();s['nodes'][5].update(unacknowledged_leases=2,oldest_unacknowledged_seconds=700)
  r=evaluate(s);self.assertTrue(r['observation_only']);self.assertTrue(r['findings'][0]['evidence']['native_verification_required'])
 def test_new_normal_claims_do_not_warn(self):
  s=self.snapshot();s['nodes'][2].update(unacknowledged_leases=1,oldest_unacknowledged_seconds=20)
  self.assertFalse(evaluate(s)['findings'])
 def test_heartbeat_does_not_mask_stale_resource_observation(self):
  s=self.snapshot();s['nodes'][4]['sample_age_seconds']=35
  self.assertIn('live_agent_stale_resource_sample',self.kinds(s))
 def test_layout_preparation_is_a_separate_gap(self):
  s=self.snapshot();s['nodes'][3].update(layout_pending=4,oldest_layout_seconds=900)
  self.assertIn('storage_preparation_gap',self.kinds(s))
 def test_missing_coverage_is_not_reported_as_healthy(self):
  s=self.snapshot();s['nodes'].pop();r=evaluate(s)
  self.assertFalse(r['all_expected_nodes_present']);self.assertIn('inspection_coverage_missing',self.kinds(s))
 def test_old_pending_fix_remains_unresolved(self):
  s=self.snapshot(pending_changes=[dict(id='fix',status='queued',age_seconds=5000)])
  first=evaluate(s);second=evaluate(dict(s,at=11000),first)
  self.assertIn('verified_fix_pending_activation',self.kinds(s));self.assertFalse(second['new_findings']);self.assertTrue(second['findings'])
 def test_repeat_age_growth_does_not_notify_again(self):
  s=self.snapshot();s['nodes'][4].update(unacknowledged_leases=61,oldest_unacknowledged_seconds=4000)
  first=evaluate(s);s['at']+=3600;s['nodes'][4]['oldest_unacknowledged_seconds']+=3600
  r=evaluate(s,first);self.assertFalse(r['new_findings']);self.assertFalse(r['worsened_findings'])
 def test_real_severity_and_count_worsening_notify(self):
  s=self.snapshot();s['nodes'][4].update(unacknowledged_leases=2,oldest_unacknowledged_seconds=700)
  first=evaluate(s);s['nodes'][4].update(unacknowledged_leases=5,oldest_unacknowledged_seconds=4000)
  self.assertTrue(evaluate(s,first)['worsened_findings'])
 def test_record_gap_is_visible(self):
  self.assertIn('inspection_record_gap',{f['kind']for f in evaluate(self.snapshot(),dict(at=1,findings=[]))['findings']})
 def test_read_only_evaluator_does_not_mutate_snapshot(self):
  s=self.snapshot();before=copy.deepcopy(s);evaluate(s);self.assertEqual(s,before)
 def test_native_check_failure_is_not_silently_skipped(self):
  s=self.snapshot();s['nodes'][0]['native_check_error']='SSH unavailable'
  self.assertIn('inspection_native_check_incomplete',self.kinds(s))
 def test_delayed_control_plane_is_detected(self):
  self.assertIn('control_plane_latency',self.kinds(self.snapshot(http=dict(status=200,seconds=12))))

 def test_native_wall_clock_limit_is_detected_without_killing_any_process(self):
  s=self.snapshot(native={'a800-ssh':{'units':[{'unit':'labcenter-science.service','RuntimeMaxUSec':'6h'}]}})
  self.assertIn('scientific_wall_time_limit_present',self.kinds(s))
 def test_unlimited_native_process_is_normal(self):
  s=self.snapshot(native={'dell-r940':{'units':[{'unit':'labcenter-science.service','RuntimeMaxUSec':'infinity'}]}})
  self.assertFalse(evaluate(s)['findings'])
 def test_reused_pid_cannot_be_treated_as_original_task(self):
  s=self.snapshot(native={'win2':{'task_processes':[{'pid':7,'same_identity':False}]}})
  self.assertIn('task_receipt_process_identity_mismatch',self.kinds(s))
 def test_one_good_observation_does_not_close_intermittent_failure(self):
  s=self.snapshot(http=dict(status=200,seconds=12));first=evaluate(s)
  good=evaluate(self.snapshot(),first);self.assertFalse(good['resolved_keys'])
  recurring=evaluate(s,good);self.assertFalse(recurring['new_findings'])
 def test_two_good_observations_confirm_recovery(self):
  first=evaluate(self.snapshot(http=dict(status=200,seconds=12)))
  second=evaluate(self.snapshot(),first);third=evaluate(self.snapshot(),second)
  self.assertIn('web:latency',third['resolved_keys']);self.assertFalse(third['findings'])

 def test_failed_runner_receipt_is_visible_before_archive_delivery(self):
  s=self.snapshot(failed_native_receipts=[{'job':'oom','failure_code':'resource_limit','status':'uploading'}])
  self.assertIn('execution_failed_while_delivery_pending',self.kinds(s))
 def test_failure_still_pending_transfer_is_not_renotified(self):
  s=self.snapshot(failed_native_receipts=[{'job':'oom','failure_code':'resource_limit'}])
  first=evaluate(s);self.assertFalse(evaluate(s,first)['new_findings'])
