import json,sqlite3,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery_links,scheduler

class RecoveryTest(unittest.TestCase):
    def test_corrupt_registry_does_not_interrupt_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'registry.json'
            for text in ('{', 'null', '{"schema_version":1,"links":[{}]}'):
                p.write_text(text);self.assertEqual(recovery_links.load(p),[])

    def test_exact_replacement_closes_issue_only_after_result_and_never_hides_new_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'links.json'
            link=dict(node='n',task='c:/old',snapshot=dict(completed=20,total=100),entry_disabled=True,generation_stopped=True,retirement_evidence='backup',source_sha256='a'*64,batch_id='b',task_keys=['unit'])
            p.write_text(json.dumps(dict(schema_version=1,links=[link])))
            db=sqlite3.connect(':memory:');db.execute('CREATE TABLE task_issues(node,task,state,resolved_at,last_seen,data)');db.execute("INSERT INTO task_issues VALUES('n','c:/old','open',NULL,0,'{}')")
            sim=dict(id='c:/old',completed=20,total=100,process_state='stopped')
            nodes=[dict(id='n',last_seen=99,telemetry=dict(simulations=[sim]))]
            jobs=[dict(id='j',batch_id='b',created=30,status='running',has_result=False,spec=dict(task_key='unit'))]
            self.assertTrue(recovery_links.reconcile(db,p,jobs,nodes,100))
            self.assertEqual(db.execute('SELECT state FROM task_issues').fetchone()[0],'open')
            jobs[0].update(status='succeeded',has_result=False)
            recovery_links.reconcile(db,p,jobs,nodes,100)
            self.assertEqual(db.execute('SELECT state FROM task_issues').fetchone()[0],'open')
            jobs[0]['has_result']=True;recovery_links.reconcile(db,p,jobs,nodes,100)
            self.assertEqual(db.execute('SELECT state FROM task_issues').fetchone()[0],'resolved')
            sim['process_state']='running'
            self.assertFalse(recovery_links.reconcile(db,p,jobs,nodes,100))
            sim['process_state']='stopped';sim['completed']=21
            self.assertFalse(recovery_links.reconcile(db,p,jobs,nodes,100))

    def test_dynamic_shared_budget_preserves_peak_and_commit_reservations(self):
        t=dict(external_busy=True,cpu_threads=16,memory_total_gb=128,memory_available_gb=100,reserve_gb=8,memory_committed_gb=40,memory_commit_limit_gb=200,resource_observation=dict(at=100,state='normal',verified=True,external_cpu_cores=2),scheduling_policy=dict(external_mode='shared',shared_memory_gb=8,shared_memory_mode='available',shared_reserve_gb=32,shared_commit_reserve_gb=36))
        j=dict(id='j',spec=dict(cores=1,cpu_ids=[0],memory_gb=16,commit_gb=32))
        b=scheduler.budget(dict(telemetry=t),[j],100)
        self.assertEqual(b['memory_gb'],52);self.assertEqual(b['commit_gb'],92)
        t['scheduling_policy'].pop('shared_memory_mode');self.assertEqual(scheduler.budget(dict(telemetry=t),[j],100)['memory_gb'],0)
