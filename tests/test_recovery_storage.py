import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import recovery_storage as store

class RecoveryStorageTests(unittest.TestCase):
    def fixture(self):
        spec=dict(cores=1,memory_gb=11,commit_gb=11,disk_gb=23,gpus=1,gpu_memory_gb=8,work_volume='old')
        job=dict(id='a'*32,lease='secret',status='failed',control='run',execution_stopped=1,node='n',spec=spec)
        node=dict(id='n',enabled=1,last_seen=100)
        proof=dict(release=store.RELEASE,verified=True,at=100,job_id=job['id'],lease_digest=hashlib.sha256(b'secret').hexdigest(),volume='old',node='n',allocated_bytes=10*2**30)
        return node,job,dict(spec,work_volume='other'),proof
    def test_receipt_clock_not_remote_clock_controls_freshness(self):
        n,j,s,p=self.fixture();p['at']=1000000
        p=store.received_observation(p,100)
        self.assertEqual(p['remote_at'],1000000)
        with patch('scheduler.disk_capacities',return_value=[('old',16)]):
            self.assertEqual(store.admit(n,[],j,s,p,100)[0]['work_volume'],'old')

    def test_only_remaining_growth_reserved_same_limit(self):
        n,j,s,p=self.fixture()
        with patch('scheduler.disk_capacities',return_value=[('old',16),('other',100)]):
            admitted,proof=store.admit(n,[],j,s,p,100)
        self.assertEqual(admitted['disk_gb'],23);self.assertEqual(admitted['work_volume'],'old');self.assertEqual(proof['growth_and_margin_gb'],15)
    def test_cannot_use_other_volume_or_spend_reserve(self):
        n,j,s,p=self.fixture()
        with patch('scheduler.disk_capacities',return_value=[('old',14),('other',100)]):
            with self.assertRaises(ValueError):store.admit(n,[],j,s,p,100)
    def test_identity_staleness_disabled_cancelled_and_budget_change_fail_closed(self):
        for mutate in [lambda n,j,s,p:p.update(at=60),lambda n,j,s,p:p.update(lease_digest='bad'),lambda n,j,s,p:p.update(volume='other'),lambda n,j,s,p:n.update(enabled=0),lambda n,j,s,p:j.update(control='cancel'),lambda n,j,s,p:j.update(execution_stopped=0),lambda n,j,s,p:s.update(disk_gb=22),lambda n,j,s,p:p.update(allocated_bytes=True)]:
            n,j,s,p=self.fixture();mutate(n,j,s,p)
            with self.assertRaises(ValueError):store.admit(n,[],j,s,p,100)
    def test_copy_keeps_source_and_contents_without_hash(self):
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'old';source.mkdir();(source/'a.part').write_bytes(b'abc');calls=[]
            dest=Path(t)/'new';result=store.relocate_copy(source,dest,stopped=lambda:True,commit_location=lambda:calls.append(True))
            self.assertEqual((source/'a.part').read_bytes(),(dest/'a.part').read_bytes());self.assertTrue(calls);self.assertTrue(result['source_preserved'])
    def test_never_move_active_or_overwrite_existing_copy(self):
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'old';source.mkdir();dest=Path(t)/'new'
            with self.assertRaises(ValueError):store.relocate_copy(source,dest,stopped=lambda:False,commit_location=lambda:self.fail('committed'))
            dest.mkdir()
            with self.assertRaises(ValueError):store.relocate_copy(source,dest,stopped=lambda:True,commit_location=lambda:self.fail('committed'))
    def test_partial_copy_or_source_changes_never_commit(self):
        import shutil
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'old';source.mkdir();(source/'a').write_text('old');dest=Path(t)/'new';real=shutil.copytree
            def changed(*a,**k):
                result=real(*a,**k);(source/'a').write_text('changed');return result
            with patch('shutil.copytree',side_effect=changed):
                with self.assertRaises(ValueError):store.relocate_copy(source,dest,stopped=lambda:True,commit_location=lambda:self.fail('committed'))
            self.assertTrue(source.exists());self.assertFalse(dest.exists())
    def test_shared_hardlinks_do_not_receive_credit(self):
        import os
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'dir';source.mkdir();(source/'a').write_bytes(b'x'*4096);os.link(source/'a',Path(t)/'alias')
            proof=store.observe(source,job_id='a',lease_digest='b',volume='v',node='n')
            self.assertEqual(proof['allocated_bytes'],0)

if __name__=='__main__':unittest.main()
