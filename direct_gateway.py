"""Authenticated node-result discovery and streaming fallback, without staging ZIPs."""
import argparse, hashlib, json, sqlite3, ssl, urllib.request, urllib.error, threading,sys
from pathlib import Path
from contextlib import contextmanager
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from flask import Flask,abort,jsonify,request,session,Response,render_template_string
import access_control
from direct_download import issue,etag

PAGE=r'''<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>领取节点结果</title><h2>领取节点结果</h2><p id="message">正在连接计算节点…</p><a id="retry" href="">重新连接 / 续传</a><script>
(async()=>{const p=location.pathname;const m=document.getElementById('message');try{const r=await fetch(p.replace(/\/download$/,'/meta'),{credentials:'same-origin',cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'暂时不可领取');m.textContent='结果位于 '+d.node+'，共 '+(d.size/1024**3).toFixed(2)+' GiB；正在选择下载连接…';let direct=false;try{const c=new AbortController();const timer=setTimeout(()=>c.abort(),4000);try{const h=await fetch(d.url,{method:'HEAD',mode:'cors',credentials:'omit',signal:c.signal,cache:'no-store'});direct=h.ok&&h.headers.get('ETag')===d.etag;}finally{clearTimeout(timer);}}catch(e){}const url=direct?d.url:p.replace(/\/download$/,'/result');m.textContent=direct?'已发起从计算节点直接下载，可使用下载工具断点续传。':'节点无法直连，已使用中心流式转发；无需等待结果回传完成。';location.href=url;}catch(e){m.textContent=e.message+'。请在中心登录后重试。';}})();</script>'''


def create_app(config_path, service_path, opener=None):
    config_path=Path(config_path);cfg=json.loads(config_path.read_text(encoding='utf-8-sig'));service=json.loads(Path(service_path).read_text(encoding='utf-8-sig'));root=Path(service['data_root'])
    app=Flask(__name__);app.config.update(SECRET_KEY=cfg['session_secret'],SESSION_COOKIE_SECURE=True,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',PERMANENT_SESSION_LIFETIME=365*24*3600)
    slots=threading.BoundedSemaphore(4)
    if opener is None:
        context=ssl.create_default_context(cafile=service['ca_file']);opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context)).open
        from direct_transport import NodeTransport
        opener=NodeTransport(service['nodes'],opener)
    @contextmanager
    def db():
        c=sqlite3.connect('file:'+str(root/'center.sqlite3')+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        try:yield c
        finally:c.close()
    @app.before_request
    def protect():
        if request.path=='/health':return
        if request.method not in ('GET','HEAD'):abort(405)
        if not session.get('signed_in'):abort(401,'请先在中心登录，再重新打开下载入口')
    access_control.register(app,db,config_path,cfg)
    @app.after_request
    def secure(r):
        r.headers['Cache-Control']='private, no-store';r.headers['Referrer-Policy']='no-referrer';r.headers['X-Content-Type-Options']='nosniff';r.headers['X-Frame-Options']='DENY'
        return r
    for code in (400,401,403,404,409,412,416,503):
        app.register_error_handler(code,lambda e:(jsonify(error=e.description),e.code))
    def job(jid):
        with db() as c:j=c.execute('SELECT id,node,lease,status,execution_stopped,owner FROM jobs WHERE id=?',(jid,)).fetchone()
        if j is None or not j['node'] or not j['lease']:abort(409,'任务尚未产生节点结果')
        if not j['execution_stopped'] and j['status'] not in ('succeeded','failed','cancelled'):abort(409,'计算尚未确认结束')
        n=service['nodes'].get(j['node'])
        if not n:abort(409,'节点结果连接尚未配置')
        return dict(j),n
    def locate(jid):
        j,n=job(jid);cap=dict(node=j['node'],jid=jid,lease_digest=hashlib.sha256(j['lease'].encode()).hexdigest(),kind='locate')
        ticket=issue(n['key'],cap)
        try:
            with opener(n['url']+'/meta/'+jid+'?ticket='+ticket,timeout=10) as r:
                if r.status!=200:abort(409,'节点结果尚未就绪')
                meta=json.loads(r.read(65536))
        except (OSError,ValueError,urllib.error.HTTPError):abort(409,'节点结果尚未就绪或连接暂不可用；现有回传继续保留')
        if type(meta.get('size')) is not int or type(meta.get('mtime_ns')) is not int or meta['size']<=0:abort(409,'节点结果元数据无效')
        read_ticket=issue(n['key'],dict(cap,kind='read',identity=meta))
        endpoint=opener.endpoint(n) if hasattr(opener,'endpoint') else n['url']
        return j,n,meta,endpoint+'/result/'+jid+'?ticket='+read_ticket
    @app.get('/health')
    def health():return jsonify(ok=True,direct_download_protocol=1,direct_download_revision='2026-10-23-direct-v1',nodes=list(service['nodes']))
    def metadata(jid):
        j,n,meta,url=locate(jid)
        return jsonify(node=j['node'],size=meta['size'],etag=etag(meta),url=url,expires_in=300,resume='Range + If-Range; refresh capability after expiration',relay=request.path.replace('/meta','/result'))
    def landing(jid):
        job(jid)  # Authorization and execution gate before serving a download page.
        return render_template_string(PAGE)
    def result(jid):
        j,n,meta,url=locate(jid)
        if not slots.acquire(blocking=False):abort(503,'下载连接繁忙，请稍后续传')
        try:
            headers={k:request.headers[k] for k in ('Range','If-Range','If-Match','If-None-Match') if k in request.headers}
            req=urllib.request.Request(url,headers=headers,method=request.method)
            try:source=opener(req,timeout=60)
            except urllib.error.HTTPError as e:source=e
            if source.status not in (200,206,304,412,416):source.close();abort(409,'节点结果暂不可读，请稍后续传')
            status=source.status;response_headers={k:v for k,v in source.headers.items() if k.lower() in ('content-length','content-range','content-type','content-disposition','etag','last-modified','accept-ranges')}
            if request.method=='HEAD' or status not in (200,206):source.close();slots.release();return Response(status=status,headers=response_headers)
        except BaseException:
            slots.release();raise
        closed=False
        close_lock=threading.Lock()
        def cleanup():
            nonlocal closed
            with close_lock:
                if not closed:
                    closed=True;source.close();slots.release()
        def generate():
            try:
                while True:
                    block=source.read(1024**2)
                    if not block:break
                    yield block
            finally:cleanup()
        response=Response(generate(),status=status,headers=response_headers)
        # Werkzeug closes an iterable even if the client disconnects before its first read.
        response.call_on_close(cleanup)
        return response
    for prefix in ('','/direct'):
        for name,view in [('meta',metadata),('download',landing),('result',result)]:
            app.add_url_rule(prefix+'/api/jobs/<jid>/'+name,endpoint=prefix+name,view_func=view,methods=['GET','HEAD'])
    return app


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--service',required=True);a=p.parse_args();cfg=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));s=json.loads(Path(a.service).read_text(encoding='utf-8-sig'))
    from cheroot.wsgi import Server
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    server=Server((s.get('bind','0.0.0.0'),s.get('port',8445)),create_app(a.config,a.service),numthreads=8)
    server.ssl_adapter=BuiltinSSLAdapter(cfg['certificate'],cfg['private_key']);server.start()
if __name__=='__main__':main()
