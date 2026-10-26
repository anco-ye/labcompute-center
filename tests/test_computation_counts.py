import copy
import unittest
import batches


def job(key,status='succeeded',**more):
    value=dict(id=key,batch_id='b',created=1,status=status,spec=dict(name=key,batch_name='batch'),node='win1')
    value.update(more)
    return value


class ComputationCountsTest(unittest.TestCase):
    def test_completed_and_retired_historical_stages_do_not_count(self):
        jobs=[job(str(i)) for i in range(16)]+[job('run'+str(i),'running') for i in range(3)]
        jobs += [job('old'+str(i),'cancelled',detail='复核/汇总移至提交者Mac，移出中心计算图') for i in range(20)]
        jobs += [job('review'+str(i),non_compute_stage='independent_short_source_and_numerical_review') for i in range(5)]
        original=copy.deepcopy(jobs)
        b=batches.summarize(jobs)[0]
        self.assertEqual((b['total'],b['completed'],b['running'],b['queued'],b['cancelled']),(19,16,3,0,0))
        self.assertEqual(b['historical_stage_count'],25)
        self.assertEqual(len(b['attempts']),44)
        self.assertEqual(jobs,original)
        self.assertNotIn('retry',b['actions'])
    def test_real_cancelled_calculation_and_names_are_not_hidden(self):
        jobs=[job('calculation_review','cancelled',detail='用户取消'),job('solve','running')]
        b=batches.summarize(jobs)[0]
        self.assertEqual((b['total'],b['cancelled'],b['historical_stage_count']),(2,1,0))
        self.assertIn('retry',b['actions'])
    def test_history_only_has_no_divide_by_zero_or_restart_action(self):
        j=job('review');j['spec']['stage']='scientific_review'
        b=batches.summarize([j])[0]
        self.assertTrue(b['historical_stage_only'])
        self.assertEqual(b['total'],0)
        self.assertEqual(b['completed_units_percent'],0)
        self.assertEqual(b['actions'],[])
        self.assertEqual(b['attempts'],[j])
    def test_latest_calculation_still_deduplicates_attempts(self):
        first=job('first','failed');first['spec']['unit_id']='logical'
        last=job('last','running',created=2);last['spec']['unit_id']='logical'
        b=batches.summarize([first,last])[0]
        self.assertEqual((b['total'],b['running']),(1,1))
        self.assertEqual(len(b['attempts']),2)
