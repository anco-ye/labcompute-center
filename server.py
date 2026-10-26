"""Lab compute controller. SQLite is authoritative; workers pull one leased job."""
import compute_only
import completion_estimates
import environment_snapshots
import management_policy
import task_progress
import task_activity
import external_targets
import access_control
import argparse
import io
import hashlib
import re
import hmac
import ipaddress
import json
import os
import math
import statistics
import scheduler
import batches
import result_downloads
import task_health
import handoff_resolution
import recovery_links
import resource_policy
import resource_metadata
import resource_sampling
import issue_actions
import execution_feedback
import release_identity
from legacy_terminal import merge_terminal_evidence, reconcile_shared_admission_hold
from pathlib import Path
import secrets
import sqlite3
import threading
import tempfile
import shutil
import time
import uuid
import zipfile

from flask import Flask, g, abort, jsonify, redirect, render_template, request, send_file, session
from werkzeug.security import check_password_hash
from common import atomic_json, inspect_zip, task_spec, package_specs, PLATFORM_RELEASE
from execution_contract import received_status

ACTIVE = ('leased', 'preparing', 'running', 'uploading', 'cancel_requested', 'recovery_needed', 'pause_requested', 'paused', 'resume_requested')


def ui_fingerprint(base):
    """Bind a loaded page to its actual renderer, styles and templates."""
    digest = hashlib.sha256()
    for name in ('static/app.js', 'static/style.css', 'static/brand.css', 'templates/index.html', 'templates/upload.html', 'templates/auth_base.html', 'templates/login.html'):
        digest.update(name.encode())
        digest.update((Path(base) / name).read_bytes())
    return digest.hexdigest()[:20]


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:return super().__exit__(*args)
        finally:self.close()


