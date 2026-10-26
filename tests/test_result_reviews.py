"""The retired scientific-review API must never affect calculation lifecycle."""
import json,unittest
import test_center


class ResultReviewTest(unittest.TestCase):
    setUp=test_center.CenterTest.setUp
    tearDown=test_center.CenterTest.tearDown
    submit=test_center.CenterTest.submit
    package=test_center.CenterTest.package

    def test_legacy_config_cannot_enable_review_writes(self):
        jid=self.submit().json['ids'][0]
        for route in ['review','review-resolution']:
            response=self.client.post('/api/jobs/'+jid+'/'+route,json={'verdict':'passed'},headers=self.csrf)
            self.assertEqual(response.status_code,410)
        with self.app.center_db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM result_reviews').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'queued')

    def test_old_review_history_is_read_only_and_absent_from_execution_status(self):
        jid=self.submit().json['ids'][0];record=dict(verdict='failed',summary='Historical review')
        with self.app.center_db() as c:
            c.execute('INSERT INTO result_reviews VALUES(?,?,?,?)',(jid,'historical',json.dumps(record),1))
            c.execute('INSERT INTO task_issues VALUES(?,?,?,?,?,?,?)',('win2','review:'+jid,'open',1,1,None,json.dumps({'owner':'publisher'})))
        state=self.client.get('/api/state').json
        self.assertNotIn('result_review',next(j for j in state['jobs'] if j['id']==jid))
        self.assertEqual(state['issues'],[])
        self.assertEqual(self.client.get('/api/issues').json['issues'],[])
        self.assertEqual(self.client.get('/api/issues/reports').json['reports'],[])
        self.assertEqual(self.client.get('/api/jobs/'+jid+'/reviews').json['reviews'][0]['verdict'],'failed')
        with self.app.center_db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM result_reviews').fetchone()[0],1)
