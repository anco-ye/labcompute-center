"""Controller-side SSH dispatcher using the controller's existing atomic leases.
No resident software is started on the host outside bounded job units.
"""
import argparse,ctypes,hashlib,json,os,re,shutil,shlex,ssl,subprocess,sys,threading,time,urllib.request
from pathlib import Path
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import atomic_json,PLATFORM_RELEASE,fair_transfer_order
import release_identity
from agent import UploadYielded


def atomic(path,value):
    atomic_json(path,value)


def unprotect(path):
    from ctypes import wintypes
    class Blob(ctypes.Structure):_fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_char))]
    data=Path(path).read_bytes();buf=ctypes.create_string_buffer(data);a=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_char)));b=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(a),None,None,None,None,0,ctypes.byref(b)):raise ctypes.WinError()
    try:return ctypes.string_at(b.pbData,b.cbData).decode()
    finally:ctypes.windll.kernel32.LocalFree(b.pbData)

def copy_result_slice(src,dst,offset,size,byte_budget=128*1024**2,seconds=30):
    """Pipeline bounded SFTP requests, append only confirmed contiguous bytes."""
    if offset<0 or offset>size:raise IOError('Result size changed')
    deadline=time.monotonic()+seconds;limit=min(size,offset+byte_budget);position=offset
    while position<limit:
        stop=min(limit,position+2*1024**2)
        chunks=[(p,min(32768,stop-p)) for p in range(position,stop,32768)]
        readv=getattr(src,'readv',None)
        if callable(readv):
            blocks=iter(readv(chunks,max_concurrent_prefetch_requests=32))
        else:
            src.seek(position);blocks=(src.read(length) for _,length in chunks)
        for start,length in chunks:
            try:block=next(blocks)
            except StopIteration:raise IOError('Incomplete remote result read')
            if not isinstance(block,bytes) or len(block)!=length:raise IOError('Incomplete remote result read')
            dst.write(block);position=start+length
        if time.monotonic()>=deadline:break
    return position


