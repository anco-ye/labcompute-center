"""On-demand SSH command. No resident worker; only our own transient job units."""
import fcntl,hashlib,json,os,re,shutil,subprocess,sys,tempfile,time,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HOST_CFG=json.loads((ROOT/'host-config.json').read_text()) if (ROOT/'host-config.json').exists() else {}
PYTHON=Path(HOST_CFG.get('python',str(ROOT/'runtimes/neuron902-py311/bin/python')))
SELF=ROOT/'tools/linux_rpc.py'
GPU_PYTHON=Path(HOST_CFG.get('gpu_python',str(ROOT/'runtimes/torch251-cu124-py311/bin/python')))
CPU_RESERVE=HOST_CFG.get('cpu_reserve',4)
MEMORY_RESERVE=HOST_CFG.get('memory_reserve_gb',64)
DISK_RESERVE=HOST_CFG.get('disk_reserve_gb',64)
DEFAULT_VOLUME=HOST_CFG.get('default_volume','/Dataset2')
VOLUME_MOUNTS=tuple(HOST_CFG.get('volume_roots',{})) or ('/Dataset2','/Dataset1','/Dataset5','/CodeStorage','/Dataset3','/Dataset0','/Dataset4')
SANDBOX_PROTOCOL=2
RPC_RELEASE='2026-10-25-unlimited-runtime-v1'

def execution_path(selected):
    paths=[str(selected.parent)]
    configured=HOST_CFG.get('native_toolchain_bin')
    if configured:
        directory=Path(configured).resolve()
        runtime_root=(ROOT/'runtimes').resolve()
        if not directory.is_relative_to(runtime_root) or not directory.is_dir() or ':' in str(directory):
            raise ValueError('Native toolchain must be a directory inside node runtimes')
        paths.append(str(directory))
    return ':'.join(paths+['/usr/bin','/bin'])


def cuda_visible_devices(spec):
    allocated=spec.get('gpu_ids',[])
    # The filesystem sandbox exposes only allocated /dev/nvidia* devices.
    # CUDA enumerates that visible set from zero; host IDs remain in spec.
    visible=range(len(allocated)) if spec.get('requires_isolation') else allocated
    return ','.join(map(str,visible))

def observe_disk_bytes(folder):
    """A failed directory measurement is not an application failure."""
    observed=dict(at=time.time(),state='unavailable',bytes=None)
    try:
        result=subprocess.run(['/usr/bin/du','-sb',str(folder)],
                              capture_output=True,timeout=8)
    except (OSError,subprocess.TimeoutExpired) as exc:
        return dict(observed,error=(type(exc).__name__+': '+str(exc))[:512])
    try:
        size=int(result.stdout.split()[0])
        if size<0:raise ValueError('Negative directory size')
    except (ValueError,IndexError,TypeError):
        return dict(observed,error='Invalid directory size observation')
    if result.returncode:
        message=result.stderr.decode(errors='replace') if isinstance(result.stderr,bytes) else str(result.stderr)
        return dict(observed,state='partial',bytes=size,error=message[:512],returncode=result.returncode)
    return dict(observed,state='complete',bytes=size)

def disk_limit_exceeded(observation,budget):
    # Partial du output may reflect concurrent creates/removes. Retain it as
    # evidence, but do not treat a failed sample as a reliable quota verdict.
    return observation.get('state')=='complete' and observation['bytes']>budget


def sandbox_ready():
    try:
        proof=json.loads((ROOT/'logs/isolation-verification.json').read_text())
        return proof.get('protocol')==SANDBOX_PROTOCOL and proof.get('verified') is True and bool(shutil.which('bwrap'))
    except (OSError,ValueError):return False


