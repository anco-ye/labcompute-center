import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from result_transfer import download


class Response(io.BytesIO):
    def __init__(self, body, headers, status=200):
        super().__init__(body)
        self.headers = headers
        self.status = status


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.destination = Path(self.tmp.name) / 'result.zip'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('output.txt', 'verified output' * 1000)
        self.body = stream.getvalue()
        self.etag = '"snapshot-v1"'
        self.ranges = []
        self.truncate = 0
        self.ignore_range = False
        self.client = SimpleNamespace(base='https://center', opener=SimpleNamespace(open=self.open))

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, request, **kwargs):
        if request.get_method() == 'HEAD':
            return Response(b'', {'Content-Length': str(len(self.body)), 'ETag': self.etag})
        start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.get_header('Range')).groups())
        self.ranges.append((start, end))
        self.assertEqual(request.get_header('If-range'), self.etag)
        body = self.body[start:end+1]
        if self.truncate:
            self.truncate -= 1
            body = body[:123]
        return Response(body, {'Content-Range': f'bytes {start}-{end}/{len(self.body)}',
                               'ETag': self.etag}, 200 if self.ignore_range else 206)

    @patch('result_transfer.time.sleep')
    def test_truncated_response_resumes_at_exact_byte(self, sleep):
        self.truncate = 1
        download(self.client, '/result', self.destination, chunk_bytes=1024)
        self.assertEqual(self.ranges[1][0], 123)
        self.assertEqual(self.destination.read_bytes(), self.body)
        self.assertFalse(self.destination.with_suffix('.partial').exists())

    @patch('result_transfer.time.sleep')
    def test_retry_limit_retains_verified_partial_for_next_run(self, sleep):
        self.truncate = 20
        with self.assertRaisesRegex(OSError, 'retry limit'):
            download(self.client, '/result', self.destination, chunk_bytes=1024, retries=1)
        length = self.destination.with_suffix('.partial').stat().st_size
        self.assertGreater(length, 0)
        self.truncate = 0
        self.ranges.clear()
        download(self.client, '/result', self.destination, chunk_bytes=1024)
        self.assertEqual(self.ranges[0][0], length)
        self.assertEqual(self.destination.read_bytes(), self.body)

    def test_unbound_partial_is_preserved_and_not_mixed(self):
        partial = self.destination.with_suffix('.partial')
        partial.write_bytes(b'unknown old bytes')
        download(self.client, '/result', self.destination, chunk_bytes=1024)
        self.assertEqual(self.ranges[0][0], 0)
        backups = list(self.destination.parent.glob('*.unverified-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'unknown old bytes')

    @patch('result_transfer.time.sleep')
    def test_changed_etag_starts_new_snapshot(self, sleep):
        self.truncate = 20
        with self.assertRaises(OSError):
            download(self.client, '/result', self.destination, chunk_bytes=1024, retries=0)
        self.etag = '"snapshot-v2"'
        self.truncate = 0
        self.ranges.clear()
        download(self.client, '/result', self.destination, chunk_bytes=1024)
        self.assertEqual(self.ranges[0][0], 0)
        self.assertTrue(list(self.destination.parent.glob('*.unverified-*')))

    def test_ignored_range_is_rejected_without_appending(self):
        self.ignore_range = True
        with self.assertRaisesRegex(ValueError, 'honor'):
            download(self.client, '/result', self.destination)
        self.assertFalse(self.destination.exists())

    def test_corrupt_complete_zip_is_not_published(self):
        self.body = b'invalid zip'
        with self.assertRaises(zipfile.BadZipFile):
            download(self.client, '/result', self.destination)
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.destination.with_suffix('.partial').exists())
