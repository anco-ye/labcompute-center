"""Account identity and object authorization, shared by every browser/API route."""
import hashlib,json,secrets,re
from contextlib import contextmanager
from common import atomic_json
from pathlib import Path
from flask import abort,g,request,session
from werkzeug.security import check_password_hash,generate_password_hash
import batches


def registry_path(config_path,cfg=None):
    cfg=cfg if cfg is not None else json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    return Path(cfg.get('users_file',Path(config_path).parent/'users.json'))


def accounts(config_path,cfg):
    legacy={'id':'legacy','username':cfg.get('username','lab'),'password_hash':cfg['password_hash'],'role':'admin','enabled':True}
    path=registry_path(config_path,cfg)
    if not path.exists():return [legacy]
    rows=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows,list):raise ValueError('Invalid account registry')
    return rows


@contextmanager
def registry_lock(config_path):
    path=registry_path(config_path).with_suffix('.lock')
    with path.open('a+b') as stream:
        if __import__('os').name=='nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            if __import__('os').name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def create_user(config_path,cfg,username,password):
    if not re.fullmatch(r'[\w.-]{3,32}',username):
        raise ValueError('用户名须为 3–32 个字符，可用中文、字母、数字、下划线、点或短横线')
    if not password:
        raise ValueError('请输入密码')
    password_hash=generate_password_hash(password)
    with registry_lock(config_path):
        rows=accounts(config_path,cfg)
        if any(a['username'].casefold()==username.casefold() for a in rows):
            raise ValueError('该用户名已被使用，请换一个')
        account=dict(id=secrets.token_hex(16),username=username,password_hash=password_hash,role='user',enabled=True)
        rows.append(account)
        atomic_json(registry_path(config_path,cfg),rows)
    return account


def current_id():return getattr(g,'account',{}).get('id','legacy')
def is_admin():return getattr(g,'account',{}).get('role')=='admin'
def visible_issue(c,row):
    task=row['task']
    if task.startswith('review:'):return False
    if is_admin():return True
    if not task.startswith(('job:','review:')):return False
    job=c.execute('SELECT owner FROM jobs WHERE id=?',(task.split(':',1)[1],)).fetchone()
    return bool(job and job['owner']==current_id())


def register(app,db,config_path,cfg):
    @app.before_request
    def authorize():
        if request.path.startswith('/agent/') or request.path in ('/login','/register','/health') or request.path.startswith('/static/'):return
        rows=accounts(config_path,cfg)
        uid=session.get('user_id')
        # Only pre-migration sessions from the single legacy account can upgrade.
        if uid is None and session.get('signed_in'):uid='legacy'
        account=next((a for a in rows if a['id']==uid and a.get('enabled',True)),None)
        if not account:abort(401,'请重新登录')
        if session.get('auth_version') and session['auth_version']!=hashlib.sha256(account['password_hash'].encode()).hexdigest():abort(401,'会话已失效，请重新登录')
        g.account=account
        admin=is_admin();path=request.path;args=request.view_args or {}
        if request.method!='GET' and path.endswith(('/review','/review-resolution')):abort(410,'中心只负责计算与交付，不再接收科学验收回执')
        if not admin and (path=='/operations' or path.startswith('/api/nodes/') or path.endswith(('/review','/reviews','/review-resolution'))):abort(403,'仅管理员可执行此操作')
        if not admin and request.method not in ('GET','HEAD') and ('jid' in args or 'bid' in args) and not path.endswith(('/cancel','/feedback')):
            abort(403,'用户任务操作仅支持上传、下载和取消')
        with db() as c:
            for key,table in [('jid','jobs'),('uid','uploads')]:
                if key not in args:continue
                row=c.execute('SELECT owner FROM '+table+' WHERE id=?',(args[key],)).fetchone()
                if not row or (not admin and row['owner']!=current_id()):abort(404,'记录不存在')
            if 'bid' in args:
                rows=[dict(r) for r in c.execute('SELECT * FROM jobs')]
                for r in rows:r['spec']=json.loads(r['spec'])
                matched=[r for r in rows if batches.matches_id(r,args['bid'])]
                if not matched or (not admin and any(r['owner']!=current_id() for r in matched)):abort(404,'任务不存在')
            if 'report_id' in args and not admin:
                matched=[r for r in c.execute('SELECT * FROM task_issues') if json.loads(r['data']).get('error_report',{}).get('report_id')==args['report_id']]
                if len(matched)!=1 or not visible_issue(c,matched[0]):abort(404,'记录不存在')