def sandbox_command(folder,spec):
    """Hide host-owned research/credentials and expose only this task and runtimes."""
    executable=shutil.which('bwrap')
    if not executable:raise ValueError('Linux task filesystem sandbox unavailable')
    selected=GPU_PYTHON if spec.get('runtime')=='python-gpu-linux' else PYTHON
    base=subprocess.check_output([str(selected),'-B','-c','import sys;print(sys.base_prefix)'],text=True,timeout=15).strip()
    readonly=[ROOT/'tools',ROOT/'runtimes',Path(base)]
    if HOST_CFG.get('comsol_executable'):
        readonly.append(Path(HOST_CFG['comsol_executable']).resolve().parents[1])
    if (ROOT/'host-config.json').is_file():readonly.append(ROOT/'host-config.json')
    hidden={Path('/home'),Path('/root'),Path('/run'),Path('/tmp'),Path('/var/tmp'),Path('/mnt'),Path('/media')}
    hidden.update(Path('/Dataset'+str(i)) for i in range(6));hidden.add(Path('/CodeStorage'))
    # A configured root filesystem is not a work-volume mount to mask wholesale.
    hidden.update(Path(v) for v in VOLUME_MOUNTS if v!='/')
    args=[executable,'--die-with-parent','--new-session','--unshare-user','--unshare-pid',
          '--unshare-uts','--unshare-ipc','--ro-bind','/','/']
    for path in sorted(hidden,key=str):
        if path.is_dir():args+=['--tmpfs',str(path)]
    for path in sorted(set(readonly),key=lambda p:len(p.parts)):
        if path.exists():args+=['--ro-bind',str(path),str(path)]
    args+=['--bind',str(folder),str(folder),'--proc','/proc','--dev','/dev']
    if spec.get('gpu_ids'):
        devices=[Path('/dev/nvidiactl'),Path('/dev/nvidia-uvm'),Path('/dev/nvidia-uvm-tools')]
        devices.extend(Path('/dev/nvidia'+str(i)) for i in spec['gpu_ids'])
        for path in devices:
            if path.exists():args+=['--dev-bind',str(path),str(path)]
    return args+['--chdir',str(folder/'work'),'--',str(PYTHON),'-B',str(SELF),'run',str(folder)]

def volume_root(volume):
    if volume not in VOLUME_MOUNTS:raise ValueError('Unknown work volume')
    return Path(HOST_CFG['volume_roots'][volume]) if HOST_CFG.get('volume_roots') else ROOT if volume==DEFAULT_VOLUME else Path(volume)/'kxy-lab-compute-center'

def storage_inventory():
    rows=[];seen=set()
    for volume in VOLUME_MOUNTS:
        mount=Path(volume);workspace=volume_root(volume)
        if not mount.is_mount():continue
        device=mount.stat().st_dev
        if device in seen:continue
        seen.add(device);d=shutil.disk_usage(mount)
        writable=workspace.is_dir() and not workspace.is_symlink() and workspace.stat().st_uid==os.getuid() and os.access(workspace,os.W_OK)
        rows.append(dict(id=volume,path=str(workspace/'jobs'),total_gb=d.total/2**30,free_gb=d.free/2**30,reserve_gb=DISK_RESERVE,writable=writable,schedulable=writable and d.free/2**30>DISK_RESERVE+2))
    return rows

def atomic(path,value):
    # Concurrent status/prepare requests must never share a staging pathname.
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix='.'+path.name+'.',suffix='.tmp',delete=False) as stream:
            tmp=Path(stream.name);stream.write(json.dumps(value))
        tmp.replace(path)
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)

def environment():
    uid=str(os.getuid())
    return dict(os.environ,XDG_RUNTIME_DIR='/run/user/'+uid,DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/'+uid+'/bus',CUDA_VISIBLE_DEVICES='',PYTHONDONTWRITEBYTECODE='1')

def ctl(unit):
    p=subprocess.run(['systemctl','--user','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID','-p','Result','-p','ExecMainStatus','-p','ControlGroup','-p','MemoryPeak','-p','TasksCurrent'],capture_output=True,text=True,timeout=8,env=environment())
    return dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)

