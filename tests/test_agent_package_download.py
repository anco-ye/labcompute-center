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

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import DownloadInterrupted,PackageIdentityChanged,Worker,download_package,upload_result,UploadYielded


class Response(io.BytesIO):
    def __init__(self,body,headers,status=200):
        super().__init__(body);self.headers=headers;self.status=status


class PackageDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.destination=Path(self.tmp.name)/'package.zip'
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('input.txt','retained states'*1000)
        self.body=stream.getvalue();self.truncate=0;self.ranges=[]
        self.opener=SimpleNamespace(open=self.open)

    def tearDown(self):self.tmp.cleanup()

    def open(self,request,**kwargs):
        self.assertEqual(request.get_header('X-lease'),'lease')
        if request.get_method()=='HEAD':
            return Response(b'',{'Content-Length':str(len(self.body))})
        start,end=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',request.get_header('Range')).groups())
        self.ranges.append((start,end))
        body=self.body[start:end+1]
        if self.truncate:
            self.truncate-=1;body=body[:123]
        return Response(body,{'Content-Range':f'bytes {start}-{end}/{len(self.body)}'},206)

    @patch('agent.time.sleep')
    def test_interrupted_package_resumes_and_promotes_after_length_check(self,sleep):
        self.truncate=1
        download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024)
        self.assertEqual(self.ranges[1][0],123)
        self.assertEqual(self.destination.read_bytes(),self.body)
        self.assertFalse(self.destination.with_suffix('.zip.download').exists())

    @patch('agent.time.sleep')
    def test_exhausted_transfer_keeps_partial_for_same_identity(self,sleep):
        self.truncate=20
        with self.assertRaises(DownloadInterrupted):
            download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024,retries=1)
        size=self.destination.with_suffix('.zip.download').stat().st_size
        self.assertGreater(size,0);self.assertFalse(self.destination.exists())
        self.truncate=0;self.ranges.clear()
        download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024)
        self.assertEqual(self.ranges[0][0],size)

    def test_changed_identity_does_not_mix_partial(self):
        partial=self.destination.with_suffix('.zip.download');partial.write_bytes(b'old bytes')
        partial.with_suffix('.download.json').write_text(json.dumps(dict(url='https://old/package',length=len(self.body))))
        download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024)
        self.assertEqual(self.ranges[0][0],0)
        self.assertEqual(self.destination.read_bytes(),self.body)

    def test_changed_range_discards_partial_and_rechecks(self):
        original=self.open
        def wrong_range(request,**kwargs):
            response=original(request,**kwargs)
            if request.get_method()=='GET':response.headers['Content-Range']='bytes 0-0/1'
            return response
        self.opener=SimpleNamespace(open=wrong_range)
        with self.assertRaises(PackageIdentityChanged):
            download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024)
        self.assertFalse(self.destination.with_suffix('.zip.download').exists())
        self.assertFalse(self.destination.exists())
        self.opener=SimpleNamespace(open=original)
        download_package(self.opener,'https://center/package',{'X-Lease':'lease'},self.destination,chunk_bytes=1024)
        self.assertEqual(self.destination.read_bytes(),self.body)

    def test_prelaunch_transfer_retries_same_job_without_false_exit(self):
        worker=Worker.__new__(Worker);worker.root=Path(self.tmp.name)/'jobs'
        worker.root.mkdir();worker.states={};worker._states_lock=__import__('threading').RLock()
        worker.state_path=Path(self.tmp.name)/'active_jobs.json';worker.clock_offset=0
        job=dict(id='a'*32,lease='same-lease',spec=dict(runtime='unused',dependency_jobs={}))
        def interrupted(*args,**kwargs):raise DownloadInterrupted('transient transfer')
        worker.api=interrupted
        folder=worker.root/job['id']
        for attempt in range(1,4):
            worker.prepare(job)
            self.assertFalse((folder/'exit.json').exists())
            retry=json.loads((folder/'preparation-retry.json').read_text())
            self.assertEqual(retry['attempts'],attempt)
            self.assertEqual(worker.states[job['id']]['lease'],'same-lease')
        with self.assertRaises(DownloadInterrupted):worker.prepare(job)
        self.assertTrue((folder/'exit.json').exists())
        self.assertEqual(json.loads((folder/'exit.json').read_text())['failure_code'],'environment_setup')


class PackageRangeEndpointTest(unittest.TestCase):
    def test_agent_package_supports_identity_bound_ranges(self):
        from test_center import CenterTest
        fixture=CenterTest();fixture.setUp()
        try:
            fixture.client.get('/api/submission-document/download')
            fixture.submit();fixture.heartbeat();job=fixture.claim()
            path=f"/agent/win2/jobs/{job['id']}/package"
            headers={**fixture.auth,'X-Lease':job['lease']}
            head=fixture.client.head(path,headers=headers)
            self.assertEqual(head.status_code,200)
            etag=head.headers['ETag'];self.assertTrue(etag)
            length=int(head.headers['Content-Length']);self.assertGreater(length,1)
            response=fixture.client.get(path,headers={**headers,'Range':'bytes=0-0','If-Range':etag})
            self.assertEqual(response.status_code,206)
            self.assertEqual(response.headers['Content-Range'],f'bytes 0-0/{length}')
            self.assertEqual(len(response.data),1)
            head.close();response.close()
        finally:fixture.tearDown()


