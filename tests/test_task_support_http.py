"""Real sockets: disconnected and trickling transfers cannot lose a prefix or hang."""
import http.server,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,os.environ.get('TASK_SUPPORT_PATH',str(Path(__file__).parents[1]/'runtime_support')))
from lab_task_support import download_resource

class HTTPTests(unittest.TestCase):
    def server(self,handler):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d/a'%server.server_port
    def test_real_disconnect_resumes_without_duplicate_bytes(self):
        offsets=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                start,end=map(int,self.headers['Range'][6:].split('-'));offsets.append(start)
                self.send_response(206);self.send_header('Content-Range',f'bytes {start}-{end}/6');self.send_header('Content-Length',str(end-start+1));self.send_header('ETag','"same"');self.end_headers()
                self.wfile.write(b'abcdef'[start:2 if len(offsets)==1 else end+1]);self.wfile.flush();self.close_connection=True
        url=self.server(Handler)
        with tempfile.TemporaryDirectory()as t:
            p=Path(t)/'a';download_resource(url,p,expected_size=6,sleep=lambda _:None)
            self.assertEqual(p.read_bytes(),b'abcdef');self.assertEqual(offsets,[0,2])
    def test_real_slow_read_has_absolute_deadline_and_retains_partial(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                self.send_response(206);self.send_header('Content-Range','bytes 0-1/2');self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'a');self.wfile.flush();time.sleep(.6)
        url=self.server(Handler)
        with tempfile.TemporaryDirectory()as t:
            p=Path(t)/'a';start=time.monotonic()
            with self.assertRaises(TimeoutError):download_resource(url,p,expected_size=2,attempts=1,deadline_seconds=.15)
            self.assertLess(time.monotonic()-start,.55);self.assertEqual(p.with_name('a.part').read_bytes(),b'a')
    def test_external_http_connection_gets_finite_read_timeout(self):
        code="""import http.client,json
from unittest.mock import patch
from lab_network_policy import install
# Separate process avoids altering the operator/test runner's networking.
class Sock:
 def __init__(self):self.timeout=None
 def gettimeout(self):return self.timeout
 def settimeout(self,value):self.timeout=value
http.client.HTTPConnection._lab_network_policy=None
with patch.object(http.client.HTTPConnection,'getresponse',return_value='ok'):
 install();c=http.client.HTTPConnection('example.org');c.sock=Sock();assert c.getresponse()=='ok';assert c.sock.timeout==60
print('bounded')
"""
        env=dict(os.environ,PYTHONPATH=str(Path(download_resource.__code__.co_filename).parent))
        x=subprocess.run([sys.executable,'-B','-c',code],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(x.returncode,0,x.stderr);self.assertIn('bounded',x.stdout)

if __name__=='__main__':unittest.main()
