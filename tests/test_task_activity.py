import copy
import unittest
import task_activity

class TaskActivityTest(unittest.TestCase):
    def test_own_observation_and_stage_only(self):
        job=dict(status='running',spec=dict(kind='comsol',cores=8),progress=dict(scope='stage',stage='Memory: 3000'),progress_updated_at=95)
        before=copy.deepcopy(job)
        a=task_activity.summarize(job,dict(at=99,cpu_cores=7.8),100,True)
        self.assertEqual(a['cpu_cores'],7.8)
        self.assertEqual(a['state'],'cpu_active')
        self.assertIn('未报告具体阶段',a['stage_label'])
        self.assertEqual(a['progress_updated_at'],95)
        self.assertEqual(job,before)
    def test_stale_offline_zero_missing_invalid(self):
        j=dict(status='running',spec={})
        for usage,online in [(dict(at=50,cpu_cores=8),True),(dict(at=99,cpu_cores=8),False),({},True),(dict(at=101,cpu_cores=8),True)]:
            self.assertFalse(task_activity.summarize(j,usage,100,online)['cpu_fresh'])
        zero=task_activity.summarize(j,dict(at=99,cpu_cores=0),100,True)
        self.assertEqual(zero['cpu_cores'],0)
        self.assertEqual(zero['state'],'observed')
        for value in [float('nan'),-1,True]:
            self.assertIsNone(task_activity.summarize(j,dict(at=99,cpu_cores=value),100,True)['cpu_cores'])
