"""Independent scheduled recovery of the observer, never of scientific jobs."""
import argparse
import json
import math
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.request
import psutil
from legacy_terminal import guard_evidence
from common import atomic_json, relative_path


TASK='LabCompute-ScienceControl'


def publish_terminal_evidence(root):
    root=Path(root);registry=root/'config'/'legacy-terminal-sources.json'
    if not registry.exists():return
    statusfile=root/'data'/'terminal-evidence-status.json'
    try:
        entries=json.loads(registry.read_text(encoding='utf-8-sig'))
        if not isinstance(entries,list) or len(entries)>40:raise ValueError('invalid terminal source registry')
        rows=[];errors=[]
        for entry in entries:
            try:
                row=guard_evidence(entry,time.time())
                if row:rows.append(row)
            except (OSError,ValueError,TypeError,KeyError) as exc:
                errors.append(str(exc)[:200])
        cfg=json.loads((root/'config/agent.json').read_text(encoding='utf-8-sig'))
        context=ssl.create_default_context(cafile=cfg['ca_file'])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
        req=urllib.request.Request(cfg['controller'].rstrip('/')+'/agent/'+cfg['id']+'/terminal-evidence',
            data=json.dumps(dict(items=rows)).encode(),headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json'})
        with opener.open(req,timeout=10) as response:response.read()
        atomic_json(statusfile,dict(at=time.time(),status='published',items=rows,errors=errors))
    except (OSError,ValueError,TypeError,KeyError) as exc:
        atomic_json(statusfile,dict(at=time.time(),status='error',detail=str(exc)[:300]))


def controller_row(key, entry, processes, verified=True):
    """Observe a registered sequencer separately from its scientific solver."""
    normal=lambda x:str(x).replace('\\','/').casefold()
    matching=[p for p in processes if any(normal(a)==normal(entry['script']) for a in p.get('args',[]))]
    env=entry['environment']
    solvers=[p for p in processes if '--environment' in p.get('args',[]) and
             env in p['args'] and '--duration-ms' in p['args'] and '5000' in p['args'] and
             any('full_window_network_fixed_active_' in a for a in p['args'])]
    row=dict(id='controller:'+key,name=entry.get('name',env+' · 三条件5s'),kind='external_controller',
             environment=env,actions=[],process_state='running' if matching else 'stopped' if verified else 'unknown',
             process_refs=[dict(pid=p['pid'],created=p['created']) for p in matching],
             covered_by_solver=bool(solvers),remaining_seconds=None)
    try:
        path=Path(entry['status_file'])
        if path.stat().st_size>16384:raise ValueError('controller state exceeds 16 KiB')
        state=json.loads(path.read_text(encoding='utf-8-sig'));phase=state.get('phase')
        state_mtime=path.stat().st_mtime
        row.update(state_observed_at=state_mtime,state_age_seconds=time.time()-state_mtime)
        if matching and state_mtime+2<max(p['created'] for p in matching):
            raise ValueError('state belongs to a previous controller execution')
        if not isinstance(state.get('completed',[]),list) or len(state.get('completed',[]))>3:
            raise ValueError('invalid completed condition list')
        if phase in ('waiting_for_prior_win4_network_batch','waiting_for_memory_before_condition') and time.time()-path.stat().st_mtime>300:
            raise ValueError('controller wait state has stopped updating')
        row.update(controller_phase=phase,completed=len(state.get('completed',[])),total=3,unit='条件',
                   percent=100*len(state.get('completed',[]))/3)
        if not verified:raise ValueError('incomplete process observation')
        if phase=='three_conditions_computed_pending_mac_review' and not matching:
            row.update(lifecycle='succeeded',status='计算完成')
        elif phase in ('condition_failed_or_incomplete','partial_condition_output_requires_review') and not matching:
            row.update(lifecycle='failed',status='已中断',failure_detail='外部控制器报告条件失败或不完整；保留结果，由中心核查退出证据。')
        elif not matching:
            row.update(lifecycle='failed',status='已中断',failure_detail='批次未确认完成且控制器已退出；中心保留现场调查，不重复派发。')
        elif phase=='waiting_for_prior_win4_network_batch':
            row.update(lifecycle='queued',status='等待前序批次',status_detail='等待 '+str(state.get('prior_batch','前序批次'))+' 结束；求解尚未启动。')
        elif phase=='waiting_for_memory_before_condition':
            row.update(lifecycle='queued',status='等待资源',status_detail='等待外部控制器声明的物理内存与提交额度；求解尚未启动。')
        elif solvers:
            row.update(lifecycle='running',status='运行中',progress_state='fresh')
        elif phase=='three_conditions_computed_pending_mac_review':
            row.update(lifecycle='running',status='结果已写出，进程收尾中',progress_state='finalizing',result_written=True)
        elif phase in ('condition_computed_pending_mac_review','prior_condition_exists_pending_mac_review'):
            row.update(lifecycle='queued',status='等待下一条件')
        else:
            raise ValueError('controller has no verified active solver or recognized wait state')
    except (OSError,ValueError,TypeError,KeyError) as exc:
        row.update(lifecycle='unknown',status='进程状态待确认',status_detail='外部批次状态尚未确认：'+str(exc)[:200])
    return row


def publish_controller_observations(root):
    root=Path(root);registry=root/'config'/'external-controllers.json'
    if not registry.exists():return
    statusfile=root/'data'/'controller-observer-status.json'
    try:
        entries=json.loads(registry.read_text(encoding='utf-8-sig'))
        if not isinstance(entries,dict) or len(entries)>40:raise ValueError('invalid controller registry')
        processes=[];verified=True
        for p in psutil.process_iter(['pid','name','create_time']):
            if 'python' not in (p.info['name'] or '').lower():continue
            try:processes.append(dict(pid=p.pid,created=p.create_time(),args=p.cmdline()))
            except psutil.NoSuchProcess:pass
            except psutil.AccessDenied:verified=False
        rows=[controller_row(key,entry,processes,verified) for key,entry in entries.items()]
        cfg=json.loads((root/'config/agent.json').read_text(encoding='utf-8-sig'))
        context=ssl.create_default_context(cafile=cfg['ca_file'])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
        req=urllib.request.Request(cfg['controller'].rstrip('/')+'/agent/'+cfg['id']+'/controller-observations',
            data=json.dumps(dict(items=rows)).encode(),headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json'})
        with opener.open(req,timeout=10) as response:response.read()
        atomic_json(statusfile,dict(at=time.time(),status='published',items=rows))
    except (OSError,ValueError,TypeError,KeyError) as exc:
        atomic_json(statusfile,dict(at=time.time(),status='error',detail=str(exc)[:300]))


def sync_progress_sources(root, now=None):
    """Adapt explicitly registered legacy progress without touching the solver.

    The scheduled observer owns this mapping; task output is data, not a path
    discovery mechanism. A runner identity and current-generation source are
    required before publishing the standard root progress file.
    """
    root=Path(root);now=time.time() if now is None else now
    config=root/'config'/'progress-sources.json'
    rules_path=root/'config'/'progress-source-rules.json'
    if not config.exists() and not rules_path.exists():return {}
    statuses={}
    try:
        entries=json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
        if not isinstance(entries,dict) or len(entries)>200:raise ValueError('invalid progress mapping')
        rules=json.loads(rules_path.read_text(encoding='utf-8-sig')) if rules_path.exists() else []
        if not isinstance(rules,list) or len(rules)>20:raise ValueError('invalid progress rules')
        for rule in rules:
            relative_path(rule['path_glob'])
            if '**' in rule['path_glob']:raise ValueError('recursive progress discovery is not allowed')
    except (OSError,ValueError,TypeError,KeyError) as exc:
        statuses['_config']=dict(status='error',detail=str(exc)[:300])
        atomic_json(root/'data'/'progress-adapter-status.json',dict(at=now,jobs=statuses))
        return statuses
    # Administrator-declared formats only; sources must belong to the current
    # active execution. Never import task code or infer arbitrary JSON meaning.
    if rules:
        for folder in (root/'jobs').glob('*'):
            jid=folder.name
            if not re.fullmatch(r'[a-f0-9]{32}',jid) or jid in entries or (folder/'exit.json').exists():continue
            try:
                ref=json.loads((folder/'runner.json').read_text(encoding='utf-8-sig'))
                usage=json.loads((folder/'usage.json').read_text(encoding='utf-8-sig'))
                if usage.get('phase')!='running' or not 0<=now-usage.get('at',0)<30:continue
                work=(folder/'work').resolve();target=work/'progress.json'
                if target.exists():
                    prior=json.loads(target.read_text(encoding='utf-8-sig'))
                    if not prior.get('_center_progress_source'):continue
                candidates={}
                for rule in rules:
                    for source in work.glob(rule['path_glob']):
                        if not source.is_file() or not source.resolve().is_relative_to(work):continue
                        stamp=source.stat().st_mtime
                        if source.stat().st_size>4096 or stamp<ref['create_time'] or not 0<=now-stamp<120:continue
                        name=source.relative_to(work).as_posix()
                        candidates[name]=dict(path=name,completed_key=rule.get('completed_key','completed'),total_key=rule.get('total_key','total'))
                if len(candidates)==1:entries[jid]=next(iter(candidates.values()))
                elif len(candidates)>1:statuses[jid]=dict(status='ambiguous_sources',detail='匹配多个当前进度源，需要显式映射；未发布进度。')
            except (OSError,ValueError,TypeError,KeyError):continue
    for jid,entry in entries.items():
        try:
            if not re.fullmatch(r'[a-f0-9]{32}',jid):raise ValueError('invalid job id')
            folder=root/'jobs'/jid;work=(folder/'work').resolve()
            if not (folder/'runner.json').exists():
                statuses[jid]=dict(status='not_started_here');continue
            if (folder/'exit.json').exists():
                statuses[jid]=dict(status='finished');continue
            ref=json.loads((folder/'runner.json').read_text(encoding='utf-8-sig'))
            process=psutil.Process(ref['pid'])
            if abs(process.create_time()-ref['create_time'])>=.01 or not process.is_running():
                raise ValueError('runner identity changed')
            usage=json.loads((folder/'usage.json').read_text(encoding='utf-8-sig'))
            if usage.get('phase')!='running' or not 0<=now-usage.get('at',0)<30:
                statuses[jid]=dict(status='inactive_or_unobserved');continue
            name=relative_path(entry['path']);source=work/name;target=work/'progress.json'
            if source.resolve()==target.resolve() or not source.resolve().is_relative_to(work) or target.is_symlink():
                raise ValueError('progress path escapes work or targets root progress')
            if not source.exists() and not target.exists():
                statuses[jid]=dict(status='awaiting_first_progress',detail='执行进程已确认，尚未产生首个进度文件。')
                continue
            stamp=source.stat().st_mtime
            if source.stat().st_size>4096:raise ValueError('progress exceeds 4 KiB')
            if stamp<ref['create_time'] or not 0<=now-stamp<120:
                statuses[jid]=dict(status='source_stale',source_mtime=stamp);continue
            value=json.loads(source.read_text(encoding='utf-8-sig'))
            done=float(value[entry.get('completed_key','completed')]);total=float(value[entry.get('total_key','total')])
            if not all(math.isfinite(v) for v in (done,total)) or not 0<=done<=total or total<=0:
                raise ValueError('invalid progress values')
            prior=json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else {}
            if prior and prior.get('_center_progress_source')!=name:
                raise ValueError('root progress is owned by the task')
            if prior and (total!=prior['total'] or done<prior['completed']):
                raise ValueError('progress regressed or total changed')
            if not prior or done!=prior.get('completed'):
                atomic_json(target,dict(completed=done,total=total,_center_progress_source=name,source_mtime=stamp))
            statuses[jid]=dict(status='current',completed=done,total=total,source_mtime=stamp)
        except (OSError,ValueError,TypeError,KeyError,psutil.Error) as exc:
            statuses[jid]=dict(status='error',detail=str(exc)[:300])
    atomic_json(root/'data'/'progress-adapter-status.json',dict(at=now,jobs=statuses))
    return statuses

def recover(root,task_state=None,restart=None,now=None):
    root=Path(root);spool=root/'science-control';now=time.time() if now is None else now
    def read(path):
        try:return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):return {}
    state=read(spool/'watchdog.json');snapshot=read(spool/'snapshot.json')
    attempts=[at for at in state.get('attempts',[]) if now-1800<at<=now]
    output=dict(at=now,attempts=attempts)
    fresh=0<=now-snapshot.get('at',0)<60
    if fresh:
        output.update(status='healthy' if snapshot.get('observation_verified',True) else 'needs_admin',
                      detail='监测正常' if snapshot.get('observation_verified',True) else '监测服务运行但无权读取完整进程状态；需要管理员修复权限。')
    elif state.get('next_try',0)>now:
        output.update(status='recovering',detail='中心已安排监测服务恢复，等待复核。',next_try=state['next_try'])
    elif len(attempts)>=3:
        output.update(status='needs_admin',detail='监测服务在 30 分钟内恢复失败 3 次；需要管理员检查服务日志。')
    else:
        if task_state is None:
            task_state=lambda: subprocess.check_output(['powershell','-NoProfile','-Command',f'(Get-ScheduledTask -TaskName {TASK}).State'],text=True,timeout=20).strip()
        if restart is None:
            def restart(running):
                command=(f'Stop-ScheduledTask -TaskName {TASK}; ' if running else '')+f'Start-ScheduledTask -TaskName {TASK}'
                subprocess.run(['powershell','-NoProfile','-Command',"$ErrorActionPreference='Stop'; "+command],check=True,capture_output=True,timeout=30)
        try:
            running=task_state()=='Running'
            pending=[p for p in (spool/'requests').glob('*.json') if not (spool/'responses'/p.name).exists()]
            if running and pending:
                output.update(status='needs_admin',detail='监测服务失去响应且有未确认的控制操作；中心保留现场，需核查操作是否生效。')
            else:
                attempts.append(now);output['attempts']=attempts
                restart(running)
                output.update(status='recovering',detail='中心已自动重启进程监测服务；科研计算进程保持原状。',next_try=now+min(300,60*2**(len(attempts)-1)))
        except (OSError,subprocess.SubprocessError) as exc:
            if not attempts or attempts[-1]!=now:attempts.append(now)
            output['attempts']=attempts
            output.update(status='recovering',detail='监测服务恢复失败，中心将按退避间隔重试。',error=str(exc)[:300],next_try=now+120)
    atomic_json(spool/'watchdog.json',output)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='C:/LabComputeCenter')
    root=parser.parse_args().root
    recover(root)
    sync_progress_sources(root)
    publish_controller_observations(root)
    publish_terminal_evidence(root)
