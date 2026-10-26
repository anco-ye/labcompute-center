import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client_session import CenterClient
from receive_error_reports import receive
from server import create_app
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


class ClientSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = dict(data_dir=str(self.root/'data'), session_secret='test-secret',
                   password_hash=generate_password_hash('test'), secure_cookie=True, nodes=[])
        (self.root/'server.json').write_text(json.dumps(cfg))
        self.app = create_app(self.root/'server.json')
        self.logins = 0
        from flask import request
        @self.app.before_request
        def count():
            if request.path == '/login' and request.method == 'POST':
                self.logins += 1
        cert, key = str(self.root/'cert.pem'), str(self.root/'key.pem')
        cnf = self.root/'openssl.cnf'
        cnf.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n'
                       '[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n'
                       'basicConstraints=critical,CA:TRUE\n')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-keyout',key,'-out',cert,'-config',str(cnf)], check=True,
                       capture_output=True)
        self.server = make_server('127.0.0.1', 0, self.app, ssl_context=(cert, key),
                                  request_handler=QuietHandler, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = self.root/'publisher.json'
        self.config.write_text(json.dumps(dict(server=f'https://localhost:{self.server.server_port}',
            ca_file=cert, username='lab', password='test')))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(timeout=5); self.server.server_close(); self.tmp.cleanup()
        self.assertFalse(self.thread.is_alive(), "local test server failed to stop")

    def test_repeated_pollers_and_new_clients_share_one_login(self):
        client = CenterClient(self.config)
        for _ in range(12):
            self.assertEqual(receive(self.config, self.root/'inbox', client=client), 0)
            self.assertIn('nodes', CenterClient(self.config).state())
        self.assertEqual(self.logins, 1)
        self.assertEqual(client.cache.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test', client.cache.read_text())

    def test_expired_session_read_recovers_once_and_csrf_is_current(self):
        client = CenterClient(self.config)
        old = client.csrf
        self.app.secret_key = 'rotated-test-secret'
        self.assertIn('nodes', client.state())
        self.assertEqual(self.logins, 2)
        self.assertNotEqual(client.csrf, old)
        client.get('/api/submission-document/download')
        self.assertIn('id', client.post('/api/uploads', {'name':'test.zip'}))
        self.app.secret_key = 'rotated-again'
        self.assertIn('nodes', client.state())
        self.assertEqual(self.logins, 3)

    def test_limit_cools_down_without_repeating_login_post(self):
        client = CenterClient(self.config)
        self.app.secret_key = 'expired-secret'
        original = client.opener.open
        attempts = []
        def limited(url, data=None, **kwargs):
            if str(url).endswith('/login') and data is not None:
                attempts.append(1)
                raise urllib.error.HTTPError(url, 403, 'Forbidden', {},
                    io.BytesIO(json.dumps({'error':'尝试次数过多，请 5 分钟后重试'}).encode()))
            return original(url, data, **kwargs)
        with patch.object(client.opener, 'open', side_effect=limited):
            with self.assertRaisesRegex(RuntimeError, '尝试次数过多'):
                client.authenticate()
            with self.assertRaisesRegex(RuntimeError, '冷却中'):
                client.authenticate()
        self.assertEqual(len(attempts), 1)

    def test_mutation_failure_is_not_replayed(self):
        client = CenterClient(self.config)
        self.app.secret_key = 'expired-secret'
        with self.assertRaises(urllib.error.HTTPError):
            client.post('/logout')
        self.assertEqual(self.logins, 1)


if __name__ == '__main__':
    unittest.main()
