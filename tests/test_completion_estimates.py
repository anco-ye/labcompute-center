import tempfile
import unittest
from pathlib import Path
from completion_estimates import CompletionEstimates


class CompletionRangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'eta.json'
        self.model=CompletionEstimates(self.path)
        self.job=dict(id='task',status='running',node='win1',compute_started=1,
                      elapsed_seconds=3600,progress_fresh=True,
                      progress=dict(completed=25,total=100,stage='solve'))
    def tearDown(self):self.tmp.cleanup()
    def test_history_and_recent_stage_work_contribute(self):
        self.model.apply(self.job,[14000,15000,16000],100000)
        self.job.update(elapsed_seconds=4200)
        self.job['progress']['completed']=30
        result=self.model.apply(self.job,[14000,15000,16000],100600)
        self.assertEqual(result['state'],'range')
        self.assertEqual(result['history_samples'],3)
        self.assertIn('当前阶段近期速度',result['source'])
        self.assertLess(result['start'],result['end'])
    def test_no_per_refresh_timestamp_drift_and_survives_reload(self):
        first=self.model.apply(self.job,[],100000)
        self.job['elapsed_seconds']+=10
        self.job['progress']['completed']+=.1
        second=self.model.apply(self.job,[],100010)
        self.assertEqual((first['start'],first['end']),(second['start'],second['end']))
        restored=CompletionEstimates(self.path)
        third=restored.apply(self.job,[],100020)
        self.assertEqual((first['start'],first['end']),(third['start'],third['end']))
    def test_stage_percent_is_never_overall(self):
        self.job['progress']['scope']='stage'
        self.assertEqual(self.model.apply(self.job,[],100000)['state'],'learning')
        with_history=self.model.apply(self.job,[15000],100000)
        self.assertEqual(with_history['state'],'range')
        self.assertNotIn('整体进度',with_history['source'])
    def test_phase_change_and_regression_discard_old_speed(self):
        self.model.apply(self.job,[],100000)
        self.job['progress'].update(stage='export',completed=2)
        self.model.apply(self.job,[],100030)
        record=self.model.records['task']
        self.assertEqual(len(record['points']),1)
        self.assertEqual(record['points'][0][1],2)
    def test_never_promises_while_queued_stale_paused_done_or_offline(self):
        for status in ['queued','preparing','paused','uploading','succeeded','failed']:
            self.job['status']=status
            self.assertNotEqual(self.model.apply(self.job,[15000],100000)['state'],'range')
        self.job['status']='running';self.job['progress_fresh']=False
        self.assertEqual(self.model.apply(self.job,[15000],100000)['state'],'stale')
        self.job['progress_fresh']=True
        self.assertNotEqual(self.model.apply(self.job,[15000],100000,False)['state'],'range')
        self.job['progress']['completed']=100
        self.assertEqual(self.model.apply(self.job,[15000],100000)['state'],'finalizing')
    def test_no_mutation_of_execution_spec_and_no_bad_history(self):
        self.job['spec']=dict(memory_gb=10,cores=2)
        self.model.apply(self.job,[float('nan'),-1,True],100000)
        self.assertEqual(self.job['spec'],dict(memory_gb=10,cores=2))
        self.assertEqual(self.job['completion_estimate']['history_samples'],0)
    def test_expired_window_is_not_held_and_no_progress_reestimated(self):
        first=self.model.apply(self.job,[],100000)
        late=self.model.apply(self.job,[],first['end']+1)
        self.assertGreater(late['end'],first['end']+1)
        self.job['elapsed_seconds']+=1000
        self.assertEqual(self.model.apply(self.job,[],first['end']+1000)['state'],'learning')
