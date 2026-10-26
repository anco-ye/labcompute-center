import copy
import hashlib
import io
import struct
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import resource_contracts
import resource_envelope
import resource_policy
import scheduler
import test_active_reservations


class SourceEnvelopeTest(unittest.TestCase):
    def fixture(self):
        m=dict(family='audited',allocation_contract='buffered-linear-v1',coverage_duration_ms=5000,
               features=dict(segments=400,cells=4,retained_values=4000),retained_dimensions=['retained_values'],state_binding_sha256='same-input')
        rows=[]
        for n in (100,200,300):
            model=dict(m,features=dict(segments=n,cells=n/100,retained_values=n*10),state_binding_sha256=str(n))
            rows.append(dict(id=str(n),model=model,observation=dict(cleanup_verified=True,peak_gb=.2+n*.002,peak_private_gb=.3+n*.003)))
        return m,rows

    def test_affine_fit_covers_samples_and_predicts_scale(self):
        m,rows=self.fixture();fit=resource_envelope.completed_envelope(m,rows)
        self.assertIsNotNone(fit);self.assertEqual(fit['samples'],3)
        self.assertGreaterEqual(fit['memory_gb'],(.2+400*.002)*1.6+.75)
        for row in rows:
            target=dict(m,features=row['model']['features'])
            fitted=resource_envelope.completed_envelope(target,rows)
            self.assertGreaterEqual(fitted['memory_gb'],row['observation']['peak_gb']*1.6+.75)

    def test_rejects_incomplete_other_code_far_scale_or_duplicate_shapes(self):
        m,rows=self.fixture()
        for change in ('cleanup','family','duration','contract'):
            changed=copy.deepcopy(rows)
            if change=='cleanup':changed[0]['observation']['cleanup_verified']=False
            elif change=='family':changed[0]['model']['family']='other'
            elif change=='duration':changed[0]['model']['coverage_duration_ms']=600
            else:changed[0]['model']['allocation_contract']='unverified'
            self.assertIsNone(resource_envelope.completed_envelope(m,changed),change)
        far=copy.deepcopy(m);far['features']['segments']=1201
        self.assertIsNone(resource_envelope.completed_envelope(far,rows))
        self.assertIsNone(resource_envelope.completed_envelope(m,[rows[0]]*3))

    def test_exact_source_state_anchor_adds_only_learned_growth(self):
        m,rows=self.fixture();anchor=dict(id='anchor',model=dict(m,coverage_duration_ms=600,
            features=dict(m['features'],retained_values=480)),observation=dict(cleanup_verified=True,peak_gb=.7,peak_private_gb=.8))
        fit=resource_envelope.completed_envelope(m,rows+[anchor]);self.assertEqual(fit['anchor_ids'],['anchor'])
        # Wrong input identity cannot anchor, even with identical numeric shape.
        anchor['model']['state_binding_sha256']='other-input'
        self.assertEqual(resource_envelope.completed_envelope(m,rows+[anchor])['anchor_ids'],[])

    def test_static_bound_needs_kernel_and_preserves_original_limits(self):
        now,_,spec,usage,_=test_active_reservations.ActiveReservationsTest().fixture()
        model=dict(family='source',allocation_contract='sequential-fixed-workspace-v1',features=dict(sequential_units=100),static_memory_gb=1.5,static_commit_gb=2)
        before=copy.deepcopy(spec);hold=resource_policy.running_reservation(spec,model,[],usage,now)
        self.assertEqual(hold['method'],'verified_workspace_reservation');self.assertGreaterEqual(hold['memory_gb'],usage['peak_gb']*1.6+.75)
        self.assertEqual(spec,before)
        self.assertIsNone(resource_policy.running_reservation(dict(spec,kernel_memory_required=False),model,[],usage,now))
        self.assertIsNone(resource_policy.running_reservation(spec,model,[],dict(usage,at=now-16),now))
        self.assertIsNone(resource_policy.running_reservation(dict(spec,resource_retries=1),model,[],usage,now))

    def test_legacy_kernel_evidence_binds_budget_identity_lease_and_freshness(self):
        now,node,job=test_active_reservations.ActiveReservationsTest().scheduler_fixture()
        job['spec'].pop('kernel_memory_required');job['lease']='lease-generation'
        proof=dict(verified=True,pid=10,created=1,limit_bytes=int(20.25*2**30),lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest())
        node['telemetry']['memory_guard_observation']=dict(received_at=now,items={job['id']:proof})
        self.assertTrue(scheduler.kernel_guard_verified(job,node,now))
        original=copy.deepcopy(job['spec']);self.assertLess(scheduler.reservation(job,node,now)['memory_gb'],16);self.assertEqual(job['spec'],original)
        for field,value in [('limit_bytes',int(19*2**30)),('pid',11),('created',2),('lease_digest','old'),('verified',False)]:
            saved=proof[field];proof[field]=value;self.assertFalse(scheduler.kernel_guard_verified(job,node,now));proof[field]=saved
        self.assertFalse(scheduler.kernel_guard_verified(job,node,now+16))

    def test_retired_pair_reference_adapter_is_never_used_for_resource_admission(self):
        task=dict(id='unit',environment='env',stage='inferred_pair_calibration_group',pairs=['a_b'])
        spec=dict(task_key='unit',kind='python',runtime='solver',entry='main.py',cores=1)
        self.assertFalse(hasattr(resource_contracts,'_pair_shapes'))
        self.assertIsNone(resource_contracts.model('unused.zip',spec,(next(iter(resource_contracts.AUDITED_BUNDLES)),{'unit':task},True)))



