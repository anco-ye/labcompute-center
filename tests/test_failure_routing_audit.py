from test_center import finish_clean
"""Independent checks for evidence-based ownership of platform task failures."""
import json
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import task_health
from server import create_app


class FailureRoutingAudit(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE task_issues(node TEXT,task TEXT,state TEXT,first_seen REAL,last_seen REAL,resolved_at REAL,data TEXT,PRIMARY KEY(node,task))')

    def tearDown(self):self.db.close()

    def report(self):
        row=dict(self.db.execute('SELECT * FROM task_issues').fetchone())
        row['data']=json.loads(row['data'])
        return row

    def test_controller_wait_recovery_closes_only_control_fault(self):
        failed=dict(id='controller:next',kind='external_controller',lifecycle='failed',process_state='stopped',failure_detail='批次未确认完成且控制器已退出；中心保留现场调查，不重复派发。')
        task_health.reconcile(self.db,'win4',dict(simulations=[failed]),100)
        wait=dict(id=failed['id'],kind='external_controller',lifecycle='queued',process_state='running',
                  controller_phase='waiting_for_prior_win4_network_batch',process_refs=[dict(pid=42,created=105)],state_observed_at=109,state_age_seconds=1,
                  status_detail='waiting for original predecessor')
        task_health.reconcile(self.db,'win4',dict(simulations=[dict(wait,process_refs=[])]),110)
        self.assertEqual(self.report()['state'],'open')
        stale={};task_health.merge_controller_observations(stale,dict(received_at=110,items=[wait]),400)
        task_health.reconcile(self.db,'win4',stale,400)
        self.assertEqual(self.report()['state'],'open')
        task_health.reconcile(self.db,'win4',dict(simulations=[wait]),410)
        self.assertEqual(self.report()['state'],'resolved')
        self.assertEqual(self.report()['data']['resolution']['process_refs'],wait['process_refs'])
        self.assertEqual(wait['lifecycle'],'queued')
        self.assertIn('error_report',self.report()['data'])

    def test_condition_failure_survives_generic_controller_exit_then_wait(self):
        failed=dict(id='controller:next',kind='external_controller',lifecycle='failed',process_state='stopped',
                    controller_phase='condition_failed_or_incomplete',
                    failure_detail='外部控制器报告条件失败或不完整；保留结果，由中心核查退出证据。')
        task_health.reconcile(self.db,'win4',dict(simulations=[failed]),100)
        generic=dict(failed,controller_phase='waiting_for_prior_win4_network_batch',
                     failure_detail='批次未确认完成且控制器已退出；中心保留现场调查，不重复派发。')
        task_health.reconcile(self.db,'win4',dict(simulations=[generic]),105)
        wait=dict(id=failed['id'],kind='external_controller',lifecycle='queued',process_state='running',
                  controller_phase='waiting_for_prior_win4_network_batch',process_refs=[dict(pid=42,created=107)],
                  state_observed_at=109,state_age_seconds=1)
        task_health.reconcile(self.db,'win4',dict(simulations=[wait]),110)
        self.assertEqual(self.report()['state'],'open')
        self.assertEqual(self.report()['data']['error_report']['evidence']['controller_phase'],'condition_failed_or_incomplete')

    def test_wait_is_not_recovery_for_input_or_resource_failure(self):
        for code in ('missing_input','resource_limit','condition_incomplete'):
            self.db.execute('DELETE FROM task_issues')
            failed=dict(id='controller:next',kind='external_controller',lifecycle='failed',failure_code=code,failure_detail='外部控制器报告条件失败或不完整；保留结果，由中心核查退出证据。')
            task_health.reconcile(self.db,'win4',dict(simulations=[failed]),100)
            wait=dict(id=failed['id'],kind='external_controller',lifecycle='queued',process_state='running',
                      controller_phase='waiting_for_memory_before_condition',process_refs=[dict(pid=42,created=105)],state_observed_at=109,state_age_seconds=1)
            task_health.reconcile(self.db,'win4',dict(simulations=[wait]),110)
            self.assertEqual(self.report()['state'],'open')

    def test_written_result_with_live_writer_stays_running_until_observed_exit(self):
        sim=dict(id='network',kind='network',name='network',lifecycle='succeeded',status='计算完成',process_state='running',completed=5000,total=5000)
        task_health.reconcile(self.db,'win2',dict(simulations=[sim]),100)
        self.assertEqual(sim['lifecycle'],'running')
        self.assertTrue(sim['result_written'])
        self.assertIn('收尾',self.report()['data']['detail'])
        self.assertEqual(self.report()['state'],'open')
        done=dict(sim,lifecycle='succeeded',status='计算完成',process_state='stopped',progress_state='fresh')
        task_health.reconcile(self.db,'win2',dict(simulations=[done]),110)
        self.assertEqual(done['lifecycle'],'succeeded')
        self.assertEqual(self.report()['state'],'resolved')

    def test_written_result_without_process_evidence_is_not_success(self):
        sim=dict(id='network',kind='network',lifecycle='succeeded',process_state='unknown')
        task_health.reconcile(self.db,'win2',dict(simulations=[sim]),100)
        self.assertEqual(sim['lifecycle'],'unknown')
        self.assertEqual(self.report()['data']['code'],'monitor_unavailable')

    def test_unexplained_exit_remains_center_responsibility(self):
        for extra in ({},{'failure_code':'unknown'},{'failure_code':'unrecognized_code'},
                      {'failure_detail':'missing_input: missing data.csv'},
                      {'failure_detail':'进程退出，退出原因未记录'}):
            with self.subTest(extra=extra):
                diagnosis=task_health.diagnose(dict(lifecycle='failed',**extra))
                self.assertEqual(diagnosis['owner'],'center')

    def test_platform_and_resource_failures_are_not_sent_to_publisher(self):
        for code in ('memory_pressure','out_of_memory','resource_exhausted','worker_lost','network_error','disk_full'):
            with self.subTest(code=code):
                diagnosis=task_health.diagnose(dict(lifecycle='failed',failure_code=code,
                    failure_detail='保留已有结果'))
                self.assertEqual(diagnosis['owner'],'center')

    def test_only_confirmed_task_error_codes_route_to_publisher(self):
        for code in ('missing_input','invalid_input','model_error'):
            with self.subTest(code=code):
                diagnosis=task_health.diagnose(dict(lifecycle='failed',failure_code=code,
                    failure_detail='任务输入错误',failure_evidence={'path':'inputs/data.csv'}))
                self.assertEqual(diagnosis['owner'],'publisher')
                self.assertTrue(diagnosis['detail'])
                self.assertTrue(diagnosis['next_action'])

    def test_unreadable_progress_without_confirmed_cause_stays_with_center(self):
        diagnosis=task_health.diagnose(dict(lifecycle='running',process_state='running',progress_state='unreadable'))
        self.assertEqual(diagnosis['owner'],'center')
        self.assertNotIn('由发布者',diagnosis['next_action'])

    def test_repeated_failure_updates_one_instance_and_recovery_closes_it(self):
        for now,path in ((100,'C:\\Research\\Progress.json'),(110,'c:/research/progress.json')):
            task_health.reconcile(self.db,'win2',dict(simulations=[dict(id=path,name='task',
                lifecycle='failed',failure_code='missing_input',failure_detail='data.csv不存在')]),now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM task_issues').fetchone()[0],1)
        row=self.report()
        self.assertEqual(row['state'],'open')
        self.assertEqual(row['first_seen'],100)
        self.assertEqual(row['last_seen'],110)
        self.assertEqual(row['data']['owner'],'publisher')
        task_health.reconcile(self.db,'win2',dict(simulations=[]),120)
        self.assertEqual(self.report()['state'],'open')
        task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='c:/research/progress.json',
            lifecycle='running',process_state='running',progress_state='initializing')]),125)
        self.assertEqual(self.report()['state'],'open','Starting a process is not evidence that its prior failure is fixed')
        task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='c:/research/progress.json',
            lifecycle='running',process_state='running',progress_state='fresh')]),130)
        self.assertEqual(self.report()['state'],'resolved')
        self.assertEqual(self.report()['resolved_at'],130)

    def test_structured_report_preserves_evidence_and_excludes_unrelated_fields(self):
        sim=dict(id='C:\\Research\\Progress.json',name='input check',lifecycle='failed',
                 process_state='stopped',failure_code='missing_input',failure_detail='inputs/data.csv不存在',
                 failure_evidence=dict(exception_type='FileNotFoundError',path='inputs/data.csv'),
                 result_url='/api/jobs/example/result',batch_id='batch',pid=123,
                 secret_token='must-not-export',untrusted_command='do not execute this')
        task_health.reconcile(self.db,'win2',dict(simulations=[sim]),100)
        report=self.report()['data']['error_report']
        self.assertEqual(report['schema_version'],1)
        self.assertEqual(report['node'],'win2')
        self.assertEqual(report['task'],'c:/research/progress.json')
        self.assertEqual(report['classification_basis'],'structured_failure_code')
        self.assertEqual(report['evidence']['failure_code'],'missing_input')
        self.assertEqual(report['evidence']['failure_detail'],'inputs/data.csv不存在')
        self.assertEqual(report['evidence']['failure_evidence']['path'],'inputs/data.csv')
        self.assertEqual(report['evidence']['result_url'],'/api/jobs/example/result')
        self.assertEqual(report['delivery'],'publisher_inbox')
        self.assertTrue(report['next_action']);self.assertTrue(report['acceptance'])
        self.assertNotIn('must-not-export',json.dumps(report))
        self.assertNotIn('do not execute this',json.dumps(report))
        task_health.reconcile(self.db,'win2',dict(simulations=[sim]),110)
        self.assertEqual(self.report()['data']['error_report']['report_id'],report['report_id'])

    def test_initialization_only_resolves_monitoring_outage(self):
        for original,expected in ((dict(lifecycle='unknown'),'resolved'),
                (dict(lifecycle='failed'),'open'),
                (dict(lifecycle='running',progress_state='stale',source_age_seconds=600),'open')):
            with self.subTest(original=original):
                self.db.execute('DELETE FROM task_issues')
                task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='task',**original)]),100)
                task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='task',lifecycle='running',
                    process_state='running',progress_state='initializing')]),110)
                self.assertEqual(self.report()['state'],expected)

    def test_confirmed_failure_survives_unknown_stale_and_initializing_until_recovery(self):
        for recovery in (dict(lifecycle='running',process_state='running',progress_state='fresh'),
                         dict(lifecycle='succeeded'),dict(lifecycle='cancelled')):
            with self.subTest(recovery=recovery):
                self.db.execute('DELETE FROM task_issues')
                failed=dict(id='task',name='input task',lifecycle='failed',failure_code='missing_input',
                            failure_detail='inputs/data.csv不存在',
                            failure_evidence=dict(exception_type='FileNotFoundError',path='inputs/data.csv'))
                task_health.reconcile(self.db,'win2',dict(simulations=[failed]),100)
                original=self.report()['data']['error_report']
                observations=[dict(lifecycle='unknown'),
                              dict(lifecycle='running',process_state='running',progress_state='stale',source_age_seconds=600),
                              dict(lifecycle='running',process_state='running',progress_state='unreadable'),
                              dict(lifecycle='running',process_state='running',progress_state='finalizing')]
                for now,observation in enumerate(observations,110):
                    sim=dict(id='task',**observation)
                    task_health.reconcile(self.db,'win2',dict(simulations=[sim]),now)
                    record=self.report()
                    self.assertEqual(record['state'],'open')
                    self.assertEqual(record['first_seen'],100)
                    self.assertEqual(record['last_seen'],now)
                    self.assertEqual(record['data']['owner'],'publisher')
                    self.assertEqual(record['data']['code'],'missing_input')
                    self.assertEqual(record['data']['severity'],'error')
                    self.assertEqual(record['data']['error_report'],original)
                    self.assertEqual(record['data']['latest_observation']['at'],now)
                    self.assertEqual(sim['health']['unresolved_failure'],'inputs/data.csv不存在')
                task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='task',lifecycle='running',
                    process_state='running',progress_state='initializing')]),120)
                self.assertEqual(self.report()['state'],'open')
                self.assertEqual(self.report()['data']['error_report'],original)
                task_health.reconcile(self.db,'win2',dict(simulations=[dict(id='task',**recovery)]),130)
                self.assertEqual(self.report()['state'],'resolved')
                self.assertEqual(self.report()['resolved_at'],130)


class ReportAPIAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        cfg=dict(data_dir=str(self.root/'data'),session_secret='audit-session',password_hash='unused',
                 secure_cookie=False,nodes=[dict(id='win2',label='Win2',enabled=True,
                 token_hash=hashlib.sha256(b'audit-token').hexdigest())])
        path=self.root/'config.json';path.write_text(json.dumps(cfg))
        self.app=create_app(path);self.client=self.app.test_client()
        with self.client.session_transaction() as session:session.update(signed_in=True,csrf='csrf')
        self.auth={'Authorization':'Bearer audit-token'};self.csrf={'X-CSRF-Token':'csrf'}

    def tearDown(self):self.tmp.cleanup()

    def heartbeat(self,simulations=None):
        response=self.client.post('/agent/win2/heartbeat',json=dict(telemetry=dict(
            cpu_threads=8,memory_total_gb=16,memory_available_gb=12,reserve_gb=4,
            runtimes={'python':{'kind':'python'}},simulations=simulations or [])),headers=self.auth)
        self.assertEqual(response.status_code,200)

    def result(self,code=None):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('exit.json',json.dumps(dict(failure_code=code,detail='inputs/data.csv不存在',
                failure_evidence=dict(exception_type='FileNotFoundError',path='inputs/data.csv'))))
        return buffer.getvalue()

    def submit_claim(self):
        self.client.get('/api/submission-document/download')
        self.heartbeat()
        package=io.BytesIO()
        with zipfile.ZipFile(package,'w') as archive:
            archive.writestr('main.py','print(42)')
            archive.writestr('manifest.json',json.dumps(dict(schema_version=1,tasks=[dict(
                entry='main.py',kind='python',memory_gb=1,cores=1)])))
        package.seek(0)
        response=self.client.post('/api/jobs',data={'package':(package,'task.zip')},headers=self.csrf)
        self.assertEqual(response.status_code,201,response.json)
        job=self.client.post('/agent/win2/claim',headers=self.auth).json['job']
        self.assertIsNotNone(job)
        return job

    def finish(self,job,status='failed',code='missing_input'):
        response=finish_clean(self.client,'/agent/win2/jobs/'+job['id']+'/finish',data=self.result(code),
            headers=dict(self.auth,**{'X-Lease':job['lease'],'X-Result-Status':status}))
        self.assertEqual(response.status_code,200)

    def reports(self,owner='publisher'):
        response=self.client.get('/api/issues/reports?owner='+owner)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.mimetype,'application/json')
        return response.json['reports']

    def test_report_export_is_authenticated_and_owner_filtered(self):
        self.heartbeat([dict(id='task1',lifecycle='failed',failure_code='missing_input'),
                        dict(id='task2',lifecycle='failed',failure_code='unknown')])
        publisher=self.reports();center=self.reports('center')
        self.assertEqual(len(publisher),1);self.assertEqual(publisher[0]['task'],'task1')
        self.assertEqual(len(center),1);self.assertEqual(center[0]['task'],'task2')
        self.assertEqual(len(self.reports('all')),2)
        self.assertEqual(len(self.client.get('/api/issues?owner=publisher').json['issues']),1)
        self.assertEqual(self.client.get('/api/issues/reports?owner=invalid').status_code,400)
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.get('/api/issues/reports').status_code,401)
        self.assertEqual(anonymous.get('/api/issues').status_code,401)

    def test_uploaded_job_finish_produces_report_with_result_evidence(self):
        job=self.submit_claim();self.finish(job)
        reports=self.reports()
        self.assertEqual(len(reports),1)
        report=reports[0]
        self.assertEqual(report['task'],'job:'+job['id'])
        self.assertEqual(report['evidence']['failure_code'],'missing_input')
        self.assertEqual(report['evidence']['failure_evidence']['path'],'inputs/data.csv')
        self.assertEqual(report['evidence']['result_url'],'/api/jobs/'+job['id']+'/result')
        self.assertTrue(report['evidence']['batch_id'])
        self.finish(job)
        self.assertEqual(len(self.reports()),1)

    def test_unknown_uploaded_failure_is_not_exported_to_publisher(self):
        job=self.submit_claim();self.finish(job,code='unknown_runtime_error')
        self.assertEqual(self.reports(),[])
        self.assertEqual(len(self.reports('center')),1)

    def test_successful_retry_resolves_previous_attempt_report(self):
        job=self.submit_claim();self.finish(job)
        retry=self.client.post('/api/jobs/'+job['id']+'/retry',headers=self.csrf)
        self.assertEqual(retry.status_code,200)
        self.assertEqual(len(self.reports()),1,'Requeueing alone is not recovery evidence')
        next_job=self.client.post('/agent/win2/claim',headers=self.auth).json['job']
        self.assertEqual(next_job['id'],retry.json['id'])
        self.finish(next_job,status='succeeded',code=None)
        self.assertEqual(self.reports(),[])

    def test_result_transfer_failure_is_center_incident(self):
        job=self.submit_claim();base='/agent/win2/jobs/'+job['id']
        auth=dict(self.auth,**{'X-Lease':job['lease']})
        self.assertEqual(self.client.post(base+'/update',json={'status':'uploading'},headers=auth).status_code,200)
        self.assertEqual(self.client.post(base+'/result-error',json={'kind':'too_large'},headers=auth).status_code,200)
        self.assertEqual(self.reports(),[])
        self.assertEqual(len(self.reports('center')),1)


