"""Admin-configured work volumes; stable canonical job paths via NTFS junctions."""
import os,re,subprocess
from pathlib import Path
import psutil
from common import read_json_shared


def managed_processes(canonical, config):
    """Verify the supervisor's recorded identities without global process scans."""
    import json
    try:states=json.loads((Path(canonical)/'active_jobs.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return []
    found={}
    for jid in states:
        try:
            folder=trusted_folder(canonical,jid,config);ref=read_json_shared(folder/'runner.json')
            parent=psutil.Process(ref['pid'])
            if abs(parent.create_time()-ref['create_time'])>=.01:continue
            refs=[ref]
            for name in ('child.json','process_tree.json'):
                try:
                    row=read_json_shared(folder/name);refs.extend(row if isinstance(row,list) else [row])
                except (OSError,ValueError):pass
            for ref in refs[:400]:
                try:
                    process=psutil.Process(ref['pid']);created=process.create_time()
                    if abs(created-ref['create_time'])<.01:found[process.pid]=dict(pid=process.pid,created=created,job=jid)
                except (KeyError,psutil.Error):continue
        except (OSError,ValueError,KeyError,psutil.Error):continue
    return list(found.values())[:400]


def normalize_external(telemetry, observation, now, node_sampled):
    """Do not count a verified central task again as external/shared work."""
    if not observation or not 0<=now-observation.get('at',0)<15 or not isinstance(node_sampled,(int,float)):return
    managed={p['pid']:p for p in observation.get('managed',[])};remaining=[];removed=0
    for p in telemetry.get('external_jobs',[]):
        known=managed.get(p.get('pid'));elapsed=p.get('elapsed_minutes')
        # Legacy workers round process age to 0.1 minute (up to 3 seconds),
        # then finish collecting telemetry. Allow that quantization plus scan
        # time, while retaining the independent fresh PID/birth-time evidence.
        if known and isinstance(elapsed,(float,int)) and abs(node_sampled-elapsed*60-known['created'])<5:removed+=1
        else:remaining.append(p)
    if not removed:return
    telemetry['external_jobs']=remaining;telemetry['external_busy']=bool(remaining)
    pool=telemetry.get('cpu_pool',list(range(max(1,int(telemetry.get('cpu_threads',1))-1))))
    if any(p.get('cpu_ids') is None for p in remaining):
        ids=[];cores=min(len(pool),telemetry.get('external_reserved_cores',len(pool)))
    else:
        ids=sorted(set(pool)&set().union(*(set(p['cpu_ids']) for p in remaining))) if remaining else [];cores=len(ids)
    telemetry.update(external_cpu_ids=ids,external_reserved_cores=cores,managed_external_reclassified=removed)


def roots(config, canonical):
    canonical=Path(canonical).resolve()
    paths=[canonical]+[Path(p).resolve() for p in config.get('work_roots',[])]
    out={}
    for path in paths:
        if not path.is_absolute() or any(c in str(path) for c in '\r\n\x00'):raise ValueError('Invalid work root')
        key=path.drive.rstrip(':\\/').upper() if os.name=='nt' else str(path)
        if key in out and out[key]!=path:raise ValueError('One work root per volume is required')
        out[key]=path
    return out


def inventory(config, canonical):
    out=[]
    for index,(key,path) in enumerate(roots(config,canonical).items()):
        if not path.is_dir():continue
        disk=psutil.disk_usage(str(path))
        reserve=max(10,disk.total/2**30*.05) if index==0 else max(4,disk.total/2**30*.02)
        out.append(dict(id=key,root=str(path),free_gb=disk.free/2**30,total_gb=disk.total/2**30,reserve_gb=reserve))
    return out


def job_folder(canonical,jid,config,volume=None):
    if not re.fullmatch('[a-f0-9]{32}',jid):raise ValueError('Invalid job id')
    canonical=Path(canonical);link=canonical/jid;allowed=roots(config,canonical)
    default=next(iter(allowed));selected=volume or default
    if selected not in allowed:raise ValueError('Unconfigured work volume')
    target=allowed[selected]/jid
    if link.exists():
        if link.resolve()!=target:raise ValueError('Existing job directory does not match its allocated volume')
        return link
    if selected==default:link.mkdir();return link
    if not allowed[selected].is_dir():raise ValueError('Work volume unavailable')
    if target.exists() and any(target.iterdir()):raise ValueError('Unlinked work directory is not empty')
    target.mkdir(exist_ok=True)
    try:
        if os.name=='nt':
            subprocess.run(['cmd','/d','/c','mklink','/J',str(link),str(target)],check=True,capture_output=True,timeout=15)
        else:link.symlink_to(target,target_is_directory=True)
    except Exception:
        target.rmdir();raise
    return link


def trusted_folder(canonical,jid,config):
    if not re.fullmatch('[a-f0-9]{32}',jid):raise ValueError('Invalid job id')
    actual=(Path(canonical)/jid).resolve()
    for root in roots(config,canonical).values():
        if actual==root/jid:return actual
    raise ValueError('Job path escapes configured work roots')


def retire_empty_isolated_setup(canonical,jid,config):
    """Close a failed prelaunch directory preparation, never a live execution.

    Caller has a node-authenticated preparing lease with zero compute start.
    The old worker cannot resume prepare after that failed call. Require an
    entirely empty folder and no protected broker/runner launch evidence.
    """
    from common import atomic_json
    folder=trusted_folder(canonical,jid,config);spool=Path(canonical).parent/'broker'
    request=spool/'requests'/(jid+'.json');receipt=spool/'receipts'/(jid+'.json')
    if request.exists() or receipt.exists() or any(folder.iterdir()):return False
    atomic_json(folder/'exit.json',dict(status='failed',exit_code=-1,failure_code='environment_setup',failure_evidence=dict(phase='preparing',empty_prelaunch_directory=True),cleanup_verified=True,detail='工作目录准备未完成，中心自动重新准备'))
    atomic_json(receipt,dict(status='retired',no_execution_created=True,storage_recovery=True))
    return True



def verify_incomplete_prelaunch(canonical,jid,config):
    """Evidence for a transport failure, never a scientific/checker failure."""
    import zipfile
    folder=trusted_folder(canonical,jid,config);spool=Path(canonical).parent/'broker'
    if any((folder/name).exists() for name in ('launch.json','launching.json','runner.json')):return False
    if any((spool/name/(jid+'.json')).exists() for name in ('requests','receipts')):return False
    package=folder/'package.zip'
    return package.is_file() and not zipfile.is_zipfile(package)


def remove_retired_sid(path, sid):
    """Remove a deleted account's explicit grant without resolving its name.

    Open the directory itself (including a junction) and compare binary SIDs.
    icacls /remove can fail with 1332 once the disposable account is deleted.
    """
    import ctypes as c
    from ctypes import wintypes as w
    k=c.WinDLL('kernel32',use_last_error=True);a=c.WinDLL('advapi32',use_last_error=True)
    ptr=c.c_void_p;pp=c.POINTER(ptr)
    k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ptr,w.DWORD,w.DWORD,w.HANDLE];k.CreateFileW.restype=w.HANDLE
    k.CloseHandle.argtypes=[w.HANDLE];k.LocalFree.argtypes=[ptr];k.LocalFree.restype=ptr
    a.ConvertStringSidToSidW.argtypes=[w.LPCWSTR,pp];a.ConvertStringSidToSidW.restype=w.BOOL
    a.GetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,pp,pp,pp,pp,pp];a.GetSecurityInfo.restype=w.DWORD
    a.SetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,ptr,ptr,ptr,ptr];a.SetSecurityInfo.restype=w.DWORD
    a.GetAce.argtypes=[ptr,w.DWORD,pp];a.GetAce.restype=w.BOOL
    a.DeleteAce.argtypes=[ptr,w.DWORD];a.DeleteAce.restype=w.BOOL
    a.EqualSid.argtypes=[ptr,ptr];a.EqualSid.restype=w.BOOL
    handle=k.CreateFileW(str(path),0x20000|0x40000,7,None,3,0x02000000|0x00200000,None)
    if handle==w.HANDLE(-1).value:raise c.WinError(c.get_last_error())
    descriptor=ptr();wanted=ptr();acl=ptr()
    try:
        if not a.ConvertStringSidToSidW(sid,c.byref(wanted)):raise c.WinError(c.get_last_error())
        error=a.GetSecurityInfo(handle,1,4,None,None,c.byref(acl),None,c.byref(descriptor))
        if error:raise c.WinError(error)
        if not acl:raise ValueError('Refusing null task DACL')
        # ACL header: BYTE revision, BYTE reserved, WORD size, WORD AceCount.
        count=c.c_ushort.from_address(acl.value+4).value;removed=0
        for index in range(count-1,-1,-1):
            ace=ptr()
            if not a.GetAce(acl,index,c.byref(ace)):raise c.WinError(c.get_last_error())
            # Only an explicit ACCESS_ALLOWED_ACE. Preserve inherited/system ACLs.
            kind=c.c_ubyte.from_address(ace.value).value;flags=c.c_ubyte.from_address(ace.value+1).value
            if kind==0 and not flags&0x10 and a.EqualSid(ptr(ace.value+8),wanted):
                if not a.DeleteAce(acl,index):raise c.WinError(c.get_last_error())
                removed+=1
        if removed:
            error=a.SetSecurityInfo(handle,1,4,None,None,acl,None)
            if error:raise c.WinError(error)
        return removed
    finally:
        if descriptor:k.LocalFree(descriptor)
        if wanted:k.LocalFree(wanted)
        k.CloseHandle(handle)


