"""Publish authenticated node execution receipts verbatim, without AI or result review."""
import json
from execution_contract import received_status, utf8_limit

REQUEST_ID='system:native-exit-v1'


def native_report(job_id, report):
    if not isinstance(report,dict) or received_status('succeeded',report)[1]:return None
    state,_=received_status('succeeded',report)
    if state!='failed':return None
    outcome=report.get('execution_outcome')
    if not isinstance(outcome,dict):outcome=report.get('failure_evidence',{})
    if not isinstance(outcome,dict):outcome={}
    evidence=[]
    items=outcome.get('evidence',report.get('evidence',[]))
    if isinstance(items,list):
        for item in items[:4]:
            if not isinstance(item,dict):continue
            evidence.append(dict(source=utf8_limit(item.get('source',''),240),message=utf8_limit(item.get('message',''),600),context=utf8_limit(item.get('context',''),1200)))
    native=outcome.get('native_exit_code',report.get('native_exit_code',report['exit_code']))
    if type(native) is not int:native=None
    return dict(schema_version=1,source='node_runner',job_id=job_id,status='failed',native_exit_code=native,
                exit_code=report['exit_code'],detail=utf8_limit(report.get('detail',''),1000),
                failure_code=utf8_limit(report.get('failure_code',''),160),evidence=evidence,
                execution_started=report.get('started'),
                result_url='/api/jobs/'+job_id+'/result',ai_generated=False)


def publish(c, job_id, report, now):
    data=native_report(job_id,report)
    if data is None:return
    serialized=json.dumps(data,ensure_ascii=False,indent=2)
    key=REQUEST_ID
    previous=c.execute('SELECT reply FROM job_feedback WHERE job=? AND request_id=?',(job_id,key)).fetchone()
    if previous:
        try:
            if json.loads(previous['reply'])==data:return
        except (ValueError,TypeError):pass
        import hashlib
        key+=':'+hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:24]
    c.execute('INSERT OR IGNORE INTO job_feedback(job,request_id,message,created,reply,replied_at) VALUES(?,?,?,?,?,?)',
              (job_id,key,'节点原始错误回执（自动记录，未经 AI 改写）',now,serialized,now))


def read(rows):
    for row in reversed(rows):
        if row['request_id']==REQUEST_ID or row['request_id'].startswith(REQUEST_ID+':'):
            try:return json.loads(row['reply'])
            except (ValueError,TypeError):return None
    return None


def ensure_archived_receipt(c, job_id, now):
    """Recover legacy platform exit metadata, never scientific output content."""
    from pathlib import Path
    import zipfile
    row=c.execute('SELECT status,execution_stopped,result,finished FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row or row['status']!='failed' or not row['execution_stopped'] or not row['result']:return False
    previous=c.execute("SELECT replied_at,reply FROM job_feedback WHERE job=? AND (request_id=? OR request_id LIKE ?) ORDER BY id DESC LIMIT 1",(job_id,REQUEST_ID,REQUEST_ID+':%')).fetchone()
    previous_data=None
    if previous:
        try:previous_data=json.loads(previous['reply'])
        except (ValueError,TypeError):pass
    if previous_data and previous_data.get('evidence') and row['finished'] and previous['replied_at']>=row['finished']:return False
    try:
        with zipfile.ZipFile(Path(row['result'])) as archive:
            def metadata(name):
                item=archive.getinfo(name)
                if item.file_size>16384:raise ValueError('Oversized platform receipt')
                return json.loads(archive.read(item))
            report=metadata('exit.json')
            if not isinstance(report,dict):return False
            try:report['execution_outcome']=metadata('execution-outcome.json')
            except KeyError:
                if isinstance(report.get('failure_evidence'),dict):
                    report['execution_outcome']=report['failure_evidence']
    except (OSError,ValueError,TypeError,KeyError,zipfile.BadZipFile):return False
    data=native_report(job_id,report)
    if data is None:return False
    if previous_data and row['finished'] and previous['replied_at']>=row['finished']:
        # Enrich only this exact execution, never resurrect an older archive.
        if not data.get('evidence') or data.get('execution_started') is None or data['execution_started']!=previous_data.get('execution_started'):return False
    before=c.total_changes
    publish(c,job_id,report,now)
    return c.total_changes>before
