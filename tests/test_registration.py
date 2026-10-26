"""Accounts are provisioned locally by the administrator, never anonymously."""
import json,unittest,concurrent.futures
from test_center import CenterTest
from werkzeug.security import check_password_hash
import access_control

class RegistrationTest(unittest.TestCase):
 setUp=CenterTest.setUp
 tearDown=CenterTest.tearDown
 def test_public_registration_is_retired_without_changing_existing_users(self):
  client=self.app.test_client()
  self.assertEqual(client.get('/register').status_code,410)
  client.get('/login')
  with client.session_transaction() as s:csrf=s['csrf']
  r=client.post('/register',data=dict(csrf=csrf,username='outside',password='secret'))
  self.assertEqual(r.status_code,410)
  self.assertFalse((self.root/'users.json').exists())
  self.assertEqual(client.get('/api/state').status_code,401)
  self.assertNotIn('href="/register"',client.get('/login').get_data(as_text=True))
 def test_admin_provisioned_account_can_login_and_remains_unprivileged(self):
  cfg=json.loads((self.root/'config.json').read_text());user=access_control.create_user(self.root/'config.json',cfg,'测试用户','test-password')
  client=self.app.test_client();client.get('/login')
  with client.session_transaction() as s:csrf=s['csrf']
  self.assertEqual(client.post('/login',data=dict(csrf=csrf,username='测试用户',password='test-password')).status_code,302)
  self.assertEqual(client.get('/api/state').json['account']['id'],user['id'])
  self.assertEqual(client.get('/operations').status_code,403)
  self.assertEqual(user['role'],'user');self.assertTrue(check_password_hash(user['password_hash'],'test-password'))
 def test_concurrent_local_provisioning_preserves_accounts(self):
  cfg=json.loads((self.root/'config.json').read_text())
  def create(name):return access_control.create_user(self.root/'config.json',cfg,name,'safe-test-password')
  with concurrent.futures.ThreadPoolExecutor() as pool:list(pool.map(create,['one','two','three']))
  rows=json.loads((self.root/'users.json').read_text());self.assertEqual({r['username'] for r in rows},{'lab','one','two','three'})
  with self.assertRaises(ValueError):create('ONE')
