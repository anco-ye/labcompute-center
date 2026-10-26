"""Outbound HTTPS worker. Resource-bounded slots, persistent leases, no shell execution."""
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
import threading
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error
import zipfile
import psutil
from common import atomic_json, extract_zip, worker_release, inspect_zip, read_json_shared, PLATFORM_RELEASE, fair_transfer_order
from simulation_monitor import SimulationMonitor
from runner import kill_tree
from science_control import normal
from resource_monitor import PressureWindow,WindowsCounters


def gpu_inventory():
    """Desktop VRAM is not compute occupancy; leases reserve whole GPUs."""
    gpus=[]
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    def query(fields):
        return subprocess.check_output(['nvidia-smi',fields,'--format=csv,noheader,nounits'],timeout=4,text=True,creationflags=flags)
    try:
        output=query('--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu')
        try:
            processes=query('--query-compute-apps=gpu_uuid,pid,process_name')
            # WDDM reports the desktop/browser as C+G too. Reserve known
            # external compute interpreters; center leases cover its own jobs.
            occupied=set()
            for line in processes.strip().splitlines():
                if not line.strip():continue
                uuid,pid,name=[v.strip() for v in line.split(',',2)]
                base=name.replace('\\','/').rsplit('/',1)[-1].lower()
                if os.name!='nt' or any(x in base for x in ('python','matlab','comsol','nrniv','julia','rscript')):
                    occupied.add(uuid)
            verified=True
        except (OSError,ValueError,subprocess.SubprocessError):occupied=set();verified=False
        for line in output.strip().splitlines():
            index,uuid,name,total,free,util=[v.strip() for v in line.split(',')]
            gpus.append(dict(index=int(index),name=name,memory_total_gb=float(total)/1024,memory_free_gb=float(free)/1024,
                utilization=float(util),compute_occupied=uuid in occupied,compute_verified=verified,
                available=verified and uuid not in occupied and float(util)<10))
    except (OSError,ValueError,subprocess.SubprocessError):pass
    return gpus


def cpu_specification():
    name=None
    if os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                name=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
        except OSError:pass
    else:
        import platform
        name=platform.processor() or None
    return dict(cpu_model=name,cpu_cores=psutil.cpu_count(logical=False),cpu_threads=psutil.cpu_count() or 1)


def apply_science_control(sim, control):
    """Process confirmation overrides stale sensor status, while preserving completion."""
    status=control['status']
    sim['process_state']={'running':'running','stopped':'stopped','paused':'paused'}.get(status,'unknown')
    ended=sim.get('status') in ('已完成','计算完成','批次结束，含失败或未完成项')
    if ended:
        sim['actions']=[];sim['control_detail']='';sim['remaining_seconds']=None
        return
    if status=='running' and not ended:
        if sim.get('status') in ('等待前序批次','等待启动','队列已暂停'):sim['status']='初始化中';sim['remaining_seconds']=None;sim['progress_state']='initializing'
        if sim.get('status') in ('进程状态待确认','已中断','已暂停','正在续跑','进程已结束，等待续跑'):sim['status']='运行中'
        sim.pop('failure_detail',None)
    elif status=='stopped' and not ended:
        sim['status']='已中断'
        sim['remaining_seconds']=None
        if sim['status']=='已中断':sim.setdefault('failure_detail','计算进程已退出，任务未完成；退出原因未记录，已有输出保留。')
    elif status=='unknown' and not ended:
        sim.update(status='进程状态待确认',remaining_seconds=None,actions=[])
        sim['status_detail']='进程监测不完整，无法确认任务是否仍在运行。'
    elif status in ('paused','cancelled','starting'):
        sim['status']=('队列已暂停' if sim.get('status')=='等待前序批次' else '已暂停') if status=='paused' else '已取消' if status=='cancelled' else '正在续跑'
        sim['remaining_seconds']=None
    if ended:
        sim['actions']=[];sim['control_detail']=''


def simulation_lifecycle(sim):
    status=sim.get('status','')
    if status in ('已完成','计算完成'):return 'succeeded'
    if status in ('已中断','批次结束，含失败或未完成项','进程已结束，等待续跑'):return 'failed'
    if status in ('已取消','cancelled'):return 'cancelled'
    if status in ('等待前序批次','等待启动','队列已暂停'):return 'queued'
    if status=='已暂停':return 'paused'
    if status=='状态文件暂不可读' and sim.get('process_state')=='running':return 'running'
    if status in ('状态文件暂不可读','进程状态待确认'):return 'unknown'
    return 'running'


def external_reservation(external, pool, declared_budget=None):
    """Reserve enforced affinity, never infer capacity from momentary CPU use."""
    if not external:return [],0
    if any(p.get('cpu_ids') is None for p in external):
        return [],min(len(pool),declared_budget if declared_budget is not None else len(pool))
    ids=sorted(set(pool) & set().union(*(set(p['cpu_ids']) for p in external)))
    return ids,len(ids)


def observed_process(snapshot, pid, created):
    row=snapshot.get('affinities',{}).get(str(pid),{})
    if 0<=time.time()-snapshot.get('at',0)<15 and abs(row.get('created',0)-created)<.01:
        return row
    return {}


def observed_affinity(snapshot, pid, created):
    return observed_process(snapshot,pid,created).get('cpu_ids')


