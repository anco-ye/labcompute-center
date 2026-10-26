import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('disk_rpc',Path(os.environ.get('LINUX_RPC_PATH',str(Path(__file__).parents[1]/'external/linux_rpc.py'))))
rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)

class DiskObservationTests(unittest.TestCase):
    def test_complete_sample_still_enforces_budget(self):
        with patch.object(rpc.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'200\t/job\n',stderr=b'')):
            value=rpc.observe_disk_bytes('/job')
        self.assertTrue(rpc.disk_limit_exceeded(value,199))
        self.assertFalse(rpc.disk_limit_exceeded(value,200))

    def test_concurrent_removal_is_partial_not_application_failure(self):
        with patch.object(rpc.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'200\t/job\n',stderr=b'du: cannot access temporary: No such file or directory')):
            value=rpc.observe_disk_bytes('/job')
        self.assertEqual(value['state'],'partial')
        self.assertIn('No such file',value['error'])
        self.assertFalse(rpc.disk_limit_exceeded(value,1))

    def test_timeout_is_visible_without_terminating_task(self):
        with patch.object(rpc.subprocess,'run',side_effect=subprocess.TimeoutExpired('du',8)):
            value=rpc.observe_disk_bytes('/job')
        self.assertEqual(value['state'],'unavailable')
        self.assertIn('TimeoutExpired',value['error'])
        self.assertFalse(rpc.disk_limit_exceeded(value,1))

    def test_bad_or_empty_output_is_not_a_size(self):
        for output in [b'',b'invalid',b'-1\t/job']:
            with patch.object(rpc.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=output,stderr=b'')):
                value=rpc.observe_disk_bytes('/job')
            self.assertEqual(value['state'],'unavailable')

    def test_later_valid_sample_recovers(self):
        with patch.object(rpc.subprocess,'run',side_effect=[subprocess.TimeoutExpired('du',8),SimpleNamespace(returncode=0,stdout=b'5\t/job',stderr=b'')]):
            self.assertEqual(rpc.observe_disk_bytes('/job')['state'],'unavailable')
            self.assertEqual(rpc.observe_disk_bytes('/job')['state'],'complete')
