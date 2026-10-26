"""Provision selected work paths before a lease reaches the existing worker.

No scheduling, process termination or data migration. Node credentials authorize
only inventory and the controller's pending directory-allocation handshakes.
"""
import argparse,json,ssl,time,urllib.request,urllib.error
from pathlib import Path
import storage_layout
from concurrent.futures import ThreadPoolExecutor
from common import atomic_json


_disk_executor=ThreadPoolExecutor(max_workers=2)
_disk_future=None
_disk_snapshot={}
_disk_started=0
_acl_future=None
_acl_started=0
_acl_error=None

def background(operation,canonical,cfg):
    # Lower only the scanner thread; keep timely control observations responsive.
    import os
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentThread.restype=ctypes.c_void_p
        kernel.SetThreadPriority.argtypes=[ctypes.c_void_p,ctypes.c_int]
        kernel.SetThreadPriority(kernel.GetCurrentThread(),-2)
    return operation(canonical,cfg)


def disk_usage_snapshot(canonical,cfg):
    # Filesystem scans must never stall fresh process/volume observations.
    global _disk_future,_disk_snapshot,_disk_started,_acl_future,_acl_started,_acl_error
    if _disk_future is not None and _disk_future.done():
        try:_disk_snapshot=_disk_future.result()
        except OSError:_disk_snapshot={}
        _disk_future=None
    if _disk_future is None and time.time()-_disk_started>=20:
        _disk_started=time.time()
        _disk_future=_disk_executor.submit(background,storage_layout.active_disk_usage,canonical,cfg)
    if _acl_future is not None and _acl_future.done():
        error=_acl_future.exception();_acl_error=str(error)[:500] if error else None;_acl_future=None
    if _acl_future is None and time.time()-_acl_started>=20:
        _acl_started=time.time();_acl_future=_disk_executor.submit(background,storage_layout.cleanup_volume_acls,canonical,cfg)
    return _disk_snapshot


def once(config, opener=None):
    path=Path(config);cfg=json.loads(path.read_text(encoding='utf-8-sig'));canonical=Path(cfg['work_dir'])
    disk_usage=disk_usage_snapshot(canonical,cfg)
    volumes=storage_layout.inventory(cfg,canonical)
    if opener is None:opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cfg['ca_file'])))
    base=cfg['controller'].rstrip('/')+'/agent/'+cfg['id']
    def post(route,data,lease=None):
        headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json'}
        if lease:headers['X-Lease']=lease
        req=urllib.request.Request(base+route,data=json.dumps(data).encode(),headers=headers)
        with opener.open(req,timeout=20) as response:return json.loads(response.read())
    evidence_path=canonical.parent/'broker/storage-prelaunch.json'
    try:verified=json.loads(evidence_path.read_text())
    except (OSError,ValueError):verified=[]
    response=post('/storage',dict(volumes=volumes,default=next(iter(storage_layout.roots(cfg,canonical))),managed=storage_layout.managed_processes(canonical,cfg),disk_usage=disk_usage,prelaunch_failures=verified))
    verified=[j['id'] for j in response.get('failed_prelaunch',[]) if storage_layout.verify_incomplete_prelaunch(canonical,j['id'],cfg)]
    atomic_json(evidence_path,verified)
    handled=[]
    for jid in response.get('cleanup_pending',[]):
        if storage_layout.confirm_finished_cleanup(canonical,jid,cfg):handled.append(dict(id=jid,cleanup_verified=True))
    for job in response.get('unstarted',[]):
        if storage_layout.retire_empty_isolated_setup(canonical,job['id'],cfg):handled.append(dict(id=job['id'],setup_recovery=True))
    for job in response.get('jobs',[]):
        try:
            storage_layout.job_folder(canonical,job['id'],cfg,job['volume'])
            payload=dict(ready=True)
        except Exception as e:payload=dict(ready=False,detail=str(e)[:500])
        try:post('/jobs/'+job['id']+'/storage-ready',payload,job['lease'])
        except urllib.error.HTTPError as e:
            if e.code!=403:raise  # A concurrently cancelled/requeued lease is stale.
        handled.append(dict(id=job['id'],**payload))
    atomic_json(canonical.parent/'broker/storage-health.json',dict(at=time.time(),volumes=volumes,handled=handled,status='ready',maintenance_error=_acl_error))
    return handled

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);args=ap.parse_args()
    while True:
        try:once(args.config)
        except Exception as e:
            cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
            atomic_json(Path(cfg['work_dir']).parent/'broker/storage-health.json',dict(at=time.time(),status='error',detail=str(e)[:1000]))
        time.sleep(3)
