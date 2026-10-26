"""Complete missing OS commit guards at original budgets, without restarting work.

Original kill-on-close ownership is retained; no affinity or suspension is used. Only complete
PID/creation-verified trees are advertised; loss of telemetry revokes borrowing.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
import ssl
import time
import urllib.request
from ctypes import wintypes as w
from pathlib import Path
import psutil


class Basic(ctypes.Structure):
    _fields_=[('per_process',ctypes.c_longlong),('per_job',ctypes.c_longlong),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
class IO(ctypes.Structure):
    _fields_=[(n,ctypes.c_ulonglong) for n in ('ro','wo','oo','rb','wb','ob')]
class Extended(ctypes.Structure):
    _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]


class HandleEntry(ctypes.Structure):
    _fields_=[('value',w.HANDLE),('handles',ctypes.c_size_t),('pointers',ctypes.c_size_t),('access',w.DWORD),('type',w.DWORD),('attributes',w.DWORD),('reserved',w.DWORD)]


class Guard:
    def __init__(self,name,limit):
        self.k=ctypes.WinDLL('kernel32',use_last_error=True);self.nt=ctypes.WinDLL('ntdll')
        self.k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];self.k.OpenProcess.restype=w.HANDLE
        self.k.GetCurrentProcess.restype=w.HANDLE
        self.k.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
        self.k.QueryInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
        self.k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.k.CloseHandle.argtypes=[w.HANDLE]
        self.nt.NtQueryInformationProcess.argtypes=[w.HANDLE,w.DWORD,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]
        self.nt.NtQueryInformationProcess.restype=ctypes.c_long
        self.limit=int(limit*2**30)

    def attach(self,root):
        # Reuse the runner's original JobObject. A new nested job cannot always
        # contain already-running venv redirector job hierarchies. Duplicates
        # are closed within this call so original kill-on-close ownership stays.
        identity=(root.pid,root.create_time());processes=self.payload_tree(root)
        if sum(getattr(p.memory_info(),'private',p.memory_info().rss) for p in processes)>=self.limit:
            raise RuntimeError('Current tree exceeds original budget; no change')
        if psutil.Process(root.pid).create_time()!=identity[1]:raise RuntimeError('Runner generation changed')
        owner=self.k.OpenProcess(0x400|0x40,False,root.pid)
        if not owner:raise ctypes.WinError(ctypes.get_last_error())
        try:
            size=65536;needed=w.DWORD()
            while size<=1024**2:
                buffer=ctypes.create_string_buffer(size)
                status=self.nt.NtQueryInformationProcess(owner,51,buffer,size,ctypes.byref(needed))
                if status>=0:break
                if needed.value<=size:raise RuntimeError('Runner handle snapshot unavailable')
                size=needed.value
            else:raise RuntimeError('Runner handle snapshot too large')
            count=ctypes.c_size_t.from_buffer(buffer).value;offset=2*ctypes.sizeof(ctypes.c_size_t)
            if offset+count*ctypes.sizeof(HandleEntry)>size or count>4096:raise RuntimeError('Invalid handle snapshot')
            for i in range(count):
                entry=HandleEntry.from_buffer(buffer,offset+i*ctypes.sizeof(HandleEntry));handle=w.HANDLE()
                if not self.k.DuplicateHandle(owner,entry.value,self.k.GetCurrentProcess(),ctypes.byref(handle),0,False,2):continue
                try:
                    info=Extended()
                    if not self.k.QueryInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info),None):continue
                    ids=ctypes.create_string_buffer(65536)
                    if not self.k.QueryInformationJobObject(handle,3,ids,len(ids),None):continue
                    assigned=w.DWORD.from_buffer(ids).value;listed=w.DWORD.from_buffer(ids,4).value
                    if assigned!=listed or listed>4096:continue
                    members={ctypes.c_size_t.from_buffer(ids,8+j*ctypes.sizeof(ctypes.c_size_t)).value for j in range(listed)}
                    current=self.payload_tree(root)
                    if psutil.Process(root.pid).create_time()!=identity[1]:raise RuntimeError('Runner generation changed')
                    if members!={p.pid for p in current}:continue
                    if info.basic.flags&0x200 and info.job_memory!=self.limit:continue
                    flags=info.basic.flags;info.basic.flags|=0x200;info.job_memory=self.limit
                    if not self.k.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)):raise ctypes.WinError(ctypes.get_last_error())
                    verified=Extended()
                    if not self.k.QueryInformationJobObject(handle,9,ctypes.byref(verified),ctypes.sizeof(verified),None):raise ctypes.WinError(ctypes.get_last_error())
                    if verified.job_memory!=self.limit or verified.basic.flags!=flags|0x200:raise RuntimeError('Kernel budget verification failed')
                    return dict(verified=True,limit_bytes=verified.job_memory,pid=identity[0],created=identity[1],processes=len(members),flags_preserved=True)
                finally:self.k.CloseHandle(handle)
        finally:self.k.CloseHandle(owner)
        raise RuntimeError('No exclusive existing execution scope found')

    @staticmethod
    def payload_tree(root):
        system=os.path.normcase(os.path.realpath(os.path.join(os.environ.get('SystemRoot','C:/Windows'),'System32','conhost.exe')))
        def console(process):
            # Windows creates the parent's console host before the runner joins
            # its job. It is OS infrastructure, not a task payload process.
            return (process.ppid()==root.pid and process.name().lower()=='conhost.exe'
                    and os.path.normcase(os.path.realpath(process.exe()))==system)
        return [root]+[p for p in root.children(recursive=True) if not console(p)]



def once(config):
    cfg=json.loads(Path(config).read_text(encoding='utf-8-sig'));root=Path(cfg['work_dir'])
    states=json.loads((root/'active_jobs.json').read_text(encoding='utf-8'));items=[];errors={}
    for jid,job in states.items():
        spec=job['spec'];folder=root/jid
        if spec.get('kernel_memory_required') or (folder/'exit.json').exists():continue
        try:
            usage=json.loads((folder/'usage.json').read_text(encoding='utf-8'))
            if usage.get('phase')!='running' or not 0<=time.time()-usage['at']<20:continue
            runner=json.loads((folder/'runner.json').read_text(encoding='utf-8'));process=psutil.Process(runner['pid'])
            if process.create_time()!=runner['create_time']:continue
            limit=max(spec.get('commit_gb',spec['memory_gb']),2)+.25
            if not math.isfinite(limit) or limit<=0:continue
            observation=Guard(jid,limit).attach(process)
            items.append(dict(id=jid,lease=job['lease'],**observation))
        except (OSError,ValueError,KeyError,RuntimeError,psutil.Error) as error:
            errors[jid]=dict(type=type(error).__name__,detail=str(error)[:200])
    status=Path(config).parent.parent/'data/memory-guard-status.json'
    temporary=status.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(at=time.time(),items=[{k:v for k,v in item.items() if k!='lease'} for item in items],errors=errors)),encoding='utf-8')
    temporary.replace(status)
    request=urllib.request.Request(cfg['controller'].rstrip('/')+'/agent/'+cfg['id']+'/memory-observations',
        data=json.dumps(dict(items=items,sampled_at=time.time())).encode(),headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cfg['ca_file'])))
    with opener.open(request,timeout=20) as response:return json.loads(response.read())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS);psutil.Process().cpu_affinity([psutil.cpu_count()-1])
    while True:
        try:once(args.config)
        except Exception as error:print(type(error).__name__,str(error)[:150],flush=True)
        time.sleep(5)
