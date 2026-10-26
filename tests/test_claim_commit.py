"""A scheduler projection is not a delivered execution lease."""
import concurrent.futures
import json
import time
import unittest
from server import create_app
import test_center

class ClaimCommitTest(unittest.TestCase):
    setUp=test_center.CenterTest.setUp
    tearDown=test_center.CenterTest.tearDown
    package=test_center.CenterTest.package
    submit=test_center.CenterTest.submit
    heartbeat=test_center.CenterTest.heartbeat
    claim=test_center.CenterTest.claim

    def rows(self):
        with self.app.center_db() as c:
            return {r['id']:dict(r) for r in c.execute('SELECT * FROM jobs')}

    def test_one_response_commits_only_one_lease(self):
        ids=[self.submit().json['ids'][0] for _ in range(4)]
        self.heartbeat(max_parallel=20,active_ids=[])
        job=self.claim();rows=self.rows()
        self.assertEqual(job['id'],ids[0])
        self.assertEqual(sum(r['status']=='leased' for r in rows.values()),1)
        for jid in ids[1:]:
            self.assertEqual(rows[jid]['status'],'queued')
            self.assertIsNone(rows[jid]['node']);self.assertIsNone(rows[jid]['lease'])

    def test_retry_returns_identical_offer_without_reserving_followers(self):
        ids=[self.submit().json['ids'][0] for _ in range(3)]
        self.heartbeat(max_parallel=20,active_ids=[])
        first=self.claim();again=self.claim()
        self.assertEqual((again['id'],again['lease']),(first['id'],first['lease']))
        self.assertTrue(all(self.rows()[j]['status']=='queued' for j in ids[1:]))

    def test_acknowledged_worker_can_claim_more_disjoint_tasks(self):
        ids=[self.submit().json['ids'][0] for _ in range(3)]
        self.heartbeat(max_parallel=20,active_ids=[]);a=self.claim()
        self.heartbeat(max_parallel=20,active_ids=[a['id']]);b=self.claim()
        self.assertEqual([a['id'],b['id']],ids[:2])
        self.assertFalse(set(a['spec']['cpu_ids'])&set(b['spec']['cpu_ids']))
        self.assertEqual(self.rows()[ids[2]]['status'],'queued')

    def test_new_priority_can_pass_uncommitted_projection(self):
        ids=[self.submit().json['ids'][0] for _ in range(3)]
        self.heartbeat(max_parallel=20,active_ids=[]);first=self.claim()
        self.client.post('/api/jobs/'+ids[2]+'/priority',json={'priority':100},headers=self.csrf)
        self.heartbeat(max_parallel=20,active_ids=[first['id']]);urgent=self.claim()
        self.assertEqual(urgent['id'],ids[2]);self.assertEqual(self.rows()[ids[1]]['status'],'queued')
        self.assertEqual(self.rows()[first['id']]['lease'],first['lease'])

    def test_poll_does_not_commit_another_nodes_plan(self):
        config=self.root/'config.json';cfg=json.loads(config.read_text());cfg['nodes'].append(dict(cfg['nodes'][0],id='dell-r940',label='Dell'));config.write_text(json.dumps(cfg));self.app=create_app(config);self.client=self.app.test_client()
        with self.client.session_transaction()as s:s.update(signed_in=True,csrf='csrf')
        ids=[self.submit().json['ids'][0]for _ in range(2)]
        self.heartbeat(max_parallel=20,active_ids=[])
        self.client.post('/agent/dell-r940/heartbeat',headers=self.auth,json={'telemetry':dict(cpu_threads=8,memory_total_gb=16,memory_available_gb=12,reserve_gb=4,max_parallel=20,active_ids=[],runtimes={'python':{'kind':'python'}})})
        self.assertIsNone(self.claim())
        self.assertTrue(all(r['status']=='queued'for r in self.rows().values()))
        chosen=self.client.post('/agent/dell-r940/claim',headers=self.auth).json['job']
        self.assertEqual(chosen['id'],ids[0]);self.assertEqual(self.rows()[ids[1]]['status'],'queued')

    def test_layout_pending_does_not_fill_host_with_undelivered_leases(self):
        ids=[self.submit().json['ids'][0]for _ in range(3)]
        now=time.time();self.heartbeat(max_parallel=20,active_ids=[],storage_observation=dict(at=now,volumes=[dict(id='D',free_gb=100,reserve_gb=4)],default='D',managed=[]))
        self.assertIsNone(self.claim());self.assertIsNone(self.claim())
        rows=self.rows();self.assertEqual(rows[ids[0]]['status'],'leased')
        self.assertFalse(json.loads(rows[ids[0]]['spec'])['work_layout_ready'])
        self.assertTrue(all(rows[j]['status']=='queued'for j in ids[1:]))

    def test_parallel_retries_do_not_double_commit(self):
        ids=[self.submit().json['ids'][0]for _ in range(3)];self.heartbeat(max_parallel=20,active_ids=[])
        def call():
            with self.app.test_client()as client:return client.post('/agent/win2/claim',headers=self.auth).json['job']
        with concurrent.futures.ThreadPoolExecutor(2)as ex:a,b=list(ex.map(lambda _:call(),range(2)))
        self.assertEqual((a['id'],a['lease']),(b['id'],b['lease']))
        self.assertEqual(sum(r['status']=='leased'for r in self.rows().values()),1)

    def test_new_offer_leaves_running_identity_unchanged(self):
        a=self.submit().json['ids'][0];b=self.submit().json['ids'][0]
        self.heartbeat(max_parallel=20,active_ids=[]);job=self.claim()
        self.client.post('/agent/win2/jobs/'+a+'/update',json={'status':'running'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
        before=self.rows()[a]
        self.heartbeat(max_parallel=20,active_ids=[a]);self.assertEqual(self.claim()['id'],b)
        after=self.rows()[a]
        for key in ['status','node','lease','spec','started','compute_started']:
            self.assertEqual(after[key],before[key])
