import io,tempfile,unittest,threading
from pathlib import Path
from unittest.mock import Mock,patch
from concurrent.futures import ThreadPoolExecutor
from external.ssh_bridge import Bridge,copy_result_slice

class VectorFile:
 def __init__(self,data,short_at=None):self.data=data;self.calls=[];self.short_at=short_at
 def readv(self,chunks,max_concurrent_prefetch_requests=None):
  self.calls.append((chunks,max_concurrent_prefetch_requests))
  for offset,length in chunks:yield self.data[offset:offset+length-(1 if offset==self.short_at else 0)]

class StreamingTest(unittest.TestCase):
 def test_pipelined_slice_and_resume_are_contiguous_and_bounded(self):
  data=(bytes(range(256))*24000);src=VectorFile(data);dst=io.BytesIO()
  end=copy_result_slice(src,dst,0,len(data),byte_budget=3*1024**2)
  self.assertEqual(end,3*1024**2);self.assertEqual(dst.getvalue(),data[:end])
  self.assertTrue(all(n==32 and all(size<=32768 for _,size in chunks) for chunks,n in src.calls))
  copy_result_slice(src,dst,end,len(data));self.assertEqual(dst.getvalue(),data)
 def test_short_read_preserves_confirmed_prefix_for_offset_retry(self):
  data=b'a'*100000;src=VectorFile(data,short_at=32768);dst=io.BytesIO()
  with self.assertRaises(IOError):copy_result_slice(src,dst,0,len(data))
  self.assertEqual(len(dst.getvalue()),32768)
  copy_result_slice(VectorFile(data),dst,dst.tell(),len(data));self.assertEqual(dst.getvalue(),data)
 def test_non_vector_file_fallback_and_time_bound(self):
  data=b'x'*(4*1024**2);dst=io.BytesIO()
  with patch('external.ssh_bridge.time.monotonic',side_effect=[0,10]):
   end=copy_result_slice(io.BytesIO(data),dst,0,len(data),seconds=8)
  self.assertEqual(end,2*1024**2);self.assertEqual(dst.getvalue(),data[:end])
 def test_artifact_session_reused_and_failed_session_is_replaced(self):
  b=Bridge.__new__(Bridge);client=Mock();client.get_transport.return_value.is_active.return_value=True;b.connect=Mock(return_value=client);b.deliver=Mock()
  job=dict(id='j',lease='lease',spec={});b.artifact_pass(job,{});b.artifact_pass(job,{})
  self.assertEqual(b.connect.call_count,1);client.close.assert_not_called()
  b.deliver.side_effect=IOError('broken')
  with self.assertRaises(IOError):b.artifact_pass(job,{})
  self.assertIsNone(b._delivery_client);client.close.assert_called_once()
 def test_blocked_delivery_does_not_starve_preparation_or_claim_lane(self):
  b=Bridge.__new__(Bridge);b.transfers={};b.controls={};b.cfg={};b.stage=Mock(return_value=None);client=Mock();client.get_transport.return_value.is_active.return_value=True;b.connect=Mock(return_value=client)
  b.artifact_pool=ThreadPoolExecutor(1);b.stage_pool=ThreadPoolExecutor(1);entered=threading.Event();release=threading.Event()
  def blocked(*a):entered.set();release.wait(3)
  b.deliver=blocked
  try:
   b.queue_artifact(dict(id='old',lease='a',spec={}),{'state':'finished'});self.assertTrue(entered.wait(1))
   b.queue_artifact(dict(id='new',lease='b',spec={}))
   b.transfers['new'].result(1);b.stage.assert_called_once();self.assertFalse(b.transfers['old'].done())
  finally:release.set();b.stage_pool.shutdown();b.artifact_pool.shutdown()

 def test_delivery_queue_does_not_spread_every_result_into_small_unfinished_slices(self):
  from concurrent.futures import Future
  b=Bridge.__new__(Bridge);b.transfers={};future=Future();b.artifact_pool=Mock();b.artifact_pool.submit.return_value=future
  b.queue_artifact(dict(id='first'),{'state':'finished'})
  b.queue_artifact(dict(id='second'),{'state':'finished'})
  b.artifact_pool.submit.assert_called_once();self.assertEqual(set(b.transfers),{'first'})
  future.set_result(None);b.queue_artifact(dict(id='second'),{'state':'finished'});self.assertEqual(b.artifact_pool.submit.call_count,2)

 def test_preparation_does_not_build_an_unbounded_future_fifo(self):
  from concurrent.futures import Future
  b=Bridge.__new__(Bridge);b.transfers={};future=Future();b.artifact_pool=Mock();b.stage_pool=Mock();b.stage_pool.submit.return_value=future
  b.queue_artifact(dict(id='large'))
  b.queue_artifact(dict(id='small'))
  self.assertEqual(b.stage_pool.submit.call_count,1);self.assertNotIn('small',b.transfers)
  future.set_result(None);b.queue_artifact(dict(id='small'));self.assertEqual(b.stage_pool.submit.call_count,2)

if __name__=='__main__':unittest.main()
