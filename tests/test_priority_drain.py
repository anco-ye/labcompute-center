import copy,unittest
from test_scheduler import SchedulingTest
import scheduler


class PriorityDrainTest(unittest.TestCase):
    node=SchedulingTest.node
    job=SchedulingTest.job

    def test_waiting_highest_priority_blocks_all_lower_backfill(self):
        a=self.node('a');b=self.node('b',cpu_pool=[0,1])
        running=self.job('run',cores=4);running.update(status='running',node='a')
        high=self.job('high',cores=6);high['priority']=20
        jobs=[running,high,self.job('ordinary',created=0)]
        before=copy.deepcopy(jobs);placed,reasons=scheduler.plan([a,b],jobs)
        self.assertEqual(placed,{})
        self.assertIn('最高优先级',reasons['ordinary'])
        self.assertEqual(jobs,before)

    def test_lower_claims_resume_after_highest_demand_is_satisfied(self):
        high=self.job('high',cores=6);high['priority']=20
        nodes=[self.node('a'),self.node('b')]
        placed,_=scheduler.plan(nodes,[high,self.job('ordinary')])
        self.assertIn('high',placed);self.assertNotIn('ordinary',placed)
        # A hypothetical placement must not release the barrier. Only the
        # next state containing the real lease may admit lower priorities.
        high.update(status='leased',node=placed['high']['node'],spec=placed['high']['spec'])
        placed,_=scheduler.plan(nodes,[high,self.job('ordinary')])
        self.assertIn('ordinary',placed)

    def test_dependency_inherits_priority_without_mutating_input(self):
        pre=self.job('pre',task_key='pre');high=self.job('high',task_key='high',depends_on=['pre']);high['priority']=20
        for j in [pre,high]:j['batch_id']='batch'
        jobs=[self.job('ordinary',created=0),pre,high];before=copy.deepcopy(jobs)
        placed,reasons=scheduler.plan([self.node()],jobs)
        self.assertIn('pre',placed);self.assertNotIn('ordinary',placed)
        self.assertIn('最高优先级',reasons['ordinary']);self.assertEqual(jobs,before)

    def test_running_predecessor_keeps_lower_queue_drained(self):
        pre=self.job('pre',task_key='pre');pre.update(status='running',node='a',batch_id='batch')
        high=self.job('high',task_key='high',depends_on=['pre']);high.update(priority=10,batch_id='batch')
        placed,_=scheduler.plan([self.node()],[pre,high,self.job('ordinary')])
        self.assertEqual(placed,{})

    def test_normal_priority_keeps_existing_bounded_backfill(self):
        a=self.node('a');b=self.node('b',cpu_pool=[0,1]);running=self.job('run',cores=4);running.update(status='running',node='a')
        placed,_=scheduler.plan([a,b],[running,self.job('big',cores=6),self.job('ordinary')])
        self.assertEqual(placed['ordinary']['node'],'b')

    def test_cancelled_or_explicitly_held_high_priority_does_not_block(self):
        for status in ['cancelled','held']:
            high=self.job('high');high.update(status=status,priority=20)
            placed,_=scheduler.plan([self.node()],[high,self.job('ordinary')])
            self.assertIn('ordinary',placed)
