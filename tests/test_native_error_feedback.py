import json,tempfile,unittest
from pathlib import Path
import test_center as fixture
from execution_contract import outcome
from execution_feedback import REQUEST_ID

class NativeErrorTests(unittest.TestCase):
    setUp=fixture.CenterTest.setUp
    tearDown=fixture.CenterTest.tearDown
    submit=fixture.CenterTest.submit
    package=fixture.CenterTest.package
    heartbeat=fixture.CenterTest.heartbeat
    claim=fixture.CenterTest.claim

    def job(self):
        jid=self.submit().json['ids'][0];self.heartbeat();job=self.claim()
        return jid, '/agent/win2/jobs/'+jid+'/stopped', {**self.auth,'X-Lease':job['lease']}

    def receipt(self):
        return dict(exit_code=1,native_exit_code=0,cleanup_verified=True,detail='application failed',execution_outcome=dict(native_exit_code=0,evidence=[dict(source='work/comsol_batch.log',message='/*****Error********/',context='/*****Error********/\nFailed to find consistent initial values.\nLast time step is not converged.')]))

    def test_raw_report_automatic_before_archive_and_deduplicated(self):
        jid,url,headers=self.job()
        for _ in range(2):self.assertEqual(self.client.post(url,json=dict(cleanup_verified=True,execution_report=self.receipt()),headers=headers).status_code,200)
        data=self.client.get('/api/jobs/'+jid+'/feedback').json
        self.assertEqual(len(data['feedback']),1)
        self.assertEqual(data['native_error']['native_exit_code'],0)
        self.assertIn('Last time step',data['native_error']['evidence'][0]['context'])
        self.assertFalse(data['native_error']['ai_generated'])
        with self.app.center_db() as db:
            row=db.execute('SELECT status,result FROM jobs WHERE id=?',(jid,)).fetchone()
            self.assertEqual(row['status'],'uploading');self.assertIsNone(row['result'])

    def test_unverified_receipt_and_wrong_lease_cannot_publish(self):
        jid,url,headers=self.job()
        self.assertEqual(self.client.post(url,json=dict(cleanup_verified=True,execution_report=self.receipt()),headers={**headers,'X-Lease':'wrong'}).status_code,403)
        report=self.receipt();report['cleanup_verified']=False
        self.client.post(url,json=dict(cleanup_verified=True,execution_report=report),headers=headers)
        self.assertIsNone(self.client.get('/api/jobs/'+jid+'/feedback').json['native_error'])

    def test_system_feedback_cannot_be_forged_or_edited(self):
        jid,url,headers=self.job();endpoint='/api/jobs/'+jid+'/feedback'
        self.assertEqual(self.client.post(endpoint,json=dict(request_id=REQUEST_ID,message='fake'),headers=self.csrf).status_code,400)
        self.client.post(url,json=dict(cleanup_verified=True,execution_report=self.receipt()),headers=headers)
        row=self.client.get(endpoint).json['feedback'][0]
        self.assertEqual(self.client.post(endpoint+'/'+str(row['id'])+'/reply',json=dict(reply='rewritten'),headers=self.csrf).status_code,409)

    def test_success_never_creates_error_record(self):
        jid,url,headers=self.job()
        self.client.post(url,json=dict(cleanup_verified=True,execution_report=dict(exit_code=0,cleanup_verified=True)),headers=headers)
        self.assertIsNone(self.client.get('/api/jobs/'+jid+'/feedback').json['native_error'])

class ErrorContextTests(unittest.TestCase):
    def test_utf16_marker_includes_actual_error_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'work').mkdir()
            (folder/'work/comsol_batch.log').write_text('Normal\n/*****Error********/\nFeature: sol1/t1\nFailed to find consistent initial values.\nLast time step is not converged.\n',encoding='utf-16')
            report=outcome(folder,'comsol',0)
            self.assertIn('Failed to find consistent initial values.',report['evidence'][0]['context'])
            self.assertEqual(report['derived_exit_code'],1)

    def test_multibyte_flood_is_bounded_for_exit_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'work').mkdir()
            (folder/'work/comsol_batch.log').write_text(('/*****Error********/\n'+('错误'*3000)+'\n')*15)
            report=outcome(folder,'comsol',0)
            self.assertLess(len(json.dumps(report,ensure_ascii=False).encode()),9000)
            self.assertLessEqual(len(report['evidence']),4)

class WrapperLogTests(unittest.TestCase):
    def test_wrapper_can_declare_owned_native_logs_and_escape_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'job';work=folder/'work';work.mkdir(parents=True)
            (root/'secret.log').write_text('/*****Error********/\nnot an owned task log\n')
            (work/'native.log').write_text('/*****Error********/\nFailed to find consistent initial values.\n')
            receipt=dict(status='failed',exit_code=1,detail='child failed',error_logs=[dict(path='../../secret.log',kind='comsol'),dict(path='native.log',kind='comsol')])
            (work/'execution-result.json').write_text(json.dumps(receipt))
            report=outcome(folder,'python',1)
            self.assertTrue(any('Failed to find' in item.get('context','') for item in report['evidence']))
            self.assertNotIn('not an owned',json.dumps(report))