def memory_commitment():
    """Windows commit includes pages not currently resident in working sets.

    https://learn.microsoft.com/windows/win32/api/psapi/ns-psapi-performance_information
    """
    if os.name!='nt':return {}
    import ctypes
    class PerformanceInfo(ctypes.Structure):
        _fields_=[('cb',ctypes.c_uint32)]+[(name,ctypes.c_size_t) for name in
            ('CommitTotal','CommitLimit','CommitPeak','PhysicalTotal','PhysicalAvailable',
             'SystemCache','KernelTotal','KernelPaged','KernelNonpaged','PageSize')]+[
            (name,ctypes.c_uint32) for name in ('HandleCount','ProcessCount','ThreadCount')]
    try:
        info=PerformanceInfo();info.cb=ctypes.sizeof(info)
        query=ctypes.WinDLL('psapi',use_last_error=True).GetPerformanceInfo
        query.argtypes=[ctypes.POINTER(PerformanceInfo),ctypes.c_uint32];query.restype=ctypes.c_int
        if not query(ctypes.byref(info),info.cb) or not info.PageSize or not info.CommitLimit:
            return dict(memory_commit_verified=False)
        return dict(memory_commit_verified=True,memory_committed_gb=info.CommitTotal*info.PageSize/2**30,
                    memory_commit_limit_gb=info.CommitLimit*info.PageSize/2**30)
    except (OSError,AttributeError):return dict(memory_commit_verified=False)


def disk_inventory():
    volumes = []; seen = set()
    for partition in psutil.disk_partitions(all=False):
        mount = partition.mountpoint
        if os.name == 'nt':
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(mount)) != 3:
                continue
        key = (partition.device or mount).casefold()
        if key in seen:
            continue
        try:
            usage = psutil.disk_usage(str(mount))
        except (OSError, PermissionError):
            continue
        seen.add(key)
        volumes.append(dict(mount=mount, total_gb=usage.total/1024**3,
                            used_gb=usage.used/1024**3, free_gb=usage.free/1024**3, percent=usage.percent))
    return volumes


def command(spec, runtime, work):
    entry=(work/spec['entry']).resolve()
    if not entry.is_relative_to(work.resolve()):
        raise ValueError('入口超出任务目录')
    exe=runtime['executable']
    if spec['kind']=='python':return [exe,'-u',str(entry),*spec['args']]
    if spec['kind']=='matlab':return [exe,'-batch',"run('"+str(entry).replace('\\','/').replace("'","''")+"')"]
    return [exe,'-np',str(spec['cores']),'-inputfile',str(entry),'-outputfile',str(work/'computed_model.mph'),'-batchlog',str(work/'comsol_batch.log')]


class DownloadInterrupted(ConnectionError):
    """A partial package can be continued under the same lease."""


class PackageIdentityChanged(DownloadInterrupted):
    """Discard the old partial and recheck the package on the next attempt."""


def download_package(opener, url, headers, destination,
                     chunk_bytes=32*1024**2, retries=5):
    """Resume a package by length and byte range under its job lease."""
    destination=Path(destination)
    partial=destination.with_suffix(destination.suffix+'.download')
    metadata=partial.with_suffix(partial.suffix+'.json')
    head=urllib.request.Request(url,headers={**headers,'Accept-Encoding':'identity'},method='HEAD')
    try:
        with opener.open(head,timeout=120) as response:
            try:total=int(response.headers['Content-Length'])
            except (KeyError,TypeError,ValueError) as e:raise ConnectionError('任务包缺少长度标识') from e
    except urllib.error.HTTPError as e:
        if e.code not in (408,429,500,502,503,504):raise
        raise DownloadInterrupted('任务包服务暂不可用') from e
    except (urllib.error.URLError,http.client.HTTPException,TimeoutError) as e:
        raise DownloadInterrupted('任务包连接暂不可用') from e
    if total<=0:raise ConnectionError('任务包长度无效')
    identity=dict(url=url,length=total)
    try:previous=json.loads(metadata.read_text(encoding='utf-8'))
    except (OSError,ValueError):previous=None
    if partial.exists() and (previous!=identity or partial.stat().st_size>total):
        partial.unlink()
    atomic_json(metadata,identity)
    offset=partial.stat().st_size if partial.exists() else 0
    failures=0
    while offset<total:
        end=min(total-1,offset+chunk_bytes-1)
        request=urllib.request.Request(url,headers={**headers,'Range':f'bytes={offset}-{end}',
            'Accept-Encoding':'identity'},method='GET')
        try:
            with opener.open(request,timeout=120) as response:
                match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                if (response.status!=206 or not match
                        or tuple(map(int,match.groups()))!=(offset,end,total)):
                    partial.unlink(missing_ok=True);metadata.unlink(missing_ok=True)
                    raise PackageIdentityChanged('任务包身份变化或服务器未按请求范围传输')
                with partial.open('ab') as output:
                    while offset<=end:
                        try:block=response.read(min(256*1024,end+1-offset))
                        except http.client.IncompleteRead as e:
                            output.write(e.partial);offset+=len(e.partial)
                            raise DownloadInterrupted('任务包连接提前关闭') from e
                        except OSError as e:
                            raise DownloadInterrupted('任务包连接中断') from e
                        if not block:raise DownloadInterrupted('任务包连接提前关闭')
                        output.write(block);offset+=len(block)
            failures=0
        except urllib.error.HTTPError as e:
            if e.code not in (408,429,500,502,503,504):raise
            failures+=1
        except PackageIdentityChanged:
            raise
        except (DownloadInterrupted,urllib.error.URLError,http.client.HTTPException,TimeoutError):
            failures+=1
        if failures:
            if failures>retries:raise DownloadInterrupted('任务包下载重试次数已用尽；已保留可续传部分')
            time.sleep(min(failures,5))
    if partial.stat().st_size!=total:raise DownloadInterrupted('任务包长度不符；已保留可续传部分')
    if not zipfile.is_zipfile(partial):
        partial.unlink();metadata.unlink(missing_ok=True)
        raise zipfile.BadZipFile('任务包不是完整ZIP')
    partial.replace(destination);metadata.unlink(missing_ok=True)


class UploadYielded(Exception):
    """A durable upload offset exists; release the lane for another result."""


