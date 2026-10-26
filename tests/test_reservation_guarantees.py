import copy
import time
import unittest

import resource_policy
import scheduler
import test_active_reservations
import test_scheduler


class ReservationGuaranteesTest(unittest.TestCase):
    def test_future_release_cannot_depend_on_revocable_running_hold(self):
        now, node, running = test_active_reservations.ActiveReservationsTest().scheduler_fixture()
        node['last_seen'] = now
        node['telemetry'].update(cpu_pool=list(range(7)), max_parallel=7)
        short = test_scheduler.SchedulingTest().job('short', cores=2, cpu_ids=[1, 2], wall_minutes=1)
        short.update(status='running', node='a', compute_started=now)
        head = test_scheduler.SchedulingTest().job('head', cores=6, memory_gb=12, commit_gb=13)
        original = copy.deepcopy([node, running, short, head])
        # The 16-GiB execution has no finite release. Its present learned hold
        # would fit the head after short exits, but its full limit would not.
        self.assertIsNone(scheduler.reservation_release(node, head['spec'], [running, short], now))
        self.assertEqual([node, running, short, head], original)
        running['spec']['wall_minutes'] = 5
        running['compute_started'] = now
        release = scheduler.reservation_release(node, head['spec'], [running, short], now)
        # Historical wall_minutes no longer terminate scientific work. They
        # cannot promise resources will be released for a later reservation.
        self.assertIsNone(release)

    def test_future_release_cannot_depend_on_temporary_elastic_credit(self):
        now = time.time()
        fixture = test_scheduler.SchedulingTest()
        node = fixture.node(memory_total_gb=32, memory_available_gb=24,
                            memory_committed_gb=10, memory_commit_limit_gb=80,
                            memory_commit_verified=True,
                            resource_observation=dict(at=now, verified=True, state='normal'),
                            scheduling_policy=dict(elastic_memory=dict(enabled=True, fraction=.1)))
        running = fixture.job('running', cores=1, cpu_ids=[0], memory_gb=16, commit_gb=16,
                              resource_mode='estimated', restart_policy='from_start')
        running.update(status='running', node='a')
        short = fixture.job('short', cores=2, cpu_ids=[1, 2], memory_gb=2, wall_minutes=1)
        short.update(status='running', node='a', compute_started=now)
        node['telemetry']['usages'] = {'running': dict(at=now, phase='running', elapsed_seconds=120,
                                                      rss_gb=1, private_gb=1),
                                     'short': dict(at=now, rss_gb=2, private_gb=2)}
        head = fixture.job('head', cores=6, memory_gb=8, commit_gb=8)
        # After short exits: 26 + 1 - 16 - 4 = 7 GiB, below 8.
        # The current 10% elastic credit must not become a future promise.
        self.assertIsNone(scheduler.reservation_release(node, head['spec'], [running, short], now))

    def test_completed_old_samples_cannot_shrink_an_expanded_retry(self):
        spec = dict(memory_gb=32, commit_gb=40, resource_mode='estimated',
                    restart_policy='from_start', resource_retries=1)
        samples = [dict(cleanup_verified=True, peak_gb=1, peak_private_gb=1.5)] * 3
        original = copy.deepcopy(spec)
        self.assertEqual(resource_policy.calibrate(spec, samples), original)
        self.assertEqual(spec, original)
        normal = dict(spec, resource_retries=0)
        self.assertLess(resource_policy.calibrate(normal, samples)['memory_gb'], 32)


if __name__ == '__main__':
    unittest.main()