def client_address(environ, trusted_proxies=()):
    """Use only the last proxy-appended address from an explicitly trusted peer."""
    peer = environ.get('REMOTE_ADDR', '')
    if peer in trusted_proxies:
        forwarded = environ.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def create_app(config_path):
    loaded_release=release_identity.snapshot(Path(__file__).parent,'web')
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
    root = Path(cfg['data_dir']).resolve()
    upload_locks={}
    upload_locks_guard=threading.Lock()

    def upload_lock(jid):
        with upload_locks_guard:return upload_locks.setdefault(jid,threading.Lock())
    root.mkdir(parents=True, exist_ok=True)
    for name in ('packages', 'results', 'logs'):
        (root / name).mkdir(exist_ok=True)
    app = Flask(__name__)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config.update(SECRET_KEY=cfg['session_secret'], MAX_CONTENT_LENGTH=cfg.get('max_upload_mb', 2048)*1024**2,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=cfg.get('secure_cookie', True),
                      SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=365*24*60*60, SESSION_REFRESH_EACH_REQUEST=True)
    lock = threading.Lock()
    attempts = {}
    batch_archives = result_downloads.BatchArchives(root / 'batch-downloads')
    completion_ranges = completion_estimates.CompletionEstimates(root / 'completion-estimates.json')

    @app.context_processor
    def ui_context():
        return {'ui_version': ui_fingerprint(app.root_path), 'is_admin': access_control.is_admin(), 'account_name': getattr(g, 'account', {}).get('username', '')}

    @app.get('/api/ui-version')
    def ui_version():
        return jsonify(ui_version=ui_fingerprint(app.root_path))

    def db():
        c = sqlite3.connect(root/'center.sqlite3', timeout=30, factory=Connection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT,
          enabled INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0, telemetry TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, spec TEXT NOT NULL,
          package TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          node TEXT, lease TEXT, started REAL, finished REAL, detail TEXT, result TEXT);
        CREATE INDEX IF NOT EXISTS queue_idx ON jobs(status, created);
        CREATE TABLE IF NOT EXISTS run_history(job TEXT PRIMARY KEY, profile TEXT, node TEXT,
          cores INTEGER, seconds REAL, peak_gb REAL);
        CREATE TABLE IF NOT EXISTS audit(at REAL, action TEXT, subject TEXT);
        CREATE TABLE IF NOT EXISTS submissions(key TEXT PRIMARY KEY,digest TEXT,batch TEXT,ids TEXT);
        CREATE TABLE IF NOT EXISTS task_issues(node TEXT,task TEXT,state TEXT,first_seen REAL,last_seen REAL,resolved_at REAL,data TEXT,PRIMARY KEY(node,task));
        CREATE TABLE IF NOT EXISTS result_reviews(job TEXT,report_id TEXT,payload TEXT,created REAL,PRIMARY KEY(job,report_id));
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,name TEXT,status TEXT,created REAL,updated REAL,detail TEXT);
        CREATE TABLE IF NOT EXISTS job_events(job TEXT,at REAL,status TEXT);
        CREATE INDEX IF NOT EXISTS job_events_idx ON job_events(job,at);
        CREATE TABLE IF NOT EXISTS node_updates(node TEXT PRIMARY KEY,release TEXT,restore_enabled INTEGER);
        CREATE TABLE IF NOT EXISTS science_commands(id TEXT PRIMARY KEY,node TEXT,simulation TEXT,
          action TEXT,generation TEXT,status TEXT,detail TEXT,created REAL,updated REAL);
        ''')
        columns = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        if 'control' not in columns:c.execute("ALTER TABLE jobs ADD COLUMN control TEXT NOT NULL DEFAULT 'run'")
        if 'priority' not in columns:c.execute('ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
        for n in cfg.get('nodes', []):
            c.execute('INSERT OR IGNORE INTO nodes(id,label,token_hash,enabled) VALUES(?,?,?,?)',
                      (n['id'], n['label'], n.get('token_hash'), int(n.get('enabled', False))))
        for table in ('jobs','uploads'):
            columns={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
            if 'owner' not in columns:c.execute("ALTER TABLE "+table+" ADD COLUMN owner TEXT NOT NULL DEFAULT 'legacy'")
        columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        for name,kind,default in [('delivery_state','TEXT',"'pending'"),('delivery_attempts','INTEGER','0'),('delivery_next','REAL','0'),('execution_stopped','INTEGER','0'),('compute_started','REAL','0')]:
            if name not in columns:c.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind+' NOT NULL DEFAULT '+default)
        c.execute("UPDATE jobs SET delivery_state='ready' WHERE result IS NOT NULL AND delivery_state='pending'")
        c.execute("CREATE TABLE IF NOT EXISTS job_feedback(id INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL,request_id TEXT NOT NULL,message TEXT NOT NULL,created REAL NOT NULL,reply TEXT NOT NULL DEFAULT '',replied_at REAL,UNIQUE(job,request_id))")
    app.center_db = db

    def audit(c, action, subject):
        c.execute('INSERT INTO audit VALUES(?,?,?)', (time.time(), action, subject))

    issue_actions.register_routes(app,db,audit)

    def reconcile_handoffs(c, now):
        registry=config_path.parent/'legacy-handoffs.json'
        if not registry.exists():return
        nodes=[dict(n) for n in c.execute('SELECT id,last_seen,telemetry FROM nodes')]
        for node in nodes:node['telemetry']=json.loads(node['telemetry'])
        jobs=[dict(j) for j in c.execute('SELECT id,batch_id,spec,status,result FROM jobs')]
        for job in jobs:
            job['spec']=json.loads(job['spec'])
            job['has_result']=bool(job['result'] and Path(job['result']).is_file())
        outcome=handoff_resolution.reconcile(c,registry,jobs,nodes,now)
        for item in outcome['resolved']:
            audit(c,'legacy_handoff_resolved',json.dumps(item,ensure_ascii=False))

    def scheduling_nodes(nodes):
        path=config_path.parent/'scheduling-overrides.json'
        overrides=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        for n in nodes:
            t=json.loads(n['telemetry']) if isinstance(n['telemetry'],str) else n['telemetry']
            if n['id'] in overrides:
                t=dict(t,scheduling_policy={**t.get('scheduling_policy',{}),**overrides[n['id']]})
            versions=t.get('scheduling_policy',{}).get('verified_runtime_versions',{})
            if versions:
                runtimes={k:dict(v) for k,v in t.get('runtimes',{}).items()}
                for rid,proof in versions.items():
                    runtime=runtimes.get(rid)
                    if runtime is not None and (runtime.get('executable') is None or runtime.get('executable')==proof.get('executable')):
                        runtime.update(version=proof['version'],version_verified_at=proof.get('at'))
                t=dict(t,runtimes=runtimes)
            if t.get('execution_backend')=='ssh-push' and t.get('sandbox_protocol',0)<2:
                t=dict(t,isolation_verified=False)
            storage=t.get('storage_observation',{})
            if storage and 'volumes' in storage and 'default' in storage and 0<=time.time()-storage.get('at',0)<30:
                t=dict(t,work_volumes=storage['volumes'],default_work_volume=storage['default'],storage_protocol=1)
            elif t.get('storage_protocol')==1:
                t=dict(t);t.pop('work_volumes',None);t.pop('default_work_volume',None);t.pop('storage_protocol',None)
            n['telemetry']=t
        return nodes

    learning_lock=threading.Lock()
    from learning_cache import LearningCache
    learning_cache=LearningCache()
    learning_snapshot=dict(signature=None,observations={},families={},models={})

    def refresh_learning():
        nonlocal learning_snapshot
        # A slow metadata adapter must not queue every claim/state request behind
        # it. The previous complete snapshot is conservative until publication.
        if not learning_lock.acquire(blocking=False):return
        try:
            with db() as c:
                rows=[dict(r) for r in c.execute("SELECT id,owner,spec,package,result,status FROM jobs WHERE status IN ('succeeded','queued','running') ORDER BY id")]
            # File metadata invalidates a changed receipt/package even if the SQL
            # row is unchanged. LearningCache avoids reopening unchanged ZIPs.
            learned=learning_cache.build(rows,resource_policy.final_observation,
                resource_metadata.current_model,resource_metadata.reservation_model)
            learning_snapshot=dict(signature=None,**learned)
        finally:learning_lock.release()

    def rebalance_pending(c):
        # Retired review tasks can never re-enter execution, including old retries.
        for row in c.execute("SELECT id,spec,package FROM jobs WHERE status='queued'").fetchall():
            if compute_only.review_stage(row['package'],json.loads(row['spec'])):
                c.execute("UPDATE jobs SET status='held',control='pause',detail='科学复核已停用，仅允许计算',updated=? WHERE id=? AND status='queued'",(time.time(),row['id']))
        policy=cfg.get('pending_resource_adjustment',{})
        if not policy.get('enabled'):return
        nodes=[dict(n) for n in c.execute('SELECT id,telemetry FROM nodes')]
        observations=learning_snapshot['observations'];families=learning_snapshot['families']
        for row in c.execute("SELECT id,owner,spec,package FROM jobs WHERE status='queued'").fetchall():
            original=json.loads(row['spec']);s=dict(original)
            if s.get('platforms'):continue # choose platform before comparing runtime histories
            if cfg.get('kernel_memory_limits'):s['kernel_memory_required']=True
            model=learning_snapshot['models'].get((row['package'],json.dumps(original,sort_keys=True)))
            if model:s['resource_model']=model
            revisions=set()
            for node in nodes:
                if s.get('target','auto') not in ('auto',node['id']):continue
                runtime=json.loads(node['telemetry']).get('runtimes',{}).get(s['runtime'],{})
                if runtime.get('revision'):revisions.add(runtime['revision'])
            revision=next(iter(revisions)) if len(revisions)==1 else None
            calibration=s.get('resource_calibration',{})
            if calibration.get('method') in ('bounded_startup_budget','completed_scale_envelope','completed_affine_envelope','verified_workspace_bound','completed_identical_inputs','completed_peak_cap') and not s.get('resource_retries',0):
                s.update(memory_gb=calibration['original_memory_gb'],commit_gb=calibration['original_commit_gb']);s.pop('resource_calibration',None)
            samples=observations.get((row['owner'],s.get('resource_signature'),revision),[]) if revision else []
            revised=resource_policy.calibrate(s,samples)
            if model and revision:revised=resource_policy.fit_completed(revised,families.get((row['owner'],model['family'],revision),[]))
            if revised!=original:
                changed=c.execute("UPDATE jobs SET spec=? WHERE id=? AND status='queued'",(json.dumps(revised,ensure_ascii=False),row['id'])).rowcount
                if changed:audit(c,'pending_resource_adjustment',row['id']+':'+str(original['memory_gb'])+'->'+str(revised['memory_gb']))

    def admission_jobs(c,jobs,nodes,now):
        if not cfg.get('active_resource_reservations',{}).get('enabled'):return
        samples=learning_snapshot['families']
        bynode={n['id']:n for n in nodes}
        for job in jobs:
            job.pop('admission_reservation',None)
            if job['status']!='running' or job.get('execution_stopped') or job.get('node') not in bynode:continue
            spec=scheduler.spec(job);model=learning_snapshot['models'].get((job['package'],json.dumps(spec,sort_keys=True)))
            if not model:continue
            t=scheduler.telemetry(bynode[job['node']]);usage=t.get('usages',{}).get(job['id'],{})
            fitting=dict(spec,kernel_memory_required=True) if scheduler.kernel_guard_verified(job,bynode[job['node']],now) else spec
            evidence=resource_policy.running_reservation(fitting,model,samples.get((job['owner'],model['family'],spec.get('runtime_revision')),[]),usage,now)
            if evidence:job['admission_reservation']=evidence

    def reconcile_recoveries(c,now):
        path=config_path.parent/'execution-recoveries.json'
        if not path.exists():return {}
        nodes=[dict(n) for n in c.execute('SELECT id,last_seen,telemetry FROM nodes')]
        for n in nodes:n['telemetry']=json.loads(n['telemetry'])
        jobs=[dict(j) for j in c.execute('SELECT id,batch_id,created,spec,status,result FROM jobs')]
        for j in jobs:
            j['spec']=json.loads(j['spec']);j['has_result']=bool(j['result'] and Path(j['result']).is_file())
        return recovery_links.reconcile(c,path,jobs,nodes,now)

    def event(c, jid, status):
        last=c.execute('SELECT status FROM job_events WHERE job=? ORDER BY rowid DESC LIMIT 1',(jid,)).fetchone()
        if not last or last[0]!=status:c.execute('INSERT INTO job_events VALUES(?,?,?)',(jid,time.time(),status))

    def settle_dependencies(c,batch):
        # Exhausted upstream attempts terminate blocked descendants; an automatic
        # retry inserted before this call remains the latest, nonterminal attempt.
        while True:
            rows=[dict(r) for r in c.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY created,id',(batch,))]
            for row in rows:row['spec']=json.loads(row['spec'])
            latest={r['spec']['task_key']:r for r in rows if r['spec'].get('task_key')}
            changed=False
            for row in rows:
                if row['status'] not in ('queued','held'):continue
                blocked=[key for key in row['spec'].get('depends_on',[]) if latest.get(key,{}).get('status') in ('failed','cancelled')]
                if blocked:
                    c.execute("UPDATE jobs SET status='failed',finished=?,updated=?,detail=? WHERE id=?",(time.time(),time.time(),'前序计算未完成，本项未执行：'+', '.join(blocked),row['id']))
                    event(c,row['id'],'failed');changed=True
            if not changed:break

    @app.before_request
    def protect():
        if request.path.startswith('/agent/'):
            if request.path.endswith('/finish'):request.max_content_length=cfg.get('max_result_mb',65536)*1024**2
            if request.is_json:
                request.get_json(silent=True)
            return
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not hmac.compare_digest(str(session.get('csrf', 'missing')), (request.headers.get('X-CSRF-Token') or request.form.get('csrf', ''))):
            abort(403, '请刷新页面后重试')
        if request.path in ('/login', '/register', '/health') or request.path.startswith('/static/'):
            return
        if not session.get('signed_in'):
            if request.path.startswith('/api/'):
                abort(401)
            return redirect('/login')

    access_control.register(app,db,config_path,cfg)
    import submission_documents
    submission_documents.register(app,db)

    @app.after_request
    def headers(response):
        if request.method=='GET' and request.path.startswith('/api/') and response.status_code==200 and response.is_json and not response.direct_passthrough:
            response.vary.add('Accept-Encoding')
            if request.accept_encodings.best_match(['gzip','identity'])=='gzip' and not response.headers.get('Content-Encoding'):
                data=response.get_data()
                if len(data)>=2048:
                    import gzip
                    compressed=gzip.compress(data,compresslevel=5,mtime=0)
                    if len(compressed)<len(data):
                        response.set_data(compressed)
                        response.headers['Content-Encoding']='gzip'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(401)
    @app.errorhandler(404)
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(409)
    @app.errorhandler(410)
    @app.errorhandler(413)
    def errors(e):
        return jsonify(error=str(e.description)), e.code

    @app.get('/health')
    def health():
        return jsonify(ok=True,platform_release=PLATFORM_RELEASE,dispatch_revision='2026-10-23-portable-cache-v1',native_error_protocol=1,loaded_release=release_identity.public(loaded_release))

    @app.get('/api/components')
    def components():
        if not access_control.is_admin():abort(403)
        with db() as c:
            rows=c.execute('SELECT id,telemetry,last_seen FROM nodes').fetchall()
        return jsonify(web=release_identity.status(Path(__file__).parent,loaded_release),
            nodes=[dict(id=row['id'],last_seen=row['last_seen'],
                agent_release=json.loads(row['telemetry']).get('agent_release'),
                loaded_release=json.loads(row['telemetry']).get('loaded_release'),
                transport_revision=json.loads(row['telemetry']).get('transport_revision'),
                rpc_release=json.loads(row['telemetry']).get('rpc_release'))for row in rows])

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method=='GET' and session.get('signed_in'):
            account=next((a for a in access_control.accounts(config_path,cfg) if a.get('enabled',True) and a['id']==session.get('user_id','legacy')),None)
            if account and session.get('auth_version')==hashlib.sha256(account['password_hash'].encode()).hexdigest():
                return redirect('/')
        session.setdefault('csrf', secrets.token_hex(24))
        error = ''
        if request.method == 'POST':
            ip = client_address(request.environ, cfg.get('trusted_reverse_proxy_ips', ()))
            with lock:
                recent = [t for t in attempts.get(ip, []) if t > time.time()-300]
                if len(recent) >= 10:
                    abort(403, '尝试次数过多，请 5 分钟后重试')
                recent.append(time.time())
                attempts[ip] = recent
            account=next((a for a in access_control.accounts(config_path,cfg) if a.get('enabled',True) and a['username']==request.form.get('username')),None)
            if account and check_password_hash(account['password_hash'], request.form.get('password','')):
                session.clear()
                session.update(signed_in=True,user_id=account['id'],auth_version=hashlib.sha256(account['password_hash'].encode()).hexdigest(), csrf=secrets.token_hex(24))
                session.permanent = True
                return redirect('/')
            error = '账号或密码不正确'
        return render_template('login.html', error=error, csrf=session['csrf'])

    @app.route('/register', methods=['GET','POST'])
    def register_user():
        abort(410,'账户由实验室管理员开通，请联系管理员；已有账户可正常登录')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    @app.get('/')
    def index():
        return render_template('index.html', csrf=session['csrf'], operations=False)

    @app.get('/operations')
    def operations():
        return render_template('index.html', csrf=session['csrf'], operations=True)

    @app.get('/upload')
    def upload_page():
        return render_template('upload.html', csrf=session['csrf'])

    @app.get('/api/state')
    def state():
        refresh_learning()
        now = time.time()
        with db() as c:
            rebalance_pending(c)
            nodes = [dict(n) for n in c.execute('SELECT id,label,enabled,last_seen,telemetry FROM nodes ORDER BY id')]
            updating={r[0] for r in c.execute('SELECT node FROM node_updates')}
            jobs = [dict(j) for j in c.execute('SELECT * FROM jobs WHERE batch_id IN (SELECT batch_id FROM jobs WHERE finished IS NULL) OR batch_id IN (SELECT batch_id FROM jobs WHERE finished IS NOT NULL ORDER BY finished DESC LIMIT 300) ORDER BY created DESC')]
            if request.args.get('include_archived')=='1':jobs=[dict(j) for j in c.execute('SELECT * FROM jobs ORDER BY created DESC')]
            commands=[dict(r) for r in c.execute('SELECT * FROM science_commands ORDER BY created DESC LIMIT 200')]
            c.execute("UPDATE uploads SET status='failed',detail='上传连接中断，请重新上传' WHERE status IN ('receiving','processing') AND updated<?",(now-3600,))
            uploads=[dict(r) for r in c.execute("SELECT * FROM uploads WHERE status NOT IN ('complete','cancelled') ORDER BY created LIMIT 1000")]
            recovery_map=reconcile_recoveries(c,now)
            issues=[dict(r) for r in c.execute("SELECT * FROM task_issues WHERE state='open' AND task NOT LIKE 'review:%' ORDER BY first_seen")]
            events={}
            submission_times={r['batch_id']:r['at'] for r in c.execute('SELECT batch_id,MIN(created) AS at FROM jobs GROUP BY batch_id')}
            for e in c.execute('SELECT job,at,status FROM job_events WHERE job IN ('+','.join('?' for _ in jobs)+') ORDER BY at',[j['id'] for j in jobs]):
                events.setdefault(e['job'],[]).append(dict(at=e['at'],status=e['status']))
        native_result_paths={j['id']:j.get('result') for j in jobs}
        nodes=[n for n in nodes if n['id'] not in cfg.get('hidden_nodes',[])]
        policy=management_policy.load(config_path)
        planning_jobs=[dict(j) for j in jobs]
        queue_ages={j['id']:j.get('queue_created',j['created']) for j in scheduler.dependency_jobs(planning_jobs)}
        workload=dict(active_units=sum(j['status'] in ACTIVE and not j.get('execution_stopped') for j in planning_jobs), queued_units=sum(j['status']=='queued' for j in planning_jobs))
        show_all=access_control.is_admin() and request.args.get('scope')!='mine'
        jobs=[j for j in jobs if j['id'] not in cfg.get('hidden_jobs',[]) and (show_all or j['owner']==access_control.current_id())]
        uploads=[u for u in uploads if show_all or u['owner']==access_control.current_id()]
        names={a['id']:a['username'] for a in access_control.accounts(config_path,cfg)}
        lease_digests={j['id']:hashlib.sha256((j.get('lease') or '').encode()).hexdigest() for j in jobs}
        for j in jobs:
            j.update(submitted_by=names.get(j['owner'],'已移除账户'),submitted_at=submission_times[j['batch_id']],can_set_priority=access_control.is_admin() and j['status'] in ('queued','held'))
            j['queue_created']=queue_ages[j['id']]
        for u in uploads:u['submitted_by']=names.get(u['owner'],'已移除账户')
        with db() as c:issues=[i for i in issues if access_control.visible_issue(c,i)]
        for j in jobs:
            j['spec'] = json.loads(j['spec'])
            # Task declarations only: classify historical stages without reading results.
            j['non_compute_stage'] = compute_only.review_stage(j.get('package'), j['spec'])
            status=j['status']
            j['actions']=(['start','cancel'] if status=='held' else ['start','pause','cancel'] if status=='queued'
                          else ['retry'] if status in ('failed','cancelled','succeeded')
                          else ['resume','cancel'] if status in ('paused','pause_requested')
                          else ['cancel'] if status in ('recovery_needed','uploading','cancel_requested')
                          else ['pause','cancel'])
            if status in ACTIVE and j['spec'].get('os')=='linux':j['actions']=['cancel']
            if not access_control.is_admin():
                j['actions'] = ['cancel'] if status not in ('failed','cancelled','succeeded') else []
            j['has_result'] = bool(j.get('result'))
            j['events']=events.get(j['id'],[])[-30:]
            for key in ('package', 'lease', 'result'):
                j.pop(key, None)
        for n in nodes:
            n['telemetry'] = json.loads(n['telemetry'])
            if request.args.get('include_archived')!='1':management_policy.filter_legacy(n['telemetry'],n['id'],policy)
            n['updating']=n['id'] in updating
            transferred={j['spec'].get('science_batch') for j in jobs if j['spec'].get('source')=='existing_science_queue'}
            n['telemetry']['simulations']=[s for s in n['telemetry'].get('simulations',[]) if s.get('name') not in transferred and (n['id'],recovery_links.normal(s['id'])) not in recovery_map]
            n['online'] = bool(n['last_seen'] > now-35)
            active = [j for j in jobs if j['node'] == n['id'] and j['status'] in ACTIVE and not j.get('execution_stopped')]
            n['current_jobs'] = [j['id'] for j in active]
            n['active_unit_count'] = sum(j.get('node')==n['id'] and j['status'] in ACTIVE and not j.get('execution_stopped') for j in planning_jobs)
            for sim in n['telemetry'].get('simulations',[]):
                command=next((x for x in commands if x['node']==n['id'] and x['simulation']==sim.get('control_id')),None)
                if command:sim['command']=command
        by_node = {n['id']: n for n in nodes}
        # Separate display history from the scheduler's point-duration estimate.
        duration_history = {}
        comparable_history = {}
        with db() as c:
            histories = c.execute("SELECT h.seconds,h.node,h.cores,j.owner,j.spec FROM run_history h JOIN jobs j ON j.id=h.job WHERE j.status='succeeded' ORDER BY h.rowid DESC LIMIT 2000").fetchall()
        for row in histories:
            hs = json.loads(row['spec'])
            key = (row['owner'], hs.get('profile'), row['node'], row['cores'], hs.get('runtime'), hs.get('runtime_revision'))
            values = duration_history.setdefault(key, [])
            if len(values)<20:values.append(row['seconds'])
            model = hs.get('resource_model') or {}
            if model.get('family') and model.get('features'):
                comparable_key = (row['owner'], row['node'], row['cores'], hs.get('runtime'), hs.get('runtime_revision'),
                                  model['family'], json.dumps(model['features'], sort_keys=True), model.get('coverage_duration_ms'))
                comparable = comparable_history.setdefault(comparable_key, [])
                if len(comparable)<20:comparable.append(row['seconds'])
        for j in jobs:
            s = j['spec']
            n = by_node.get(j['node'])
            j['node_online']=n['online'] if n else None
            seconds = estimate(s, j['node'])
            j['estimate_source'] = '历史实测' if seconds else '首次运行，等待进度'
            observed=n['telemetry'].get('progress_observation',{}).get('items',{}).get(j['id'],{}) if n else {}
            observed=observed if observed.get('lease_digest')==lease_digests[j['id']] else {}
            observed_fresh=bool(observed and 0<=now-observed.get('received_at',0)<35)
            if n and (n['online'] or observed_fresh):
                usage=n['telemetry'].get('usages',{}).get(j['id'],n['telemetry'].get('usage',{}))
                if j['status']=='running' and usage.get('stage')=='installing':j['stage']='installing'
                progress=observed.get('progress',{}) if observed_fresh else usage.get('progress',{})
                task_progress.apply(j,progress,now,observed.get('received_at') if observed_fresh else usage.get('at',n['last_seen']))
            if j['status'] in ACTIVE:
                # Never substitute another job's legacy aggregate usage sample.
                own_usage=n['telemetry'].get('usages',{}).get(j['id'],{}) if n else {}
                j['activity']=task_activity.summarize(j,own_usage,now,bool(n and n['online']))
            if j['status'] in ('held','paused','pause_requested'):
                j['estimate_source']='已暂停，继续后更新预计时间'
                j['estimated_seconds']=seconds
                continue
            if j['status'] in ACTIVE and n and n['online']:
                u = n['telemetry'].get('usages', {}).get(j['id'], n['telemetry'].get('usage', {}))
                elapsed = u.get('elapsed_seconds')
                if elapsed is None:elapsed = max(0,now-j['compute_started']) if j.get('compute_started') and j['status']=='running' else 0
                j['elapsed_seconds']=elapsed
                progress = j.get('progress',{})
                done, total = progress.get('completed', 0), progress.get('total', 0)
                if j.get('progress_fresh') and progress.get('scope','overall')=='overall' and total > 0 and 0 < done <= total and elapsed > 0:
                    seconds = elapsed * total / done
                    j['estimate_source'] = '实时进度'
                    j['progress_percent'] = done / total * 100
                    if done==total:j['estimate_source']='计算阶段完成，正在整理结果'
                if seconds and seconds > elapsed:
                    j['estimated_end'] = now + seconds - elapsed
                elif seconds and j['estimate_source']!='计算阶段完成，正在整理结果':
                    j['estimate_source'] = '已超过历史用时，更新中'
            j['estimated_seconds'] = seconds
            history_key = (j['owner'], s.get('profile'), j.get('node'), s.get('cores'), s.get('runtime'), s.get('runtime_revision'))
            duration_samples = duration_history.get(history_key, [])
            j['duration_history_source'] = '同主机同任务历史耗时'
            model = s.get('resource_model') or {}
            if not duration_samples and model.get('family') and model.get('features'):
                comparable_key = (j['owner'], j.get('node'), s.get('cores'), s.get('runtime'), s.get('runtime_revision'),
                                  model['family'], json.dumps(model['features'], sort_keys=True), model.get('coverage_duration_ms'))
                duration_samples = comparable_history.get(comparable_key, [])
                j['duration_history_source'] = '同主机同规模任务历史耗时'
            completion_ranges.apply(j, duration_samples, now, bool(n and n['online']))
        for n in nodes:
            for sim in n['telemetry'].get('simulations', []):
                item = dict(id='legacy:'+n['id']+':'+sim['id'], status=sim.get('lifecycle'), node=n['id'],
                            compute_started=sim.get('started', sim.get('pid')), elapsed_seconds=sim.get('elapsed_seconds'),
                            progress_fresh=sim.get('progress_state')=='fresh',
                            progress=dict(completed=sim.get('completed'), total=sim.get('total'),
                                          scope='overall' if sim.get('kind')=='network' else 'stage'))
                sim['completion_estimate'] = completion_ranges.apply(item, [], now, n['online'])
        for n in nodes:
            n['telemetry'].pop('progress_observation',None)
        scheduling_nodes(nodes)
        with db() as c:admission_jobs(c,planning_jobs,nodes,now)
        placements,waiting=scheduler.plan(nodes,planning_jobs,now,estimate)
        for j in jobs:
            if j['status']!='queued':continue
            assignment=placements.get(j['id'])
            if assignment:
                j['estimated_node']=assignment['node']
                j['waiting_reason']='已匹配可用资源，等待服务器领取'
                seconds=estimate(j['spec'],assignment['node'])
                if seconds:j.update(estimated_seconds=seconds,estimate_source='历史实测')
            else:j['waiting_reason']=waiting.get(j['id'],'等待资源')
        for n in nodes:
            n['capacity']=scheduler.budget(n,[j for j in planning_jobs if j.get('node')==n['id'] and j['status'] in ACTIVE and not j.get('execution_stopped')],now)
        # Expose actual effective holds separately from immutable execution limits.
        planned={j['id']:j for j in planning_jobs}
        for j in jobs:
            if j['status'] not in ACTIVE or j.get('execution_stopped') or j.get('node') not in by_node:continue
            item=planned[j['id']];hold=scheduler.reservation(item,by_node[j['node']],now)
            evidence=item.get('admission_reservation') or {}
            original=scheduler.spec(item)
            j['scheduling_reservation']=dict(memory_gb=hold['memory_gb'],commit_gb=hold.get('commit_gb',hold['memory_gb']),
                learned=hold['memory_gb']!=original['memory_gb'] or hold.get('commit_gb')!=original.get('commit_gb'),peak_headroom_cap=evidence.get('peak_headroom_cap'))
        for j in jobs:
            spec=j['spec'];hold=j.get('scheduling_reservation')
            active=j['status'] in ACTIVE and not j.get('execution_stopped')
            ob=spec.get('resource_observation',{});full=None
            if j['status']=='succeeded':full=resource_policy.final_observation(spec,native_result_paths.get(j['id']))
            observed=resource_policy.observed_memory(native_result_paths.get(j['id']))
            basis=(hold or {}).get('peak_headroom_cap') or {}
            j['memory_allocation']=dict(
                reservation_memory_gb=hold.get('memory_gb') if hold else spec['memory_gb'] if j['status'] in ('queued','held') or active else None,
                reservation_commit_gb=hold.get('commit_gb') if hold else spec.get('commit_gb',spec['memory_gb']) if j['status'] in ('queued','held') or active else None,
                execution_limit_memory_gb=spec['memory_gb'],execution_limit_commit_gb=spec.get('commit_gb',spec['memory_gb']),
                initial_declaration=spec.get('resource_estimate'),memory_basis=spec.get('resource_budget',{}).get('basis'),
                learned=bool((hold or {}).get('learned') or spec.get('resource_calibration')),
                reference_sample_ids=basis.get('sample_ids',[]),reference_peak_gb=basis.get('reference_peak_gb'),
                final_peak_gb=full.get('peak_gb') if full else None,
                final_peak_private_gb=full.get('peak_private_gb') if full else None,
                observed_peak_gb=observed.get('peak_gb'),observed_peak_private_gb=observed.get('peak_private_gb'),
                peak_complete=bool(full),state='completed_reference' if basis else 'estimated_or_unconfirmed',
                guarantee='Scheduling reservation is not a physical allocation or a guarantee of headroom up to the execution limit.')
        for n in nodes:
            n['telemetry'].pop('memory_guard_observation',None)
        for issue in issues:issue['data']=task_health.memory_budget_guidance(json.loads(issue['data']))
        issue_by_job={i['task'][4:]:i['data'] for i in issues if i['task'].startswith('job:')}
        for job in jobs:
            if job['id'] in issue_by_job:job['health']=issue_by_job[job['id']]
        if request.args.get('include_archived')!='1':
            jobs=[j for j in jobs if not management_policy.archived(j,policy)]
        if not access_control.is_admin():
            for n in nodes:
                original=n['telemetry']
                public={k:v for k,v in original.items() if k in ('cpu_percent','cpu_threads','cpu_model','cpu_cores','gpus','memory_total_gb','memory_available_gb','memory_percent','sampled_at')}
                for field in ('storage_volumes','disks'):
                    public[field]=[{k:v for k,v in disk.items() if k in ('id','mount','total_gb','free_gb','schedulable')} for disk in original.get(field,[])]
                inventory=original.get('host_inventory') or {}
                if inventory.get('home_storage'):
                    public['host_inventory']={'home_storage':{k:v for k,v in inventory['home_storage'].items() if k in ('path','total_gb','free_gb','used_gb')}}
                n['telemetry']=public
                n['telemetry']['simulations']=[]
        summaries=batches.summarize(jobs)
        if not access_control.is_admin():
            for batch in summaries:batch['actions']=[a for a in batch['actions'] if a=='cancel']
        return jsonify(workload=workload,account=dict(id=access_control.current_id(),admin=access_control.is_admin(),username=g.account.get('username','')),service_paused=not any(n['enabled'] and n['online'] for n in nodes),now=now, ui_version=ui_fingerprint(app.root_path), nodes=nodes, jobs=jobs, batches=summaries, uploads=uploads,issues=issues)

    @app.post('/api/jobs/<jid>/review')
    def record_result_review(jid):
        abort(410,'中心只负责计算与交付；科学复核已停用')

    @app.get('/api/jobs/<jid>/reviews')
    def result_review_history(jid):
        with db() as c:
            if not c.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():abort(404)
            rows=c.execute('SELECT * FROM result_reviews WHERE job=? ORDER BY created,rowid',(jid,)).fetchall()
        return jsonify(reviews=[dict(json.loads(r['payload']),report_id=r['report_id'],recorded_at=r['created']) for r in rows])

    @app.post('/api/jobs/<jid>/review-resolution')
    def resolve_review_with_replacement(jid):
        abort(410,'中心只负责计算与交付；科学复核已停用')

    @app.get('/api/issues')
    def issues():
        # Publishers can poll this durable work list without an AI or browser.
        with db() as c:rows=[dict(r) for r in c.execute("SELECT * FROM task_issues WHERE state='open' AND task NOT LIKE 'review:%' ORDER BY first_seen") if access_control.visible_issue(c,r)]
        for row in rows:row['data']=json.loads(row['data'])
        owner=request.args.get('owner')
        if owner:
            if owner not in ('center','publisher'):abort(400)
            rows=[row for row in rows if row['data'].get('owner')==owner]
        return jsonify(issues=rows)

    @app.get('/api/issues/reports')
    def issue_reports():
        owner=request.args.get('owner','publisher')
        if owner not in ('center','publisher','all'):abort(400)
        with db() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM task_issues WHERE state='open' AND task NOT LIKE 'review:%' ORDER BY first_seen") if access_control.visible_issue(c,r)]
            feedback={}
            for row in c.execute('SELECT report_id,first_seen,message,created FROM issue_feedback ORDER BY id'):
                feedback.setdefault((row['report_id'],row['first_seen']),[]).append(dict(message=row['message'],created=row['created']))
        reports=[]
        for row in rows:
            data=json.loads(row['data'])
            if owner!='all' and data.get('owner')!=owner:continue
            if data.get('error_report'):
                report=data['error_report']
                reports.append(dict(report,first_seen=row['first_seen'],last_seen=row['last_seen'],
                    feedback=feedback.get((report['report_id'],row['first_seen']),[])))
        content=json.dumps(dict(schema_version=1,reports=reports),ensure_ascii=False,indent=2).encode('utf-8')
        return send_file(io.BytesIO(content),mimetype='application/json',as_attachment=True,download_name='task-error-reports.json')

    def set_priority(c,rows,subject):
        if not access_control.is_admin():abort(403,'仅管理员可调整优先级')
        data=request.get_json(silent=True)
        value=data.get('priority') if isinstance(data,dict) else None
        if isinstance(value,bool) or not isinstance(value,int) or not -100<=value<=100:abort(400,'优先级须为 -100 至 100 的整数')
        eligible=[j for j in rows if j['status'] in ('queued','held')]
        if not eligible:abort(409,'没有可调整的排队任务；已启动任务保持原分配')
        for j in eligible:c.execute('UPDATE jobs SET priority=? WHERE id=?',(value,j['id']))
        audit(c,'queue_priority',json.dumps(dict(subject=subject,by=access_control.current_id(),priority=value,previous={j['id']:j['priority'] for j in eligible}),ensure_ascii=False))
        return jsonify(ok=True,priority=value,ids=[j['id'] for j in eligible],scope='unstarted_only')

    @app.post('/api/batches/<bid>/<action>')
    def batch_action(bid, action):
        if action not in ('pause', 'resume', 'cancel', 'retry','priority'):abort(404)
        now=time.time();changed=[]
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=[dict(j) for j in c.execute('SELECT * FROM jobs')]
            for j in rows:j['spec']=json.loads(j['spec'])
            batch=next((b for b in batches.summarize(rows) if b['id']==bid or any(batches.matches_id(j,bid) for j in b['attempts'])),None)
            if not batch:abort(404)
            if action=='priority':return set_priority(c,batch['units'],bid)
            for j in (batch['units'] if action=='retry' else batch['attempts']):
                status=j['status'];jid=j['id']
                if action=='retry':
                    if status not in ('failed','cancelled'):continue
                    if compute_only.review_stage(j['package'],j['spec']):abort(410,'科学复核已停用，仅允许计算')
                    if j['spec'].get('result_transfer_error'):
                        c.execute("UPDATE jobs SET status='uploading',control='run',finished=NULL,updated=?,detail='正在重新回传已有结果' WHERE id=?",(now,jid))
                        event(c,jid,'uploading');changed.append(jid);continue
                    spec=dict(j['spec']);spec['unit_id']=batches.unit_id(j);spec['retry_origin']='manual';spec['queue_origin_created']=now+len(changed)/1000000
                    for key in ('cpu_ids','gpu_ids','deadline','backfill_deadline','execution_validation'):spec.pop(key,None)
                    new=uuid.uuid4().hex
                    c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,detail,owner,priority) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (new,j['batch_id'],json.dumps(spec,ensure_ascii=False),j['package'],'queued',now+len(changed)/1000000,now,'批次重试；原记录和结果保留',j['owner'],j['priority']))
                    event(c,new,'queued');changed.append(new);continue
                if status in batches.TERMINAL:continue
                if action=='cancel':
                    target='cancelled' if status in ('queued','held') else 'cancel_requested';control='cancel'
                elif action=='pause' and status in ('queued','leased','preparing','running','resume_requested'):
                    target='held' if status=='queued' else 'pause_requested';control='pause'
                elif action=='resume' and status in ('held','paused','pause_requested'):
                    target='queued' if status=='held' else 'resume_requested';control='run'
                else:continue
                c.execute('UPDATE jobs SET status=?,control=?,updated=?,finished=?,detail=? WHERE id=?',
                          (target,control,now,now if target=='cancelled' else None,'批次操作已提交',jid))
                changed.append(jid)
                event(c,jid,target)
            audit(c,'batch_'+action,bid)
        return jsonify(ok=True,ids=changed)

    @app.post('/api/nodes/<nid>/simulations/control')
    def science_action(nid):
        refresh_learning()
        data=request.get_json() or {};key=data.get('simulation');action=data.get('action')
        if management_policy.load(config_path).get('center_only') and action!='cancel':
            abort(409,'旧调度入口已退役，请通过中心任务包提交')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            rebalance_pending(c)
            n=c.execute('SELECT * FROM nodes WHERE id=?',(nid,)).fetchone()
            if not n or n['last_seen']<time.time()-35:abort(409,'节点离线，请重连后操作旧实验')
            t=json.loads(n['telemetry'])
            sim=next((s for s in t.get('simulations',[]) if s.get('control_id')==key),None)
            if not sim or action not in sim.get('actions',[]):abort(409,'该操作当前不可用，请刷新状态')
            if c.execute("SELECT 1 FROM science_commands WHERE node=? AND simulation=? AND status='pending'",(nid,key)).fetchone():abort(409,'已有操作正在执行，请等待节点确认')
            cid=uuid.uuid4().hex;now=time.time()
            c.execute('INSERT INTO science_commands VALUES(?,?,?,?,?,?,?,?,?)',(cid,nid,key,action,sim['generation'],'pending','等待节点执行',now,now))
            audit(c,'science_'+action,nid+':'+key)
        return jsonify(ok=True,id=cid)

    @app.route('/api/jobs/<jid>/feedback',methods=['GET','POST'])
    def job_feedback(jid):
        with db() as c:
            if request.method=='POST':
                data=request.get_json() or {};message=str(data.get('message','')).strip();key=str(data.get('request_id',''))
                if not 1<=len(message)<=2000 or not 1<=len(key)<=100:abort(400,'请填写 1–2000 字反馈')
                if key.startswith('system:'):abort(400,'系统回执标识不可用于用户反馈')
                prior=c.execute('SELECT message FROM job_feedback WHERE job=? AND request_id=?',(jid,key)).fetchone()
                if prior and prior['message']!=message:abort(409,'反馈标识已使用')
                c.execute('INSERT OR IGNORE INTO job_feedback(job,request_id,message,created) VALUES(?,?,?,?)',(jid,key,message,time.time()))
            job=c.execute('SELECT spec,status FROM jobs WHERE id=?',(jid,)).fetchone()
            execution_feedback.ensure_archived_receipt(c,jid,time.time())
            rows=[dict(r) for r in c.execute('SELECT * FROM job_feedback WHERE job=? ORDER BY id',(jid,))]
        return jsonify(name=json.loads(job['spec'])['name'],status=job['status'],feedback=rows,native_error=execution_feedback.read(rows),message='反馈已记录；可在此查看平台回复。')

    @app.get('/api/feedback')
    def pending_feedback():
        if not access_control.is_admin():abort(403)
        with db() as c:rows=[dict(r) for r in c.execute("SELECT f.*,j.owner,j.spec FROM job_feedback f JOIN jobs j ON j.id=f.job WHERE f.reply='' ORDER BY f.created LIMIT 200")]
        for row in rows:row['name']=json.loads(row.pop('spec'))['name']
        return jsonify(feedback=rows)

    @app.post('/api/jobs/<jid>/feedback/<int:fid>/reply')
    def reply_feedback(jid,fid):
        if not access_control.is_admin():abort(403)
        reply=str((request.get_json() or {}).get('reply','')).strip()
        if not 1<=len(reply)<=2000:abort(400,'回复须为 1–2000 字')
        with db() as c:
            row=c.execute('SELECT request_id FROM job_feedback WHERE id=? AND job=?',(fid,jid)).fetchone()
            if row and row['request_id'].startswith('system:'):abort(409,'节点原始回执不可改写')
            if not c.execute('UPDATE job_feedback SET reply=?,replied_at=? WHERE id=? AND job=?',(reply,time.time(),fid,jid)).rowcount:abort(404)
        return jsonify(ok=True)

    @app.get('/api/batches/<bid>/result')
    def batch_result(bid):
        with db() as c:rows=[dict(r) for r in c.execute('SELECT * FROM jobs')]
        for row in rows:row['spec']=json.loads(row['spec'])
        rows=batches.current_results([r for r in rows if batches.matches_id(r,bid)])
        if not rows:abort(404,'结果尚未就绪')
        try:
            output,etag=batch_archives.archive(rows)
            response=send_file(output,mimetype='application/zip',as_attachment=True,
                               download_name='batch-'+bid+'.zip',conditional=True,etag=etag)
            return response
        except OSError as error:
            app.logger.warning('Batch archive preparation failed: %s',error)
            abort(503,'结果打包暂未完成，请稍后重试或分别下载计算单元结果')

    @app.post('/api/uploads')
    def begin_upload():
        data=request.get_json() or {};name=str(data.get('name',''))[:200]
        if not name.lower().endswith('.zip'):abort(400,'请上传 ZIP 任务包')
        uid=uuid.uuid4().hex;now=time.time()
        with db() as c:
            c.execute('INSERT INTO uploads(id,name,status,created,updated,detail,owner) VALUES(?,?,?,?,?,?,?)',(uid,name,'receiving',now,now,'上传中',access_control.current_id()))
        return jsonify(id=uid),201

    @app.post('/api/uploads/<uid>/cancel')
    def cancel_upload(uid):
        with db() as c:
            changed=c.execute("UPDATE uploads SET status='cancelled',updated=?,detail='上传已取消' WHERE id=? AND status IN ('receiving','processing','failed')",(time.time(),uid)).rowcount
        if not changed:abort(409,'上传已结束')
        return jsonify(ok=True)

    @app.post('/api/uploads/<uid>/progress')
    def upload_progress(uid):
        data=request.get_json() or {};percent=data.get('percent')
        if isinstance(percent,bool) or not isinstance(percent,(int,float)) or not math.isfinite(percent):abort(400)
        percent=max(0,min(100,int(percent)))
        with db() as c:
            c.execute("UPDATE uploads SET updated=?,detail=? WHERE id=? AND status IN ('receiving','processing')",
                      (time.time(),'上传中 · '+str(percent)+'%' if percent<100 else '上传完成，正在校验任务包',uid))
        return jsonify(ok=True)

    @app.post('/api/uploads/<uid>/failed')
    def failed_upload(uid):
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',updated=?,detail='上传连接中断，请重新上传' WHERE id=? AND status IN ('receiving','processing')",(time.time(),uid))
        return jsonify(ok=True)

    @app.post('/api/jobs')
    @app.post('/api/uploads/<uid>/content')
    def submit(uid=None):
        # Claim a persistent upload placeholder before reading the multipart body.
        if uid:
            with db() as c:
                if not c.execute("UPDATE uploads SET status='processing',updated=? WHERE id=? AND status='receiving'",(time.time(),uid)).rowcount:
                    abort(409,'上传已提交或已取消')
        submission_key=request.headers.get('Idempotency-Key','')
        if len(submission_key)>120 or any(ord(ch)<33 or ord(ch)>126 for ch in submission_key):abort(400,'无效的提交标识')
        if submission_key:submission_key=access_control.current_id()+':'+submission_key
        batch=uuid.uuid4().hex;package=root/'packages'/f'{batch}.zip'
        try:
            upload=request.files.get('package')
            if not upload or not upload.filename.lower().endswith('.zip'):raise ValueError('请上传 ZIP 任务包')
            upload.save(package)
            files=inspect_zip(package);specs=package_specs(package,files);ids=[]
            with package.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                if uid and c.execute('SELECT status FROM uploads WHERE id=?',(uid,)).fetchone()[0]!='processing':
                    raise ValueError('上传已取消')
                if submission_key:
                    prior=c.execute('SELECT * FROM submissions WHERE key=?',(submission_key,)).fetchone()
                    if prior:
                        if prior['digest']!=digest:raise ValueError('相同提交标识对应不同输入包；请核对发布者状态')
                        if uid:c.execute("UPDATE uploads SET status='complete',updated=?,detail='已上传' WHERE id=?",(time.time(),uid))
                        package.unlink(missing_ok=True)
                        return jsonify(ids=json.loads(prior['ids']),batch_id=prior['batch'],duplicate=True),200
                known={n[0] for n in c.execute('SELECT id FROM nodes')}
                for spec in specs:
                    if spec['target']!='auto' and spec['target'] not in known:raise ValueError('目标节点不存在')
                now=time.time()
                for position,spec in enumerate(specs):
                    jid=uuid.uuid4().hex;ids.append(jid)
                    spec['unit_id']=jid
                    spec['tenant_id']=access_control.current_id()
                    spec['requires_isolation']=not access_control.is_admin()
                    if cfg.get('kernel_memory_limits'):spec['kernel_memory_required']=True
                    spec['resource_signature']=hashlib.sha256((digest+':'+str(position)).encode()).hexdigest()
                    revisions=set()
                    for node in c.execute('SELECT id,telemetry FROM nodes'):
                        if spec['target'] not in ('auto',node['id']):continue
                        runtime=json.loads(node['telemetry']).get('runtimes',{}).get(spec['runtime'],{})
                        if runtime:revisions.add(runtime.get('revision'))
                    if not spec.get('platforms') and len(revisions)==1 and next(iter(revisions)):
                        revision=next(iter(revisions));observations=[]
                        for previous in c.execute("SELECT spec,result FROM jobs WHERE owner=? AND status='succeeded'",(access_control.current_id(),)):
                            prior=json.loads(previous['spec'])
                            if prior.get('resource_signature')==spec['resource_signature'] and prior.get('runtime_revision')==revision:
                                observation=resource_policy.final_observation(prior,previous['result'])
                                if observation:observations.append(observation)
                        spec=resource_policy.calibrate(spec,observations)
                    spec.setdefault('batch_name',spec['name'] if len(specs)==1 else Path(upload.filename).stem[:120])
                    c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,owner) VALUES(?,?,?,?,?,?,?,?)',
                              (jid,batch,json.dumps(spec,ensure_ascii=False),str(package),'queued',now+position/1000000,now,access_control.current_id()))
                    event(c,jid,'queued')
                if uid:c.execute("UPDATE uploads SET status='complete',updated=?,detail='已上传' WHERE id=?",(now,uid))
                if submission_key:c.execute('INSERT INTO submissions VALUES(?,?,?,?)',(submission_key,digest,batch,json.dumps(ids)))
                audit(c,'submit_batch',batch)
        except Exception as e:
            package.unlink(missing_ok=True)
            with db() as c:
                if uid:c.execute("UPDATE uploads SET status='failed',updated=?,detail=? WHERE id=? AND status!='cancelled'",(time.time(),str(e)[:300],uid))
            if isinstance(e,(ValueError,TypeError,KeyError,OverflowError,zipfile.BadZipFile)):abort(400,str(e))
            raise
        return jsonify(ids=ids,batch_id=batch),201

    @app.post('/api/nodes/<nid>/enabled')
    def enable(nid):
        enabled = bool((request.get_json() or {}).get('enabled'))
        with db() as c:
            n = c.execute('SELECT * FROM nodes WHERE id=?',(nid,)).fetchone()
            if not n or (enabled and not n['token_hash']):
                abort(400, '该节点尚未接入')
            c.execute('UPDATE nodes SET enabled=? WHERE id=?',(int(enabled),nid))
            c.execute('UPDATE node_updates SET restore_enabled=? WHERE node=?',(int(enabled),nid))
            if c.execute('SELECT 1 FROM node_updates WHERE node=?',(nid,)).fetchone():c.execute('UPDATE nodes SET enabled=0 WHERE id=?',(nid,))
            audit(c, 'enable' if enabled else 'pause', nid)
        return jsonify(ok=True)

    @app.post('/api/nodes/<nid>/update-worker')
    def update_worker(nid):
        release=str((request.get_json() or {}).get('release',''))
        if len(release)!=64 or any(c not in '0123456789abcdef' for c in release):abort(400)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            n=c.execute('SELECT enabled FROM nodes WHERE id=?',(nid,)).fetchone()
            if not n:abort(404)
            c.execute('INSERT INTO node_updates VALUES(?,?,?) ON CONFLICT(node) DO UPDATE SET release=excluded.release',(nid,release,n['enabled']))
            c.execute('UPDATE nodes SET enabled=0 WHERE id=?',(nid,))
            audit(c,'worker_update_requested',nid)
        return jsonify(ok=True)

    @app.get('/agent/<nid>/update-status')
    def worker_update_status(nid):
        agent(nid)
        with db() as c:
            pending=c.execute('SELECT release FROM node_updates WHERE node=?',(nid,)).fetchone()
            active=c.execute('SELECT COUNT(*) FROM jobs WHERE node=? AND finished IS NULL AND status NOT IN (\'queued\',\'held\')',(nid,)).fetchone()[0]
        return jsonify(release=pending[0] if pending else None,active=active)

    @app.post('/api/jobs/<jid>/cancel')
    def cancel(jid):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            j = c.execute('SELECT status,batch_id FROM jobs WHERE id=?',(jid,)).fetchone()
            if not j:
                abort(404)
            if j['status']=='cancelled':return jsonify(ok=True,already_cancelled=True)
            status = 'cancelled' if j['status'] in ('queued','held') else 'cancel_requested'
            if j['status'] not in (*ACTIVE, 'queued','held'):
                abort(409, '任务已经结束')
            c.execute("UPDATE jobs SET status=?,control='cancel',updated=?,finished=?,detail=? WHERE id=?",(status,time.time(),time.time() if status=='cancelled' else None,'已取消' if status=='cancelled' else '等待节点确认取消；离线时将在重连后执行',jid))
            settle_dependencies(c,j['batch_id'])
            audit(c, 'cancel', jid)
            event(c,jid,status)
        return jsonify(ok=True)

    @app.post('/api/jobs/<jid>/<action>')
    def job_action(jid,action):
        if action not in ('start','pause','resume','retry','priority'):abort(404)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            j=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not j:abort(404)
            if action=='priority':return set_priority(c,[j],jid)
            status=j['status'];now=time.time()
            if status in ACTIVE and json.loads(j['spec']).get('os')=='linux' and action in ('start','pause','resume'):abort(409,'SSH 外部计算当前支持取消；排队阶段可暂停')
            if action=='retry':
                if compute_only.review_stage(j['package'],json.loads(j['spec'])):abort(410,'科学复核已停用，仅允许计算')
                if status not in ('failed','cancelled','succeeded'):abort(409,'请先取消当前执行，等待节点确认后再重试')
                current_unit=batches.unit_id(dict(id=jid,spec=json.loads(j['spec'])))
                for other in c.execute("SELECT id,spec,status FROM jobs WHERE batch_id=? AND status NOT IN ('succeeded','failed','cancelled')",(j['batch_id'],)):
                    if batches.unit_id(dict(id=other['id'],spec=json.loads(other['spec'])))==current_unit:return jsonify(ok=True,id=other['id'],duplicate=True)
                if json.loads(j['spec']).get('result_transfer_error'):
                    c.execute("UPDATE jobs SET status='uploading',control='run',finished=NULL,updated=?,detail='正在重新回传已有结果' WHERE id=?",(now,jid))
                    event(c,jid,'uploading');audit(c,'retry_transfer',jid)
                    return jsonify(ok=True,id=jid)
                new=uuid.uuid4().hex;s=json.loads(j['spec']);s.setdefault('unit_id',s.get('science_identity') or s.get('retry_of') or jid);s['retry_origin']='manual';s['queue_origin_created']=now;s.pop('cpu_ids',None);s.pop('gpu_ids',None);s.pop('deadline',None);s.pop('backfill_deadline',None)
                s.pop('execution_validation',None)
                c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,detail,owner,priority) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (new,j['batch_id'],json.dumps(s,ensure_ascii=False),j['package'],'queued',now,now,'重新提交自 '+jid[:8],j['owner'],j['priority']))
                audit(c,'retry',jid+' -> '+new)
                event(c,new,'queued')
                return jsonify(ok=True,id=new)
            if status in ('queued','held'):
                status='held' if action=='pause' else 'queued'
                priority=j['priority']
                c.execute('UPDATE jobs SET status=?,priority=?,control=?,detail=?,updated=? WHERE id=?',
                          (status,priority,'pause' if status=='held' else 'run','队列已暂停' if status=='held' else '按原顺序等待可用资源' if action=='start' else '恢复排队',now,jid))
            elif status in ('leased','preparing','running','paused','pause_requested','resume_requested'):
                pause=action=='pause'
                c.execute('UPDATE jobs SET status=?,control=?,detail=?,updated=? WHERE id=?',
                          ('pause_requested' if pause else 'resume_requested','pause' if pause else 'run','等待节点暂停' if pause else '等待节点继续',now,jid))
            else:abort(409,'当前阶段不能暂停或继续；可取消或结束后重试')
            audit(c,action,jid)
            event(c,jid,status if status in ('queued','held') else 'pause_requested' if action=='pause' else 'resume_requested')
        return jsonify(ok=True)

    @app.get('/api/jobs/<jid>/log')
    def log(jid):
        if not all(ch in '0123456789abcdef' for ch in jid) or len(jid)!=32:
            abort(404)
        p = root/'logs'/f'{jid}.log'
        text=p.read_text(encoding='utf-8',errors='replace')[-65536:] if p.exists() else '等待任务启动…'
        with db() as c:row=c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()
        check=json.loads(row['spec']).get('execution_validation') if row else None
        if check:text+='\n\n历史执行诊断：\n'+json.dumps(check,ensure_ascii=False,indent=2)
        return jsonify(text=text)

    @app.get('/api/jobs/<jid>/result')
    def result(jid):
        with db() as c:
            j=c.execute('SELECT result FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j or not j['result']:
            abort(404)
        return send_file(j['result'], as_attachment=True, download_name=f'result-{jid[:8]}.zip')

    def agent(nid):
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        with db() as c:
            n = c.execute('SELECT * FROM nodes WHERE id=?',(nid,)).fetchone()
        if not n or not n['token_hash'] or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), n['token_hash']):
            abort(401)
        return n

    def owned(c,nid,jid):
        j = c.execute('SELECT * FROM jobs WHERE id=? AND node=?',(jid,nid)).fetchone()
        if not j or not hmac.compare_digest(j['lease'] or '',request.headers.get('X-Lease','')):
            abort(403)
        return j

    @app.post('/agent/<nid>/storage')
    def storage_inventory(nid):
        agent(nid);data=request.get_json() or {};volumes=data.get('volumes',[])
        if len(json.dumps(data))>65536:abort(400)
        if not 1<=len(volumes)<=26:abort(400)
        seen=set()
        for v in volumes:
            if (not isinstance(v,dict) or not isinstance(v.get('id'),str) or len(v['id'])!=1
                    or not 'A'<=v['id']<='Z' or v['id'] in seen):abort(400)
            seen.add(v['id'])
            for key in ('free_gb','total_gb','reserve_gb'):
                value=v.get(key)
                if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<0:abort(400)
        if data.get('default') not in seen:abort(400)
        with db() as c:
            t=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            owned_ids={j['id'] for j in c.execute('SELECT id FROM jobs WHERE node=? AND execution_stopped=0 AND status IN ('+','.join('?' for _ in ACTIVE)+')',(nid,*ACTIVE))}
            managed=[]
            if not isinstance(data.get('managed',[]),list):abort(400)
            for p in data.get('managed',[])[:400]:
                if (isinstance(p,dict) and p.get('job') in owned_ids and isinstance(p.get('pid'),int) and not isinstance(p['pid'],bool) and p['pid']>0
                        and isinstance(p.get('created'),(int,float)) and math.isfinite(p['created'])):managed.append(p)
            usage=data.get('disk_usage',{})
            if not isinstance(usage,dict):abort(400)
            disk_usage={}
            for jid,row in usage.items():
                if jid not in owned_ids or not isinstance(row,dict):continue
                value=row.get('allocated_gb');age=row.get('at')
                if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 and isinstance(age,(int,float)) and not isinstance(age,bool) and math.isfinite(age) and 0<=time.time()-age<60 and row.get('verified') is True:disk_usage[jid]=dict(allocated_gb=value,at=age,verified=True)
            t['storage_observation']=dict(at=time.time(),volumes=volumes,default=data['default'],managed=managed,disk_usage=disk_usage)
            c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(t),nid))
            verified=data.get('prelaunch_failures',[])
            if not isinstance(verified,list) or len(verified)>100:abort(400)
            for jid in verified:
                if not isinstance(jid,str):abort(400)
                j=c.execute("SELECT * FROM jobs WHERE id=? AND node=? AND status='failed' AND compute_started=0 AND control='run' AND detail='File is not a zip file'",(jid,nid)).fetchone()
                if not j:continue
                spec=json.loads(j['spec']);unit=batches.unit_id(dict(id=jid,spec=spec))
                siblings=c.execute('SELECT id,spec,created FROM jobs WHERE batch_id=? AND id!=?',(j['batch_id'],jid)).fetchall()
                if any(p['created']>=j['created'] and batches.unit_id(dict(id=p['id'],spec=json.loads(p['spec'])))==unit for p in siblings):continue
                report=dict(failure_code='environment_setup',failure_evidence=dict(phase='preparing',incomplete_package=True,no_execution_created=True),cleanup_verified=True)
                revised=resource_policy.recovery(spec,report,cfg.get('resource_recovery',{}))
                if not revised:continue
                c.execute('UPDATE jobs SET execution_stopped=1 WHERE id=?',(jid,))
                revised.setdefault('unit_id',unit);new=uuid.uuid4().hex;now=time.time()
                c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,detail,owner,priority) VALUES(?,?,?,?,?,?,?,?,?,?)',(new,j['batch_id'],json.dumps(revised,ensure_ascii=False),j['package'],'queued',now,now,'中心已修复提交包传输中断，等待重新准备',j['owner'],j['priority']))
                event(c,new,'queued');audit(c,'verified_prelaunch_transfer_retry',jid+' -> '+new)
            failed_prelaunch=[dict(id=j['id']) for j in c.execute("SELECT id FROM jobs WHERE node=? AND status='failed' AND compute_started=0 AND control='run' AND detail='File is not a zip file' ORDER BY created DESC LIMIT 100",(nid,))]
            jobs=[]
            for j in c.execute("SELECT id,spec,lease FROM jobs WHERE node=? AND status='leased'",(nid,)):
                s=json.loads(j['spec'])
                if s.get('work_layout_ready') is False:jobs.append(dict(id=j['id'],lease=j['lease'],volume=s['work_volume']))
            unstarted=[]
            for j in c.execute("SELECT id,spec,started FROM jobs WHERE node=? AND status IN ('preparing','recovery_needed') AND compute_started=0 AND control='run'",(nid,)):
                s=json.loads(j['spec'])
                if s.get('requires_isolation') and s.get('work_layout_ready') is True and time.time()-(j['started'] or time.time())>60:unstarted.append(dict(id=j['id']))
            cleanup_pending=[j[0] for j in c.execute("SELECT id FROM jobs WHERE node=? AND status='uploading' AND execution_stopped=0",(nid,))]
        return jsonify(jobs=jobs,unstarted=unstarted,failed_prelaunch=failed_prelaunch,cleanup_pending=cleanup_pending)

    @app.post('/agent/<nid>/jobs/<jid>/storage-ready')
    def storage_ready(nid,jid):
        agent(nid);data=request.get_json() or {}
        with db() as c:
            c.execute('BEGIN IMMEDIATE');j=owned(c,nid,jid);s=json.loads(j['spec'])
            if s.get('work_layout_ready') is not False:return jsonify(ok=True)
            if j['status']!='leased':return jsonify(ok=True)
            if data.get('ready') is True:
                s['work_layout_ready']=True;c.execute('UPDATE jobs SET spec=? WHERE id=?',(json.dumps(s),jid))
                audit(c,'work_layout_ready',jid+':'+s['work_volume'])
            else:
                failures=dict(s.get('storage_failures',{}));failures[nid+':'+s['work_volume']]=time.time();s['storage_failures']=failures
                s.pop('work_volume',None);s.pop('work_layout_ready',None)
                c.execute("UPDATE jobs SET spec=?,status='queued',node=NULL,lease=NULL,updated=?,detail=? WHERE id=?",(json.dumps(s),time.time(),'中心正在重新选择工作盘',jid))
                audit(c,'work_layout_retry',jid+':'+str(data.get('detail',''))[:300])
                event(c,jid,'queued')
        return jsonify(ok=True)

    def estimate(spec, nid=None):
        if not spec.get('profile'):
            return (spec.get('estimated_minutes') or 0) * 60 or None
        with db() as c:
            rows = c.execute('SELECT seconds FROM run_history WHERE profile=? AND node=? ORDER BY rowid DESC LIMIT 10',
                             (spec['profile'], nid)).fetchall() if nid else []
        # No cross-machine speed assumption: each node learns its own duration.
        return statistics.median([r[0] for r in rows]) if rows else None

    def environment_nodes():
        with db() as c:
            return scheduling_nodes([dict(n) for n in c.execute('SELECT id,label,last_seen,telemetry FROM nodes')])

    environment_publisher=environment_snapshots.Publisher(Path(app.root_path)/'static',environment_nodes,output=root/'preparation-downloads')
    app.start_environment_publisher=environment_publisher.start

    @app.get('/static/运行环境快照.json')
    def runtime_snapshot_download():
        path=root/'preparation-downloads/运行环境快照.json'
        response=send_file(path if path.is_file() else Path(app.root_path)/'static/运行环境快照.json',conditional=True)
        response.cache_control.no_cache=True
        return response

    @app.get('/static/agent-preparation-kit.zip')
    def preparation_kit_download():
        path=root/'preparation-downloads/agent-preparation-kit.zip'
        response=send_file(path if path.is_file() else Path(app.root_path)/'static/agent-preparation-kit.zip',conditional=True)
        response.cache_control.no_cache=True
        return response

    @app.get('/api/external-targets')
    def external_target_inventory():
        if not access_control.is_admin():abort(403)
        return jsonify(targets=external_targets.inventory(config_path.parent / 'external-targets.json'))

    @app.get('/api/environments')
    def environments():
        environment_publisher.request()
        return jsonify(environments=environment_snapshots.catalog(environment_nodes(),time.time()))

    @app.post('/agent/<nid>/terminal-evidence')
    def terminal_evidence(nid):
        agent(nid)
        items=(request.get_json() or {}).get('items')
        if not isinstance(items,list) or len(items)>40 or len(json.dumps(items))>48000:abort(400)
        for item in items:
            if not isinstance(item,dict):abort(400)
            if any(not isinstance(item.get(k),str) for k in ('id','name','failure_detail')):abort(400)
            if item.get('failure_code')!='memory_pressure' or not isinstance(item.get('failure_evidence'),dict):abort(400)
            if any(not isinstance(item.get(k),(int,float)) or not math.isfinite(item[k]) or item[k]<0
                   for k in ('completed','total','source_age_seconds')):abort(400)
        if len({x['id'] for x in items})!=len(items):abort(400)
        received=time.time()
        with db() as c:
            telemetry=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            merge_terminal_evidence(telemetry,dict(received_at=received,items=items),received)
            management_policy.filter_legacy(telemetry,nid,management_policy.load(config_path))
            task_health.reconcile(c,nid,telemetry,received)
            reconcile_shared_admission_hold(telemetry)
            c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(telemetry),nid))
        return jsonify(ok=True)

    @app.post('/agent/<nid>/memory-observations')
    def memory_observations(nid):
        agent(nid);data=request.get_json() or {};items=data.get('items')
        if not isinstance(items,list) or len(items)>200:abort(400)
        received=time.time();accepted={}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in items:
                if not isinstance(item,dict):abort(400)
                j=c.execute('SELECT * FROM jobs WHERE id=? AND node=?',(item.get('id'),nid)).fetchone()
                if not j or j['execution_stopped'] or j['status']!='running':continue
                if not isinstance(item.get('lease'),str) or not hmac.compare_digest(j['lease'] or '',item['lease']):continue
                s=json.loads(j['spec']);limit=int(scheduler.commit_budget(dict(s,kernel_memory_required=True))*2**30)
                pid=item.get('pid');created=item.get('created')
                if (item.get('verified') is not True or isinstance(item.get('limit_bytes'),bool) or item.get('limit_bytes')!=limit
                        or isinstance(pid,bool) or not isinstance(pid,int) or pid<=0
                        or isinstance(created,bool) or not isinstance(created,(int,float)) or not math.isfinite(created)):abort(400)
                accepted[j['id']]=dict(verified=True,limit_bytes=limit,pid=pid,created=created,
                    lease_digest=hashlib.sha256(item['lease'].encode()).hexdigest())
            t=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            t['memory_guard_observation']=dict(items=accepted,received_at=received)
            c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(t),nid))
        return jsonify(accepted=len(accepted))

    @app.post('/agent/<nid>/progress-observations')
    def progress_observations(nid):
        agent(nid);data=request.get_json() or {};items=data.get('items')
        if not isinstance(items,list) or len(items)>200:abort(400)
        received=time.time();accepted={}
        sampled=data.get('sampled_at',received)
        if isinstance(sampled,bool) or not isinstance(sampled,(int,float)) or not math.isfinite(sampled):abort(400)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in items:
                if not isinstance(item,dict):abort(400)
                j=c.execute('SELECT * FROM jobs WHERE id=? AND node=?',(item.get('id'),nid)).fetchone()
                if not j or j['execution_stopped'] or j['status'] not in ACTIVE:continue
                if not isinstance(item.get('lease'),str) or not hmac.compare_digest(j['lease'] or '',item['lease']):continue
                created=item.get('created');pid=item.get('pid')
                if isinstance(pid,bool) or not isinstance(pid,int) or pid<=0 or isinstance(created,bool) or not isinstance(created,(int,float)) or not math.isfinite(created):abort(400)
                raw=item.get('progress',{})
                try:progress=task_progress.normalize(raw) if raw else {}
                except (ValueError,TypeError,KeyError):abort(400)
                if progress:
                    stamp=raw.get('updated_at',received)
                    if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp) or stamp>sampled+1:abort(400)
                    progress.update(source=str(raw.get('source','task'))[:80],updated_at=received-(sampled-stamp))
                accepted[j['id']]=dict(progress=progress,received_at=received,pid=pid,created=created,
                    lease_digest=hashlib.sha256(item['lease'].encode()).hexdigest())
            t=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            previous=t.get('progress_observation',{}).get('items',{})
            for jid,observation in accepted.items():
                old=previous.get(jid,{})
                if (not observation['progress'] and old.get('lease_digest')==observation['lease_digest']
                        and old.get('pid')==observation['pid'] and old.get('created')==observation['created']):
                    observation['progress']=old.get('progress',{})
            t['progress_observation']=dict(items=accepted,received_at=received)
            c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(t),nid))
        return jsonify(accepted=len(accepted))

    @app.post('/agent/<nid>/controller-observations')
    def controller_observations(nid):
        agent(nid)
        payload=request.get_json() or {};items=payload.get('items')
        if not isinstance(items,list) or len(items)>40 or len(json.dumps(items))>24000:abort(400)
        if any(not isinstance(x,dict) or not isinstance(x.get('id'),str) or
               not x['id'].startswith('controller:') or not isinstance(x.get('name'),str) or
               x.get('lifecycle') not in ('queued','running','unknown','failed','succeeded','paused','cancelled') for x in items):abort(400)
        if len({x['id'] for x in items})!=len(items):abort(400)
        received=time.time()
        with db() as c:
            telemetry=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            task_health.merge_controller_observations(telemetry,dict(received_at=received,items=items),received)
            management_policy.filter_legacy(telemetry,nid,management_policy.load(config_path))
            task_health.reconcile(c,nid,telemetry,received)
            # A controller sample cannot refresh worker liveness or its resource sample.
            c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(telemetry),nid))
        return jsonify(ok=True)

    @app.post('/agent/<nid>/resource-sample')
    def resource_sample(nid):
        agent(nid)
        received=time.time()
        payload=request.get_json() or {}
        if payload=={'probe':True}:return jsonify(server_time=time.time())
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                old=c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()
                telemetry=json.loads(old[0])
                sample=resource_sampling.receive(payload,time.time(),
                    telemetry.get('independent_resource_sample'))
                resource_sampling.merge(telemetry,sample,time.time())
                # Deliberately do not update last_seen: an observer is not a Worker.
                c.execute('UPDATE nodes SET telemetry=? WHERE id=?',(json.dumps(telemetry),nid))
        except ValueError as exc:return jsonify(error=str(exc)),400
        return jsonify(ok=True)

    @app.post('/agent/<nid>/heartbeat')
    def heartbeat(nid):
        agent(nid)
        data=request.get_json() or {}
        telemetry=data.get('telemetry',{})
        if len(json.dumps(telemetry)) > 65536:
            abort(400)
        # Compare sample ages, never wall clocks from different computers.
        # Preserve stale usage ages while translating into the controller clock.
        received=time.time();sampled=telemetry.get('sampled_at')
        if isinstance(sampled,(int,float)) and math.isfinite(sampled):
            for sample in [telemetry.get('resource_observation'),*telemetry.get('usages',{}).values()]:
                if isinstance(sample,dict) and isinstance(sample.get('at'),(int,float)):
                    sample['at']=received-(sampled-sample['at'])
            telemetry['sampled_at']=received
        with db() as c:
            previous=json.loads(c.execute('SELECT telemetry FROM nodes WHERE id=?',(nid,)).fetchone()[0])
            if previous.get('storage_observation') and not any(n['id']==nid and n.get('transport')=='ssh' for n in cfg.get('nodes',[])):telemetry['storage_observation']=previous['storage_observation']
            if previous.get('progress_observation'):telemetry['progress_observation']=previous['progress_observation']
            if previous.get('memory_guard_observation'):telemetry['memory_guard_observation']=previous['memory_guard_observation']
            import storage_layout
            storage_layout.normalize_external(telemetry,previous.get('storage_observation'),received,sampled)
            if previous.get('controller_observation'):
                task_health.merge_controller_observations(telemetry,previous['controller_observation'],received)
            if previous.get('terminal_evidence'):
                merge_terminal_evidence(telemetry,previous['terminal_evidence'],received)
            resource_sampling.merge(telemetry,previous.get('independent_resource_sample'),received)
            management_policy.filter_legacy(telemetry,nid,management_policy.load(config_path))
            task_health.reconcile(c,nid,telemetry,received)
            reconcile_shared_admission_hold(telemetry,previous)
            c.execute('UPDATE nodes SET last_seen=?,telemetry=? WHERE id=?',(received,json.dumps(telemetry),nid))
            reconcile_handoffs(c,received)
            reconcile_recoveries(c,received)
            pending=c.execute('SELECT * FROM node_updates WHERE node=?',(nid,)).fetchone()
            if pending and telemetry.get('agent_release')==pending['release']:
                c.execute('UPDATE nodes SET enabled=? WHERE id=?',(pending['restore_enabled'],nid))
                c.execute('DELETE FROM node_updates WHERE node=?',(nid,))
                audit(c,'worker_update_applied',nid)
            acknowledgements=[]
            for result in data.get('science_results',[])[:50]:
                if result.get('status') not in ('succeeded','failed'):continue
                c.execute("UPDATE science_commands SET status=?,detail=?,updated=? WHERE id=? AND node=? AND status='pending'",
                          (result['status'],str(result.get('detail',''))[:1500],time.time(),result.get('id'),nid))
                acknowledgements.append(result.get('id'))
            science_commands=[dict(r) for r in c.execute("SELECT id,simulation,action,generation FROM science_commands WHERE node=? AND status='pending' ORDER BY created",(nid,))]
            cancels=[j[0] for j in c.execute("SELECT id FROM jobs WHERE node=? AND status='cancel_requested'",(nid,))]
            controls={j['id']:j['control'] for j in c.execute('SELECT id,control FROM jobs WHERE node=? AND finished IS NULL',(nid,))}
            recover=[]
            if 'active_ids' in telemetry:
                for j in c.execute("SELECT * FROM jobs WHERE node=? AND (finished IS NULL OR (status='cancelled' AND delivery_state!='ready' AND execution_stopped=1)) AND started IS NOT NULL AND status NOT IN ('queued','held','leased')",(nid,)):
                    if j['id'] not in telemetry['active_ids']:
                        recover.append(dict(id=j['id'],spec=json.loads(j['spec']),lease=j['lease'],control=j['control'],package_ref=package_ref(j)))
        return jsonify(cancel=cancels,controls=controls,science_commands=science_commands,science_ack=acknowledgements,recover=recover,server_time=time.time())

    def package_ref(job):
        info=Path(job['package']).stat()
        return job['batch_id']+'-'+str(info.st_mtime_ns)+'-'+str(info.st_size)

    @app.post('/agent/<nid>/claim')
    def claim(nid):
        agent(nid)
        refresh_learning()
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            # Learning must progress without a browser polling /api/state.
            rebalance_pending(c)
            n=c.execute('SELECT * FROM nodes WHERE id=?',(nid,)).fetchone()
            t=json.loads(n['telemetry'])
            active=c.execute('SELECT * FROM jobs WHERE node=? AND status IN ('+','.join('?' for _ in ACTIVE)+')',(nid,*ACTIVE)).fetchall()
            local_ids=t.get('active_ids',[])
            layout_pending=False
            for j in active:
                if j['status'] in ('leased','pause_requested','resume_requested','cancel_requested') and j['id'] not in local_ids and not (t.get('worker_busy') and 'active_ids' not in t):
                    pending_spec=json.loads(j['spec'])
                    if j['status']=='leased' and pending_spec.get('work_layout_ready') is False:
                        if time.time()-j['updated']>60:
                            failures=dict(pending_spec.get('storage_failures',{}));failures[nid+':'+pending_spec['work_volume']]=time.time()
                            pending_spec['storage_failures']=failures;pending_spec.pop('work_volume',None);pending_spec.pop('work_layout_ready',None)
                            c.execute("UPDATE jobs SET spec=?,status='queued',node=NULL,lease=NULL,updated=?,detail=? WHERE id=?",(json.dumps(pending_spec),time.time(),'工作盘准备超时，中心正在重新分配',j['id']))
                            audit(c,'work_layout_timeout',j['id']);event(c,j['id'],'queued')
                        else:layout_pending=True
                        continue
                    return jsonify(job=dict(id=j['id'],spec=pending_spec,lease=j['lease'],control=j['control'],package_ref=package_ref(j)))
            # A layout reservation is already being offered through work-layout.
            # Wait for its acknowledgement instead of accumulating more leases.
            if layout_pending:return jsonify(job=None)
            all_nodes=[dict(row) for row in c.execute('SELECT * FROM nodes')]
            all_jobs=[dict(row) for row in c.execute("SELECT * FROM jobs WHERE batch_id IN (SELECT batch_id FROM jobs WHERE status='queued' OR status IN ("+','.join('?' for _ in ACTIVE)+'))',ACTIVE)]
            scheduling_nodes(all_nodes)
            admission_jobs(c,all_jobs,all_nodes,time.time())
            placements,_=scheduler.plan(all_nodes,all_jobs,time.time(),estimate)
            selected=None
            # Planning is hypothetical. Commit only the one offer this worker
            # can receive (or prepare its layout for) in this request. Other
            # placements remain queued and retain priority and resource freedom.
            for jid,placement in placements.items():
                if placement['node']!=nid:continue
                s=placement['spec']
                placed=next(node for node in all_nodes if node['id']==placement['node'])
                if placed['telemetry'].get('storage_protocol')==1 and s.get('work_volume'):
                    s['work_layout_ready']=False
                s['max_minutes']=0
                s['runtime_limit_enforced']=False
                s.pop('deadline',None)
                s.pop('backfill_deadline',None)
                placed=next(node for node in all_nodes if node['id']==placement['node'])
                s['runtime_revision']=json.loads(placed['telemetry']).get('runtimes',{}).get(s['runtime'],{}).get('revision') if isinstance(placed['telemetry'],str) else placed['telemetry'].get('runtimes',{}).get(s['runtime'],{}).get('revision')
                lease=secrets.token_hex(32)
                c.execute("UPDATE jobs SET status='leased',node=?,lease=?,updated=?,spec=? WHERE id=? AND status='queued'",(placement['node'],lease,time.time(),json.dumps(s),jid))
                audit(c,'claim',jid)
                event(c,jid,'leased')
                if placement['node']==nid and selected is None and s.get('work_layout_ready') is not False:selected=dict(id=jid,spec=s,lease=lease,package_ref=package_ref(next(j for j in all_jobs if j['id']==jid)))
                break
            if selected:return jsonify(job=selected)
        return jsonify(job=None)

    @app.get('/agent/<nid>/jobs/<jid>/package')
    def package(nid,jid):
        agent(nid)
        with db() as c:
            j=owned(c,nid,jid)
        return send_file(j['package'],as_attachment=True)

    @app.get('/agent/<nid>/jobs/<jid>/dependency/<key>')
    def dependency_result(nid,jid,key):
        agent(nid)
        with db() as c:
            j=owned(c,nid,jid);dependency=json.loads(j['spec']).get('dependency_jobs',{}).get(key)
            if not dependency:abort(404)
            prior=c.execute("SELECT result FROM jobs WHERE id=? AND batch_id=? AND status='succeeded'",(dependency,j['batch_id'])).fetchone()
            if not prior or not prior['result']:abort(409,'前序结果尚未就绪')
        return send_file(prior['result'],as_attachment=True)

    @app.post('/agent/<nid>/jobs/<jid>/update')
    def update(nid,jid):
        agent(nid)
        data=request.get_json() or {}
        status=data.get('status','running')
        if status not in ('preparing','running','uploading','recovery_needed','paused'):
            abort(400)
        with db() as c:
            j=owned(c,nid,jid)
            if j['status'] in ('succeeded','failed','cancelled'):
                return jsonify(ok=True, finished=True, delivery_ready=j['delivery_state']=='ready')
            if j['status'] not in (*ACTIVE, 'recovery_needed'):
                abort(409,'任务已结束')
            detail=str(data.get('detail',''))[:2000]
            normal_order={'preparing':0,'running':1,'uploading':2}
            if (status in normal_order and j['status'] in normal_order
                    and normal_order[status]<normal_order[j['status']]):
                # A delayed heartbeat / unreadable teardown sample cannot restart
                # preparation or move result transfer back into computation.
                status=j['status'];detail=j['detail']
            if j['control']=='cancel':
                status='cancel_requested'
            elif status not in ('uploading','recovery_needed'):
                if j['control']=='pause' and status!='paused':status='pause_requested'
                elif j['control']=='run' and status=='paused':status='resume_requested'
            c.execute('UPDATE jobs SET status=?,started=COALESCE(started,?),updated=?,detail=? WHERE id=?',
                      (status,time.time(),time.time(),detail,jid))
            if status=='running':c.execute('UPDATE jobs SET compute_started=? WHERE id=? AND compute_started=0',(time.time(),jid))
            event(c,jid,status)
        if 'log' in data:
            (root/'logs'/f'{jid}.log').write_text(str(data['log'])[-65536:],encoding='utf-8')
        return jsonify(ok=True)

    @app.post('/agent/<nid>/jobs/<jid>/stopped')
    def stopped(nid,jid):
        agent(nid);data=request.get_json() or {}
        if data.get('cleanup_verified') is not True:abort(409,'尚未确认进程已清理')
        with db() as c:
            c.execute('BEGIN IMMEDIATE');j=owned(c,nid,jid)
            if j['status'] in batches.TERMINAL and j['status']!='failed':return jsonify(ok=True)
            if not (j['control']=='cancel' or j['status']=='cancelled'):
                execution_feedback.publish(c,jid,data.get('execution_report'),time.time())
            if j['status'] in batches.TERMINAL:return jsonify(ok=True)
            cancelled=j['control']=='cancel'
            status='cancelled' if cancelled else 'uploading'
            if j['execution_stopped'] and j['status']==status:return jsonify(ok=True)
            c.execute("UPDATE jobs SET status=?,finished=?,updated=?,started=COALESCE(started,updated),execution_stopped=1,detail=? WHERE id=?",(status,time.time() if cancelled else None,time.time(),'已取消；已有输出正在回传' if cancelled else '计算已结束，正在回传结果',jid))
            event(c,jid,status);settle_dependencies(c,j['batch_id']);audit(c,'execution_cleanup_confirmed',jid)
        return jsonify(ok=True)

    def upload_claim(jid,lease,limit):
        try:size=int(request.headers['X-Upload-Size'])
        except (KeyError,TypeError,ValueError):abort(400,'缺少结果长度')
        if not 0<size<=limit:
            abort(400,'结果上传身份无效')
        identity=dict(size=size,lease=lease)
        version=request.headers.get('X-Upload-Mtime-Ns')
        if version is not None:
            if not re.fullmatch(r'[0-9]{1,20}',version) or int(version)>=2**64:abort(400,'结果修改版本无效')
            identity['mtime_ns']=int(version)
        partial=root/'results'/f'{jid}.resumable.upload'
        metadata=root/'results'/f'{jid}.resumable.upload.json'
        return identity,partial,metadata

    def same_upload(previous, identity):
        # Extra fields from old agents are ignored, but never the lease binding.
        return (isinstance(previous,dict) and type(previous.get('size')) is int
                and previous['size']==identity['size'] and previous.get('lease')==identity['lease']
                and previous.get('mtime_ns')==identity.get('mtime_ns'))

    @app.route('/agent/<nid>/jobs/<jid>/finish-upload',methods=['GET','POST'])
    def finish_upload(nid,jid):
        agent(nid)
        with db() as c:j=owned(c,nid,jid)
        if j['delivery_state']=='ready':return jsonify(finished=True)
        if j['status'] not in (*ACTIVE,'cancelled'):abort(409)
        limit=min(cfg.get('max_result_mb',65536)*1024**2,(json.loads(j['spec']).get('disk_gb',4)+1)*1024**3)
        identity,partial,metadata=upload_claim(jid,j['lease'],limit)
        with upload_lock(jid):
            with db() as c:j=owned(c,nid,jid)
            if j['delivery_state']=='ready':return jsonify(finished=True)
            try:previous=json.loads(metadata.read_text(encoding='utf-8'))
            except (OSError,ValueError):previous=None
            if partial.exists() and (not same_upload(previous,identity) or partial.stat().st_size>identity['size']):
                # Never destroy a prefix whose ownership/version cannot be reused.
                retained=partial.with_name(partial.name+'.retained-'+uuid.uuid4().hex)
                partial.replace(retained)
                if metadata.exists():metadata.replace(retained.with_name(retained.name+'.json'))
            if previous!=identity or not metadata.exists():
                atomic_json(metadata,identity)
            offset=partial.stat().st_size if partial.exists() else 0
            if request.method=='GET':return jsonify(offset=offset,size=identity['size'])
            try:claimed=int(request.headers['X-Upload-Offset'])
            except (KeyError,TypeError,ValueError):abort(400,'缺少上传偏移')
            length=request.content_length
            if claimed!=offset:return jsonify(offset=offset),409
            if length is None or not 0<length<=8*1024**2 or offset+length>identity['size']:
                abort(400,'结果分块大小无效')
            if length>max(0,shutil.disk_usage(root/'results').free-1024**3):
                abort(413,'中心结果盘空间不足')
            with partial.open('ab') as output:
                remaining=length
                while remaining:
                    block=request.stream.read(min(1024**2,remaining))
                    if not block:abort(400,'结果分块传输不完整')
                    output.write(block);remaining-=len(block)
            return jsonify(offset=partial.stat().st_size)

    @app.post('/agent/<nid>/jobs/<jid>/finish')
    def finish(nid,jid):
        agent(nid)
        # Commit and chunks share ownership of the same partial file.
        with upload_lock(jid):
            return finish_locked(nid,jid)

    def finish_locked(nid,jid):
        status=request.headers.get('X-Result-Status')
        if status not in ('succeeded','failed','cancelled'):
            abort(400)
        with db() as c:
            j=owned(c,nid,jid)
        if j['status'] in ('succeeded','failed','cancelled') and not (j['status']=='cancelled' and j['delivery_state']!='ready'):
            return jsonify(ok=True)
        if j['status'] not in (*ACTIVE,'cancelled'):
            abort(409)
        result_limit=min(cfg.get('max_result_mb',65536)*1024**2,(json.loads(j['spec']).get('disk_gb',4)+1)*1024**3)
        path=root/'results'/f'{jid}.zip'
        committed=request.headers.get('X-Upload-Commit')=='1'
        metadata=None
        if committed:
            identity,temp,metadata=upload_claim(jid,j['lease'],result_limit)
            try:previous=json.loads(metadata.read_text(encoding='utf-8'))
            except (OSError,ValueError):previous=None
            if not same_upload(previous,identity) or not temp.is_file() or temp.stat().st_size!=identity['size']:
                abort(409,'结果上传尚未完成或身份已变化')
        else:
            disk_limit=max(0,shutil.disk_usage(root/'results').free-1024**3)
            temp=path.with_suffix('.'+uuid.uuid4().hex+'.upload')
            try:
                with temp.open('wb') as f:
                    total=0
                    while True:
                        chunk=request.stream.read(1024**2)
                        if not chunk:break
                        total+=len(chunk)
                        if total > result_limit or total > disk_limit:
                            abort(413,'结果超出任务磁盘预算或中心当前可用存储，中心保留原输出并重试')
                        f.write(chunk)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
        try:
            # Results are retained ZIPs, never unpacked into Windows work dirs.
            inspect_zip(temp,max_bytes=result_limit,max_files=cfg.get('max_result_files',200000),case_sensitive=True)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            abort(400,'结果包未通过校验：'+str(exc)[:200])
        detail=request.headers.get('X-Exit-Code','')[:100];exit_report={}
        try:
            with zipfile.ZipFile(temp) as z:
                if z.getinfo('exit.json').file_size<=16384:
                    exit_report=json.loads(z.read('exit.json'))
                    if not isinstance(exit_report,dict):exit_report={}
                    detail=str(exit_report.get('detail',detail))[:2000]
        except (KeyError,ValueError,TypeError):pass
        # The node runner reports application execution failures in exit.json.
        # The center transports the result and records that report; it does not
        # inspect task-specific logs or judge application output.
        status,receipt_error=received_status(status,exit_report)
        if receipt_error:
            detail=receipt_error
            exit_report=dict(exit_report,failure_code='execution_report_invalid')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            current=owned(c,nid,jid)
            if current['status'] in batches.TERMINAL and not (current['status']=='cancelled' and current['delivery_state']!='ready'):
                temp.unlink(missing_ok=True)
                return jsonify(ok=True)
            if current['control']=='cancel':status='cancelled';detail='用户取消；已有结果保留'
            if not current['execution_stopped'] and exit_report.get('cleanup_verified') is not True:
                if not committed:temp.unlink(missing_ok=True)
                abort(409,'计算进程清理待确认；中心保留资源并继续核对')
            os.replace(temp,path)
            if metadata:metadata.unlink(missing_ok=True)
            c.execute("UPDATE jobs SET status=?,finished=COALESCE(finished,?),updated=?,result=?,detail=?,delivery_state='ready' WHERE id=?",
                      (status,time.time(),time.time(),str(path),detail,jid))
            c.execute('UPDATE jobs SET execution_stopped=1 WHERE id=?',(jid,))
            if status=='failed':execution_feedback.publish(c,jid,exit_report,time.time())
            spec = json.loads(j['spec'])
            spec.pop('result_transfer_error',None)
            spec.pop('execution_validation',None)
            if status=='succeeded' and exit_report.get('cleanup_verified') is True:
                spec['resource_observation']={key:exit_report.get(key) for key in ('cleanup_verified','peak_gb','peak_private_gb','peak_complete','memory_measurement')}
                full=resource_policy.final_observation(spec,path)
                if full:spec['resource_observation']=full
                else:spec['resource_observation']['peak_complete']=False
            c.execute('UPDATE jobs SET spec=? WHERE id=?',(json.dumps(spec,ensure_ascii=False),jid))
            if status == 'succeeded' and spec.get('profile'):
                try:
                    with zipfile.ZipFile(path) as z:
                        if z.getinfo('exit.json').file_size > 16384:
                            raise ValueError('metrics too large')
                        metrics = json.loads(z.read('exit.json'))
                    seconds = float(metrics['elapsed_seconds'])
                    peak = float(metrics['peak_gb'])
                    if math.isfinite(seconds) and 0 < seconds <= 43200*60 and math.isfinite(peak) and 0 <= peak <= 1024:
                        c.execute('INSERT OR IGNORE INTO run_history VALUES(?,?,?,?,?,?)',
                                  (jid,spec['profile'],nid,spec['cores'],seconds,peak))
                except (KeyError, ValueError, TypeError, OverflowError):
                    pass
            task_health.reconcile(c,nid,dict(simulations=[dict(id='job:'+jid,name=spec.get('name',jid),
                lifecycle=status,process_state='stopped',failure_code=exit_report.get('failure_code'),
                failure_detail=detail,failure_evidence=exit_report.get('failure_evidence'),result_url='/api/jobs/'+jid+'/result',batch_id=j['batch_id'])]),time.time())
            if status=='succeeded':
                # A queued retry is not recovery; only a verified successful result
                # closes incidents from older attempts of the same logical unit.
                unit=batches.unit_id(dict(id=jid,spec=spec))
                for prior in c.execute('SELECT id,node,spec FROM jobs WHERE batch_id=? AND id!=?',(j['batch_id'],jid)).fetchall():
                    if batches.unit_id(dict(id=prior['id'],spec=json.loads(prior['spec'])))==unit:
                        c.execute("UPDATE task_issues SET state='resolved',resolved_at=?,last_seen=? WHERE node=? AND task=? AND state='open'",
                                  (time.time(),time.time(),prior['node'],'job:'+prior['id']))
            if status=='failed':
                revised=resource_policy.recovery(spec,exit_report,cfg.get('resource_recovery',{}))
                if revised:
                    revised.pop('execution_validation',None)
                    revised.setdefault('unit_id',spec.get('science_identity') or spec.get('retry_of') or jid)
                    new=uuid.uuid4().hex;now=time.time()
                    c.execute('INSERT INTO jobs(id,batch_id,spec,package,status,created,updated,detail,owner,priority) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (new,j['batch_id'],json.dumps(revised,ensure_ascii=False),j['package'],'queued',now,now,'中心已安排自动恢复，等待重新执行',j['owner'],j['priority']))
                    event(c,new,'queued');audit(c,'automatic_resource_retry',jid+' -> '+new)
            settle_dependencies(c,j['batch_id'])
            audit(c,'finish',jid)
            event(c,jid,status)
        return jsonify(ok=True)

    @app.post('/agent/<nid>/jobs/<jid>/result-error')
    def result_error(nid,jid):
        agent(nid)
        messages={'too_large':'结果包超过回传大小上限', 'invalid_archive':'结果包未通过校验', 'archive_failed':'结果打包失败，请检查设备磁盘空间或文件权限'}
        data=request.get_json() or {};kind=data.get('kind')
        if kind not in messages:abort(400)
        with db() as c:
            c.execute('BEGIN IMMEDIATE');j=owned(c,nid,jid)
            if j['delivery_state']=='ready':return jsonify(ok=True,finished=True)
            if j['status'] not in ('uploading','cancel_requested','recovery_needed','cancelled'):abort(409)
            detail='结果回传暂未完成，中心自动重试：'+messages[kind]
            spec=json.loads(j['spec']);spec['result_transfer_error']=True
            attempts=j['delivery_attempts']+1;retry_at=time.time()+min(3600,30*2**min(attempts-1,7))
            c.execute("UPDATE jobs SET delivery_state='retrying',delivery_attempts=?,delivery_next=?,updated=?,detail=?,spec=? WHERE id=?",
                      (attempts,retry_at,time.time(),detail,json.dumps(spec,ensure_ascii=False),jid))
            task_health.reconcile(c,nid,dict(simulations=[dict(id='job:'+jid,name=spec.get('name',jid),lifecycle='failed',process_state='stopped',failure_code='result_transfer_error',failure_detail=detail,batch_id=j['batch_id'])]),time.time())
            audit(c,'result_transfer_retry',jid+':'+kind)
        return jsonify(ok=True)

    app.start_external_bridges=lambda: external_targets.launch(config_path.parent / 'external-targets.json',Path(__file__).parent)
    return app


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args()
    cfg=json.loads(Path(a.config).read_text(encoding='utf-8-sig'))
    from cheroot.wsgi import Server
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    center_app=create_app(a.config)
    server=Server((cfg.get('bind','0.0.0.0'),cfg.get('port',8443)),center_app,numthreads=12)
    import download_transport
    download_transport.configure(server)
    server.ssl_adapter=BuiltinSSLAdapter(cfg['certificate'],cfg['private_key'])
    center_app.start_environment_publisher()
    center_app.start_external_bridges()
    server.start()
