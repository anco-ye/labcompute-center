"""Platform execution evidence only; never evaluates scientific results."""
import codecs
import json
import re
import uuid
from collections import deque
from pathlib import Path


def received_status(requested, report):
    """Accept execution evidence from either native runner format, never outputs."""
    if (not isinstance(report,dict) or type(report.get('exit_code')) is not int
            or report.get('cleanup_verified') is not True
            or report.get('status') not in (None,'succeeded','failed','cancelled')):
        return 'failed','执行回执缺失或无效；结果已保留，中心需核对节点执行终态'
    if requested=='cancelled' or report.get('cancelled') is True or report.get('status')=='cancelled':
        return 'cancelled',None
    if requested=='failed' or report.get('status')=='failed' or report['exit_code']!=0:
        return 'failed',None
    return 'succeeded',None


def prepare(folder, kind, argv):
    """Run under the actual job identity before launching the application."""
    folder=Path(folder);work=folder/'work'
    if (work/'execution-result.json').exists():
        raise ValueError('execution-result.json 为本次执行回执，不能随输入包预置')
    probe=work/('.platform-write-'+uuid.uuid4().hex)
    try:
        probe.write_bytes(b'platform');assert probe.read_bytes()==b'platform'
    finally:
        probe.unlink(missing_ok=True)
    argv=list(argv)
    if kind=='comsol':
        prefs=folder/'comsol-prefs';prefs.mkdir(exist_ok=True)
        (prefs/'comsol.prefs').write_text('security.external.enable=on\nsecurity.external.filepermission=full\nsecurity.external.propertypermission=on\nsecurity.external.netpermission=off\nsecurity.external.socketpermission=off\nsecurity.external.runtimepermission=off\nsecurity.external.reflectpermission=off\nsecurity.external.securitypermission=off\nsecurity.external.allowexternalprocess=off\n',encoding='utf-8')
        if '-prefsdir' in argv:
            i=argv.index('-prefsdir');del argv[i:i+2]
        argv[1:1]=['-prefsdir',str(prefs)]
    return argv


def lines(path, offset=0):
    # Stream the entire execution log, including UTF-16 native COMSOL logs.
    with Path(path).open('rb') as f:
        head=f.read(2);f.seek(offset)
        encoding='utf-16-le' if head==b'\xff\xfe' else 'utf-16-be' if head==b'\xfe\xff' else 'utf-8'
        decoder=codecs.getincrementaldecoder(encoding)(errors='replace');pending=''
        while chunk:=f.read(65536):
            pending+=decoder.decode(chunk)
            parts=pending.split('\n');pending=parts.pop()
            for line in parts:yield line[:4096]
            pending=pending[-16384:]
        yield (pending+decoder.decode(b'',final=True))[:4096]


def outcome(folder, kind, exit_code, log_offset=0):
    folder=Path(folder);evidence=[];extra_logs=[]
    # Optional portable contract for wrappers which swallow child exit codes.
    receipt=folder/'work'/'execution-result.json'
    if receipt.exists():
        try:
            if receipt.stat().st_size>65536:raise ValueError('receipt too large')
            r=json.loads(receipt.read_text(encoding='utf-8'))
            if r.get('status') not in ('succeeded','failed') or type(r.get('exit_code')) is not int:raise ValueError('invalid execution receipt')
            if r['status']=='failed' or r['exit_code']!=0:evidence.append({'source':'execution-result.json','message':str(r.get('detail','程序报告失败'))[:1000]})
            declared=r.get('error_logs',[])
            if isinstance(declared,list):
                for item in declared[:4]:
                    if not isinstance(item,dict) or item.get('kind') not in ('python','comsol','matlab'):continue
                    relative=Path(str(item.get('path','')))
                    if relative.is_absolute() or '..' in relative.parts:continue
                    path=folder/'work'/relative
                    if path.resolve().is_relative_to((folder/'work').resolve()) and path.is_file():extra_logs.append((path,0,item['kind']))
        except (ValueError,OSError,AttributeError) as e:evidence.append({'source':'execution-result.json','message':str(e)[:1000]})
    paths=[(folder/'task.log',log_offset,kind)]
    if kind=='comsol':paths.append((folder/'work/comsol_batch.log',0,kind))
    paths.extend(extra_logs)
    java=re.compile(r'^(?:Exception in thread "[^\"]+"\s+)?(?:java\.[\w.]+(?:Exception|Error)|com\.comsol\.[\w.]+Exception):')
    fatal=re.compile(r'^(?:Fatal Python error:|terminate called after throwing|Segmentation fault(?:\s|$)|Aborted \(core dumped\))')
    for path,offset,log_kind in paths:
        if not path.exists():continue
        traceback=False;previous=deque(maxlen=5);pending=[]
        for line in lines(path,offset):
            for item in pending[:]:
                item['context']=utf8_limit(item['context']+'\n'+line,1200)
                item['_remaining']-=1
                if item['_remaining']<=0 or len(item['context'].encode('utf-8'))>=1200:pending.remove(item)
            s=line.strip().lstrip('\ufeff')
            if s=='Traceback (most recent call last):':traceback=True
            hit=bool(java.match(s) or fatal.match(s))
            if log_kind=='matlab' and re.match(r'^(?:Error using |Error in |错误使用 |出错 )',s):hit=True
            if traceback and re.match(r'^[\w.]+(?:Error|Exception):',s):hit=True;traceback=False
            if log_kind=='comsol' and re.match(r'^(?:Error in sweep\.|Error running java class\.|扫描错误[。.]?|运行\s*Java\s*类时出错[。.]|/\*+\s*(?:Error|错误)\s*\*+/)',s,re.I):hit=True
            if log_kind=='comsol' and re.match(r'^(?:Error:|错误[:：])\s*$',s):hit=True
            if hit:
                if len(evidence)<4:
                    item={'source':str(path.relative_to(folder)),'message':utf8_limit(s,600),'context':utf8_limit('\n'.join([*previous,line]),1200),'_remaining':12}
                    evidence.append(item);pending.append(item)
            previous.append(utf8_limit(line,240))
            if len(evidence)>=4 and not pending:break
    for item in evidence:
        item.pop('_remaining',None)
        item['message']=utf8_limit(item['message'],600)
    evidence=evidence[:4]
    failed=exit_code!=0 or bool(evidence)
    return dict(status='failed' if failed else 'succeeded',native_exit_code=exit_code,derived_exit_code=exit_code if exit_code else (1 if failed else 0),evidence=evidence,error_protocol=1)


def utf8_limit(value, limit):
    return str(value).encode('utf-8',errors='replace')[:limit].decode('utf-8',errors='ignore')