def cleanup_volume_acls(canonical, config):
    import json
    from common import atomic_json
    spool=Path(canonical).parent/'broker'
    for marker in (spool/'volume-acls').glob('*.json'):
        record=json.loads(marker.read_text(encoding='utf-8'))
        if record.get('cleaned'):continue
        jid=marker.stem
        try:receipt=json.loads((spool/'receipts'/(jid+'.json')).read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        if receipt.get('status')!='retired':continue
        sid=record.get('sid','')
        if not re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+',sid):raise ValueError('Invalid retired execution SID')
        physical=trusted_folder(canonical,jid,config)
        # No recursive walk through publisher-created directories or junctions.
        for path in (physical,Path(canonical)/jid):
            remove_retired_sid(path,sid)
        atomic_json(marker,dict(record,cleaned=True))


# Scanning is incremental per task and runs only in the independent storage observer.
_DISK_USAGE_CACHE={}

def active_disk_usage(canonical,config,now=None):
    """Count allocated bytes once; partial/inaccessible scans receive no credit."""
    import json,time
    now=time.time() if now is None else now
    try:states=json.loads((Path(canonical)/'active_jobs.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    out={}
    for jid in states:
        try:
            folder=trusted_folder(canonical,jid,config)
            key=str(folder);cached=_DISK_USAGE_CACHE.get(key)
            if cached and 0<=now-cached['at']<20:out[jid]=cached;continue
            size=0;seen=set()
            if os.name=='nt':
                import ctypes
                from ctypes import wintypes
                compressed=ctypes.WinDLL('kernel32',use_last_error=True).GetCompressedFileSizeW
                compressed.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(wintypes.DWORD)];compressed.restype=wintypes.DWORD
            for parent,dirs,files in os.walk(folder,followlinks=False):
                dirs[:]=[d for d in dirs if not (Path(parent)/d).is_symlink() and not getattr((Path(parent)/d).lstat(),'st_file_attributes',0)&0x400]
                for name in files:
                    path=Path(parent)/name
                    if path.is_symlink():continue
                    st=path.stat();identity=(st.st_dev,st.st_ino)
                    if identity in seen:continue
                    seen.add(identity)
                    if os.name=='nt':
                        high=wintypes.DWORD();ctypes.set_last_error(0);low=compressed(str(path),ctypes.byref(high))
                        if low==0xFFFFFFFF and ctypes.get_last_error():raise ctypes.WinError(ctypes.get_last_error())
                        size+=(high.value<<32)|low
                    else:size+=st.st_blocks*512
            row=dict(allocated_gb=size/2**30,at=now,verified=True);_DISK_USAGE_CACHE[key]=row;out[jid]=row
        except (OSError,ValueError,KeyError):continue
    return out


def confirm_finished_cleanup(canonical,jid,config):
    """Finish a failed cleanup only after the original supervisor has exited.

    Never signal processes from task-written PID files; the original JobObject
    and the privileged broker retain exclusive cleanup ownership.
    """
    import json
    folder=trusted_folder(canonical,jid,config)
    try:
        report=json.loads((folder/'exit.json').read_text(encoding='utf-8'))
        if report.get('cleanup_verified') is True:return False
        receipt=Path(canonical).parent/'broker/receipts'/(jid+'.json')
        if receipt.exists():
            if json.loads(receipt.read_text(encoding='utf-8')).get('status')!='retired':return False
            report['cleanup_verified']=True
            from common import atomic_json
            atomic_json(folder/'exit.json',report);return True
        runner=read_json_shared(folder/'runner.json')
        def original(ref):
            try:
                p=psutil.Process(ref['pid'])
                return p if abs(p.create_time()-ref['create_time'])<.001 and p.status()!=psutil.STATUS_ZOMBIE else None
            except psutil.NoSuchProcess:return None
        if original(runner):return False
        refs=[]
        for name in ('child.json','process_tree.json'):
            value=read_json_shared(folder/name)
            refs.extend(value if isinstance(value,list) else [value])
        if not refs:return False
        if any(original(ref) for ref in refs):return False
        report['cleanup_verified']=True
        from common import atomic_json
        atomic_json(folder/'exit.json',report)
        return True
    except (OSError,ValueError,KeyError,psutil.Error):return False
