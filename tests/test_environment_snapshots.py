import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import environment_snapshots as snapshots


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.nodes=[dict(id='win1',label='Win1',last_seen=100000,telemetry=dict(worker=True,runtimes={
            'python':dict(kind='python',version='3.11',revision='v1',status='ready',packages=['numpy==2'])}))]
        raw=json.dumps(dict(environments=[])).encode()
        (self.root/'运行环境快照.json').write_bytes(raw)
        with zipfile.ZipFile(self.root/'agent-preparation-kit.zip','w') as z:
            z.writestr('运行环境快照.json',raw)
            z.writestr('README.md','# Agent\n环境快照日期为 2026-10-16；旧说明。\n交付后询问是否跟进。\n')
            z.writestr('任务上传文档.md','计算提交规则')
            z.writestr('GPU示例.zip',b'unchanged-original-example')
        self.publisher=snapshots.Publisher(self.root,lambda:copy.deepcopy(self.nodes))
    def tearDown(self):self.tmp.cleanup()
    def read(self):return json.loads((self.root/'运行环境快照.json').read_text())
    def test_runtime_changes_update_package_and_date_without_touching_examples(self):
        self.assertTrue(self.publisher.sync(100000));first=self.read()
        self.assertEqual(first['environments'],snapshots.catalog(self.nodes,100000))
        self.nodes[0]['telemetry']['runtimes']['python']['version']='3.12'
        self.assertTrue(self.publisher.sync(100100));second=self.read()
        self.assertNotEqual(first['snapshot_at'],second['snapshot_at'])
        with zipfile.ZipFile(self.root/'agent-preparation-kit.zip') as z:
            self.assertEqual(json.loads(z.read('运行环境快照.json')),second)
            self.assertIn('自动同步',z.read('README.md').decode())
            self.assertIn('交付后询问是否跟进',z.read('README.md').decode())
            self.assertEqual(z.read('GPU示例.zip'),b'unchanged-original-example')
            self.assertEqual(z.read('任务上传文档.md').decode(),'计算提交规则')
    def test_online_flapping_does_not_rewrite_date_or_archive(self):
        self.publisher.sync(100000)
        original=(self.root/'agent-preparation-kit.zip').read_bytes()
        self.assertFalse(self.publisher.sync(100100))
        self.assertEqual(original,(self.root/'agent-preparation-kit.zip').read_bytes())
        restarted=snapshots.Publisher(self.root,lambda:self.nodes)
        self.assertFalse(restarted.sync(100200))
    def test_status_packages_and_added_node_trigger_updates(self):
        self.publisher.sync(100000)
        r=self.nodes[0]['telemetry']['runtimes']['python']
        r['status']='unverified';self.assertTrue(self.publisher.sync(100001))
        r['packages'].append('scipy==1');self.assertTrue(self.publisher.sync(100002))
        self.nodes.append(dict(id='win2',label='Win2',last_seen=100002,telemetry=self.nodes[0]['telemetry']))
        self.assertTrue(self.publisher.sync(100003));self.assertEqual(len(self.read()['environments']),2)
    def test_failed_pair_publication_rolls_back_and_retry_succeeds(self):
        self.publisher.sync(100000)
        before={p:p.read_bytes() for p in self.root.iterdir()}
        self.nodes[0]['telemetry']['runtimes']['python']['revision']='v2'
        original=snapshots.os.replace;calls=[]
        def fail_second(a,b):
            calls.append(b)
            if len(calls)==2:raise PermissionError('download temporarily open')
            return original(a,b)
        with patch.object(snapshots.os,'replace',side_effect=fail_second):
            with self.assertRaises(PermissionError):self.publisher.sync(100100)
        for p,data in before.items():self.assertEqual(p.read_bytes(),data)
        self.assertTrue(self.publisher.sync(100200))
    def test_generated_downloads_use_data_directory_and_follow_template_updates(self):
        output=self.root/'data-downloads'
        publisher=snapshots.Publisher(self.root,lambda:self.nodes,output=output)
        original=(self.root/'agent-preparation-kit.zip').read_bytes()
        self.assertTrue(publisher.sync(100000))
        self.assertEqual(original,(self.root/'agent-preparation-kit.zip').read_bytes())
        self.assertFalse(publisher.sync(100100))
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            entries=[(i,archive.read(i)) for i in archive.infolist()]
        with zipfile.ZipFile(self.root/'agent-preparation-kit.zip','w') as archive:
            for i,data in entries:archive.writestr(i,b'new documentation' if i.filename=='任务上传文档.md' else data)
        self.assertTrue(publisher.sync(100200))
        with zipfile.ZipFile(output/'agent-preparation-kit.zip') as archive:
            self.assertEqual(archive.read('任务上传文档.md'),b'new documentation')
