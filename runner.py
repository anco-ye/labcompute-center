"""Durable job supervisor with isolated Python, bounded preparation and unlimited scientific execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import psutil
from common import atomic_json, read_progress
from process_scope import ProcessScope
from execution_contract import prepare, outcome


def kill_tree(p):
    try:children=p.children(recursive=True)
    except psutil.NoSuchProcess:children=[]
    for child in reversed(children):
        try:child.kill()
        except psutil.NoSuchProcess:pass
    try:p.kill()
    except psutil.NoSuchProcess:pass
    psutil.wait_procs(children+[p],timeout=5)


class JobStop(Exception):pass


def publish_observation(folder, filename, data):
    """Retry observational state next sample without stopping healthy computation."""
    try:
        atomic_json(Path(folder)/filename,data)
    except PermissionError as error:
        if getattr(error,'winerror',None) not in (5,32,33):raise
        try:atomic_json(Path(folder)/'telemetry-write-health.json',dict(status='retrying',at=time.time(),file=filename,winerror=error.winerror,detail=str(error)))
        except PermissionError:pass
        return False
    health=Path(folder)/'telemetry-write-health.json'
    if health.exists():
        try:
            previous=json.loads(health.read_text(encoding='utf-8'))
            if previous.get('status')=='retrying' and previous.get('file','usage.json')==filename:atomic_json(health,dict(status='recovered',at=time.time(),previous=previous))
        except (PermissionError,ValueError):pass
    return True


def publish_usage(folder, data):
    return publish_observation(folder, 'usage.json', data)


def main(folder):
    folder=Path(folder).resolve();work=folder/'work'
    cfg=json.loads((folder/'launch.json').read_text(encoding='utf-8'));spec=cfg['spec']
    atomic_json(folder/'runner.json',dict(pid=os.getpid(),create_time=psutil.Process().create_time()))
    scope=ProcessScope()
    env=os.environ.copy()
    for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','LAB_COMPUTE_THREADS'):
        env[k]=str(spec['cores'])
    env.update(PYTHONUTF8='1',PYTHONUNBUFFERED='1',PYTHONNOUSERSITE='1',PYTHONPATH=str(work/'vendor'),MPLBACKEND='Agg',LAB_JOB_DIR=str(folder),LAB_WHEELHOUSE=cfg.get('wheelhouse',''))
    runtime=cfg.get('runtime',{})
    # Administrator-published native runtimes need their libraries in each
    # isolated job, without borrowing another experiment's environment.
    if runtime.get('python_paths'):
        env['PYTHONPATH']=os.pathsep.join([str(work/'vendor'),*runtime['python_paths']])
    if runtime.get('path_entries'):
        env['PATH']=os.pathsep.join([*runtime['path_entries'],env.get('PATH','')])
    if runtime.get('neuron_home'):env['NEURONHOME']=runtime['neuron_home']
    support=Path(__file__).with_name('runtime_support')
    env['PYTHONPATH']=os.pathsep.join([env['PYTHONPATH'],str(support)])
    env['LAB_TASK_NETWORK_POLICY']='bounded-v1'
    env.pop('PYTHONHOME',None)
    if runtime.get('pip_index_url'):
        # The administrator's CUDA index supplies matching optional wheels too.
        env['PIP_EXTRA_INDEX_URL']=runtime['pip_index_url']
    env['CUDA_VISIBLE_DEVICES']=','.join(map(str,spec.get('gpu_ids',[])))
    env['PIP_CACHE_DIR']=cfg.get('pip_cache',str(folder.parent/'cache'))
    status='failed';detail='';failure={};code=-1;start=time.time();peak=0;peak_private=0;cleanup_verified=False;last_phase=None;process=None;run_start=None;paused_seconds=0;run_pause_baseline=0

    def pause_if_requested(phase):
        nonlocal paused_seconds
        if not (folder/'pause').exists():return
        since=time.time();suspended=[]
        try:
            if process is not None and process.poll() is None:
                # Freeze the parent first so it cannot spawn more children while enumerating.
                for p in [process]+process.children(recursive=True):
                    try:p.suspend();suspended.append(p)
                    except psutil.NoSuchProcess:pass
            try:usage=json.loads((folder/'usage.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):usage={}
            usage.update(phase='paused',resume_phase=phase,peak_gb=peak/1024**3)
            publish_usage(folder,usage)
            while (folder/'pause').exists():
                if (folder/'cancel').exists():raise JobStop('用户取消')
                time.sleep(.25)
        finally:
            for p in reversed(suspended):
                try:p.resume()
                except psutil.NoSuchProcess:pass
            paused_seconds+=time.time()-since
    def execute(argv,phase,log,timeout=None):
        nonlocal process,peak,peak_private,cleanup_verified,last_phase
        attempt_started=time.monotonic()
        last_phase=phase;cleanup_verified=False
        if os.name=='nt' and spec.get('kernel_memory_required'):
            cap=max(spec.get('commit_gb',spec['memory_gb']),2 if phase!='running' else 0)+.25
            scope.set_memory_limit(cap)
            atomic_json(folder/'kernel-memory.json',dict(phase=phase,**scope.memory_state(),supervisor_allowance_gb=.25))
        if (folder/'cancel').exists():raise JobStop('用户取消')
        pause_if_requested(phase)
        log.write(('\n['+phase+'] '+str(argv[0])+'\n').encode())
        process=psutil.Popen(argv,cwd=work,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**scope.launch_options())
        scope.started(process)
        atomic_json(folder/'child.json',dict(pid=process.pid,create_time=process.create_time()))
        descendants={};previous_cpu={};previous_sample=time.monotonic()
        try:
            while process.poll() is None:
                if timeout is not None and time.monotonic()-attempt_started>timeout:raise JobStop('依赖安装超过限定时间，已有缓存保留')
                if scope.memory_limit_exceeded():raise JobStop('超过任务提交内存上限')
                pause_if_requested(phase)
                try:
                    children=process.children(recursive=True)
                    descendants.update({(p.pid,p.create_time()):p for p in children})
                    publish_observation(folder,'process_tree.json',[dict(pid=pid,create_time=created) for pid,created in descendants])
                    rss=0;private=0;current_cpu={};cpu_delta=0
                    for item in [process]+children:
                        try:
                            m=item.memory_info();rss+=m.rss;private+=getattr(m,'private',m.rss)
                            ct=item.cpu_times();key=(item.pid,item.create_time());value=ct.user+ct.system
                            current_cpu[key]=value;cpu_delta+=max(0,value-previous_cpu.get(key,value))
                        except psutil.NoSuchProcess:pass
                    sample=time.monotonic();cpu_cores=cpu_delta/max(.001,sample-previous_sample)
                    previous_cpu=current_cpu;previous_sample=sample
                except (psutil.NoSuchProcess,psutil.AccessDenied):rss=0;private=0;cpu_cores=0
                peak=max(peak,rss);peak_private=max(peak_private,private)
                step={}
                if phase=='running':
                    try:
                        dep=json.loads((folder/'dependency-status.json').read_text(encoding='utf-8'))
                        if dep.get('status')=='installing':step=dict(stage='installing',package=dep['package'])
                    except (OSError,ValueError,KeyError):pass
                publish_usage(folder,dict(at=time.time(),phase=phase,**step,cpu_cores=cpu_cores,private_gb=private/1024**3,rss_gb=rss/1024**3,peak_gb=peak/1024**3,peak_private_gb=peak_private/1024**3,
                    runtime_limit_enforced=False,elapsed_seconds=time.time()-(run_start or start)-paused_seconds+(run_pause_baseline if run_start else 0),progress=read_progress(work) if phase=='running' else {}))
                if (folder/'cancel').exists():raise JobStop('用户取消')
                if rss>spec['memory_gb']*1024**3:raise JobStop('超过任务内存上限')
                if spec.get('commit_gb') and private>spec['commit_gb']*1024**3:raise JobStop('超过任务提交内存上限')
                if phase=='preparing' and time.time()-start-paused_seconds>1800:raise JobStop('环境准备超时，请查看依赖安装日志')
                time.sleep(.5)
            result=process.wait()
            if scope.memory_limit_exceeded():raise JobStop('超过任务提交内存上限')
            if result and scope.memory_limit_bytes:
                tail=(folder/'task.log').read_bytes()[-8192:].decode('utf-8',errors='replace')
                if 'CUDA out of memory' not in tail and any(x in tail for x in ('MemoryError','bad_alloc','STATUS_NO_MEMORY','Cannot allocate memory')):raise JobStop('超过任务提交内存上限')
            # A batch entry must join its children before exiting.
            remaining=[]
            for child in descendants.values():
                try:
                    if child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:remaining.append(child)
                except psutil.NoSuchProcess:pass
            remaining.extend(scope.members())
            if result==0 and remaining:raise JobStop('任务入口已退出，但仍有子进程未完成；已停止遗留进程并保留输出，请修正入口对子进程的等待逻辑后重试')
            return result
        finally:
            if process.poll() is None:kill_tree(process)
            for child in descendants.values():
                try:
                    if child.is_running():kill_tree(child)
                except psutil.NoSuchProcess:pass
            cleanup_verified=scope.clean() and process.poll() is not None
            for child in descendants.values():
                try:
                    if child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:cleanup_verified=False
                except psutil.NoSuchProcess:pass
    try:
        parent=psutil.Process()
        if os.name=='nt':parent.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if spec.get('shared_node') else psutil.NORMAL_PRIORITY_CLASS)
        if hasattr(parent,'cpu_affinity'):
            parent.cpu_affinity(spec.get('cpu_ids',parent.cpu_affinity()[:spec['cores']]))
        runtime=cfg.get('runtime',{})
        atomic_json(folder/'environment.json',{k:v for k,v in runtime.items() if k!='executable'})
        with (folder/'task.log').open('ab',buffering=0) as log:
            argv=list(cfg['argv'])
            if spec['kind']=='python':
                venv=folder/'environment';python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                if execute([runtime.get('executable',argv[0]),'-m','venv',str(venv)],'preparing',log):
                    raise JobStop('Python 独立环境创建失败，详见日志')
                from runtime_support.lab_task_support import install_pip
                def dependency_install(command):
                    return install_pip(command,lambda cmd,remaining:execute(cmd,'preparing',log,timeout=remaining),folder/'task.log',env['PIP_CACHE_DIR'])
                pip=[str(python),'-m','pip','install']
                wheelhouse=Path(cfg.get('wheelhouse','__missing__'))
                if wheelhouse.is_dir():pip+=['--find-links',str(wheelhouse)]
                if runtime.get('protected_packages'):
                    constraints=folder/'runtime-constraints.txt'
                    constraints.write_text('\n'.join(runtime['protected_packages'])+'\n',encoding='utf-8')
                    pip+=['--constraint',str(constraints)]
                    env['PIP_CONSTRAINT']=str(constraints)
                if runtime.get('packages'):
                    # Try the administrator's cache first; a missing cached wheel
                    # must not turn an installable template into a permanent failure.
                    installed=False
                    if wheelhouse.is_dir():
                        installed=execute(pip+['--no-index','--cache-dir',env['PIP_CACHE_DIR']]+runtime['packages'],'preparing',log)==0
                    if not installed:
                        index=['--index-url',runtime['pip_index_url']] if runtime.get('pip_index_url') else []
                        if dependency_install(pip+index+runtime['packages']):raise JobStop('基础环境依赖安装失败，详见日志')
                if spec.get('requirements'):
                    if dependency_install(pip+['-r',str(work/spec['requirements'])]):raise JobStop('项目依赖安装失败，详见日志')
                if execute([str(python),'-m','pip','check'],'preparing',log):raise JobStop('依赖版本冲突，详见日志')
                if spec.get('gpu_ids') and runtime.get('gpu_probe'):
                    if execute([str(python),'-c',runtime['gpu_probe']],'preparing',log):
                        raise JobStop('GPU 环境验证失败，中心将按环境恢复策略处理')
                # Record the resolved package set outside the environment, for result download.
                with (folder/'environment-lock.txt').open('ab',buffering=0) as lock:
                    if execute([str(python),'-m','pip','freeze'],'preparing',lock):raise JobStop('依赖记录失败')
                argv=[str(python),'-u',str(Path(__file__).with_name('python_entry.py')),str(work/(spec.get('entry') or Path(argv[-1]).name)),*spec.get('args',[])]
                env['VIRTUAL_ENV']=str(venv);env['PATH']=str(python.parent)+os.pathsep+env.get('PATH','')
            argv=prepare(folder,spec['kind'],argv)
            log_offset=(folder/'task.log').stat().st_size
            run_start=time.time()
            run_pause_baseline=paused_seconds
            atomic_json(folder/'execution.json',dict(argv=argv,runtime=spec['runtime'],cores=spec['cores'],memory_gb=spec['memory_gb']))
            code=execute(argv,'running',log)
            result=outcome(folder,spec['kind'],code,log_offset)
            atomic_json(folder/'execution-outcome.json',result)
            status=result['status'];detail=f'exit={code}'
            if result['evidence']:
                detail='程序报告执行错误 · '+result['evidence'][0]['message']
                failure=dict(failure_code='application_error',failure_evidence=result)
            if code:
                tail=(folder/'task.log').read_bytes()[-8192:].decode('utf-8',errors='replace').strip().splitlines()
                if tail:detail=f'exit={code} · '+tail[-1][:1500]
            if code and (folder/'task-error.json').exists():
                error=json.loads((folder/'task-error.json').read_text(encoding='utf-8'))
                if error.get('failure_code') in ('missing_input','invalid_input','model_error'):
                    failure={key:error[key] for key in ('failure_code','failure_evidence') if key in error}
                    detail=str(error.get('detail',detail))[:2000]
            if code and (folder/'dependency-error.json').exists():
                error=json.loads((folder/'dependency-error.json').read_text(encoding='utf-8'))
                detail=error.get('summary',error['detail'])
            if spec['kind']=='python' and not (folder/'cancel').exists():
                with (folder/'environment-lock.txt').open('wb',buffering=0) as lock:
                    execute([str(python),'-m','pip','freeze'],'uploading',lock)
    except Exception as e:
        detail=str(e);status='cancelled' if detail=='用户取消' else 'failed'
        if isinstance(e,JobStop) and detail in ('超过任务内存上限','超过任务提交内存上限','超过任务声明的最长占用时间','超过最长运行时间'):
            failure=dict(failure_code='resource_limit',failure_evidence=dict(limit=detail,phase=last_phase,cleanup_verified=cleanup_verified,peak_gb=peak/1024**3,peak_private_gb=peak_private/1024**3))
        if last_phase=='preparing' and status!='cancelled' and not failure:
            failure=dict(failure_code='environment_setup',failure_evidence=dict(phase='preparing',cleanup_verified=cleanup_verified))
        if process is not None and process.poll() is None:kill_tree(process)
    cleanup_verified=scope.clean()
    kernel=scope.memory_state()
    if kernel.get('verified'):peak_private=max(peak_private,kernel.get('peak_bytes',0))
    if failure.get('failure_code')=='resource_limit':
        failure['failure_evidence']['cleanup_verified']=cleanup_verified
        if kernel.get('verified') and scope.memory_limit_exceeded():failure['failure_evidence']['kernel_peak_commit_gb']=kernel.get('peak_bytes',0)/1024**3
    atomic_json(folder/'exit.json',dict(status=status,exit_code=code,detail=detail,**failure,started=start,finished=time.time(),
        kernel_memory=kernel,runtime_limit_enforced=False,elapsed_seconds=time.time()-(run_start or start)-paused_seconds+(run_pause_baseline if run_start else 0),paused_seconds=paused_seconds,preparation_seconds=(run_start or time.time())-start-(run_pause_baseline if run_start else paused_seconds),peak_gb=peak/1024**3,peak_private_gb=peak_private/1024**3,cleanup_verified=cleanup_verified))


def supervised_entry(folder):
    # Preserve supervisor exceptions even when execution cleanup cannot finish.
    import contextlib, faulthandler, traceback
    path=Path(folder)
    with (path/'supervisor.log').open('a',encoding='utf-8',buffering=1) as log:
        with contextlib.redirect_stderr(log):
            faulthandler.enable(file=log)
            try:
                main(folder)
            except BaseException as error:
                traceback.print_exc(file=log)
                try:
                    atomic_json(path/'supervisor-error.json',dict(at=time.time(),type=type(error).__name__,detail=str(error),traceback=traceback.format_exc()))
                except Exception:
                    traceback.print_exc(file=log)
                raise
            finally:
                faulthandler.disable()


if __name__=='__main__':supervised_entry(sys.argv[1])
