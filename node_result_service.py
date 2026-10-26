"""Read-only result service. Separate from Agents, runners, and upload ownership."""
import argparse, hashlib, json, os, re, ssl, sys, threading, zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from email.utils import formatdate
from direct_download import verify,identity,etag,selection


def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def result_path(cfg,cap):
    root=Path(cfg['root']);jid=cap['jid'];candidates=[]
    if cfg['platform']=='windows':
        agent=load(root/'config/agent.json');active=root/'jobs/active_jobs.json'
        jobs=load(active) if active.exists() else {}
        if jid in jobs:
            j=jobs[jid]
            if hashlib.sha256(j['lease'].encode()).hexdigest()!=cap['lease_digest']:raise PermissionError('Lease changed')
        roots=[root/'jobs',Path(agent.get('work_dir',root/'jobs'))]
        roots.extend(Path(x) for x in cfg.get('job_roots',[]))
        if agent.get('archive_dir'):roots.append(Path(agent['archive_dir']))
        for base in roots:
            folder=base/jid
            # The junction maps this exact immutable job; never accept client paths.
            if folder.exists():candidates.extend(folder/n for n in ('result.zip','cancelled-result.zip'))
    else:
        loc=load(root/'locations'/(jid+'.json'))
        if loc['lease_digest']!=cap['lease_digest']:raise PermissionError('Lease changed')
        host=load(root/'host-config.json') if (root/'host-config.json').exists() else {}
        volume=loc['volume'];mapping=host.get('volume_roots',{})
        base=Path(mapping[volume]) if volume in mapping else Path(volume)/root.name
        candidates=[base/'jobs'/jid/'result.zip']
    for p in candidates:
        if not p.is_file() or p.is_symlink():continue
        # ZIP directory and runner receipt only; no scientific result/log review.
        with zipfile.ZipFile(p) as z:
            item=z.getinfo('exit.json')
            if item.file_size>16384:raise ValueError('Invalid execution receipt')
            report=json.loads(z.read(item))
            if report.get('cleanup_verified') is not True:raise ValueError('Execution cleanup not confirmed')
        return p
    raise FileNotFoundError('Result archive is not ready on this node')


def inspect(cfg,token):
    cap=verify(cfg['key'],token,cfg['node']);path=result_path(cfg,cap);meta=identity(path)
    if cap['kind']=='read' and cap.get('identity')!=meta:raise FileExistsError('Result version changed')
    return path,meta,cap