def delivery_paused(path):
    path=Path(path)
    if not path.exists():return False
    try:
        until=json.loads(path.read_text(encoding='utf-8')).get('until')
        if type(until) in (int,float):return time.time()<until
    except (OSError,ValueError,AttributeError):pass
    return True


def upload_result(opener, base, path, headers, archive, chunk_bytes=8*1024**2, retries=5,
                  byte_budget=None,seconds=None,on_progress=None,pause_file=None):
    """Upload once per byte under the same lease; return False for an old server."""
    archive=Path(archive);snapshot=archive.stat();size=snapshot.st_size
    identity={**headers,'X-Upload-Size':str(size),'X-Upload-Mtime-Ns':str(snapshot.st_mtime_ns)}
    def unchanged(stream=None):
        current=archive.stat()
        version=lambda st:(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
        if version(current)!=version(snapshot) or (stream is not None and version(os.fstat(stream.fileno()))!=version(snapshot)):
            raise DownloadInterrupted('Result archive changed during upload; existing prefix retained')
    transfer=base+path.removesuffix('/finish')+'/finish-upload'

    def query():
        request=urllib.request.Request(transfer,headers=identity,method='GET')
        with opener.open(request,timeout=120) as response:return json.loads(response.read())

    try:state=query()
    except urllib.error.HTTPError as e:
        if e.code==404:return False
        raise
    if state.get('finished'):return True
    offset=int(state['offset'])
    if not 0<=offset<=size:raise ConnectionError('中心结果续传偏移无效')
    if on_progress:on_progress(offset,size)
    first_offset=offset;began=time.monotonic()
    failures=0
    with archive.open('rb') as source:
        unchanged(source)
        while offset<size:
            if ((pause_file is not None and delivery_paused(pause_file)) or
                    (byte_budget is not None and offset-first_offset>=byte_budget) or
                    (seconds is not None and offset>first_offset and time.monotonic()-began>=seconds)):
                raise UploadYielded()
            unchanged(source)
            source.seek(offset);block=source.read(min(chunk_bytes,size-offset))
            request=urllib.request.Request(transfer,data=block,
                headers={**identity,'X-Upload-Offset':str(offset)},method='POST')
            try:
                with opener.open(request,timeout=120) as response:
                    new_offset=int(json.loads(response.read())['offset'])
                if new_offset!=offset+len(block):raise ConnectionError('中心结果续传偏移不一致')
                offset=new_offset;failures=0
                if on_progress:on_progress(offset,size)
            except urllib.error.HTTPError as e:
                if e.code not in (408,409,429,500,502,503,504):raise
                failures+=1
                state=query();offset=int(state['offset'])
            except (OSError,http.client.HTTPException,ValueError):
                failures+=1
                state=query();offset=int(state['offset'])
            if failures:
                if failures>retries:raise DownloadInterrupted('结果上传暂时中断；中心已保留可续传部分')
                time.sleep(min(failures,5))
    unchanged()
    request=urllib.request.Request(base+path,data=b'',
        headers={**identity,'X-Upload-Commit':'1'},method='POST')
    with opener.open(request,timeout=120) as response:return json.loads(response.read())


class Worker:
    @property
    def state(self):
        return getattr(self._task_context,'job',None)

    @state.setter
    def state(self,value):
        if not hasattr(self,'_task_context'):self._task_context=threading.local()
        self._task_context.job=value

    def remember(self,job):
        with self._states_lock:
            self.states[job['id']]=job
            atomic_json(self.state_path,self.states)

    def forget(self,jid):
        with self._states_lock:
            self.completed_ids.add(jid)
            self.states.pop(jid,None)
            atomic_json(self.state_path,self.states)

    def snapshot(self):
        with self._states_lock:return list(self.states.values())

    def reconcile_job(self,job,response):
        self.state=job
        try:self.reconcile(response.get('cancel',[]),response.get('controls',{}))
        except UploadYielded:pass
        except Exception as e:
            print('reconcile',job['id'],str(e)[:300],flush=True)
            try:self.api('/jobs/'+job['id']+'/update',dict(status='recovery_needed',detail='同步或回传失败，将自动重试；也可取消。'+str(e)[:500]))
            except Exception:pass
        finally:self.state=None

    def __init__(self, config):
        self.cfg=json.loads(Path(config).read_text(encoding='utf-8-sig'))
        self.root=Path(self.cfg['work_dir']).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self._lock=open(self.root/'worker.lock','a+b')
        self._lock.seek(0);self._lock.write(b'0');self._lock.flush();self._lock.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(self._lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(self._lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.context=ssl.create_default_context(cafile=self.cfg['ca_file'])
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=self.context))
        self.base=self.cfg['controller'].rstrip('/')+'/agent/'+self.cfg['id']
        self.state_path=self.root/'active_jobs.json'
        self.states=json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
        legacy=self.root/'active.json'
        if legacy.exists():
            job=json.loads(legacy.read_text(encoding='utf-8'));self.states[job['id']]=job
            atomic_json(self.state_path,self.states);legacy.unlink()
        self.state=None
        self._states_lock=threading.RLock()
        self._delivery_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="result-delivery")
        self._archive_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="secondary-archive")
        self._preparation_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="package-preparation")
        self._preparation_future=None
        self._preparing_id=None
        self._upload_offsets={}
        self._deliveries={}
        self._archive_future=None
        self.background_controls=False
        self.last_response={}
        self.completed_ids=set()
        self.clock_offset=0
        self.release=worker_release(Path(__file__).parent)
        atomic_json(self.root/'worker-loaded.json',dict(at=time.time(),pid=os.getpid(),
            release=self.release,platform_release=PLATFORM_RELEASE))
        self.runtimes={}
        for k,v in self.cfg.get('runtimes',{}).items():
            r=dict(v)
            if not Path(r.get('executable','__missing__')).is_file():r['status']='not_installed'
            self.runtimes[k]=r
        self.simulations=SimulationMonitor(self.cfg.get("simulation_monitor",{}),self.root/"simulation_monitor_history.json")
        psutil.cpu_percent()
        self.pressure=PressureWindow();self.counters=WindowsCounters()
        self.cpu_sample=None
        self.cpu_spec=cpu_specification()

    def api(self,path,body=None,method='POST',download=None,upload=None,extra=None):
        headers={'Authorization':'Bearer '+self.cfg['token']}
        if self.state:headers['X-Lease']=self.state['lease']
        headers.update(extra or {})
        if download:
            return download_package(self.opener,self.base+path,headers,download)
        if upload and path.endswith('/finish'):
            jid=self.state['id'] if self.state else path.split('/')[-2]
            if not hasattr(self,'_upload_offsets'):self._upload_offsets={}
            def progress(offset,size):self._upload_offsets[jid]=(offset,size)
            completed=upload_result(self.opener,self.base,path,headers,upload,
                byte_budget=64*1024**2,seconds=10,on_progress=progress,
                pause_file=self.root/jid/'delivery.pause')
            if completed:return completed
        if upload:
            headers['Content-Type']='application/zip';headers['Content-Length']=str(Path(upload).stat().st_size)
            data=open(upload,'rb')
        else:
            data=json.dumps(body or {}).encode() if method=='POST' else None
            if data:headers['Content-Type']='application/json'
        try:
            req=urllib.request.Request(self.base+path,data=data,headers=headers,method=method)
            with self.opener.open(req,timeout=120 if upload else 20) as response:
                return json.loads(response.read())
        finally:
            if upload:data.close()

    def telemetry(self):
        mem=psutil.virtual_memory(); external=[]
        isolation_verified=False
        if os.name=='nt' and self.cfg.get('isolation_enabled'):
            try:
                health=json.loads((self.root.parent/'broker/health.json').read_text(encoding='utf-8'))
                isolation_verified=health.get('status')=='ready' and 0<=time.time()-health['at']<20
            except (OSError,ValueError,KeyError):pass
        try:control_snapshot=json.loads((self.root.parent/'science-control'/'snapshot.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):control_snapshot={}
        # Only this center's work tree is excluded; existing H01 tasks remain reserved.
        if self.cfg.get('respect_external_jobs',True):
            for p in psutil.process_iter(['pid','name','cmdline','create_time']):
                try:
                    name=(p.info['name'] or '').lower();cmd=' '.join(p.info['cmdline'] or [])
                    if observed_process(control_snapshot,p.pid,p.info['create_time']).get('managed'):continue
                    if p.pid!=os.getpid() and any(x in name for x in ('python','matlab','comsol','nrniv')) and normal(Path(__file__).parent) not in normal(cmd) and normal(self.root) not in normal(cmd):
                        try:cpu_ids=p.cpu_affinity()
                        except (psutil.AccessDenied,psutil.NoSuchProcess,AttributeError):cpu_ids=observed_affinity(control_snapshot,p.pid,p.info['create_time'])
                        external.append(dict(pid=p.pid,name=name,cpu_ids=cpu_ids,elapsed_minutes=round((time.time()-p.info['create_time'])/60,1)))
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        pool=self.cfg.get('cpu_pool',list(range(max(1,(psutil.cpu_count() or 1)-1))))
        external_ids,external_cores=external_reservation(external,pool,self.cfg.get('external_cpu_budget'))
        gpus=gpu_inventory()
        disk=psutil.disk_usage(str(self.root))
        volumes=disk_inventory()
        usages={}
        for jid in list(self.states):
            try:usages[jid]=read_json_shared(self.root/jid/'usage.json')
            except (ValueError,OSError):pass
        now=time.time();times=psutil.cpu_times();total=sum(times);idle=times.idle+getattr(times,'iowait',0)
        cpu=psutil.cpu_percent(interval=.1)
        if self.cpu_sample:
            prior_total,prior_idle=self.cpu_sample;delta=total-prior_total
            if delta>0:cpu=max(0,min(100,100*(1-(idle-prior_idle)/delta)))
        self.cpu_sample=(total,idle)
        managed=sum(u.get('cpu_cores',0) for u in usages.values() if 0<=now-u.get('at',0)<15)
        observation=self.pressure.observe(now,cpu_percent=cpu,total_gb=mem.total/2**30,
            available_gb=mem.available/2**30,reserve_gb=self.cfg.get('reserve_gb',4),
            managed_cores=managed,cpu_threads=psutil.cpu_count() or 1,counters=self.counters.read())
        observations=control_snapshot.get('research_processes') if 0<=time.time()-control_snapshot.get('at',0)<15 and control_snapshot.get('observation_verified',True) else None
        simulations=self.simulations.collect(observations,observation_verified=observations is not None)
        simulations=[dict(s,actions=[]) for s in simulations]
        try:
            snapshot=control_snapshot
            if 0<=time.time()-snapshot['at']<15 and snapshot.get('observation_verified',True):
                for key,control in snapshot['items'].items():
                    sim=next((s for s in simulations if normal(s['id'])==normal(key)),None)
                    if sim is None:
                        sim=dict(id=key,name=control['name'],status='运行中' if control['status']=='running' else '等待启动');simulations.append(sim)
                    sim.update(control_id=key,control_status=control['status'],generation=control['generation'],actions=control['actions'],control_detail=control.get('detail',''),activity=control.get('activity',{}))
                    apply_science_control(sim,control)
        except (OSError,ValueError,KeyError):pass
        hidden=self.cfg.get('hidden_simulations',[])
        simulations=[s for s in simulations if s.get('observed_running') or not any(term in s.get('id','') or term==s.get('name') for term in hidden)]
        for sim in simulations:
            metadata=self.cfg.get('queue_metadata',{}).get(sim.get('name'),{})
            sim.update(metadata)
            sim['lifecycle']=simulation_lifecycle(sim)
            if observations is None and sim['lifecycle'] not in ('succeeded','failed','cancelled'):
                sim.update(status='进程状态待确认',lifecycle='unknown',process_state='unknown',remaining_seconds=None,actions=[])
                sim['status_detail']='进程监测不可用或已过期，无法确认任务是否仍在运行。'
                try:
                    recovery=json.loads((self.root.parent/'science-control/watchdog.json').read_text(encoding='utf-8'))
                    if recovery.get('status')!='healthy':sim['status_detail']+=recovery.get('detail','')
                except (OSError,ValueError):pass
        return dict(sampled_at=time.time(),host=os.environ.get('COMPUTERNAME',os.uname().nodename if hasattr(os,'uname') else ''),
                    agent_release=self.release,platform_release=PLATFORM_RELEASE,cpu_percent=cpu,resource_observation=observation,
                    scheduling_policy=self.cfg.get('scheduling_policy',{}),**self.cpu_spec,
                    memory_total_gb=mem.total/1024**3,memory_available_gb=mem.available/1024**3,
                    memory_percent=mem.percent,**memory_commitment(),disk_free_gb=disk.free/1024**3,disks=volumes,
                    disk_total_gb=sum(d['total_gb'] for d in volumes),disk_available_gb=sum(d['free_gb'] for d in volumes),
                    reserve_gb=self.cfg.get('reserve_gb',4),external_busy=bool(external),external_jobs=external[:40],external_reserved_cores=external_cores,external_cpu_ids=external_ids,
                    worker=self.cfg.get('worker',True),worker_busy=bool(self.states),usages=usages,
                    active_ids=list(self.states),max_parallel=self.cfg.get('max_parallel',8),
                    cpu_pool=self.cfg.get('cpu_pool',list(range(max(1,(psutil.cpu_count() or 1)-1)))),
                    work_dir=str(self.root),archive_dir=self.cfg.get('archive_dir'),disk_media=self.cfg.get('disk_media',{}),
                    isolation_verified=isolation_verified,simulations=simulations,applications=self.cfg.get('applications',[]),gpus=gpus,
                    runtimes={k:{key:value for key,value in v.items() if key in ('kind','label','version','revision','status','packages','max_parallel','detail','disk_overhead_gb','gpu_framework','cuda_version')} for k,v in self.runtimes.items()})

    def prepare(self,job):
        self.local_deadline(job)
        self.state=job;self.remember(job)
        folder=self.root/job['id'];folder.mkdir(exist_ok=True)
        if job.get('control')=='pause':(folder/'pause').touch()
        if job.get('control')=='cancel' or (folder/'cancel').exists():
            (folder/'cancel').touch()
            atomic_json(folder/'exit.json',dict(status='cancelled',exit_code=-1,detail='用户取消',cleanup_verified=True))
            return
        if (folder/'launching.json').exists():
            return
        retryfile=folder/'preparation-retry.json'
        try:
            archive=folder/'package.zip'
            if not archive.is_file() or not zipfile.is_zipfile(archive):
                archive.unlink(missing_ok=True)
                self.api('/jobs/'+job['id']+'/package',method='GET',download=archive)
            extract_zip(folder/'package.zip',folder/'work')
            for key in job['spec'].get('dependency_jobs',{}):
                archive=folder/('dependency-'+key+'.zip')
                self.api('/jobs/'+job['id']+'/dependency/'+key,method='GET',download=archive)
                inspect_zip(archive)
                with zipfile.ZipFile(archive) as z:incoming=sum(x.file_size for x in z.infolist())
                existing=sum(p.stat().st_size for p in (folder/'work').rglob('*') if p.is_file())
                if incoming+existing>(job['spec'].get('disk_gb',4)-1)*1024**3:raise ValueError('前序结果超过任务磁盘预算，请增加 resources.disk_gb')
                extract_zip(archive,folder/'work'/'dependencies'/key)
                archive.unlink()

            if (folder/'cancel').exists():
                atomic_json(folder/'exit.json',dict(status='cancelled',exit_code=-1,detail='用户取消',cleanup_verified=True))
                return
            if job['spec'].get('deadline') and time.time()>job['spec']['deadline']:raise ValueError('任务包传输超过声明的最长占用时间')
            runtime=self.runtimes[job['spec']['runtime']]
            argv=command(job['spec'],runtime,folder/'work')
            atomic_json(folder/'launch.json',dict(argv=argv,spec=job['spec'],runtime=runtime,
                pip_cache=str(folder/'pip-cache') if job['spec'].get('requires_isolation') else str(self.root.parent/'cache'/'pip'),wheelhouse=str(self.root.parent/'wheels')))
            # Mark launching before spawn. An ambiguous restart never launches a second copy.
            atomic_json(folder/'launching.json',dict(at=time.time()))
            retryfile.unlink(missing_ok=True)
            if job['spec'].get('requires_isolation'):
                if os.name!='nt' or not self.cfg.get('isolation_enabled'):raise RuntimeError('用户隔离环境尚未配置')
                atomic_json(self.root.parent/'broker/requests'/(job['id']+'.json'),dict(id=job['id']))
                self.api('/jobs/'+job['id']+'/update',dict(status='preparing',detail='准备独立执行环境'))
                return
            flags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
            process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('runner.py')),str(folder)],
                                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                     creationflags=flags,start_new_session=os.name!='nt')
            self.api('/jobs/'+job['id']+'/update',dict(status='preparing',detail='准备运行环境'))
        except Exception as e:
            # Network failure after spawn must not overwrite the running process as failed.
            if not (folder/'launching.json').exists():
                if isinstance(e,DownloadInterrupted):
                    try:attempts=json.loads(retryfile.read_text(encoding='utf-8')).get('attempts',0)+1
                    except (OSError,ValueError):attempts=1
                    if attempts<4:
                        atomic_json(retryfile,dict(attempts=attempts,next_try=time.time()+min(300,30*2**(attempts-1)),detail=str(e)))
                        return
                retryfile.unlink(missing_ok=True)
                report=dict(status='failed',exit_code=-1,detail=str(e))
                if isinstance(e,(ConnectionError,urllib.error.URLError,zipfile.BadZipFile)):
                    report.update(failure_code='environment_setup',failure_evidence=dict(phase='preparing',transport_error=True),cleanup_verified=True)
                atomic_json(folder/'exit.json',report)
            raise

    def reconcile(self,cancels,controls=None):
        job=self.state;folder=self.root/job['id']
        retryfile=folder/'delivery-retry.json'
        retry_due=False
        if retryfile.exists():
            retry=json.loads(retryfile.read_text(encoding='utf-8'))
            retry_due=retry.get('next_try',0)>time.time()
        if not self.background_controls:
            if job['id'] in cancels:(folder/'cancel').touch()
            control=(controls or {}).get(job['id'],'run')
            if control=='pause':(folder/'pause').touch()
            else:(folder/'pause').unlink(missing_ok=True)
        exitfile=folder/'exit.json'
        logfile=folder/'task.log'
        text=''
        if logfile.exists():
            with logfile.open('rb') as f:
                f.seek(max(0,logfile.stat().st_size-60000));text=f.read().decode('utf-8',errors='replace')
        if job['spec'].get('requires_isolation'):
            try:receipt=json.loads((self.root.parent/'broker/receipts'/(job['id']+'.json')).read_text(encoding='utf-8'))
            except (OSError,ValueError):receipt={}
            # Preparation can fail before a Broker request exists. Such a job has
            # no sandbox to retire; do not report a completed calculation or wait
            # forever for a receipt that the Broker cannot produce.
            prelaunch_failed=(exitfile.exists() and not receipt and
                not (self.root.parent/'broker/requests'/(job['id']+'.json')).exists() and
                not any((folder/name).exists() for name in ('launching.json','launch.json','runner.json','child.json')))
            if prelaunch_failed:
                result=json.loads(exitfile.read_text(encoding='utf-8'))
                result.update(cleanup_verified=True)
                result.setdefault('failure_code','environment_setup')
                result.setdefault('failure_evidence',dict(phase='preparing',execution_started=False))
                atomic_json(exitfile,result)
            if receipt.get('status')!='retired' and not prelaunch_failed:
                try:phase=read_json_shared(folder/'usage.json').get('phase','preparing')
                except (OSError,ValueError):phase='preparing'
                finishing=exitfile.exists()
                status='running' if finishing else phase if phase in ('preparing','running','paused') else 'preparing'
                self.api('/jobs/'+job['id']+'/update',dict(status=status,detail='计算结束，正在自动清理' if finishing else '计算中' if phase=='running' else '准备计算'))
                return
        if exitfile.exists():
            result=json.loads(exitfile.read_text(encoding='utf-8'))
            cancelled=(folder/'cancel').exists()
            if cancelled:
                result.setdefault('execution_status',result['status'])
                result.update(status='cancelled',detail='用户取消；已有工作文件保留在节点任务目录')
                atomic_json(exitfile,result)
            elif job['spec'].get('result_transfer_error') and result.get('execution_status'):
                result['status']=result['execution_status'];result['detail']='已有计算结果重新回传'
                atomic_json(exitfile,result)
            if result.get('cleanup_verified') is True:
                self.api('/jobs/'+job['id']+'/stopped',dict(cleanup_verified=True,execution_report=result))
            if retry_due:return
            settled=self.api('/jobs/'+job['id']+'/update',dict(status='uploading',detail=result['detail'],log=text)).get('delivery_ready',False)
            archive=folder/('cancelled-result.zip' if cancelled else 'result.zip')
            if cancelled and not archive.exists() and (folder/'result.zip').is_file():
                # Cancellation after native completion changes control state,
                # not the bytes of the already complete result archive.
                archive=folder/'result.zip'
            packing=not archive.exists()
            try:
                if not archive.exists():
                    with zipfile.ZipFile(folder/'result.partial','w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
                        paths=list((folder/'work').rglob('*'))+list(folder.iterdir())
                        for path in paths:
                            internal=path.parent==folder and path.name in ('package.zip','result.zip','cancelled-result.zip','result.partial')
                            if path.is_file() and not path.is_symlink() and not internal and path.resolve().is_relative_to(folder.resolve()):
                                z.write(path,path.relative_to(folder).as_posix())
                    os.replace(folder/'result.partial',archive)
                packing=False
                if not settled:self.api('/jobs/'+job['id']+'/finish',upload=archive,extra={'X-Result-Status':result['status'],'X-Exit-Code':str(result['exit_code'])})
            except urllib.error.HTTPError as e:
                if e.code not in (400,413):raise
                try:reason=json.loads(e.read()).get('error','')
                except (ValueError,AttributeError):reason=''
                self.result_error(job,'too_large' if e.code==413 else 'invalid_archive',reason)
                return
            except OSError:
                if not packing:raise
                self.result_error(job,'archive_failed')
                return
            if self.cfg.get('archive_dir'):
                pending=self.root/'pending-archive';pending.mkdir(exist_ok=True)
                atomic_json(pending/(job['id']+'.json'),dict(source=str(archive),target=str(Path(self.cfg['archive_dir'])/job['id']/'result.zip'),next_try=0,attempts=0))
            # Only the disposable environment is removed; code, results and logs remain.
            shutil.rmtree(folder/'environment',ignore_errors=True)
            shutil.rmtree(folder/'pip-cache',ignore_errors=True)
            self.forget(job['id']);self.state=None
            return
        alive=False
        rp=folder/'runner.json'
        if rp.exists():
            r=read_json_shared(rp)
            try:
                p=psutil.Process(r['pid']);alive=abs(p.create_time()-r['create_time'])<.1 and p.status()!=psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:pass
        if alive:
            try:phase=read_json_shared(folder/'usage.json').get('phase')
            except (ValueError,OSError):return
            # A transient read failure is not evidence of a lifecycle change.
            if phase not in ('preparing','running','paused'):return
            self.api('/jobs/'+job['id']+'/update',dict(status=phase,detail='准备运行环境' if phase=='preparing' else '运行中',log=text))
        else:
            # Allow a just-spawned supervisor time to publish its identity.
            launching=folder/'launching.json'
            if launching.exists() and time.time()-launching.stat().st_mtime<30:
                return
            # Reconcile after a worker/service restart without executing the entry again.
            refs=[]
            for name in ('child.json','process_tree.json'):
                try:
                    value=read_json_shared(folder/name);refs.extend(value if isinstance(value,list) else [value])
                except (OSError,ValueError):pass
            for r in refs:
                try:
                    p=psutil.Process(r['pid'])
                    if abs(p.create_time()-r['create_time'])<.1:kill_tree(p)
                except psutil.NoSuchProcess:pass
            cancelled=(folder/'cancel').exists()
            cleanup_verified=True
            for ref in refs:
                try:
                    remaining=psutil.Process(ref['pid'])
                    if abs(remaining.create_time()-ref['create_time'])<.1 and remaining.status()!=psutil.STATUS_ZOMBIE:cleanup_verified=False
                except psutil.NoSuchProcess:pass
            try:supervisor_error=read_json_shared(folder/'supervisor-error.json')
            except (OSError,ValueError):supervisor_error={}
            if not isinstance(supervisor_error,dict):supervisor_error={}
            raw_cause=supervisor_error.get('type')
            cause=raw_cause[:100] if isinstance(raw_cause,str) else ''
            raw_message=supervisor_error.get('detail')
            message=raw_message[:500] if isinstance(raw_message,str) else ''
            failure_code='supervisor_error' if cause else 'runner_interrupted'
            detail=('用户取消；已保留输出' if cancelled else
                    f'执行监控器异常 · {cause}: {message}；已保留输出' if cause else
                    '执行意外中断，已自动收尾并保留输出；可重试未完成项')
            report=dict(status='cancelled' if cancelled else 'failed',exit_code=-1,
                        failure_code=None if cancelled else failure_code,cleanup_verified=cleanup_verified,detail=detail)
            if cause and not cancelled:
                report['failure_evidence']=dict(source='supervisor-error.json',type=cause,message=message)
            atomic_json(exitfile,report)

    def result_error(self,job,kind,reason=''):
        self.api('/jobs/'+job['id']+'/result-error',dict(kind=kind,reason=reason))
        # Keep the package, supervisor exit and original outputs for transfer-only retry.
        shutil.rmtree(self.root/job['id']/'environment',ignore_errors=True)
        retryfile=self.root/job['id']/'delivery-retry.json'
        try:record=json.loads(retryfile.read_text(encoding='utf-8'))
        except (OSError,ValueError):record={}
        attempts=record.get('attempts',0)+1
        atomic_json(retryfile,dict(attempts=attempts,next_try=time.time()+min(3600,30*2**min(attempts-1,7)),kind=kind))
        if kind=='invalid_archive':
            for name in ('result.zip','cancelled-result.zip'):
                archive=self.root/job['id']/name
                if archive.exists():archive.replace(archive.with_name(name+'.rejected-'+uuid.uuid4().hex))
        self.state=None

    def sync_controls(self):
        spool=self.root.parent/'science-control';results=[]
        for path in sorted((spool/'responses').glob('*.json'))[:50]:
            try:results.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError,ValueError):pass
        response=self.api('/heartbeat',dict(telemetry=self.telemetry(),science_results=results))
        if response.get('server_time'):self.clock_offset=time.time()-response['server_time']
        self.last_response=response
        # Control delivery must continue while another thread downloads, packages
        # or uploads a large result. These durable signals are read by the runner.
        for control_job in self.snapshot():
            jid=control_job['id']
            folder=self.root/jid;folder.mkdir(exist_ok=True)
            if jid in response.get('cancel',[]):
                (folder/'cancel').touch()
                if control_job['spec'].get('requires_isolation'):
                    atomic_json(self.root.parent/'broker/requests'/(jid+'.json'),dict(id=jid,cancel=True))
            if (folder/'exit.json').exists():
                try:
                    result=json.loads((folder/'exit.json').read_text(encoding='utf-8'))
                    isolated=control_job['spec'].get('requires_isolation')
                    retired=not isolated or json.loads((self.root.parent/'broker/receipts'/(jid+'.json')).read_text(encoding='utf-8')).get('status')=='retired'
                    if result.get('cleanup_verified') is True and retired:
                        self.api('/jobs/'+jid+'/stopped',dict(cleanup_verified=True,execution_report=result),extra={'X-Lease':control_job['lease']})
                except (OSError,ValueError,urllib.error.URLError):pass
            if response.get('controls',{}).get(jid)=='pause':(folder/'pause').touch()
            else:(folder/'pause').unlink(missing_ok=True)
        for cid in response.get('science_ack',[]):
            if isinstance(cid,str) and len(cid)==32 and all(c in '0123456789abcdef' for c in cid):
                (spool/'requests'/(cid+'.json')).unlink(missing_ok=True)
                (spool/'responses'/(cid+'.json')).unlink(missing_ok=True)
        for command in response.get('science_commands',[]):
            cid=command['id']
            if len(cid)==32 and all(c in '0123456789abcdef' for c in cid):atomic_json(spool/'requests'/(cid+'.json'),command)
        return response

    def local_deadline(self,job):
        if job['spec'].get('deadline') and not job.get('local_deadline'):
            job['spec']['deadline']+=self.clock_offset;job['local_deadline']=True

    def archive_one(self):
        # The controller already owns the result. A secondary archive failure
        # must not hold the compute slot or repeatedly re-upload completed work.
        for path in (self.root/'pending-archive').glob('*.json'):
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                if record.get('next_try',0)>time.time():continue
                target=Path(record['target']);target.parent.mkdir(parents=True,exist_ok=True)
                partial=target.with_suffix('.partial')
                shutil.copy2(record['source'],partial);os.replace(partial,target);path.unlink()
            except OSError as e:
                record['attempts']=record.get('attempts',0)+1
                record['next_try']=time.time()+min(3600,60*2**min(record['attempts']-1,6))
                record['error']=str(e)[:300];atomic_json(path,record)
            return

    def queue_preparation(self,job):
        pool=getattr(self,'_preparation_pool',None)
        if pool is None:return self.prepare(job)
        future=getattr(self,'_preparation_future',None)
        if future is not None and not future.done():return False
        if future is not None:future.result()
        self.local_deadline(job);self.remember(job)
        self._preparing_id=job['id']
        self._preparation_future=pool.submit(self.prepare,job)
        return True

    def once(self):
        self.state=None
        response=self.last_response if self.background_controls else self.sync_controls()
        for job in response.get('recover',[]):
            if job['id'] not in self.states and job['id'] not in self.completed_ids:
                self.local_deadline(job)
                (self.root/job['id']).mkdir(exist_ok=True)
                if job['spec'].get('result_transfer_error') and job.get('control')=='run':
                    (self.root/job['id']/'cancel').unlink(missing_ok=True)
                    # A transfer retry reuses the immutable completed archive.
                    # Deleting it loses a usable result and forces expensive
                    # repacking; upload identity already handles replacements.
                    for name in ('result.zip','cancelled-result.zip'):
                        archive=self.root/job['id']/name
                        if archive.exists() and not zipfile.is_zipfile(archive):
                            archive.replace(archive.with_name(name+'.rejected-'+uuid.uuid4().hex))
                self.remember(job)
        for jid,future in list(self._deliveries.items()):
            if future.done():
                future.result();del self._deliveries[jid]
        def delivery_remaining(job):
            folder=self.root/job['id']
            measured=getattr(self,'_upload_offsets',{}).get(job['id'])
            if measured:return measured[1]-measured[0]
            sizes=[p.stat().st_size for p in (folder/'result.zip',folder/'cancelled-result.zip') if p.is_file()]
            return min(sizes) if sizes else 0
        preparing=getattr(self,'_preparation_future',None)
        if preparing is not None and preparing.done():
            self._preparation_future=None;self._preparing_id=None;preparing.result()
        if not hasattr(self,'_delivery_waiting'):self._delivery_waiting={};self._delivery_served={}
        for job in fair_transfer_order(self.snapshot(),self._delivery_waiting,self._delivery_served,delivery_remaining,time.monotonic()):
            jid=job['id']
            if jid==getattr(self,'_preparing_id',None):continue
            if jid in self._deliveries:continue
            folder=self.root/jid
            if (folder/'package.zip.download').exists() and not any((folder/n).exists() for n in ('launching.json','launch.json','runner.json','execution.json','exit.json')):
                self.queue_preparation(job);continue
            retryfile=self.root/jid/'preparation-retry.json'
            if (retryfile.exists() and not (self.root/jid/'launching.json').exists()
                    and not (self.root/jid/'exit.json').exists()):
                try:retry=json.loads(retryfile.read_text(encoding='utf-8'))
                except (OSError,ValueError):retry={}
                if retry.get('next_try',0)<=time.time():self.queue_preparation(job)
                continue
            if (self.root/jid/'exit.json').exists():
                if delivery_paused(self.root/jid/'delivery.pause'):continue
                if any(not future.done() for future in self._deliveries.values()):continue
                self._delivery_served[jid]=time.monotonic()
                self._deliveries[jid]=self._delivery_pool.submit(self.reconcile_job,job,response)
            else:self.reconcile_job(job,response)
        self.state=None
        if self.cfg.get('worker',True):
            # Bounded burst: fill available slots without a poll delay per task.
            # Each claim is still planned atomically by the controller. Stop on
            # stale/repeated leases; background heartbeats deliver cancellation.
            for _ in range(max(0,self.cfg.get('max_parallel',8)-sum(not (self.root/j['id']/'exit.json').exists() for j in self.snapshot()))):
                pending=getattr(self,'_preparation_future',None)
                if pending is not None and not pending.done():break
                job=self.api('/claim').get('job')
                if not job or job['id'] in self.states:break
                self.queue_preparation(job)
        if self._archive_future is None or self._archive_future.done():
            self._archive_future=getattr(self,'_archive_pool',self._delivery_pool).submit(self.archive_one)

    def run(self):
        self.background_controls=True
        def controls():
            while True:
                try:self.sync_controls()
                except Exception as e:print('control sync',type(e).__name__,str(e)[:200],flush=True)
                time.sleep(self.cfg.get('poll_seconds',5))
        threading.Thread(target=controls,daemon=True,name='control-heartbeat').start()
        while True:
            try:self.once()
            except Exception as e:print(time.strftime('%Y-%m-%d %H:%M:%S'),type(e).__name__,str(e)[:300],flush=True)
            time.sleep(self.cfg.get('poll_seconds',5))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();Worker(a.config).run()