class MemoryObservationApiTest(unittest.TestCase):
    import test_center as _test_center
    setUp=_test_center.CenterTest.setUp
    tearDown=_test_center.CenterTest.tearDown
    package=_test_center.CenterTest.package
    submit=_test_center.CenterTest.submit
    heartbeat=_test_center.CenterTest.heartbeat
    claim=_test_center.CenterTest.claim

    def test_guards_require_node_current_lease_and_original_budget_and_are_private(self):
        jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
        self.client.post('/agent/win2/jobs/'+jid+'/update',json={'status':'running'},headers=dict(self.auth,**{'X-Lease':job['lease']}))
        with self.app.center_db() as c:
            before=c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0];spec=json.loads(before)
        item=dict(id=jid,lease=job['lease'],pid=123,created=time.time(),verified=True,
                  limit_bytes=int(scheduler.commit_budget(dict(spec,kernel_memory_required=True))*2**30))
        route='/agent/win2/memory-observations'
        self.assertEqual(self.client.post(route,json={'items':[item]}).status_code,401)
        self.assertEqual(self.client.post(route,json={'items':[dict(item,lease='old')]},headers=self.auth).json['accepted'],0)
        self.assertEqual(self.client.post(route,json={'items':[dict(item,limit_bytes=item['limit_bytes']-1)]},headers=self.auth).status_code,400)
        self.assertEqual(self.client.post(route,json={'items':[item]},headers=self.auth).json['accepted'],1)
        self.heartbeat()
        with self.app.center_db() as c:
            telemetry=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',('win2',)).fetchone()[0])
            self.assertIn(jid,telemetry['memory_guard_observation']['items'])
            self.assertEqual(c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0],before)
        state=self.client.get('/api/state').json
        self.assertNotIn('memory_guard_observation',state['nodes'][0]['telemetry'])
        self.assertNotIn(job['lease'],json.dumps(state))


class StartupPaddingTest(unittest.TestCase):
    def fixture(self):
        now,node,job=test_active_reservations.ActiveReservationsTest().scheduler_fixture()
        node['telemetry']['scheduling_policy']=dict(elastic_memory=dict(enabled=True,fraction=.1))
        request=dict(runtime='python',kind='python',cores=2,memory_gb=14,commit_gb=18,disk_gb=1,
                     resource_mode='estimated',restart_policy='from_start',kernel_memory_required=True,
                     resource_estimate=dict(memory_gb=10,commit_gb=12))
        return now,node,job,request

    def test_startup_uses_padding_within_post_admission_ten_percent_and_keeps_limits(self):
        now,node,job,request=self.fixture();original=copy.deepcopy(job)
        normal=scheduler.budget(node,[job],now);self.assertLess(normal['memory_gb'],14)
        capacity=scheduler.admission_capacity(node,request,[job],now)
        self.assertLessEqual(normal['elastic_memory_credit_gb']+capacity['startup_memory_credit_gb'],(normal['reserved_memory_gb']+14)*.1)
        placed,reason=scheduler.allocate(node,request,[job],now)
        self.assertIsNotNone(placed);self.assertEqual(placed['memory_gb'],14);self.assertEqual(placed['commit_gb'],18)
        self.assertGreater(placed['startup_memory_credit_gb'],0);self.assertNotIn(0,placed['cpu_ids']);self.assertEqual(job,original)

    def test_no_startup_credit_for_uncertain_fixed_retried_stale_or_unpadded_requests(self):
        for mode in ('fixed','never','retry','no_kernel','no_padding','unproven','stale'):
            now,node,job,request=self.fixture()
            if mode=='fixed':request['resource_mode']='fixed'
            if mode=='never':request['restart_policy']='never'
            if mode=='retry':request['resource_retries']=1
            if mode=='no_kernel':request['kernel_memory_required']=False
            if mode=='no_padding':request['resource_estimate']['memory_gb']=14
            if mode=='unproven':job.pop('admission_reservation')
            if mode=='stale':node['telemetry']['storage_observation']['at']=now-16
            self.assertIsNone(scheduler.allocate(node,request,[job],now)[0],mode)

    def test_cpu_commit_and_current_physical_memory_are_strict(self):
        for mode in ('cpu','commit','physical'):
            now,node,job,request=self.fixture()
            if mode=='cpu':request['cores']=7
            if mode=='commit':request['commit_gb']=100
            if mode=='physical':node['telemetry']['memory_available_gb']=15
            self.assertIsNone(scheduler.allocate(node,request,[job],now)[0],mode)


