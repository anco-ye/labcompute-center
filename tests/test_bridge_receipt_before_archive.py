import time,unittest
from unittest.mock import Mock
from external.ssh_bridge import Bridge


class Tests(unittest.TestCase):
 def bridge(self):
  b=Bridge.__new__(Bridge);b.cfg={'id':'a800-ssh'};b.controls={};b.inventory={'at':time.time(),'receipt_before_archive':True}
  b.observation={};b.remote_offset=0;b.persist=Mock();b.heartbeat=Mock();b.api=Mock();b.queue_artifact=Mock()
  return b
 def test_archiving_releases_once_without_waiting_for_bulk_lane(self):
  b=self.bridge();report={'cleanup_verified':True,'exit_code':0}
  b.rpc=Mock(return_value={'state':'archiving','report':report})
  job={'id':'job','lease':'lease','spec':{}}
  b.tick_job(None,job);b.tick_job(None,job)
  self.assertTrue(b.rpc.call_args.args[1]['receipt_first'])
  self.assertEqual(b.api.call_count,1);self.assertEqual(b.api.call_args.args[0],'/jobs/job/stopped')
  self.assertEqual(b.queue_artifact.call_count,2);self.assertTrue(job['_compute_released'])
 def test_unverified_cleanup_never_releases(self):
  b=self.bridge();b.rpc=Mock(return_value={'state':'archiving','report':{'cleanup_verified':False}})
  with self.assertRaises(RuntimeError):b.tick_job(None,{'id':'job','lease':'lease','spec':{}})
  b.api.assert_not_called();b.queue_artifact.assert_not_called()
 def test_archive_rpc_runs_only_in_artifact_worker(self):
  b=self.bridge();client=Mock();b.connect=Mock(return_value=client)
  result={'state':'finished','result_size':100,'report':{'cleanup_verified':True}}
  b.rpc=Mock(return_value=result);b.deliver=Mock()
  job={'id':'job','lease':'lease','spec':{}}
  b.artifact_pass(job,{'state':'archiving','report':{'cleanup_verified':True}})
  self.assertEqual(b.rpc.call_args.args[1]['action'],'archive')
  b.deliver.assert_called_once_with(client,job,result)


if __name__=='__main__':unittest.main()
