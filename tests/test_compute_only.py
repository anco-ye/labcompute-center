import io,json,sys,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import inspect_zip,package_specs
from compute_only import review_stage
from test_center import CenterTest


class ComputeOnlyTest(unittest.TestCase):
    setUp=CenterTest.setUp
    tearDown=CenterTest.tearDown
    submit=CenterTest.submit
    package=CenterTest.package
    heartbeat=CenterTest.heartbeat
    claim=CenterTest.claim

    def test_claim_never_runs_a_retired_scientific_stage(self):
        jid=self.submit().json['ids'][0]
        with self.app.center_db() as c:
            spec=json.loads(c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0]);spec['stage']='independent_long_numeric_and_reference_review'
            c.execute('UPDATE jobs SET spec=? WHERE id=?',(json.dumps(spec),jid))
        self.heartbeat();self.assertIsNone(self.claim())
        with self.app.center_db() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'held')

    def test_old_review_cannot_be_retried(self):
        jid=self.submit().json['ids'][0]
        with self.app.center_db() as c:
            spec=json.loads(c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0]);spec['stage']='independent_existing_short_review';c.execute("UPDATE jobs SET spec=?,status='failed',finished=1 WHERE id=?",(json.dumps(spec),jid))
        self.assertEqual(self.client.post('/api/jobs/'+jid+'/retry',headers=self.csrf).status_code,410)
        with self.app.center_db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_only_selected_scientific_stages_are_refused(self):
        for stage in ['long_network','independent_existing_short_review']:
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as z:
                z.writestr('main.py','print(42)')
                z.writestr('task_table.json',json.dumps({'run':{'stage':stage},'unused':{'stage':'independent_long_numeric_and_reference_review'}}))
                z.writestr('manifest.json',json.dumps({'schema_version':1,'tasks':[{'id':'run','entry':'main.py','name':'Code review data calculation'}]}))
            path=self.root/'package.zip';path.write_bytes(buffer.getvalue())
            if stage=='long_network':
                self.assertEqual(package_specs(path,inspect_zip(path))[0]['task_key'],'run')
                self.assertEqual(review_stage(path,{'task_key':'run','args':['--task','unused']}),'independent_long_numeric_and_reference_review')
            else:
                with self.assertRaisesRegex(ValueError,'Scientific review is disabled'):package_specs(path,inspect_zip(path))

    def test_ordinary_calculation_still_leases_without_any_review(self):
        jid=self.submit().json['ids'][0];self.heartbeat();self.assertEqual(self.claim()['id'],jid)


if __name__=='__main__':unittest.main()


class MaintenanceRetirementTest(unittest.TestCase):
    def test_retired_recovery_stops_before_reading_or_submitting(self):
        import importlib.util
        for name in ['repair_four_short_reviews','restore_blocked_descendants']:
            path=Path(__file__).resolve().parents[1]/'maintenance/short_review_compat'/(name+'.py')
            spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with self.assertRaisesRegex(RuntimeError,'Retired'):
                module.run('/nonexistent') if name.startswith('repair') else module.run()