class LegacyPayloadTreeTest(unittest.TestCase):
    def test_only_windows_direct_console_host_is_excluded(self):
        import legacy_memory_guard
        from unittest.mock import Mock
        root=Mock(pid=10)
        windows=Path('C:/Windows')/'System32'/'conhost.exe'
        console=Mock();console.ppid.return_value=10;console.name.return_value='conhost.exe';console.exe.return_value=str(windows)
        fake=Mock();fake.ppid.return_value=10;fake.name.return_value='conhost.exe';fake.exe.return_value=str(Path('task')/'conhost.exe')
        python=Mock();python.ppid.return_value=10;python.name.return_value='python.exe'
        nested_console=Mock();nested_console.ppid.return_value=11;nested_console.name.return_value='conhost.exe';nested_console.exe.return_value=str(windows)
        root.children.return_value=[console,fake,python,nested_console]
        with patch.dict('os.environ',{'SystemRoot':'C:/Windows'}):
            self.assertEqual(legacy_memory_guard.Guard.payload_tree(root),[root,fake,python,nested_console])


class LegacyGuardAdmissionApiTest(unittest.TestCase):
    import test_center as _test_center
    setUp=_test_center.CenterTest.setUp
    tearDown=_test_center.CenterTest.tearDown
    package=_test_center.CenterTest.package
    submit=_test_center.CenterTest.submit

    def test_real_state_and_claim_use_same_private_kernel_evidence_without_changing_running_spec(self):
        from server import create_app
        config=self.root/'config.json';cfg=json.loads(config.read_text());cfg['active_resource_reservations']={'enabled':True};config.write_text(json.dumps(cfg))
        self.app=create_app(config);self.client=self.app.test_client()
        with self.client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
        ids=[self.submit().json['ids'][0] for _ in range(5)]
        now,model,spec,usage,samples=test_active_reservations.ActiveReservationsTest().fixture();spec.update(runtime_revision='revision',entry='main.py')
        with self.app.center_db() as c:
            for index,jid in enumerate(ids):
                s=json.loads(c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0]);s.update(spec)
                if index<3:
                    s['resource_observation']=samples[index]['observation']
                    result=self.root/('native-result-'+str(index)+'.zip')
                    with zipfile.ZipFile(result,'w') as z:z.writestr('exit.json',json.dumps(dict(samples[index]['observation'],exit_code=0,peak_complete=True)))
                    c.execute('UPDATE jobs SET result=? WHERE id=?',(str(result),jid))
                if index==3:s['kernel_memory_required']=False
                if index==4:s.update(memory_gb=8,commit_gb=9)
                c.execute('UPDATE jobs SET spec=?,status=?,node=?,owner=?,lease=? WHERE id=?',(json.dumps(s),'succeeded' if index<3 else 'running' if index==3 else 'queued','win2' if index==3 else None,'lab','lease' if index==3 else None,jid))
            telemetry=dict(max_parallel=7,cpu_threads=8,memory_total_gb=24,memory_available_gb=22,reserve_gb=4,
                memory_commit_limit_gb=80,memory_committed_gb=10,memory_commit_verified=True,
                resource_observation=dict(at=now,verified=True,state='normal'),
                storage_observation=dict(at=now,volumes=[dict(id='D',free_gb=500,reserve_gb=10)],default='D',managed=[dict(job=ids[3],pid=10,created=1)]),
                usages={ids[3]:usage},active_ids=[ids[3]],runtimes={'python':dict(kind='python',revision='revision')})
            c.execute('UPDATE nodes SET telemetry=?,last_seen=? WHERE id=?',(json.dumps(telemetry),now,'win2'))
            before=c.execute('SELECT spec FROM jobs WHERE id=?',(ids[3],)).fetchone()[0]
        proof=dict(id=ids[3],lease='lease',verified=True,pid=10,created=1,limit_bytes=int(20.25*2**30))
        self.assertEqual(self.client.post('/agent/win2/memory-observations',json={'items':[proof]},headers=self.auth).json['accepted'],1)
        with patch('resource_metadata.current_model',return_value=model):
            state=self.client.get('/api/state').json
            self.assertEqual(state['nodes'][0]['capacity']['learned_reservation_jobs'],1)
            self.assertNotIn('memory_guard_observation',state['nodes'][0]['telemetry'])
            self.assertEqual(next(j for j in state['jobs'] if j['id']==ids[4])['estimated_node'],'win2')
            self.client.post('/agent/win2/claim',headers=self.auth)
        with self.app.center_db() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(ids[4],)).fetchone()[0],'leased')
            self.assertEqual(c.execute('SELECT spec FROM jobs WHERE id=?',(ids[3],)).fetchone()[0],before)