class ResultUploadEndpointTest(unittest.TestCase):
    def test_interrupted_result_upload_resumes_under_original_lease(self):
        from test_center import CenterTest
        fixture=CenterTest();fixture.setUp()
        try:
            fixture.client.get('/api/submission-document/download')
            fixture.submit();fixture.heartbeat();job=fixture.claim()
            base=f"/agent/win2/jobs/{job['id']}"
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:
                archive.writestr('work/output.txt','result')
                archive.writestr('exit.json',json.dumps(dict(status='succeeded',exit_code=0,cleanup_verified=True)))
            body=stream.getvalue()
            headers={**fixture.auth,'X-Lease':job['lease'],'X-Upload-Size':str(len(body)),
                     'X-Result-Status':'succeeded','X-Exit-Code':'0'}
            self.assertEqual(fixture.client.post(base+'/stopped',json=dict(cleanup_verified=True),headers=headers).status_code,200)
            query=base+'/finish-upload'
            self.assertEqual(fixture.client.get(query,headers=headers).json['offset'],0)
            cut=len(body)//2
            first=fixture.client.post(query,data=body[:cut],headers={**headers,'X-Upload-Offset':'0'})
            self.assertEqual(first.status_code,200)
            self.assertEqual(first.json['offset'],cut)
            self.assertEqual(fixture.client.get(query,headers=headers).json['offset'],cut)
            self.assertEqual(fixture.client.post(base+'/finish',data=b'',headers={**headers,'X-Upload-Commit':'1'}).status_code,409)
            self.assertNotEqual(fixture.client.get(query,headers={**headers,'X-Lease':'wrong-lease'}).status_code,200)
            self.assertEqual(fixture.client.post(query,data=body[cut:],headers={**headers,'X-Upload-Offset':'0'}).status_code,409)
            final=fixture.client.post(query,data=body[cut:],headers={**headers,'X-Upload-Offset':str(cut)})
            self.assertEqual(final.json['offset'],len(body))
            committed=fixture.client.post(base+'/finish',data=b'',headers={**headers,'X-Upload-Commit':'1'})
            self.assertEqual(committed.status_code,200,committed.get_data(as_text=True))
            self.assertEqual(fixture.client.get('/api/state').json['jobs'][0]['status'],'succeeded')
            self.assertFalse((fixture.root/'data/results'/f"{job['id']}.resumable.upload").exists())
        finally:fixture.tearDown()


class ResultUploadClientTest(unittest.TestCase):
    def test_bounded_upload_yields_and_resumes_without_retransmitting_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'result.zip';archive.write_bytes(b'abcdefghijkl')
            received=bytearray();sent=[];commits=[]
            def request(req,**kwargs):
                if req.full_url.endswith('/finish-upload'):
                    if req.get_method()=='GET':return Response(json.dumps(dict(offset=len(received))).encode(),{})
                    offset=int(req.get_header('X-upload-offset'));self.assertEqual(offset,len(received));sent.append(offset);received.extend(req.data)
                    return Response(json.dumps(dict(offset=len(received))).encode(),{})
                commits.append(True);return Response(b'{"ok":true}',{})
            opener=SimpleNamespace(open=request);progress=[]
            with self.assertRaises(UploadYielded):
                upload_result(opener,'https://center','/jobs/a/finish',{},archive,chunk_bytes=4,byte_budget=4,on_progress=lambda a,b:progress.append((a,b)))
            self.assertEqual(received,b'abcd');self.assertEqual(commits,[]);self.assertEqual(progress[-1],(4,12))
            upload_result(opener,'https://center','/jobs/a/finish',{},archive,chunk_bytes=4,byte_budget=16)
            self.assertEqual(sent,[0,4,8]);self.assertEqual(received,archive.read_bytes());self.assertEqual(commits,[True])

    def test_maintenance_pause_keeps_offset_without_reading_another_chunk(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'result.zip';archive.write_bytes(b'original');pause=Path(folder)/'delivery.pause';pause.touch()
            requests=[]
            def request(req,**kwargs):
                requests.append(req.get_method());return Response(b'{"offset":3}',{})
            with self.assertRaises(UploadYielded):upload_result(SimpleNamespace(open=request),'https://center','/jobs/a/finish',{},archive,pause_file=pause)
            self.assertEqual(requests,['GET']);self.assertEqual(archive.read_bytes(),b'original')

    @patch('agent.time.sleep')
    def test_lost_chunk_ack_queries_offset_instead_of_resending(self,sleep):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'result.zip';archive.write_bytes(b'abcdefghij')
            received=bytearray();lost_ack=True;sent=[]

            def open_request(request,**kwargs):
                nonlocal lost_ack
                url=request.full_url
                if url.endswith('/finish-upload') and request.get_method()=='GET':
                    return Response(json.dumps(dict(offset=len(received))).encode(),{},200)
                if url.endswith('/finish-upload'):
                    offset=int(request.get_header('X-upload-offset'))
                    sent.append(offset)
                    self.assertEqual(offset,len(received))
                    received.extend(request.data)
                    if lost_ack:
                        lost_ack=False
                        raise OSError('response lost after server accepted chunk')
                    return Response(json.dumps(dict(offset=len(received))).encode(),{},200)
                self.assertEqual(request.get_header('X-upload-commit'),'1')
                self.assertEqual(bytes(received),archive.read_bytes())
                return Response(b'{"ok":true}',{},200)

            opener=SimpleNamespace(open=open_request)
            self.assertEqual(upload_result(opener,'https://center','/jobs/a/finish',
                {'X-Lease':'same-lease'},archive,chunk_bytes=4),{'ok':True})
            self.assertEqual(sent,[0,4,8])


if __name__=='__main__':unittest.main()
