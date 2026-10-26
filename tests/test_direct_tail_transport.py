import io
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock,patch
from direct_transport import NodeTransport

class TailTransportTests(unittest.TestCase):
    def make(self,op):
        return NodeTransport({'win4':{'url':'https://192.0.2.31:8446','tail':'100.100.0.4'}},op)
    def test_verified_tail_fallback_is_sticky_and_bounds_metadata_primary(self):
        op=Mock(side_effect=[TimeoutError(),io.BytesIO(b'ok'),io.BytesIO(b'next')]);t=self.make(op)
        self.assertEqual(t('https://192.0.2.31:8446/meta/job?ticket=x',10).read(),b'ok')
        self.assertEqual(op.call_args_list[0].kwargs['timeout'],3)
        self.assertEqual(op.call_args_list[1].args[0],'https://100.100.0.4:8446/meta/job?ticket=x')
        t('https://192.0.2.31:8446/result/job?ticket=y',60)
        self.assertEqual(op.call_args_list[2].kwargs['timeout'],60)
        self.assertEqual(t.endpoint(t.nodes['win4']),'https://100.100.0.4:8446')
    def test_head_range_and_conditionals_survive_rewrite(self):
        op=Mock(side_effect=[OSError(),io.BytesIO()]);t=self.make(op)
        req=urllib.request.Request('https://192.0.2.31:8446/result/job?ticket=x',method='HEAD',headers={'Range':'bytes=10-20','If-Range':'identity','Host':'old'})
        t(req);r=op.call_args.args[0]
        self.assertEqual(r.get_method(),'HEAD');self.assertEqual(r.get_header('Range'),'bytes=10-20')
        self.assertEqual(r.get_header('If-range'),'identity');self.assertIsNone(r.get_header('Host'))
    def test_http_denials_never_trigger_fallback(self):
        for status in (401,403,404,412,416,503):
            op=Mock(side_effect=urllib.error.HTTPError('x',status,'denied',{},None));t=self.make(op)
            with self.assertRaises(urllib.error.HTTPError):t('https://192.0.2.31:8446/meta/job')
            self.assertEqual(op.call_count,1)
    def test_only_explicit_tailnet_literal_accepted(self):
        for tail in ('evil.example','127.0.0.1','100.100.0.4:99','100.100.0.4/path','192.0.2.36',None):
            op=Mock(side_effect=OSError());t=self.make(op);t.nodes['win4']['tail']=tail
            with self.assertRaises(OSError):t('https://192.0.2.31:8446/meta/job')
            self.assertEqual(op.call_count,1)
    def test_unknown_host_cannot_select_identity(self):
        t=self.make(Mock())
        for url in ('https://100.100.0.5:8446/meta/job','https://100.100.0.4:8446.evil/meta/job'):
            with self.assertRaises(ValueError):t(url)
    def test_failed_stream_is_not_replayed(self):
        response=Mock();response.read.side_effect=OSError('disconnected');op=Mock(return_value=response);t=self.make(op)
        stream=t('https://192.0.2.31:8446/result/job')
        with self.assertRaises(OSError):stream.read()
        self.assertEqual(op.call_count,1)
    def test_both_network_failures_propagate(self):
        op=Mock(side_effect=OSError('offline'));t=self.make(op)
        with self.assertRaises(OSError):t('https://192.0.2.31:8446/meta/job')
        self.assertEqual(op.call_count,2)
    def test_expiry_reprobes_primary(self):
        op=Mock(side_effect=[OSError(),io.BytesIO(),io.BytesIO()]);t=self.make(op)
        with patch('direct_transport.time.monotonic',return_value=0):t('https://192.0.2.31:8446/meta/job')
        with patch('direct_transport.time.monotonic',return_value=301):t('https://192.0.2.31:8446/meta/job')
        self.assertIn('192.0.2.31',op.call_args.args[0])