class Bridge:
    def __init__(self,path):
        self.loaded_release=release_identity.snapshot(Path(__file__).resolve().parents[1],'bridge')
        self.path=Path(path);self.cfg=json.loads(self.path.read_text(encoding='utf-8-sig'));self.root=self.path.parent;self.active_path=self.root/'active.json'
        stored=json.loads(self.active_path.read_text(encoding='utf-8-sig')) if self.active_path.exists() else {}
        self.jobs={j['id']:j for j in (stored.get('jobs',[]) if 'jobs' in stored else [stored] if stored.get('id') else [])}
        self.recovered=[];self.inventory={};self.remote_offset=0;self.observation={};self.controls={};self.stop=threading.Event();self.heartbeat_lock=threading.RLock()
        self.state_lock=threading.RLock()
        self.artifact_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ssh-delivery')
        self.stage_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ssh-preparation')
        self.transfers={}
        self.upload_offsets={}
        self.cancelled_leases=set()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=self.cfg['ca_file'])))
    def api(self,path,body=None,download=None,upload=None,lease=None,extra=None):
        headers={'Authorization':'Bearer '+self.cfg['token'],**(extra or {})}
        if lease:headers['X-Lease']=lease
        stream=None
        if upload:
            # Use the same lease/length/offset protocol as Windows workers.
            # An interrupted controller upload must not resend the whole ZIP.
            from agent import upload_result
            jid=path.split('/')[-2]
            def progress(offset,size):self.upload_offsets[jid]=(offset,size)
            if upload_result(self.opener,self.cfg['server'],
                    '/agent/'+self.cfg['id']+path,headers,upload,
                    byte_budget=64*1024**2,seconds=10,on_progress=progress):
                return {'ok':True}
            stream=open(upload,'rb');data=stream;headers.update({'Content-Type':'application/zip','Content-Length':str(Path(upload).stat().st_size)})
        else:
            data=json.dumps(body or {}).encode() if download is None else None
            if data is not None:headers['Content-Type']='application/json'
        try:
            req=urllib.request.Request(self.cfg['server']+'/agent/'+self.cfg['id']+path,data=data,headers=headers,method='GET' if download else 'POST')
            with self.opener.open(req,timeout=300 if download or upload else 20) as r:
                if download:
                    tmp=Path(str(download)+'.download')
                    with tmp.open('wb') as f:
                        while chunk:=r.read(1024**2):f.write(chunk)
                    if r.headers.get('Content-Length') and tmp.stat().st_size!=int(r.headers['Content-Length']):raise IOError('Incomplete download')
                    tmp.replace(download);return
                return json.loads(r.read())
        finally:
            if stream:stream.close()
    def connect(self):
        import paramiko,io
        client=paramiko.SSHClient();client.load_host_keys(self.cfg['known_hosts']);client.set_missing_host_key_policy(paramiko.RejectPolicy())
        options={};jump=None
        try:
            if self.cfg.get('jump_config'):
                jump=Bridge(self.cfg['jump_config']).connect()
                options['sock']=jump.get_transport().open_channel('direct-tcpip',(self.cfg['host'],self.cfg['port']),('127.0.0.1',0),timeout=8)
            if self.cfg.get('private_key_dpapi'):
                options['pkey']=paramiko.Ed25519Key.from_private_key(io.StringIO(unprotect(self.cfg['private_key_dpapi'])))
            else:options['password']=unprotect(self.cfg['password_dpapi'])
            client.connect(self.cfg['host'],port=self.cfg['port'],username=self.cfg['user'],look_for_keys=False,allow_agent=False,timeout=8,banner_timeout=8,auth_timeout=8,**options)
            # Paramiko otherwise permits an hour waiting for a new SSH channel,
            # while the independent heartbeat can still make the node look healthy.
            client.get_transport().channel_timeout=20
            client.get_transport().set_keepalive(15)
        except Exception:
            client.close()
            if jump:jump.close()
            raise
        if jump:
            close=client.close
            def close_with_jump():
                try:close()
                finally:jump.close()
            client.close=close_with_jump
        return client
    def rpc(self,client,request):
        cmd=shlex.join([self.cfg['remote_python'],'-B',self.cfg['workspace']+'/tools/linux_rpc.py'])
        # exec_command(timeout=...) overrides Transport.channel_timeout even
        # while opening a channel; status checks must not wait five minutes.
        timeout=300 if request.get('action') in ('start','archive') else 20
        i,o,e=client.exec_command(cmd,timeout=timeout)
        try:
            i.write(json.dumps(request));i.flush();i.channel.shutdown_write()
            data=o.read(2**20);error=e.read(4096);code=o.channel.recv_exit_status()
            if code:raise RuntimeError('Remote operation failed: '+error.decode(errors='replace')[-300:]+' '+data.decode(errors='replace')[-500:])
            value=json.loads(data)
            if value.get('error'):raise RuntimeError(value['error'])
            return value
        finally:
            o.channel.close()
            for stream in (i,o,e):stream.close()
    def heartbeat(self):
        with getattr(self,'heartbeat_lock',nullcontext()):return self._heartbeat()
    def _heartbeat(self):
        inv=self.inventory;now=time.time()
        with getattr(self,'state_lock',nullcontext()):
            jobs=list(self.jobs.values());usages={jid:r['usage'] for jid,r in list(self.observation.items()) if r.get('usage')}
        h=inv.get('host_inventory',{});limits=self.cfg.get('execution_limits',{})
        total=h.get('memory_total_gb',0);available=inv.get('host_available_gb',0);reserve=self.cfg.get('reserve_gb',64);pool=inv.get('cpu_ids',[])
        fresh=0<=now-inv.get('at',0)<25
        t=dict(transport_revision='2026-10-24-bridge-receipt-first-v1',platform_release=PLATFORM_RELEASE,execution_limits=limits,cpu_model=h.get('cpu_model',''),host_inventory=h,os='linux',execution_backend='ssh-push',worker=fresh,max_parallel=self.cfg.get('max_parallel',256),cpu_cores=h.get('cpu_cores'),cpu_threads=h.get('cpu_threads'),cpu_pool=pool,memory_total_gb=total,memory_available_gb=available,reserve_gb=reserve,disk_free_gb=inv.get('disk_available_gb',0),disk_available_gb=inv.get('disk_available_gb',0),work_volumes=[v for v in inv.get('work_volumes',[]) if v.get('writable')],storage_volumes=inv.get('work_volumes',[]),default_work_volume=self.cfg.get('default_work_volume','/Dataset2'),memory_commit_verified=True,memory_commit_limit_gb=total,memory_committed_gb=max(0,total-available),gpus=[dict(g,available=not g.get('compute_occupied',False),schedulable=True) for g in (h.get('gpus',[]) if h.get('gpu_observed') else [])],isolation_verified=fresh and inv.get('isolation_verified') is True,sandbox_protocol=inv.get('sandbox_protocol',0),usages=usages,active_ids=[j['id'] for j in jobs],runtimes=dict(self.cfg['runtimes'],**inv.get('published_runtimes',{})),resource_observation=dict(at=inv.get('at',0),verified=fresh,state='normal' if available>=reserve else 'pressured'),scheduling_policy=dict(require_observation=True),work_dir=self.cfg['workspace']+'/jobs',memory_accounting='Linux cgroup RSS+cache; no swap',cpu_percent=h.get('cpu_percent',0),external_reserved_cores=max(0,__import__('math').ceil(h.get('cpu_percent',0)*h.get('cpu_threads',0)/100-sum(u.get('cpu_cores',0) for u in usages.values()))),storage_observation=dict(source='ssh-push',at=inv.get('at',0),managed=[dict(p,job=jid) for jid,u in usages.items() for p in u.get('processes',[])]))
        t.update(loaded_release=release_identity.public(self.loaded_release),rpc_release=inv.get('rpc_release'))
        answer=self.api('/heartbeat',dict(telemetry=t));self.controls=answer.get('controls',{})
        self.recovered=answer.get('recover',[])
    def persist(self):
        with self.state_lock:atomic(self.active_path,dict(jobs=list(self.jobs.values())))

    def queue_artifact(self,job,result=None):
        if job['id'] not in self.transfers:
            lane='stage_future' if result is None else 'delivery_future'
            pending=getattr(self,lane,None)
            if pending is not None and not pending.done():return
            pool=getattr(self,'stage_pool',self.artifact_pool) if result is None else self.artifact_pool
            if hasattr(self,'_transfer_served'):self._transfer_served[job['id']]=time.monotonic()
            future=pool.submit(self.artifact_pass,job,result)
            self.transfers[job['id']]=future
            setattr(self,lane,future)

    def artifact_pass(self,job,result=None):
        lane='_stage_client' if result is None else '_delivery_client'
        client=getattr(self,lane,None)
        if client is None or not client.get_transport() or not client.get_transport().is_active():
            if client is not None:client.close()
            client=self.connect();setattr(self,lane,client)
        try:
            if result is not None:
                if result.get('state')=='archiving':
                    result=self.rpc(client,dict(action='archive',id=job['id'],
                        lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),
                        work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2')))
                    if result.get('state')!='finished':return
                return self.deliver(client,job,result)
            size=self.stage(client,job)
            request=dict(id=job['id'],lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),
                         work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2'))
            if self.controls.get(job['id'])=='cancel':
                self.rpc(client,dict(request,action='cancel'))
            elif size is not None:
                self.rpc(client,dict(request,action='start',spec=job['spec'],package_size=size,
                    receipt_first=self.inventory.get('receipt_before_archive') is True))
        except UploadYielded:return
        except Exception:
            client.close();setattr(self,lane,None)
            raise
    def pulse(self):
        while not self.stop.wait(5):
            try:
                # Every SSH backend observes independently of artifact transfers.
                if time.time()-self.inventory.get('at',0)>10:
                    client=self.connect()
                    try:self.refresh_inventory(client)
                    finally:client.close()
                else:self.heartbeat()
                self.deliver_controls()
            except Exception as e:print('Heartbeat:',type(e).__name__,str(e)[:300],flush=True)

    def deliver_controls(self):
        with self.state_lock:
            pending=[j for j in self.jobs.values() if self.controls.get(j['id'])=='cancel'
                     and (j['id'],j['lease']) not in self.cancelled_leases]
        if not pending:return
        client=self.connect()
        try:
            for job in pending:
                self.rpc(client,dict(action='cancel',id=job['id'],
                    lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),
                    work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2')))
                with self.state_lock:self.cancelled_leases.add((job['id'],job['lease']))
        finally:client.close()
    def remote_folder(self,job):
        volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2')
        roots=self.cfg.get('volume_roots',{self.cfg.get('default_work_volume','/Dataset2'):self.cfg['workspace']})
        if volume not in roots:raise ValueError('Unknown work volume')
        return roots[volume]+'/jobs/'+job['id']
    def result_folder(self,job):
        legacy=self.root/job['id']
        # Preserve any existing contiguous prefix, including an online storage
        # alias. New result caches use the separately configured data volume.
        if any((legacy/name).exists() for name in ('result.zip','result.zip.partial')):
            return legacy
        configured=self.cfg.get('result_cache_dir')
        return Path(configured)/job['id'] if configured else legacy

    def upload_slice(self,sftp,path,dest):
        try:existing=sftp.stat(dest).st_size
        except IOError:existing=None
        if existing is not None:
            if existing==path.stat().st_size:return True
            raise IOError('Cached package identity changed')
        try:offset=sftp.stat(dest+'.upload').st_size
        except IOError:offset=0
        if offset>path.stat().st_size:raise IOError('Staged package size changed')
        with path.open('rb') as src,sftp.open(dest+'.upload','ab') as dst:
            pipeline=getattr(dst,'set_pipelined',None)
            if callable(pipeline):pipeline(True)
            src.seek(offset);remaining=32*1024**2
            while remaining:
                chunk=src.read(min(1024**2,remaining))
                if not chunk:break
                dst.write(chunk);remaining-=len(chunk)
        if sftp.stat(dest+'.upload').st_size<path.stat().st_size:return False
        sftp.posix_rename(dest+'.upload',dest);return True

    def stage_cached(self,client,job):
        key=job['package_ref']
        if not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{32}-[0-9]{1,22}-[0-9]{1,14}',key):raise ValueError('Invalid controller package identity')
        size=int(key.rsplit('-',1)[1]);folder=self.root/job['id'];folder.mkdir(exist_ok=True)
        cache=self.root/'package-cache'/key;cache.mkdir(parents=True,exist_ok=True);local=cache/'package.zip'
        if not local.exists():
            prior=folder/'package.zip'
            if prior.is_file() and prior.stat().st_size==size:
                shutil.copyfile(prior,cache/'package.tmp');(cache/'package.tmp').replace(local)
            else:self.api('/jobs/'+job['id']+'/package',download=local,lease=job['lease'])
        if local.stat().st_size!=size:raise IOError('Controller package size changed')
        request=dict(id=job['id'],lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2'))
        prepared=self.rpc(client,dict(request,action='prepare',package_ref=key));remote=self.remote_folder(job)
        if prepared['folder'].rstrip('/')!=remote.rstrip('/'):raise RuntimeError('Work volume routing mismatch')
        sftp=client.open_sftp()
        try:
            sftp.get_channel().settimeout(15)
            if not self.upload_slice(sftp,local,prepared['package_cache']):return None
            for depkey in job['spec'].get('dependency_jobs',{}):
                dep=folder/('dependency-'+depkey+'.zip')
                if not dep.exists():self.api('/jobs/'+job['id']+'/dependency/'+depkey,download=dep,lease=job['lease'])
                if not self.upload_slice(sftp,dep,remote+'/'+dep.name):return None
        finally:sftp.close()
        self.rpc(client,dict(request,action='attach_package',package_ref=key,package_size=size))
        return size

    def stage(self,client,job):
        if job.get('package_ref'):return self.stage_cached(client,job)
        folder=self.root/job['id'];folder.mkdir(exist_ok=True)
        local=folder/'package.zip'
        if not local.exists():self.api('/jobs/'+job['id']+'/package',download=local,lease=job['lease'])
        remote=self.remote_folder(job)
        prepared=self.rpc(client,dict(action='prepare',id=job['id'],lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2')))
        if prepared['folder'].rstrip('/')!=remote.rstrip('/'):raise RuntimeError('Work volume routing mismatch')
        sftp=client.open_sftp()
        try:
            try:sftp.mkdir(remote,mode=0o700)
            except IOError:pass
            for key in job['spec'].get('dependency_jobs',{}):
                dep=folder/('dependency-'+key+'.zip')
                if not dep.exists():self.api('/jobs/'+job['id']+'/dependency/'+key,download=dep,lease=job['lease'])
            for path in [local,*folder.glob('dependency-*.zip')]:
                dest=remote+'/'+path.name
                try:
                    if sftp.stat(dest).st_size==path.stat().st_size:continue
                except IOError:pass
                # Resume bounded uploads so one large package cannot monopolize dispatch.
                try:offset=sftp.stat(dest+'.upload').st_size
                except IOError:offset=0
                if offset>path.stat().st_size:raise IOError('Staged package size changed')
                with path.open('rb') as src,sftp.open(dest+'.upload','ab') as dst:
                    src.seek(offset);remaining=4*1024**2
                    while remaining:
                        chunk=src.read(min(1024**2,remaining))
                        if not chunk:break
                        dst.write(chunk);remaining-=len(chunk)
                if sftp.stat(dest+'.upload').st_size<path.stat().st_size:return None
                sftp.posix_rename(dest+'.upload',dest)
        finally:sftp.close()
        return local.stat().st_size
    def deliver(self,client,job,result):
        # A finished RPC has verified process cleanup; release compute before artifact transfer.
        if result.get('report',{}).get('cleanup_verified') is not True:
            raise RuntimeError('Missing verified process cleanup; compute lease retained')
        settled=self.api('/jobs/'+job['id']+'/update',dict(status='uploading'),lease=job['lease']) or {}
        if settled.get('delivery_ready'):
            self.forget_delivered(client,job)
            return
        if not job.get('_compute_released'):
            self.api('/jobs/'+job['id']+'/stopped',dict(cleanup_verified=True,execution_report=result['report']),lease=job['lease'])
            job['_compute_released']=True;self.persist()
        folder=self.result_folder(job);folder.mkdir(parents=True,exist_ok=True);path=folder/'result.zip';part=folder/'result.zip.partial';size=result['result_size']
        if not path.exists():
            offset=part.stat().st_size if part.exists() else 0
            if offset>size:raise IOError('Result size changed')
            sftp=client.open_sftp()
            try:
                with sftp.open(self.remote_folder(job)+'/result.zip','rb') as src,part.open('ab') as dst:
                    timeout=getattr(src,'settimeout',None)
                    if callable(timeout):timeout(15)
                    copy_result_slice(src,dst,offset,size)
            finally:sftp.close()
            if part.stat().st_size<size:return
            if part.stat().st_size!=size:raise IOError('Result size mismatch')
            part.replace(path)
        report=result['report'];status='cancelled' if report.get('cancelled') else 'succeeded' if report['exit_code']==0 else 'failed'
        self.api('/jobs/'+job['id']+'/stopped',dict(cleanup_verified=True,execution_report=report),lease=job['lease'])
        self.api('/jobs/'+job['id']+'/finish',upload=path,lease=job['lease'],extra={'X-Result-Status':status,'X-Exit-Code':str(report['exit_code'])})
        self.forget_delivered(client,job)

    def forget_delivered(self,client,job):
        with self.state_lock:
            self.jobs.pop(job['id'],None);self.observation.pop(job['id'],None);self.persist()
        try:self.rpc(client,dict(action='retire',id=job['id'],lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest()))
        except Exception as e:print('Finished-unit metadata cleanup pending:',type(e).__name__,flush=True)
    def refresh_inventory(self,client):
        inv=self.rpc(client,dict(action='inventory'));offset=time.time()-inv['at']
        inv=dict(inv,remote_at=inv['at'],at=inv['at']+offset,remote_offset=offset)
        self.remote_offset=offset;self.inventory=inv;self.heartbeat()
    def tick(self):
        client=self.connect()
        try:
            self.refresh_inventory(client)
            for job in self.recovered:
                with self.state_lock:
                    if job['id'] not in self.jobs:self.jobs[job['id']]=job;self.persist()
            for jid,future in list(self.transfers.items()):
                if future.done():
                    del self.transfers[jid]
                    try:future.result()
                    except Exception as e:print('Artifact retry:',jid,type(e).__name__,str(e)[:300],flush=True)
            with self.state_lock:jobs=list(self.jobs.values())
            def delivery_priority(job):
                report=self.observation.get(job['id'],{});folder=self.result_folder(job)
                complete=folder/'result.zip';part=folder/'result.zip.partial'
                try:size=complete.stat().st_size if complete.exists() else part.stat().st_size
                except OSError:size=0
                total=report.get('result_size')
                sent=getattr(self,'upload_offsets',{}).get(job['id'],(0,0))[0]
                if total is not None:return (0,max(0,total-size)+max(0,total-sent))
                try:package_size=int(job.get('package_ref','').rsplit('-',1)[1])
                except (ValueError,IndexError):package_size=2**63
                return (1,package_size)
            if not hasattr(self,'_transfer_waiting'):self._transfer_waiting={};self._transfer_served={}
            jobs=fair_transfer_order(jobs,self._transfer_waiting,self._transfer_served,delivery_priority,time.monotonic())
            for job in jobs:
                if job['id'] in self.transfers:continue
                try:self.tick_job(client,job)
                except Exception as e:print('Job reconciliation:',job['id'],type(e).__name__,str(e)[:300],flush=True)
            self.refresh_inventory(client)
            # Multiple disjoint leases use the same central allocator; no local queue.
            for _ in range(8):
                if len(self.jobs)>=self.cfg.get('max_parallel',256):break
                job=self.api('/claim').get('job')
                if not job:break
                with self.state_lock:self.jobs[job['id']]=job;self.persist()
                self.tick_job(client,job);self.heartbeat()
        finally:client.close()
    def tick_job(self,client,job):
        if time.time()-self.inventory.get('at',0)>15:self.refresh_inventory(client)
        request=dict(action='status',id=job['id'],lease_digest=hashlib.sha256(job['lease'].encode()).hexdigest(),work_volume=job['spec'].get('work_volume') or self.cfg.get('default_work_volume','/Dataset2'),receipt_first=self.inventory.get('receipt_before_archive') is True)
        result=self.rpc(client,request)
        if self.controls.get(job['id'])=='cancel':result=self.rpc(client,dict(request,action='cancel'))
        if result['state']=='absent' or result['state']=='staged':
            self.api('/jobs/'+job['id']+'/update',dict(status='preparing',detail='通过 SSH 准备独立 Linux 工作目录'),lease=job['lease'])
            self.queue_artifact(job)
            return
        if result.get('usage'):
            usage=result['usage'];offset=self.inventory.get('remote_offset',self.remote_offset);usage['remote_at']=usage['at'];usage['at']+=offset
            if isinstance(usage.get('progress'),dict) and isinstance(usage['progress'].get('updated_at'),(int,float)):usage['progress']['updated_at']+=offset
        with getattr(self,'state_lock',nullcontext()):self.observation[job['id']]=result
        if result['state'] in ('finished','archiving'):
            # Completion accounting must not wait behind another result's transfer.
            if result.get('report',{}).get('cleanup_verified') is not True:
                raise RuntimeError('Missing verified process cleanup; compute lease retained')
            if not job.get('_compute_released'):
                self.api('/jobs/'+job['id']+'/stopped',dict(cleanup_verified=True,execution_report=result['report']),lease=job['lease'])
                job['_compute_released']=True;self.persist()
            self.queue_artifact(job,result)
            return
        if result['state']=='uncertain':
            self.api('/jobs/'+job['id']+'/update',dict(status='recovery_needed',detail='SSH 启动结果待对账；保留唯一租约，不重复启动'),lease=job['lease']);return
        phase='running' if result.get('usage') else 'preparing'
        log=''
        if phase=='running':
            sftp=client.open_sftp()
            try:
                with sftp.open(self.remote_folder(job)+'/task.log') as f:
                    size=f.stat().st_size;f.seek(max(0,size-65536));log=f.read().decode(errors='replace')
            except IOError:pass
            finally:sftp.close()
        self.api('/jobs/'+job['id']+'/update',dict(status=phase,detail='Linux SSH 外部计算中' if phase=='running' else 'Linux 独立环境准备中',log=log),lease=job['lease'])
        self.heartbeat()
    def run(self):
        import msvcrt
        lock=(self.root/'bridge.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        threading.Thread(target=self.pulse,daemon=True).start()
        try:
            while True:
                try:self.tick()
                except Exception as e:print('SSH bridge:',type(e).__name__,str(e)[:500],flush=True)
                time.sleep(5)
        finally:self.stop.set();lock.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    sys.path.insert(0,str(Path(args.config).resolve().parent.parent.parent/'external-ssh-libs'))
    Bridge(args.config).run()
