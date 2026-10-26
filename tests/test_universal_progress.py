import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import batches
import task_progress
from test_center import CenterTest


class ProgressContractsTest(unittest.TestCase):
    def test_goals_and_units_preserve_actual_demands(self):
        value=dict(completed=2,total=10,unit='epochs',stage='training',metrics=[dict(name='cycles',completed=800,total=1000,unit='cycles')])
        self.assertEqual(task_progress.normalize(value),value)
        for bad in [dict(completed=True,total=10),dict(completed=11,total=10),dict(completed=float('nan'),total=10),dict(completed=2,total=0)]:
            with self.assertRaises((ValueError,KeyError)):task_progress.normalize(bad)
        for bad in [dict(metrics=['invalid']),dict(completed=10**1000,total=10**1001)]:
            with self.assertRaises(ValueError):task_progress.normalize(bad)

    def test_explicit_progress_precedes_native_stage_and_old_reference_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);start=time.time()-10
            (p/'comsol_batch.log').write_text('Current Progress: 69 % - Matrix factorization\n')
            (p/'reference').mkdir();(p/'reference/progress.json').write_text('{"completed":100,"total":100}')
            native=task_progress.collect(p,'comsol',start)
            self.assertEqual(native['scope'],'stage');self.assertEqual(native['completed'],69)
            (p/'progress.json').write_text('{"completed":20,"total":100,"unit":"cycles"}')
            self.assertEqual(task_progress.collect(p,'comsol',start)['completed'],20)
            os.utime(p/'progress.json',(start-20,start-20));self.assertEqual(task_progress.collect(p,'comsol',start)['scope'],'stage')
            os.utime(p/'comsol_batch.log',(start-20,start-20));self.assertEqual(task_progress.collect(p,'comsol',start),{})

    def test_stdout_protocol_for_any_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);log=p/'task.log'
            log.write_text('[preparing]\nLAB_PROGRESS {"completed":99,"total":100}\n[running]\nLAB_PROGRESS {"completed":3,"total":9,"unit":"samples"}\n')
            for kind in ('python','matlab','comsol'):
                self.assertEqual(task_progress.collect(p,kind,time.time()-10,log)['completed'],3)

    def test_native_stage_and_stale_observation_never_create_overall_percentage(self):
        now=time.time();j=dict(status='running');p=dict(completed=69,total=100,scope='stage',stage='factorization',updated_at=now)
        task_progress.apply(j,p,now,now);self.assertNotIn('progress_percent',j);self.assertEqual(j['stage_progress_percent'],69)
        j=dict(status='running');task_progress.apply(j,dict(completed=5,total=10,updated_at=now),now,now-40)
        self.assertFalse(j['progress_fresh']);self.assertNotIn('progress_percent',j)

    def test_symlink_to_other_task_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'work').mkdir();(p/'elsewhere.json').write_text('{"completed":9,"total":10}')
            (p/'work/progress.json').symlink_to(p/'elsewhere.json')
            self.assertEqual(task_progress.collect(p/'work','python',time.time()-10),{})

    def test_unknown_or_heterogeneous_batch_is_not_zero_overall(self):
        job=dict(id='a',batch_id='b',created=1,status='running',spec=dict(name='training'))
        one=batches.summarize([job])[0];self.assertIsNone(one['progress_percent'])
        other=copy.deepcopy(job);other.update(id='c',status='succeeded')
        many=batches.summarize([job,other])[0];self.assertIsNone(many['progress_percent']);self.assertEqual(many['completed_units_percent'],50)


class ProgressApiTest(unittest.TestCase):
    setUp=CenterTest.setUp
    tearDown=CenterTest.tearDown
    package=CenterTest.package
    submit=CenterTest.submit
    heartbeat=CenterTest.heartbeat
    claim=CenterTest.claim

    def test_authenticated_current_lease_stage_survives_heartbeat_without_eta_or_private_data(self):
        jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
        self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'running'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
        item=dict(id=jid,lease=job['lease'],pid=123,created=time.time(),progress=dict(completed=69,total=100,scope='stage',stage='solver',source='comsol_batch.log',updated_at=time.time()))
        route='/agent/win2/progress-observations'
        self.assertEqual(self.client.post(route,json={'items':[item]}).status_code,401)
        bad=dict(item,lease='wrong');self.assertEqual(self.client.post(route,json={'items':[bad]},headers=self.auth).json['accepted'],0)
        self.assertEqual(self.client.post(route,json={'items':[item]},headers=self.auth).json['accepted'],1)
        self.heartbeat(worker_busy=True,usages={jid:dict(at=time.time(),elapsed_seconds=100,progress=dict(completed=69,total=100))})
        state=self.client.get('/api/state').json;j=next(j for j in state['jobs'] if j['id']==jid)
        self.assertNotIn('progress_percent',j);self.assertEqual(j['stage_progress_percent'],69)
        self.assertNotIn('estimated_end',j);self.assertNotIn('progress_observation',state['nodes'][0]['telemetry'])
        self.assertNotIn(job['lease'],json.dumps(state))
        item['progress']=dict(completed=25,total=100,unit='samples',updated_at=time.time())
        self.client.post(route,json={'items':[item]},headers=self.auth)
        j=self.client.get('/api/state').json['jobs'][0];self.assertEqual(j['progress_percent'],25);self.assertEqual(j['estimated_seconds'],400)
        self.assertEqual(j['completion_estimate']['state'],'range')
        self.assertLess(j['completion_estimate']['start'],j['completion_estimate']['end'])
        original=job['spec']
        with self.app.center_db() as db:current=json.loads(db.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0])
        self.assertEqual(original,current)


if __name__=='__main__':unittest.main()
