import io,json,os,sys,tempfile,unittest,urllib.error,http.client
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'runtime_support'))
import lab_task_support as lib
import lab_network_policy as policy

class VectorTests(unittest.TestCase):
    def test_empty_and_native_copy(self):
        import numpy as np
        class Native:
            def __init__(self, data):self.data=np.asarray(data,dtype=float)
            def as_numpy(self):return self.data
            def __array__(self,*a,**k):raise ValueError('bad native array protocol')
        for values in [[],[1.,2.]]:
            native=Native(values);arr=lib.vector_array(native);self.assertEqual(arr.tolist(),values)
            native.data.fill(7);self.assertEqual(arr.tolist(),values)
    def test_fallback_and_dtype(self):
        class Legacy:
            def to_python(self):return [2,3]
        self.assertEqual(lib.vector_array(Legacy(),dtype='int32').dtype.name,'int32')
        self.assertEqual(lib.vector_array([]).shape,(0,))
        self.assertEqual(lib.vector_array([[1,2],[3,4]]).shape,(2,2))

class LegacyVectorAdapterTests(unittest.TestCase):
    def test_policy_startup_does_not_import_numpy_or_neuron(self):
        import subprocess
        with tempfile.TemporaryDirectory()as t:
            env=dict(os.environ,PYTHONPATH=str(Path(lib.__file__).parent),LAB_TASK_NETWORK_POLICY='bounded-v1',LAB_JOB_DIR=t)
            x=subprocess.run([sys.executable,'-B','-c',"import sys;assert 'neuron' not in sys.modules;assert 'numpy' not in sys.modules"],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(x.returncode,0,x.stderr)

    def test_only_native_vectors_adapt_and_standard_flags_stay_intact(self):
        import numpy as np
        import lab_vector_compat as compat
        original_array,original_asarray=np.array,np.asarray
        previous=getattr(np,'_lab_vector_compat',None)
        class Native:
            __module__='hoc'
            def __init__(self,data):self.data=original_array(data,dtype=float)
            def hname(self):return 'Vector[0]'
            def as_numpy(self):return self.data
            def __array__(self,*a,**k):raise ValueError('broken native protocol')
        try:
            np._lab_vector_compat=None;compat.install()
            for values in [[],[1.,2.]]:
                native=Native(values);saved=np.array(native);self.assertEqual(saved.tolist(),values)
                self.assertTrue(np.shares_memory(np.asarray(native),native.data)or not values)
                native.data.fill(7);self.assertEqual(saved.tolist(),values)
            arr=original_array([[1,2],[3,4]],dtype='int16')
            self.assertIs(np.asarray(arr),arr)
            self.assertEqual(np.array(object=[1,2]).tolist(),[1,2])
            import pickle
            self.assertIs(pickle.loads(pickle.dumps(np.array)),np.array)
            out=np.array(arr,dtype='float32',order='F');self.assertEqual(out.dtype.name,'float32');self.assertTrue(out.flags.f_contiguous)
            class LegacyNative(Native):
                __module__='hoc'
                def as_numpy(self):return np.asarray(self)
                def __array__(self,dtype=None,copy=None):return self.data
            self.assertEqual(np.array(LegacyNative([3.,4.])).tolist(),[3.,4.])
            class Other(Native):
                __module__='hoc'
                def hname(self):return 'NetCon[0]'
            with self.assertRaises(ValueError):np.array(Other([]))
        finally:
            np.array,np.asarray=original_array,original_asarray;np._lab_vector_compat=previous


class Response:
    def __init__(self,data,status=206,headers=None):
        self.data=io.BytesIO(data);self.status=status;self.headers=headers or {};self.fp=None
    def read(self,n):return self.data.read(n)
    def geturl(self):return 'https://example.org/a'
    def __enter__(self):return self
    def __exit__(self,*a):pass

class DownloadTests(unittest.TestCase):
    def test_partial_read_resumes_and_reuses_finished_file(self):
        with tempfile.TemporaryDirectory() as t:
            dest=Path(t)/'a';calls=[]
            def opener(req,timeout):
                calls.append(req.get_header('Range'))
                if len(calls)==1:return Response(b'abc',headers={'Content-Range':'bytes 0-5/6','Content-Length':'6','ETag':'"v1"'})
                self.assertEqual(req.get_header('If-range'),'"v1"')
                return Response(b'def',headers={'Content-Range':'bytes 3-5/6','Content-Length':'3','ETag':'"v1"'})
            lib.download_resource('https://example.org/a',dest,expected_size=6,opener=opener,sleep=lambda _:None)
            self.assertEqual(dest.read_bytes(),b'abcdef');self.assertEqual(calls,['bytes=0-5','bytes=3-5'])
            lib.download_resource('https://example.org/a',dest,expected_size=6,opener=lambda *a,**k:self.fail('downloaded again'))
    def test_deadline_even_with_continuous_small_progress(self):
        with tempfile.TemporaryDirectory() as t:
            dest=Path(t)/'a';clock=[0]
            class Slow(Response):
                def read(self,n):clock[0]+=3;return super().read(1)
            with self.assertRaises(TimeoutError):
                lib.download_resource('https://example.org/a',dest,expected_size=6,deadline_seconds=5,clock=lambda:clock[0],opener=lambda *a,**k:Slow(b'abcdef',headers={'Content-Range':'bytes 0-5/6','Content-Length':'6'}),sleep=lambda _:None)
            self.assertTrue(dest.with_name('a.part').exists());self.assertFalse(dest.exists())
    def test_no_range_at_offset_zero_and_ignored_resume_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'a';response=lambda *a,**k:Response(b'abc',status=200,headers={'Content-Length':'3'})
            lib.download_resource('https://example.org/a',d,expected_size=3,opener=response)
            d.unlink();d.with_name('a.part').write_bytes(b'a')
            with self.assertRaises(ValueError):lib.download_resource('https://example.org/a',d,expected_size=3,opener=response)
            self.assertEqual(d.with_name('a.part').read_bytes(),b'a')
    def test_changed_validator_and_unbound_prefix_retained(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'a';p=d.with_name('a.part');p.write_bytes(b'a')
            with self.assertRaises(ValueError):lib.download_resource('https://example.org/a',d,expected_size=3)
            p.with_name('a.part.json').write_text(json.dumps({'url':'https://example.org/a','size':3,'validator':'old'}))
            with self.assertRaises(ValueError):lib.download_resource('https://example.org/a',d,expected_size=3,opener=lambda *a,**k:Response(b'bc',headers={'Content-Range':'bytes 1-2/3','Content-Length':'2','ETag':'new'}))
            self.assertEqual(p.read_bytes(),b'a')
    def test_bounded_errors_and_existing_code_verification(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'a';calls=[]
            def fail(*a,**k):calls.append(1);raise OSError('network')
            with self.assertRaises(OSError):lib.download_resource('https://example.org/a',d,expected_size=3,opener=fail,attempts=3,sleep=lambda _:None)
            self.assertEqual(len(calls),3)
            d.write_bytes(b'abc')
            with self.assertRaises(ValueError):lib.download_resource('https://example.org/a',d,expected_size=3,verify=lambda p:False)

class PolicyTests(unittest.TestCase):
    def test_explicit_shorter_timeouts_preserved(self):
        self.assertEqual(policy.bounded_timeout(.5,60),.5)
        for value in [None,0,-1,float('inf'),float('nan')]:self.assertEqual(policy.bounded_timeout(value,60),60)
    def test_loopback_local_application_servers_exempt(self):
        class Conn:pass
        for host in ['localhost','127.0.0.1','::1']:
            c=Conn();c.host=host;self.assertFalse(policy.external(c))
    def test_real_task_policy_startup_receipt(self):
        import subprocess
        with tempfile.TemporaryDirectory() as t:
            env=dict(os.environ,PYTHONPATH=str(Path(lib.__file__).parent),LAB_TASK_NETWORK_POLICY='bounded-v1',LAB_JOB_DIR=t)
            result=subprocess.run([sys.executable,'-c','import http.client;print(http.client.HTTPConnection._lab_network_policy)'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn(policy.RELEASE,result.stdout)
            self.assertEqual(len(list(Path(t).glob('network-policy-*.json'))),1)

if __name__=='__main__':unittest.main()
