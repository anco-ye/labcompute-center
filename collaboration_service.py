"""Authenticated collaborative feedback, code revisions and gated deployment."""
import argparse,hashlib,hmac,json,os,secrets,sqlite3,ssl,threading,time,urllib.request,urllib.error
from contextlib import contextmanager,closing
from pathlib import Path
from flask import Flask,request,session,g,abort,jsonify,render_template
import access_control
from common import atomic_json

SOURCE_SUFFIXES={'.py','.js','.css','.html','.md','.json','.txt','.ps1','.toml','.svg','.yml','.yaml'}
STATUSES={'open','working','resolved','closed'}
LOADED_DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

def create_app(config_path,service_path):
    config_path=Path(config_path);cfg=json.loads(config_path.read_text(encoding='utf-8-sig'));service=json.loads(Path(service_path).read_text(encoding='utf-8-sig'))
    root=Path(service['source_root']).resolve();store=Path(service['data_root']);store.mkdir(parents=True,exist_ok=True)
    started=time.time();app=Flask(__name__);app.config.update(SECRET_KEY=cfg['session_secret'],SESSION_COOKIE_SECURE=True,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',TEMPLATES_AUTO_RELOAD=True,PERMANENT_SESSION_LIFETIME=365*24*3600,MAX_CONTENT_LENGTH=2*1024**2)
    app.json.ensure_ascii=False;lock=threading.RLock()
    @contextmanager
    def db():
        c=sqlite3.connect(store/'collaboration.sqlite3',timeout=15);c.row_factory=sqlite3.Row
        try:yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    with db()as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY,title TEXT NOT NULL,message TEXT NOT NULL,job TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,owner TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1,request_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY,feedback INTEGER NOT NULL,owner TEXT NOT NULL,message TEXT NOT NULL,created REAL NOT NULL,request_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,object_id TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,owner TEXT NOT NULL,digest TEXT NOT NULL UNIQUE,auth_version TEXT NOT NULL,name TEXT NOT NULL,created REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY,path TEXT NOT NULL,base TEXT NOT NULL,content TEXT NOT NULL,owner TEXT NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',deployed REAL);''')
    def accounts():return access_control.accounts(config_path,cfg)
    def promote():
        if not service.get('full_edit'):return
        with access_control.registry_lock(config_path):
            rows=accounts();changed=False
            for a in rows:
                if a.get('enabled',True)and a.get('role')!='admin':a['role']='admin';changed=True
            if changed:atomic_json(access_control.registry_path(config_path,cfg),rows)
    app.promote_accounts=promote;app.collaboration_db=db
    def version(a):return hashlib.sha256(a['password_hash'].encode()).hexdigest()
    @app.before_request
    def authenticate():
        if request.path=='/health':return
        promote();rows=accounts();auth=request.headers.get('Authorization','');bearer=False;uid=session.get('user_id','legacy'if session.get('signed_in')else None);av=session.get('auth_version')
        if auth:
            if not auth.startswith('Bearer '):abort(401)
            digest=hashlib.sha256(auth[7:].encode()).hexdigest()
            with db()as c:token=c.execute('SELECT * FROM tokens WHERE digest=? AND revoked=0',(digest,)).fetchone()
            if token is None:abort(401)
            uid=token['owner'];av=token['auth_version'];bearer=True
        a=next((a for a in rows if a['id']==uid and a.get('enabled',True)),None)
        if not a or(av and not hmac.compare_digest(av,version(a))):abort(401)
        if not service.get('full_edit')and a.get('role')!='admin':abort(403)
        g.account=a;g.bearer=bearer
        if request.method not in ('GET','HEAD','OPTIONS')and not bearer:
            if not hmac.compare_digest(str(session.get('csrf','missing')),request.headers.get('X-CSRF-Token','')):abort(403,'需要有效CSRF令牌')
    @app.after_request
    def secure(r):
        r.headers['Cache-Control']='private, no-store';r.headers['X-Content-Type-Options']='nosniff';r.headers['Referrer-Policy']='no-referrer';r.headers['X-Frame-Options']='DENY';return r
    for status in (400,401,403,404,409,413,503):
        app.register_error_handler(status,lambda e:(jsonify(error=e.description),e.code))
    def event(c,kind,key,data):c.execute('INSERT INTO events(kind,object_id,actor,created,data)VALUES(?,?,?,?,?)',(kind,str(key),g.account['id'],time.time(),json.dumps(data,ensure_ascii=False)))
    def body():
        v=request.get_json(silent=True)
        if not isinstance(v,dict):abort(400,'需要JSON对象')
        return v
    def text(v,key,limit,default=None):
        x=v.get(key,default)
        if not isinstance(x,str)or not x.strip()or len(x)>limit:abort(400,key+'长度无效')
        return x.strip()
    def source(path):
        if not isinstance(path,str)or not path or '\\'in path or ':'in path or '\x00'in path or Path(path).is_absolute()or any(x in ('..','')for x in path.split('/')):abort(400,'源码路径无效')
        p=root/path
        if not p.resolve().is_relative_to(root)or any(x.is_symlink()for x in [p,*p.parents]if x!=root.parent):abort(400,'源码路径越界或符号链接')
        if p.suffix.lower()not in SOURCE_SUFFIXES:abort(400,'仅支持平台文本源码')
        return p
    def revision(p):return hashlib.sha256(p.read_bytes()).hexdigest()if p.is_file()else 'absent'
    def check_content(path,value):
        if not isinstance(value,str)or len(value.encode('utf-8'))>1024**2:abort(400,'源码最大1MiB')
        if Path(path).suffix=='.py':
            try:compile(value,path,'exec')
            except SyntaxError as e:abort(400,'Python语法错误：'+str(e))
        if Path(path).suffix=='.json':
            try:json.loads(value)
            except ValueError:abort(400,'JSON格式无效')
    def safe_to_deploy(path):
        # Static assets/templates affect only browser views. Backend publication
        # waits for active execution to drain; it never restarts busy services.
        if path.startswith(('static/','templates/')):return True
        center=Path(cfg['data_dir'])/'center.sqlite3'
        with closing(sqlite3.connect('file:'+str(center)+'?mode=ro',uri=True))as c:
            return c.execute("SELECT count(*) FROM jobs WHERE execution_stopped=0 AND status IN ('leased','preparing','running','paused','pause_requested','resume_requested','cancel_requested','recovery_needed')").fetchone()[0]==0
    def apply_change(c,row):
        p=source(row['path'])
        if revision(p)!=row['base']:abort(409,'源码已变化，请重新读取并合并')
        if not safe_to_deploy(row['path']):
            c.execute("UPDATE changes SET status='queued',detail=? WHERE id=?",('计算/准备仍活跃，等待安全发布；不会重启服务',row['id']));event(c,'deployment_queued',row['id'],{});return {'status':'queued','effective':False}
        directory=store/'backups'/row['id'];directory.mkdir(parents=True,exist_ok=True)
        if p.exists():(directory/'before').write_bytes(p.read_bytes())
        atomic_json(directory/'receipt.json',{'path':row['path'],'previous_revision':row['base'],'actor':g.account['id'],'at':time.time()})
        p.parent.mkdir(parents=True,exist_ok=True);stage=p.with_name(p.name+'.collaboration-new');stage.write_text(row['content'],encoding='utf-8');os.replace(stage,p)
        live=row['path'].startswith(('static/','templates/'));status='live'if live else'published_pending_activation'
        c.execute('UPDATE changes SET status=?,detail=?,deployed=? WHERE id=?',(status,'静态资源已发布'if live else'源码已发布，驻留组件仍须原安全门禁激活',time.time(),row['id']));event(c,'deployment',row['id'],{'path':row['path'],'status':status});return {'status':status,'effective':live,'revision':revision(p)}
    @app.get('/health')
    def health():return jsonify(ok=True,release='2026-10-25-collaboration-v1',loaded_release={'pid':os.getpid(),'digest':LOADED_DIGEST,'started_at':started})
    @app.get('/')
    def home():
        session.setdefault('csrf',secrets.token_hex(24));return render_template('collaboration.html',csrf=session['csrf'],username=g.account['username'])
    @app.get('/api/me')
    def me():return jsonify(id=g.account['id'],username=g.account['username'],full_edit=True,capabilities=['all_tasks','all_settings','feedback','source_edit','deploy'],source_root='platform application source')
    @app.route('/api/feedback',methods=['GET','POST'])
    def feedback():
        with db()as c:
            if request.method=='POST':
                v=body();title=text(v,'title',200);message=text(v,'message',20000);key=text(v,'request_id',100);job=v.get('job_id','')
                if not isinstance(job,str)or len(job)>100:abort(400)
                prior=c.execute('SELECT * FROM feedback WHERE request_id=?',(key,)).fetchone()
                if prior:
                    if(prior['title'],prior['message'],prior['job'])!=(title,message,job):abort(409,'幂等标识已有不同内容')
                    return jsonify(feedback=dict(prior))
                now=time.time();cur=c.execute('INSERT INTO feedback(title,message,job,status,owner,created,updated,request_id)VALUES(?,?,?,?,?,?,?,?)',(title,message,job,'open',g.account['id'],now,now,key));event(c,'feedback_created',cur.lastrowid,{'title':title})
                return jsonify(feedback=dict(c.execute('SELECT * FROM feedback WHERE id=?',(cur.lastrowid,)).fetchone())),201
            status=request.args.get('status');where=' WHERE status=?'if status else''
            rows=[dict(x)for x in c.execute('SELECT * FROM feedback'+where+' ORDER BY updated DESC LIMIT 200',(status,)if status else())]
            for row in rows:row['comments']=[dict(x)for x in c.execute('SELECT * FROM comments WHERE feedback=? ORDER BY id',(row['id'],))]
        return jsonify(feedback=rows)
    @app.patch('/api/feedback/<int:fid>')
    def edit_feedback(fid):
        v=body()
        with db()as c:
            row=c.execute('SELECT * FROM feedback WHERE id=?',(fid,)).fetchone()
            if not row:abort(404)
            if v.get('version')!=row['version']:abort(409,'反馈已更新，请刷新后合并')
            title=text(v,'title',200,row['title']);message=text(v,'message',20000,row['message']);status=v.get('status',row['status'])
            if status not in STATUSES:abort(400)
            c.execute('UPDATE feedback SET title=?,message=?,status=?,updated=?,version=version+1 WHERE id=?',(title,message,status,time.time(),fid));event(c,'feedback_updated',fid,{'status':status});return jsonify(feedback=dict(c.execute('SELECT * FROM feedback WHERE id=?',(fid,)).fetchone()))
    @app.post('/api/feedback/<int:fid>/comments')
    def comment(fid):
        v=body();message=text(v,'message',20000);key=text(v,'request_id',100)
        with db()as c:
            if not c.execute('SELECT id FROM feedback WHERE id=?',(fid,)).fetchone():abort(404)
            old=c.execute('SELECT * FROM comments WHERE request_id=?',(key,)).fetchone()
            if old:
                if old['feedback']!=fid or old['message']!=message:abort(409)
                return jsonify(comment=dict(old))
            cur=c.execute('INSERT INTO comments(feedback,owner,message,created,request_id)VALUES(?,?,?,?,?)',(fid,g.account['id'],message,time.time(),key));c.execute('UPDATE feedback SET updated=?,version=version+1 WHERE id=?',(time.time(),fid));event(c,'feedback_comment',fid,{});return jsonify(comment=dict(c.execute('SELECT * FROM comments WHERE id=?',(cur.lastrowid,)).fetchone())),201
    @app.get('/api/events')
    def events():
        try:after=max(0,int(request.args.get('after','0')))
        except ValueError:abort(400)
        with db()as c:rows=[dict(x)for x in c.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500',(after,))]
        return jsonify(events=rows,next_cursor=rows[-1]['id']if rows else after)
    @app.route('/api/tokens',methods=['GET','POST'])
    def tokens():
        if g.bearer and request.method=='POST':abort(403,'请在网页登录会话中创建Agent令牌')
        with db()as c:
            if request.method=='POST':
                name=text(body(),'name',100);token=secrets.token_urlsafe(40);tid=secrets.token_hex(12);c.execute('INSERT INTO tokens(id,owner,digest,auth_version,name,created)VALUES(?,?,?,?,?,?)',(tid,g.account['id'],hashlib.sha256(token.encode()).hexdigest(),version(g.account),name,time.time()));event(c,'token_created',tid,{});return jsonify(id=tid,token=token),201
            return jsonify(tokens=[dict(x)for x in c.execute('SELECT id,owner,name,created,revoked FROM tokens WHERE owner=?',(g.account['id'],))])
    @app.delete('/api/tokens/<tid>')
    def revoke(tid):
        with db()as c:
            if not c.execute('UPDATE tokens SET revoked=1 WHERE id=? AND owner=?',(tid,g.account['id'])).rowcount:abort(404)
            event(c,'token_revoked',tid,{})
        return jsonify(ok=True)
    @app.get('/api/files')
    def files():
        name=request.args.get('path')
        if name:
            p=source(name)
            if not p.is_file():abort(404)
            if p.stat().st_size>1024**2:abort(413)
            try:content=p.read_text(encoding='utf-8-sig')
            except UnicodeError:abort(400,'非UTF-8文本')
            return jsonify(path=name,revision=revision(p),content=content)
        folder=request.args.get('directory','');p=root/folder
        if '\\'in folder or'..'in Path(folder).parts or not p.resolve().is_relative_to(root)or p.is_symlink()or not p.is_dir():abort(400)
        return jsonify(files=[{'path':x.relative_to(root).as_posix(),'directory':x.is_dir()}for x in sorted(p.iterdir())if not x.is_symlink()and not x.name.startswith('.')and(x.is_dir()or x.suffix.lower()in SOURCE_SUFFIXES)])
    @app.route('/api/changes',methods=['GET','POST'])
    def changes():
        with db()as c:
            if request.method=='POST':
                v=body();path=text(v,'path',300);p=source(path);check_content(path,v.get('content'));base=text(v,'base_revision',100)
                if revision(p)!=base:abort(409,'源码已变化，请合并')
                cid=secrets.token_hex(16);c.execute('INSERT INTO changes(id,path,base,content,owner,created,status)VALUES(?,?,?,?,?,?,?)',(cid,path,base,v['content'],g.account['id'],time.time(),'draft'));event(c,'source_change_created',cid,{'path':path});return jsonify(id=cid,status='draft'),201
            return jsonify(changes=[dict(x)for x in c.execute('SELECT id,path,base,owner,created,status,detail,deployed FROM changes ORDER BY created DESC LIMIT 100')])
    @app.post('/api/changes/<cid>/deploy')
    def deploy(cid):
        body()  # Consume the POST entity before emitting a TLS response.
        with lock,db()as c:
            row=c.execute('SELECT * FROM changes WHERE id=?',(cid,)).fetchone()
            if not row:abort(404)
            if row['status']not in ('draft','queued'):return jsonify(status=row['status'],already_applied=True)
            return jsonify(id=cid,**apply_change(c,row))
    @app.route('/api/platform',methods=['GET','POST','PUT','PATCH','DELETE'])
    def platform():
        path=request.args.get('path','')
        if not path.startswith('/api/')or any(x in path for x in ('\\','\x00','#','..')):abort(400)
        csrf=secrets.token_hex(24);cookie=app.session_interface.get_signing_serializer(app).dumps({'signed_in':True,'user_id':g.account['id'],'auth_version':version(g.account),'csrf':csrf,'_permanent':True})
        headers={'Cookie':'session='+cookie,'X-CSRF-Token':csrf};data=None
        if request.method!='GET':data=json.dumps(body()).encode();headers['Content-Type']='application/json'
        context=ssl.create_default_context(cafile=service['ca_file']);op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context));req=urllib.request.Request(service['platform_url']+path,headers=headers,data=data,method=request.method)
        try:result=op.open(req,timeout=30)
        except urllib.error.HTTPError as e:result=e
        with result:
            content=result.read(2*1024**2+1)
            if len(content)>2*1024**2:abort(413,'此接口用于JSON编辑，结果文件请使用平台下载接口')
            if request.method!='GET':
                with db()as c:event(c,'platform_edit',path,{'status':result.status,'method':request.method})
            return app.response_class(content,status=result.status,mimetype='application/json')
    original_wsgi=app.wsgi_app
    def with_prefix(environ,start_response):
        path=environ.get('PATH_INFO','')
        if path=='/collaboration' or path.startswith('/collaboration/'):
            environ['SCRIPT_NAME']=environ.get('SCRIPT_NAME','')+'/collaboration'
            environ['PATH_INFO']=path[len('/collaboration'):] or '/'
        return original_wsgi(environ,start_response)
    app.wsgi_app=with_prefix
    return app

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--service',required=True);a=p.parse_args();cfg=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));s=json.loads(Path(a.service).read_text(encoding='utf-8-sig'));app=create_app(a.config,a.service)
    import logging
    from logging.handlers import RotatingFileHandler
    log=RotatingFileHandler(Path(s['data_root'])/'service.log',maxBytes=2*1024**2,backupCount=2,encoding='utf-8')
    logging.getLogger().addHandler(log);logging.getLogger().setLevel(logging.INFO)
    def maintain():
        while True:
            try:app.promote_accounts()
            except Exception:app.logger.exception('collaborative account synchronization failed')
            time.sleep(5)
    threading.Thread(target=maintain,daemon=True).start()
    from cheroot.wsgi import Server
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    server=Server((s.get('bind','127.0.0.1'),s.get('port',8447)),app,numthreads=8);server.ssl_adapter=BuiltinSSLAdapter(cfg['certificate'],cfg['private_key']);server.start()
if __name__=='__main__':main()