class MissingInputBoundaryAudit(unittest.TestCase):
    def execute(self,script):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir()
            entry=work/'main.py';entry.write_text(script)
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'python_entry.py'),str(entry)],
                cwd=work,env=dict(os.environ,LAB_JOB_DIR=str(root)),capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            report=root/'task-error.json'
            return json.loads(report.read_text()) if report.exists() else None

    def test_missing_task_local_file_is_confirmed_input_error(self):
        report=self.execute("from pathlib import Path\nPath('inputs/data.csv').read_text()\n")
        self.assertEqual(report['failure_code'],'missing_input')
        self.assertEqual(report['failure_evidence']['path'],'inputs/data.csv')
        self.assertEqual(report['failure_evidence']['exception_type'],'FileNotFoundError')

    def test_external_missing_file_remains_center_investigation(self):
        self.assertIsNone(self.execute("from pathlib import Path\nPath('../external_missing.csv').read_text()\n"))

    def test_missing_runtime_executable_is_not_task_input_error(self):
        self.assertIsNone(self.execute("import subprocess\nsubprocess.run(['definitely_absent_runtime_tool_8302374'])\n"))

    def test_exception_without_filename_is_not_confirmed_input_error(self):
        self.assertIsNone(self.execute("raise FileNotFoundError('unknown source')\n"))


if __name__=='__main__':unittest.main()