def validate(request):
    jid=request['id'];digest=request['lease_digest']
    if not re.fullmatch('[a-f0-9]{32}',jid) or not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('Invalid identity')
    chosen=request.get('work_volume') or request.get('spec',{}).get('work_volume')
    locations=ROOT/'locations';location=locations/(jid+'.json')
    # Bind a unique placement before upload/launch; reconnects cannot move it.
    if location.exists():
        saved=json.loads(location.read_text())
        if saved['lease_digest']!=digest or chosen and chosen!=saved['volume']:raise ValueError('Job placement or lease mismatch')
        chosen=saved['volume']
    chosen=chosen or DEFAULT_VOLUME;workspace=volume_root(chosen)
    if workspace.is_symlink() or (workspace/'jobs').is_symlink():raise ValueError('Unsafe workspace')
    folder=workspace/'jobs'/jid
    if folder.is_symlink():raise ValueError('Unsafe directory')
    if request['action'] in ('prepare','start'):
        locations.mkdir(mode=0o700,exist_ok=True)
        with (locations/(jid+'.lock')).open('a') as placement_lock:
            fcntl.flock(placement_lock,fcntl.LOCK_EX)
            if location.exists():
                saved=json.loads(location.read_text())
                if saved!=dict(volume=chosen,lease_digest=digest):raise ValueError('Job placement or lease mismatch')
            else:
                if chosen!=DEFAULT_VOLUME and (ROOT/'jobs'/jid).exists():raise ValueError('Existing job cannot move volumes')
                atomic(location,dict(volume=chosen,lease_digest=digest))
    return folder,'labcenter-'+jid+'.service'

def package_cache(key):
    if not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{32}-[0-9]{1,22}-[0-9]{1,14}',key):
        raise ValueError('Invalid controller package identity')
    cache=ROOT/'package-cache'
    if cache.is_symlink():raise ValueError('Unsafe package cache')
    cache.mkdir(mode=0o700,exist_ok=True)
    folder=cache/key
    if folder.is_symlink():raise ValueError('Unsafe package cache identity')
    folder.mkdir(mode=0o700,exist_ok=True)
    return folder/'package.zip'


def attach_package(folder,key,size):
    source=package_cache(key)
    if source.is_symlink() or not source.is_file() or source.stat().st_size!=size:
        raise ValueError('Incomplete cached package')
    dest=folder/'package.zip'
    if dest.is_symlink():raise ValueError('Unsafe package destination')
    if dest.is_file():
        if dest.stat().st_size!=size:raise ValueError('Existing package size changed')
        return
    # Each task can write its own folder: never hard-link it to the shared input.
    temp=folder/'package.zip.cache-copy'
    if temp.is_symlink():raise ValueError('Unsafe temporary package copy')
    if temp.exists():temp.unlink() # exclusive per-job RPC lock owns this recoverable copy
    try:
        with source.open('rb') as src,temp.open('xb') as dst:shutil.copyfileobj(src,dst,1024**2)
        if temp.stat().st_size!=size:raise ValueError('Incomplete package copy')
        temp.replace(dest)
    finally:temp.unlink(missing_ok=True)


