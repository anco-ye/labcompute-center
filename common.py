"""Shared file validation and atomic state helpers."""
import resource_policy
from resource_metadata import program_digest
import hashlib
import math
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid
import zipfile

PLATFORM_RELEASE='2026-10-24-component-remediation-v2'


WORKER_FILES=('compute_only.py','deploy/grant_batch_logon.ps1','storage_layout.py','storage_daemon.py','provision_job_storage.py','resource_metadata.py','resource_policy.py','process_scope.py','execution_broker.py','health_watchdog.py','legacy_terminal.py','resource_monitor.py','agent.py','runner.py','execution_contract.py','python_entry.py','runtime_support/sitecustomize.py','runtime_support/lab_task_support.py','runtime_support/lab_network_policy.py','runtime_support/lab_vector_compat.py','science_control.py','science_restart.py','simulation_monitor.py','research_discovery.py','common.py','release_identity.py','deploy/finish_worker_update.py')


def worker_release(root):
    import hashlib
    digest=hashlib.sha256()
    for name in WORKER_FILES:
        digest.update(name.encode());digest.update(b'\0');digest.update((Path(root)/name).read_bytes());digest.update(b'\0')
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for attempt in range(7):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt==6:raise
                time.sleep(.025*2**attempt)
    finally:
        tmp.unlink(missing_ok=True)


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('入口必须是 ZIP 内的相对路径，使用 / 分隔')
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ('..', '.') for x in value.split('/')):
        raise ValueError('路径不可越出任务目录')
    if any(re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', x) or x.endswith((' ', '.')) for x in p.parts):
        raise ValueError('不支持 Windows 保留文件名')
    return p.as_posix()


def inspect_zip(path, max_bytes=8 * 1024**3, max_files=20000, case_sensitive=False):
    total = 0
    names = set()
    folded = set()
    with zipfile.ZipFile(path) as z:
        if len(z.infolist()) > max_files:
            raise ValueError('ZIP 文件数量超过 '+str(max_files))
        for info in z.infolist():
            name = relative_path(info.filename.rstrip('/'))
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('ZIP 不允许包含符号链接')
            identity=name if case_sensitive else name.casefold()
            if identity in folded:
                raise ValueError('ZIP 内存在重名文件')
            folded.add(identity)
            if not info.is_dir():
                names.add(name)
            total += info.file_size
            if total > max_bytes:
                raise ValueError('ZIP 解压大小超过 '+str(round(max_bytes/1024**3,2))+' GiB')
            if info.flag_bits & 1:
                raise ValueError('不支持加密 ZIP')
    return names


def extract_zip(source, dest):
    inspect_zip(source)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as z:
        z.extractall(dest)


def task_spec(raw, files):
    kind = raw.get('kind', 'python')
    if kind not in ('python', 'matlab', 'comsol'):
        raise ValueError('不支持的任务类型')
    entry = relative_path(raw.get('entry', ''))
    if entry not in files or Path(entry).suffix.lower() != {'python': '.py', 'matlab': '.m', 'comsol': '.mph'}[kind]:
        raise ValueError('入口不存在或扩展名不匹配')
    args = raw.get('args', [])
    if not isinstance(args, list) or len(args) > 100 or any(not isinstance(x, str) or len(x) > 2048 or '\x00' in x for x in args):
        raise ValueError('参数应为字符串数组')
    if kind != 'python' and args:
        raise ValueError('MATLAB/COMSOL 参数请写在脚本或模型中')
    cores = int(raw.get('cores', 1))
    memory = float(raw.get('memory_gb', 2))
    estimate = float(raw.get('estimated_minutes', 30))
    maximum = float(raw.get('max_minutes', max(estimate * 3, 60)))
    if not (1 <= cores <= 256 and .25 <= memory <= 1024 and 1 <= estimate <= 43200 and estimate <= maximum <= 43200):
        raise ValueError('CPU、内存或时间设置超出允许范围')
    runtime = str(raw.get('runtime', kind))
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', runtime):
        raise ValueError('运行环境名称无效')
    platform=raw.get('os','windows')
    if platform not in ('windows','linux'):raise ValueError('os 必须是 windows 或 linux')
    return dict(os=platform,name=str(raw.get('name', Path(entry).stem))[:120], kind=kind,
                entry=entry, args=args, runtime=runtime, cores=cores,
                memory_gb=memory, estimated_minutes=estimate, max_minutes=maximum,
                target=str(raw.get('target', 'auto'))[:50])


def package_specs(path, files):
    """Versioned, self-contained upload contract. Resource policy is controller owned."""
    with zipfile.ZipFile(path) as z:
        if 'manifest.json' not in files:
            raise ValueError('任务包根目录缺少 manifest.json；请将任务上传文档交给 AI 准备 ZIP')
        if z.getinfo('manifest.json').file_size > 1024**2:
            raise ValueError('manifest.json 过大')
        manifest = json.loads(z.read('manifest.json').decode('utf-8-sig'))
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
            raise ValueError('manifest.json 需要 schema_version: 1')
        tasks = manifest.get('tasks')
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 200:
            raise ValueError('每包支持 1–200 个独立任务')
        code = hashlib.sha256()
        for name in sorted(files):
            if name != 'manifest.json':
                code.update(name.encode())
                with z.open(name) as f:
                    for chunk in iter(lambda: f.read(1024**2), b''):
                        code.update(chunk)
        program=program_digest(z)
        specs = []
        for position,raw in enumerate(tasks):
            if not isinstance(raw, dict):
                raise ValueError('tasks 中的每一项必须是对象')
            # Ignore client supplied scheduling values, including legacy time fields.
            clean = {k: raw[k] for k in ('name','kind','entry','args','runtime','os') if k in raw}
            s = task_spec(clean, files)
            key=raw.get('id','task-'+str(position+1));depends=raw.get('depends_on',[])
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key):raise ValueError('任务 id 仅支持字母、数字、横线、下划线')
            if not isinstance(depends,list) or len(depends)>200 or any(not isinstance(x,str) for x in depends) or len(set(depends))!=len(depends):raise ValueError('depends_on 应为不重复的任务 id 数组')
            s.update(task_key=key,depends_on=depends)
            from compute_only import require_computation
            require_computation(path, dict(raw, task_key=key))

            if manifest.get('name'):
                if not isinstance(manifest['name'],str):raise ValueError('批次名称应为文本')
                s['batch_name']=manifest['name'][:120]
            s.update(auto_resources=True, target='auto', estimated_minutes=None, max_minutes=10080)
            requirement = raw.get('requirements', 'requirements.txt' if 'requirements.txt' in files and s['kind']=='python' else None)
            if requirement:
                requirement = relative_path(requirement)
                if s['kind'] != 'python' or requirement not in files:
                    raise ValueError('依赖文件不存在或任务不是 Python')
                if z.getinfo(requirement).file_size > 1024**2:
                    raise ValueError('依赖文件过大')
            s['requirements'] = requirement
            from platform_contract import normalize
            variants=normalize(raw,s,files,z)
            if variants:s['platforms']=variants
            resource = raw.get('resources', {})
            if not isinstance(resource, dict): raise ValueError('resources 必须为对象')
            cpu = resource.get('cpus', 1 if s['kind']=='python' else 4)
            memory = resource.get('memory_gb', 4 if s['kind']=='python' else 8)
            disk = resource.get('disk_gb', 4)
            if isinstance(cpu, bool) or not isinstance(cpu, int) or not 1 <= cpu <= 256:
                raise ValueError('resources.cpus 应为 1–256 的整数')
            if any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in (memory,disk)) or not .25 <= memory <= 1024 or not 1 <= disk <= 16384:
                raise ValueError('内存或磁盘资源请求无效')
            unpacked = sum(z.getinfo(n).file_size for n in files)/1024**3
            commit=resource.get('commit_gb',memory)
            if isinstance(commit,bool) or not isinstance(commit,(int,float)) or not math.isfinite(commit) or not memory<=commit<=4096:
                raise ValueError('resources.commit_gb 应不小于 memory_gb，且不超过 4096 GiB')
            s.update(cores=cpu, memory_gb=memory, commit_gb=commit, disk_gb=max(disk, unpacked*2+2))
            resource_policy.configure(s,resource,raw.get('restart_policy','never'))
            gpus=resource.get('gpus',0);gpu_memory=resource.get('gpu_memory_gb',0);wall=resource.get('wall_minutes')
            if isinstance(gpus,bool) or not isinstance(gpus,int) or not 0<=gpus<=16:
                raise ValueError('resources.gpus 应为 0–16 的整数')
            if isinstance(gpu_memory,bool) or not isinstance(gpu_memory,(int,float)) or not math.isfinite(gpu_memory) or not 0<=gpu_memory<=256:
                raise ValueError('显存请求无效')
            if wall is not None and (isinstance(wall,bool) or not isinstance(wall,(int,float)) or not math.isfinite(wall) or not 1<=wall<=10080):
                raise ValueError('最长运行时间应为 1–10080 分钟')
            s.update(gpus=gpus,gpu_memory_gb=gpu_memory)
            if wall is not None:s['wall_minutes']=wall
            identity = [code.hexdigest(), s['kind'], s['runtime'], s['entry'], s['args'], s['cores'], s['requirements'],s.get('gpus',0),s.get('gpu_memory_gb',0)]
            if s['os']=='linux':identity.append('linux')
            if s.get('platforms'):identity.append(s['platforms'])
            s['profile'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            model=resource_policy.workload_model(resource.get('workload'),[program,s['kind'],s['runtime'],s['entry'],s['requirements'],s['cores']])
            if model:s['resource_model']=model
            specs.append(s)
        keys={s['task_key']:s for s in specs}
        if len(keys)!=len(specs):raise ValueError('任务 id 重复')
        visited=set();visiting=set()
        def visit(key):
            if key not in keys:raise ValueError('依赖任务不存在：'+key)
            if key in visiting:raise ValueError('任务依赖存在循环')
            if key in visited:return
            visiting.add(key)
            for dependency in keys[key]['depends_on']:visit(dependency)
            visiting.remove(key);visited.add(key)
        for key in keys:visit(key)
        if any(s['depends_on'] for s in specs) and any(n.split('/')[0]=='dependencies' for n in files):raise ValueError('dependencies 目录由中心保存前序结果，请勿放入输入包')
        return specs


def read_progress(work):
    try:
        p = Path(work)/'progress.json'
        if p.stat().st_size > 4096:
            return {}
        v = json.loads(p.read_text(encoding='utf-8-sig'))
        if any(k in v for k in ('scope','stage','metrics','unit')):
            from task_progress import normalize
            return normalize(v)
        done, total = float(v['completed']), float(v['total'])
        if math.isfinite(done) and math.isfinite(total) and 0 <= done <= total and total > 0:
            return dict(completed=done, total=total)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def read_json_shared(path):
    """Share deletion where supported and close the handle before JSON parsing."""
    path=Path(path)
    if os.name!='nt':
        with path.open('r',encoding='utf-8-sig') as file:data=file.read()
        return json.loads(data)
    import ctypes,msvcrt
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,7,None,3,0x80,None)
    if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:kernel.CloseHandle(handle);raise
    with os.fdopen(fd,'r',encoding='utf-8-sig') as file:data=file.read()
    return json.loads(data)


def fair_transfer_order(jobs, waiting, served, priority, now, max_wait=30):
    """Small transfers go first until an older bounded pass becomes overdue."""
    ids={j['id'] for j in jobs}
    for state in (waiting,served):
        for jid in list(state):
            if jid not in ids:state.pop(jid,None)
    for jid in ids:waiting.setdefault(jid,now)
    def key(job):
        last=served.get(job['id'],waiting[job['id']])
        return (0,last,priority(job)) if now-last>=max_wait else (1,priority(job),last)
    return sorted(jobs,key=key)
