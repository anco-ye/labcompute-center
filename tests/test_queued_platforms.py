import json, sqlite3, unittest
from copy import deepcopy
import queued_platforms

class QueuedPlatformsTest(unittest.TestCase):
    def row(self, **changes):
        spec=dict(kind='comsol',entry='models/input.mph',os='windows',runtime='comsol',
                  args=[],requirements=None,cores=8,memory_gb=60.5,commit_gb=60.5,
                  disk_gb=1024,target='auto',profile='input-identity',resource_signature='unchanged',
                  restart_policy='from_start',unit_id='job',tenant_id='owner')
        row=dict(id='job',status='queued',spec=json.dumps(spec),node=None,lease=None,
                 started=None,compute_started=0,finished=None,result=None,execution_stopped=0,control='run',updated=1)
        return dict(row, **changes)
    def test_only_execution_platform_and_profile_change(self):
        row=self.row();original=json.loads(row['spec'])
        revised=queued_platforms.linux_comsol_spec(row,minimum_version='6.4',target='dell-r940')
        changed={k for k in original.keys() | revised.keys() if original.get(k)!=revised.get(k)}
        self.assertEqual(changed,{'os','runtime','platforms','min_application_version','selected_platform','portable_profile','profile','target'})
        self.assertEqual(revised['os'],'linux');self.assertEqual(revised['runtime'],'comsol-linux')
        self.assertEqual(revised['resource_signature'],original['resource_signature'])
        self.assertNotEqual(revised['profile'],original['profile'])
        self.assertEqual(row['spec'],json.dumps(original))
    def test_refuses_active_previously_started_and_cancelled_work(self):
        for changes in [dict(status='leased'),dict(node='win3'),dict(lease='lease'),dict(started=1),
                        dict(compute_started=1),dict(finished=1),dict(result='result.zip'),
                        dict(execution_stopped=1),dict(control='cancel')]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                queued_platforms.linux_comsol_spec(self.row(**changes),minimum_version='6.4')
    def test_rejects_other_apps_and_uninspected_runtime_arguments(self):
        for changes in [dict(kind='python'),dict(entry='script.class'),dict(args=['--flag']),
                        dict(requirements='req.txt'),dict(runtime_revision='ran-before')]:
            row=self.row();s=json.loads(row['spec']);s.update(changes);row['spec']=json.dumps(s)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                queued_platforms.linux_comsol_spec(row,minimum_version='6.4')
    def test_compare_and_swap_preserves_other_jobs_and_no_duplicate_submission(self):
        c=sqlite3.connect(':memory:');row=self.row()
        c.execute('CREATE TABLE jobs ('+','.join(k for k in row)+')')
        for j in [row,self.row(id='healthy',status='running',node='a800-ssh',lease='original',started=1)]:
            c.execute('INSERT INTO jobs VALUES ('+','.join('?' for _ in row)+')',tuple(j.values()))
        c.commit();before=c.execute("SELECT * FROM jobs WHERE id='healthy'").fetchone()
        with self.assertRaises(ValueError):queued_platforms.apply(c,'job',expected_spec=row['spec'],minimum_version='6.4')
        c.execute('BEGIN IMMEDIATE')
        with self.assertRaises(ValueError):queued_platforms.apply(c,'job',expected_spec='stale',minimum_version='6.4')
        queued_platforms.apply(c,'job',expected_spec=row['spec'],minimum_version='6.4',target='dell-r940');c.commit()
        self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
        self.assertEqual(c.execute("SELECT * FROM jobs WHERE id='healthy'").fetchone(),before)
        with self.assertRaises(ValueError):queued_platforms.linux_comsol_spec(row,minimum_version='latest')
        with self.assertRaises(ValueError):queued_platforms.linux_comsol_spec(row,minimum_version='6.4',target='../node')