def extract(source,dest):
    with zipfile.ZipFile(source) as z:
        total=0;names=set()
        for i in z.infolist():
            p=Path(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or i.filename.casefold() in names or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe archive')
            names.add(i.filename.casefold());total+=i.file_size
            if total>8*2**30 or len(names)>20000:raise ValueError('Archive too large')
        z.extractall(dest)

def application_command(folder, spec, python):
    work=Path(folder)/'work'
    if spec['kind']=='python':
        return [str(python),'-B',str(work/spec['entry']),*spec.get('args',[])]
    if spec['kind']!='comsol' or spec.get('runtime')!='comsol-linux':
        raise ValueError('Unsupported Linux application')
    executable=Path(HOST_CFG.get('comsol_executable','/__unpublished_comsol__'))
    if not executable.is_file():raise ValueError('COMSOL runtime unavailable')
    if Path(spec['entry']).suffix.lower()!='.mph' or spec.get('args') or spec.get('requirements'):
        raise ValueError('COMSOL requires an MPH model without Python requirements or extra arguments')
    return [str(executable),'-np',str(spec['cores']),'-tmpdir',str(Path(folder)/'tmp'),
            '-recoverydir',str(Path(folder)/'tmp'), 'batch','-inputfile',str(work/spec['entry']),
            '-outputfile',str(work/'computed_model.mph'),'-batchlog',str(work/'comsol_batch.log')]


def supported_runtime(spec):
    kind=spec.get('kind');runtime=spec.get('runtime')
    available=HOST_CFG.get('runtimes',('python-linux','python-science-linux','python-neuron-linux','python-gpu-linux'))
    if runtime not in available:return False
    return (kind=='python' and runtime.startswith('python-') or
            kind=='comsol' and runtime=='comsol-linux' and not spec.get('args') and not spec.get('requirements')
            and Path(spec.get('entry','')).suffix.lower()=='.mph'
            and Path(HOST_CFG.get('comsol_executable','/__missing__')).is_file())


def run_job(folder):
    from execution_contract import prepare, outcome
    import psutil
    request=json.loads((folder/'request.json').read_text());s=request['spec'];work=folder/'work'
    os.sched_setaffinity(0,set(s['cpu_ids']))
    env=dict(environment(),OMP_NUM_THREADS=str(s['cores']),OPENBLAS_NUM_THREADS=str(s['cores']),MKL_NUM_THREADS=str(s['cores']),NUMEXPR_NUM_THREADS=str(s['cores']),TMPDIR=str(folder/'tmp'),HOME=str(folder/'home'),NRN_NOGUI='1',LAB_JOB_DIR=str(folder),LAB_COMPUTE_THREADS=str(s['cores']),PYTHONNOUSERSITE='1',PYTHONUNBUFFERED='1',MPLBACKEND='Agg',PATH=str(PYTHON.parent)+':/usr/bin:/bin')
    for name in ('tmp','home'):(folder/name).mkdir(exist_ok=True)
    selected=GPU_PYTHON if s.get('runtime')=='python-gpu-linux' else PYTHON
    env.update(CUDA_VISIBLE_DEVICES=cuda_visible_devices(s),PATH=execution_path(selected))
    if s['kind']=='python':
        env.update(PYTHONPATH=str(ROOT/'tools/runtime_support'),LAB_TASK_NETWORK_POLICY='bounded-v1')
    if s['kind']=='comsol':
        for key in ('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY'):env.pop(key,None)
    # Dependencies were installed in an immutable, separately prepared runtime.
    start=time.time();peak=0;cpu={};main=None
    disk_observation={};next_disk_sample=0
    try:
        with (folder/'task.log').open('w') as log:
            python=selected
            if s.get('requirements'):
                requirement=Path(s['requirements'])
                if requirement.is_absolute() or '..' in requirement.parts:raise ValueError('Invalid requirements path')
                venv=folder/'venv'
                subprocess.run([str(selected),'-B','-m',HOST_CFG.get('venv_module','venv'),str(venv)],check=True,stdout=log,stderr=subprocess.STDOUT,env=env,timeout=120)
                # Nested venvs inherit the system interpreter, not the published
                # parent runtime's packages. Expose only that controlled runtime;
                # per-job requirements retain precedence over its packages.
                query='import sysconfig;print(sysconfig.get_path("purelib"))'
                base_site=subprocess.check_output([str(selected),'-B','-c',query],env=env,text=True,timeout=15).strip()
                job_site=subprocess.check_output([str(venv/'bin/python'),'-B','-c',query],env=env,text=True,timeout=15).strip()
                (Path(job_site)/'labcenter-runtime.pth').write_text(base_site+'\n')
                python=venv/'bin/python';env.update(PIP_CACHE_DIR=str(folder/'pip-cache'),PATH=str(venv/'bin')+':'+env['PATH'])
                from runtime_support.lab_task_support import install_pip
                def pip_attempt(command, remaining):
                    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,env=env,timeout=remaining)
                    log.flush()
                    return result.returncode
                install_pip([str(python),'-B','-m','pip','--isolated','install','--only-binary=:all:','--index-url','https://pypi.org/simple','-r',str(work/requirement)],pip_attempt,folder/'task.log',folder/'pip-cache')
            start=time.time()
            argv=prepare(folder,s['kind'],application_command(folder,s,python))
            log.flush();log_offset=(folder/'task.log').stat().st_size
            main=subprocess.Popen(argv,cwd=work,env=env,stdout=log,stderr=subprocess.STDOUT)
            while main.poll() is None:
                now=time.time();processes=[]
                try:processes=[psutil.Process(main.pid),*psutil.Process(main.pid).children(recursive=True)]
                except psutil.Error:pass
                ram=0;seconds=0;identities=[]
                for p in processes:
                    try:
                        ram+=p.memory_info().rss;ct=p.cpu_times();seconds+=ct.user+ct.system;identities.append(dict(pid=p.pid,created=p.create_time()))
                    except psutil.Error:pass
                prior=cpu;cpu=dict(at=now,seconds=seconds);cores=max(0,(seconds-prior['seconds'])/max(.001,now-prior['at'])) if prior else 0
                cgroup=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().strip().split('::')[1].lstrip('/')
                measured=int((cgroup/'memory.current').read_text());kernel_peak=cgroup/'memory.peak';peak=max(peak,ram,measured,int(kernel_peak.read_text()) if kernel_peak.exists() else 0)
                progress={}
                pp=work/'progress.json'
                if pp.is_file() and not pp.is_symlink() and pp.stat().st_size<65536:
                    try:
                        progress=json.loads(pp.read_text())
                        if not isinstance(progress,dict):progress={}
                        if progress:progress['updated_at']=pp.stat().st_mtime
                    except (ValueError,OSError,TypeError):progress={}
                if now>=next_disk_sample:
                    disk_observation=observe_disk_bytes(folder);next_disk_sample=time.time()+5
                    if disk_limit_exceeded(disk_observation,s.get('disk_gb',4)*2**30):raise RuntimeError('disk_limit: 任务工作目录超过磁盘预算，已有输出保留')
                atomic(folder/'usage.json',dict(at=now,cpu_cores=cores,rss_gb=ram/2**30,peak_gb=peak/2**30,elapsed_seconds=now-start,processes=identities,progress=progress,disk_observation=disk_observation,runner_release=RPC_RELEASE))
                time.sleep(1)
            code=main.returncode
        result=outcome(folder,s['kind'],code,log_offset)
        atomic(folder/'execution-outcome.json',result)
        native_code=code;code=result['derived_exit_code']
        events=dict(line.split() for line in (cgroup/'memory.events').read_text().splitlines()) if 'cgroup' in locals() else {}
        atomic(folder/'program-exit.json',dict(exit_code=code,native_exit_code=native_code,execution_outcome=result,detail=('Linux cgroup 内存限额触发；应提高 memory_gb 或 commit_gb 预算，不自动重算' if int(events.get('oom_kill',0)) else result['evidence'][0]['message'] if result['evidence'] else f'exit={code}'),started=start,elapsed_seconds=time.time()-start,peak_gb=peak/2**30,**(dict(failure_code='resource_limit',failure_evidence={'recommendation':'提高 memory_gb 或 commit_gb 预算；不自动重算'}) if int(events.get('oom_kill',0)) else {})))
    except Exception as e:
        atomic(folder/'program-exit.json',dict(exit_code=1,started=start,elapsed_seconds=time.time()-start,peak_gb=peak/2**30,detail=type(e).__name__+': '+str(e)[:300]))
        raise

