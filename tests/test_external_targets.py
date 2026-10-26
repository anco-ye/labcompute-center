import json
import tempfile
import unittest
from pathlib import Path
import external_targets

class ExternalTargetsTest(unittest.TestCase):
    def test_existing_bridge_lock_survives_web_restart_without_new_owner(self):
        import os
        if os.name=='nt':self.skipTest('POSIX lock holder; Windows verified natively')
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            lock=Path(tmp)/'bridge.lock';lock.write_bytes(b'0')
            self.assertFalse(external_targets.bridge_running(lock))
            with lock.open('r+b') as stream:
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertTrue(external_targets.bridge_running(lock))
            self.assertFalse(external_targets.bridge_running(lock))
    def test_never_advertise_machine_or_enable_dispatch(self):
        target=dict(id='a800',enabled=True,limits=dict(cpu_threads=8,memory_gb=16,disk_gb=20),password='secret',host='private',gpu_allowed=True)
        s=external_targets.summary(target)
        self.assertFalse(s['enabled']);self.assertFalse(s['dispatch_ready']);self.assertEqual(s['gpus'],0)
        self.assertNotIn('secret',json.dumps(s));self.assertNotIn('host',s)
        self.assertEqual(s['limits']['cpu_threads'],8)
        self.assertTrue(any('后端待实现' in b for b in s['blockers']))
    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'targets.json';self.assertEqual(external_targets.inventory(path),[])
            path.write_text(json.dumps(dict(targets=[dict(id='test',enabled=False,limits=dict(cpu_threads=True,memory_gb=0,disk_gb=1))])))
            self.assertTrue(any('有界资源' in b for b in external_targets.inventory(path)[0]['blockers']))

from test_center import CenterTest
class ExternalTargetsApiTest(unittest.TestCase):
    package=CenterTest.package
    submit=CenterTest.submit
    heartbeat=CenterTest.heartbeat
    claim=CenterTest.claim
    setUp=CenterTest.setUp
    tearDown=CenterTest.tearDown
    def test_admin_inventory_does_not_register_worker(self):
        path=self.root/'external-targets.json'
        path.write_text(json.dumps(dict(targets=[dict(id='a800',enabled=False,limits=dict(cpu_threads=8,memory_gb=16,disk_gb=20))])))
        response=self.client.get('/api/external-targets')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json['targets'][0]['dispatch_ready'])
        self.assertEqual([n['id'] for n in self.client.get('/api/state').json['nodes']],['win2'])
        from unittest.mock import patch
        with patch('access_control.is_admin',return_value=False):
            self.assertEqual(self.client.get('/api/external-targets').status_code,403)
        self.assertEqual(self.client.post('/api/external-targets',headers=self.csrf).status_code,405)

    def test_external_pid_observation_is_not_windows_storage_layout(self):
        import time
        self.submit(os='linux')
        self.heartbeat(os='linux',storage_observation=dict(at=time.time(),source='ssh-push',managed=[]))
        self.assertEqual(self.client.get('/api/state').status_code,200)
        self.assertIsNotNone(self.claim())

    def test_invalid_external_registry_never_stops_web_startup(self):
        from unittest.mock import patch
        path=self.root/'external-targets.json';path.write_bytes(b'bad json')
        with patch('logging.Logger.exception'):
            self.assertEqual(external_targets.launch(path,self.root),[])