def stdio(cfg):
    """Same capabilities over an existing authenticated SSH route when HTTPS is blocked."""
    sent=False
    try:
        req=json.loads(sys.stdin.buffer.readline(16384));path,meta,cap=inspect(cfg,req['ticket'])
        if req['op']=='meta':
            data=json.dumps(meta).encode();status=200;headers={'Content-Length':str(len(data))}
        else:
            if cap['kind']!='read':raise PermissionError()
            status,start,end=selection(meta,req.get('headers',{}));length=end-start+1 if status in (200,206) else 0
            headers={'Content-Length':str(length),'ETag':etag(meta),'Last-Modified':formatdate(meta['mtime_ns']/1000000000,usegmt=True),'Accept-Ranges':'bytes','Content-Type':'application/zip','Content-Disposition':'attachment; filename="result-'+cap['jid']+'.zip"'}
            if status==206:headers['Content-Range']=f'bytes {start}-{end}/{meta["size"]}'
            if status==416:headers['Content-Range']=f'bytes */{meta["size"]}'
        with path.open('rb') as stream:
            st=os.fstat(stream.fileno())
            if dict(size=st.st_size,mtime_ns=st.st_mtime_ns)!=meta:raise FileExistsError()
            sys.stdout.buffer.write((json.dumps(dict(status=status,headers=headers))+'\n').encode());sys.stdout.buffer.flush();sent=True
            if req.get('method')=='HEAD':return
            if req['op']=='meta':sys.stdout.buffer.write(data)
            elif status in (200,206):
                stream.seek(start);left=length
                while left:
                    chunk=stream.read(min(left,1024**2))
                    if not chunk:raise OSError('Result changed')
                    sys.stdout.buffer.write(chunk);left-=len(chunk)
    except PermissionError:
        if sent:raise
        sys.stdout.buffer.write(b'{"status":403,"headers":{"Content-Length":"0"}}\n')
    except FileExistsError:
        if sent:raise
        sys.stdout.buffer.write(b'{"status":412,"headers":{"Content-Length":"0"}}\n')
    except (OSError,ValueError,KeyError,zipfile.BadZipFile):
        if sent:raise
        sys.stdout.buffer.write(b'{"status":409,"headers":{"Content-Length":"0"}}\n')


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass  # Never put signed download URLs in logs.
    def do_HEAD(self):self.serve(False)
    def do_GET(self):self.serve(True)
    def do_OPTIONS(self):
        self.send_response(204);self.common();self.send_header('Content-Length','0');self.end_headers()
    def common(self):
        self.send_header('X-LabCompute-Download-Release','2026-10-23-direct-v1')
        self.send_header('Cache-Control','private, no-store');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Content-Type-Options','nosniff')
        origin=self.headers.get('Origin')
        if origin in self.server.cfg.get('origins',[]):
            self.send_header('Access-Control-Allow-Origin',origin);self.send_header('Vary','Origin')
            self.send_header('Access-Control-Expose-Headers','ETag, Content-Length, Content-Range, Accept-Ranges')
            self.send_header('Access-Control-Allow-Methods','GET, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers','Range, If-Range, If-Match')
    def fail(self,status,message):
        if getattr(self,'reply_started',False):self.close_connection=True;return
        data=json.dumps({'error':message}).encode();self.send_response(status);self.common();self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(data)
    def serve(self,body):
        self.reply_started=False
        self.connection.settimeout(60);parts=urlsplit(self.path);m=re.fullmatch('/(meta|result)/([a-f0-9]{32})',parts.path)
        if not m:return self.fail(404,'Not found')
        try:
            token=parse_qs(parts.query).get('ticket',[''])[0];path,meta,cap=inspect(self.server.cfg,token)
            if cap['jid']!=m[2]:raise PermissionError('Job mismatch')
            if m[1]=='meta':
                data=json.dumps(meta).encode();self.send_response(200);self.common();self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.reply_started=True
                if body:self.wfile.write(data)
                return
            if cap['kind']!='read':raise PermissionError('Read capability required')
            status,start,end=selection(meta,self.headers)
            if not self.server.streams.acquire(blocking=False):return self.fail(503,'Download capacity busy; retry shortly')
            try:
                with path.open('rb') as stream:
                    st=os.fstat(stream.fileno())
                    if dict(size=st.st_size,mtime_ns=st.st_mtime_ns)!=meta:raise FileExistsError('Result version changed')
                    self.send_response(status);self.common();self.send_header('ETag',etag(meta));self.send_header('Accept-Ranges','bytes');self.send_header('Last-Modified',formatdate(st.st_mtime,usegmt=True))
                    length=end-start+1 if status in (200,206) else 0
                    self.send_header('Content-Length',str(length));self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition','attachment; filename="result-'+cap['jid']+'.zip"')
                    if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{meta["size"]}')
                    if status==416:self.send_header('Content-Range',f'bytes */{meta["size"]}')
                    self.end_headers();self.reply_started=True
                    if body and length:
                        stream.seek(start);left=length
                        while left:
                            chunk=stream.read(min(1024**2,left))
                            if not chunk:raise OSError('Result changed during read')
                            self.wfile.write(chunk);left-=len(chunk)
            finally:self.server.streams.release()
        except PermissionError:self.fail(403,'Invalid or expired download permission; reopen from the center')
        except (FileNotFoundError,KeyError):self.fail(409,'Node result is not ready')
        except FileExistsError:self.fail(412,'Result version changed; reopen from the center')
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        except (OSError,ValueError,zipfile.BadZipFile):self.fail(409,'Node result is unavailable')


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--stdio',action='store_true');a=p.parse_args();cfg=load(a.config)
    if a.stdio:return stdio(cfg)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2;ctx.load_cert_chain(cfg['certificate'],cfg['private_key'])
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            sock,addr=super().get_request();sock.settimeout(5)
            return ctx.wrap_socket(sock,server_side=True,do_handshake_on_connect=False),addr
        def process_request(self,sock,addr):
            if not self.connections.acquire(blocking=False):sock.close();return
            try:super().process_request(sock,addr)
            except BaseException:self.connections.release();raise
        def process_request_thread(self,sock,addr):
            try:super().process_request_thread(sock,addr)
            finally:self.connections.release()
    server=Server((cfg.get('bind','0.0.0.0'),cfg.get('port',8446)),Handler);server.cfg=cfg;server.streams=threading.BoundedSemaphore(4);server.connections=threading.BoundedSemaphore(16)
    server.serve_forever()
if __name__=='__main__':main()
