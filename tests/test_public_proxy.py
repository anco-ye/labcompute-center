import unittest
from server import client_address

class PublicProxyTest(unittest.TestCase):
    def test_untrusted_client_cannot_spoof_rate_limit_address(self):
        self.assertEqual(client_address({'REMOTE_ADDR':'100.100.0.3','HTTP_X_FORWARDED_FOR':'8.8.8.8'},['127.0.0.1']),'100.100.0.3')
    def test_proxy_uses_last_address_not_client_supplied_prefix(self):
        self.assertEqual(client_address({'REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_FOR':'8.8.8.8, 203.0.113.4'},['127.0.0.1']),'203.0.113.4')
    def test_bad_or_missing_forwarded_address_retains_limit(self):
        for value in ['', 'bad', '8.8.8.8, bad']:
            self.assertEqual(client_address({'REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_FOR':value},['127.0.0.1']),'127.0.0.1')