def finalize(folder,unit,state,archive_result=True):
    if state.get('LoadState')=='not-found':
        if state.get('MainPID') not in (None,'0',''):return False
        if state.get('TasksCurrent') not in (None,'0','[not set]','18446744073709551615'):return False
        # Cancelled units may be collected before writing a native exit; still require cgroup and PID cleanup.
        launch=folder/'launch.json'
        rejected=json.loads(launch.read_text()) if launch.exists() else {}
        rejected=type(rejected.get('code')) is int and rejected['code']!=0
        if not (folder/'program-exit.json').exists() and not (folder/'cancelled.json').exists() and not rejected:return False
        # Transient successful units can be garbage-collected by systemd.
        # Require both native exit and disappearance of the exact unit cgroup
        # plus the last known PID/create-time identities before finalization.
        import psutil
        group=Path('/sys/fs/cgroup/user.slice')/('user-'+str(os.getuid())+'.slice')/('user@'+str(os.getuid())+'.service')/'app.slice'/unit
        if group.exists():return False
        usage=json.loads((folder/'usage.json').read_text()) if (folder/'usage.json').exists() else {}
        for ref in usage.get('processes',[]):
            try:
                if abs(psutil.Process(ref['pid']).create_time()-ref['created'])<.01:return False
            except psutil.NoSuchProcess:pass
        if rejected and not (folder/'program-exit.json').exists():
            launch_report=json.loads(launch.read_text())
            atomic(folder/'program-exit.json',dict(exit_code=1,elapsed_seconds=0,
                failure_code='environment_setup',failure_evidence=dict(phase='preparing',execution_started=False),
                detail='Linux 执行环境拒绝启动：'+str(launch_report.get('detail',''))[:1000]))
        state=dict(state,ActiveState='inactive',Result='success',TasksCurrent='0',collected_unit_cleanup_verified=True)
    exited=state.get('ActiveState')=='active' and state.get('SubState')=='exited' and state.get('MainPID')=='0'
    if state.get('ActiveState') not in ('inactive','failed') and not exited:return False
    if state.get('TasksCurrent') not in (None,'0','[not set]','18446744073709551615'):return False
    output=folder/'exit.json'
    if not output.exists():
        native=folder/'program-exit.json';report=json.loads(native.read_text()) if native.exists() else dict(exit_code=1,elapsed_seconds=max(0,time.time()-json.loads((folder/'start.json').read_text())['at']))
        result=state.get('Result');code=report.get('exit_code',1)
        if result not in ('success',None):code=code or 1
        report.update(exit_code=code,cleanup_verified=True,systemd_result=result,platform='linux',memory_measurement='sampled_process_tree_rss_and_cgroup_memory',peak_complete=False,collected_unit_cleanup_verified=state.get('collected_unit_cleanup_verified',False))
        if result=='oom-kill':report.update(failure_code='resource_limit',failure_evidence=dict(basis='linux_cgroup_v2',systemd_result='oom-kill'),detail='Linux cgroup 内存限额触发；请提高 memory_gb 或 commit_gb 预算，不自动重算')
        if result=='timeout':report.update(failure_code='time_limit',detail='运行到达任务时间上限，已有输出保留')
        if (folder/'cancelled.json').exists():report.update(cancelled=True,detail='用户取消，已有输出保留')
        atomic(output,report)
    if not archive_result:return True
    archive=folder/'result.zip'
    if not archive.exists():
        part=folder/'result.zip.partial'
        with zipfile.ZipFile(part,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.write(output,'exit.json')
            if (folder/'task.log').exists():z.write(folder/'task.log','task.log')
            for p in (folder/'work').rglob('*'):
                if p.is_file() and not p.is_symlink() and (folder/'work').resolve() in p.resolve().parents:z.write(p,p.relative_to(folder).as_posix())
        part.replace(archive)
    return True

def host_inventory():
    """Read-only shared-host hardware, never used as a dispatch allocation."""
    import psutil,csv,io
    cache=ROOT/'logs/host-inventory.json'
    try:
        previous=json.loads(cache.read_text())
        if 0<=time.time()-previous['at']<30:return previous
    except (OSError,ValueError,KeyError):pass
    v=psutil.virtual_memory();d=shutil.disk_usage(ROOT)
    models=[x.split(':',1)[1].strip() for x in Path('/proc/cpuinfo').read_text().splitlines() if x.startswith('model name')]
    info=dict(at=time.time(),cpu_model=models[0] if models else 'Unknown',cpu_cores=psutil.cpu_count(logical=False),cpu_threads=psutil.cpu_count(),cpu_percent=psutil.cpu_percent(interval=1.0),memory_total_gb=v.total/2**30,memory_available_gb=v.available/2**30,disk_total_gb=d.total/2**30,disk_free_gb=d.free/2**30,gpus=[],gpu_observed=False,scope='group_owned_host')
    # Display the dedicated home filesystem separately; never add it to work-volume capacity.
    try:
        home=Path('/home/kxy')
        if home.is_mount():
            usage=shutil.disk_usage(home)
            info['home_storage']=dict(path=str(home),total_gb=usage.total/2**30,used_gb=usage.used/2**30,free_gb=usage.free/2**30,scope='kxy_private',at=info['at'])
    except OSError:pass
    try:
        raw=subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.total,memory.free,utilization.gpu','--format=csv,noheader,nounits'],timeout=5,text=True)
        for row in csv.reader(io.StringIO(raw)):
            index,name,total,free,util=row
            info['gpus'].append(dict(index=int(index),name=name.strip(),memory_total_gb=float(total)/1024,memory_free_gb=float(free)/1024,utilization=float(util),schedulable=False))
        raw=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid','--format=csv,noheader,nounits'],timeout=5,text=True)
        uuids={row[1].strip():int(row[0]) for row in csv.reader(io.StringIO(raw))}
        raw=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader,nounits'],timeout=5,text=True)
        busy={uuids.get(row[0].strip()) for row in csv.reader(io.StringIO(raw)) if len(row)==2}
        for g in info['gpus']:g.update(compute_occupied=g['index'] in busy,schedulable=True)
        info['gpu_observed']=True
    except (OSError,ValueError,subprocess.SubprocessError):pass
    atomic(cache,info);return info

def rpc(request):
    action=request['action']
    if action=='inventory':
        import psutil
        v=psutil.virtual_memory();cpu_ids=sorted(os.sched_getaffinity(0))[CPU_RESERVE:]
        volumes=storage_inventory()
        return dict(receipt_before_archive=True,published_runtimes=HOST_CFG.get('published_runtimes',{}),at=time.time(),host_inventory=host_inventory(),cpu_ids=cpu_ids,work_volumes=volumes,host_available_gb=v.available/2**30,disk_available_gb=sum(max(0,row['free_gb']-row['reserve_gb']) for row in volumes if row['writable']),runtime=json.loads((ROOT/'logs/runtime-setup.json').read_text()),isolation_verified=sandbox_ready(),sandbox_protocol=SANDBOX_PROTOCOL,rpc_release=RPC_RELEASE)
    folder,unit=validate(request)
    if action=='status' and not folder.exists():return dict(state='absent')
    if action=='prepare':(folder.parent).mkdir(mode=0o700,exist_ok=True)
    folder.mkdir(mode=0o700,exist_ok=True)
    with (folder/'rpc.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        identity=folder/'identity.json'
        if identity.exists():
            if json.loads(identity.read_text())['lease_digest']!=request['lease_digest']:raise ValueError('Lease mismatch; no execution')
        else:atomic(identity,dict(lease_digest=request['lease_digest']))
        if action=='prepare':
            result=dict(state='staged',folder=str(folder))
            if request.get('package_ref'):result['package_cache']=str(package_cache(request['package_ref']))
            return result
        if action=='attach_package':
            attach_package(folder,request['package_ref'],request['package_size'])
            return dict(state='staged',folder=str(folder))
        state=ctl(unit)
        if action=='start' and not (folder/'start.json').exists():
            s=request['spec']
            allowed=set(sorted(os.sched_getaffinity(0))[CPU_RESERVE:])
            import psutil
            max_ram=psutil.virtual_memory().total/2**30-MEMORY_RESERVE
            if s.get('os')!='linux' or not supported_runtime(s) or not 1<=s['cores']<=len(allowed) or not .25<=s['memory_gb']<=max_ram or not s['memory_gb']<=s.get('commit_gb',s['memory_gb'])<=max_ram or not 1<=s.get('disk_gb',4)<=16384:raise ValueError('Unsupported task resources, runtime or OS')
            gpu_ids=s.get('gpu_ids',[])
            if len(set(gpu_ids))!=s.get('gpus',0) or any(type(i)!=int or i<0 or i>=8 for i in gpu_ids):raise ValueError('GPU allocation invalid')
            if gpu_ids and s.get('runtime')!='python-gpu-linux':raise ValueError('GPU task requires GPU runtime')
            if s.get('runtime')=='python-gpu-linux' and not GPU_PYTHON.is_file():raise ValueError('GPU runtime unavailable')
            if len(set(s['cpu_ids']))!=s['cores'] or not set(s['cpu_ids'])<=allowed:raise ValueError('CPU allocation invalid')
            if s.get('requires_isolation') and not sandbox_ready():raise ValueError('Linux 文件隔离尚未通过平台验证，保留任务等待环境准备')
            if (folder/'package.zip').stat().st_size!=request['package_size']:raise ValueError('Incomplete package transfer')
            work=folder/'work';work.mkdir(exist_ok=True);extract(folder/'package.zip',work)
            entry=Path(s['entry'])
            if entry.is_absolute() or '..' in entry.parts or not (work/entry).is_file():raise ValueError('Entry invalid')
            for key in s.get('dependency_jobs',{}):
                if not re.fullmatch('[A-Za-z0-9_-]{1,80}',key):raise ValueError('Dependency invalid')
                dest=work/'dependencies'/key;dest.mkdir(parents=True,exist_ok=True);extract(folder/('dependency-'+key+'.zip'),dest)
            atomic(folder/'request.json',request)
            atomic(folder/'start.json',dict(at=time.time(),unit=unit)) # durable intent precedes start: never retry an ambiguous start
            args=['systemd-run','--user','--no-block','--unit='+unit]
            for prop in ['MemoryMax='+str(int(s['memory_gb']*2**30)),'MemorySwapMax=0','TasksMax='+str(max(512,s['cores']*64)),'RuntimeMaxSec=infinity','Nice=0','NoNewPrivileges=yes','PrivateUsers=yes','UMask=0077','ProtectSystem=strict','ProtectHome=read-only','PrivateTmp=yes','PrivateDevices='+('no' if gpu_ids else 'yes'),'ReadWritePaths='+str(folder),'WorkingDirectory='+str(work),'KillMode=control-group','RemainAfterExit=yes']:
                args+=['-p',prop]
            args+=(sandbox_command(folder,s) if s.get('requires_isolation') else [str(PYTHON),'-B',str(SELF),'run',str(folder)])
            p=subprocess.run(args,capture_output=True,text=True,timeout=15,env=environment())
            atomic(folder/'launch.json',dict(code=p.returncode,detail=p.stderr[-1000:]))
            state=ctl(unit)
        if action=='retire' and (folder/'exit.json').exists():
            subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=20,env=environment())
        if action=='cancel':
            atomic(folder/'cancelled.json',dict(at=time.time()))
            if not (folder/'start.json').exists():
                (folder/'work').mkdir(exist_ok=True)
                atomic(folder/'start.json',dict(at=time.time(),unit=unit,no_execution_created=True))
                atomic(folder/'program-exit.json',dict(exit_code=-1,elapsed_seconds=0,peak_gb=0))
                state=dict(ActiveState='inactive',LoadState='loaded',Result='success',TasksCurrent='0')
            else:
                subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=20,env=environment());state=ctl(unit)
        # A task-visible or old receipt never overrides current OS liveness.
        cleanup_confirmed=False
        if (folder/'start.json').exists():cleanup_confirmed=finalize(folder,unit,state,archive_result=not request.get('receipt_first',False))
        if cleanup_confirmed and (folder/'exit.json').exists():
            report=json.loads((folder/'exit.json').read_text())
            if not (folder/'result.zip').exists():return dict(state='archiving',report=report,unit=state)
            return dict(state='finished',report=report,result_size=(folder/'result.zip').stat().st_size,unit=state)
        usage=json.loads((folder/'usage.json').read_text()) if (folder/'usage.json').exists() else {}
        return dict(state='running' if state.get('ActiveState') in ('active','activating') else 'uncertain' if (folder/'start.json').exists() else 'staged',usage=usage,unit=state)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='run':run_job(Path(sys.argv[2]))
    else:
        try:print(json.dumps(rpc(json.load(sys.stdin))))
        except Exception as e:print(json.dumps(dict(error=type(e).__name__+': '+str(e)[:1000])));sys.exit(1)
