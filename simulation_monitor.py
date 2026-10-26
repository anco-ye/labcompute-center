"""Read-only adapters for existing H01 queues; no simulation imports or writes."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import time
import psutil
from common import atomic_json
from research_discovery import task_from_args,normal


def read_json(path):
    path = Path(path)
    # Windows readers must allow atomic replacement by the scientific writer.
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        create = ctypes.windll.kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(path),0x80000000,7,None,3,0x80,None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(),str(path))
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise
        stream = os.fdopen(fd,'rb')
    else:
        stream = path.open('rb')
    with stream:
        data = stream.read(4*1024**2+1)
    if len(data) > 4*1024**2:
        raise ValueError('Status file too large')
    return json.loads(data.decode('utf-8-sig'))


def epoch(value):
    try:return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError):return None


def alive(pid, source_time):
    try:
        p=psutil.Process(int(pid))
        return p.is_running() and p.create_time() <= source_time+2
    except psutil.AccessDenied:return None
    except (psutil.Error,ValueError,TypeError):return False


class SimulationMonitor:
    def __init__(self, config, cache):
        self.cfg=config;self.cache=Path(cache);self.last=0;self.rows=[];self.observed_signature=None
        try:self.history=read_json(self.cache)
        except (OSError,ValueError):self.history={}

    def estimate(self, key, generation, completed, total, timestamp, elapsed, active):
        samples=self.history.get(key, {})
        if samples.get('generation') != generation:
            samples={'generation':generation,'points':[]}
        points=samples['points']
        if points and completed < points[-1][1]:points=[]
        if not points or timestamp > points[-1][0]:
            points.append([timestamp,completed])
        points=[p for p in points if p[0]>=timestamp-1800][-180:]
        samples['points']=points;self.history[key]=samples
        if not active or completed>=total:return None,None
        rate=None;source='累计平均速度'
        # Use at least 30 seconds of current-run progress, then a 30-minute window.
        if len(points)>1 and points[-1][0]-points[0][0]>=30 and points[-1][1]>points[0][1]:
            rate=(points[-1][1]-points[0][1])/(points[-1][0]-points[0][0]);source='最近进度速度'
        elif completed>0 and elapsed and elapsed>0:rate=completed/elapsed
        remaining=(total-completed)/rate if rate and rate>0 else None
        return (remaining,source) if remaining is not None and math.isfinite(remaining) else (None,None)

    def collect(self, observations=None, observation_verified=None):
        now=time.time()
        if observation_verified is None:observation_verified=observations is not None
        signature=tuple(sorted((p['pid'],p['start']) for p in observations)) if observations is not None else None
        signature=(observation_verified,signature)
        if now-self.last<10 and signature==self.observed_signature:return self.rows
        self.observed_signature=signature
        rows=[]
        if observations is None:
            observations=[]
            for p in psutil.process_iter(['pid','cmdline','create_time']):
                try:observations.append(dict(pid=p.pid,args=p.info.get('cmdline') or [],start=p.info['create_time']))
                except psutil.Error:pass
        roots=self.cfg.get('discovery_roots',[])+self.cfg.get('network_roots',[])+self.cfg.get('batch_dirs',[])
        discovered=[]
        for p in observations:
            task=task_from_args(p.get('args',[]),roots)
            if task:discovered.append(dict(task,**{k:p[k] for k in ('pid','start')}))
        remembered=self.history.get('_discovered_roots',{})
        def remembered_roots(kind):
            return [p for p in remembered.get(kind,[]) if any(normal(p).startswith(normal(r).rstrip('/')+'/') or normal(p)==normal(r) for r in roots)]
        batch_dirs=list(dict.fromkeys(self.cfg.get('batch_dirs',[])+remembered_roots('batch')+[p['root'] for p in discovered if p['kind']=='batch']))
        network_roots=list(dict.fromkeys(self.cfg.get('network_roots',[])+remembered_roots('network')+[p['root'] for p in discovered if p['kind']=='network']))
        self.history['_discovered_roots']=dict(batch=batch_dirs,network=network_roots)
        for folder in batch_dirs:
            folder=Path(folder)
            for path in sorted(folder.glob('*_manifest.json'))[:100]:
                name=path.name.removesuffix('_manifest.json')
                statepath=folder/(name+'_runner_state.json')
                # Only established queue manifests; don't turn probes into pending jobs.
                if not statepath.exists() and name not in self.cfg.get('waiting_batches',[]):continue
                try:
                    manifest=read_json(path)
                    queue=manifest.get('queue',manifest.get('tasks',[]))
                    if not queue:continue
                    if not statepath.exists():
                        rows.append(dict(id=str(statepath),name=name,kind='batch',status='等待前序批次',completed=0,total=len(queue),percent=0,running=[],remaining_seconds=None,source_age_seconds=None))
                        continue
                    s=read_json(statepath);stamp=epoch(s.get('last_update_utc') or s.get('updated_at')) or statepath.stat().st_mtime
                    done=int(s.get('completed',0));failed=int(s.get('failed',0));total=int(s.get('total',len(queue)))
                    matched=next((p for p in discovered if normal(p['key'])==normal(statepath)),None)
                    if matched and stamp+2<matched['start']:
                        rows.append(dict(id=str(statepath),name=name,kind='batch',status='初始化中',observed_running=True,pid=matched['pid'],completed=0,total=len(queue),percent=0,running=[],remaining_seconds=None))
                        continue
                    age=max(0,now-stamp);running=bool(matched) or (not observation_verified and alive(s.get('pid'),stamp))
                    finished=s.get('phase') in ('finished','finished_pending_mac_review','probe_finished_pending_mac_review')
                    status=('已完成' if done==total else '批次结束，含失败或未完成项') if finished else ('运行中' if running else '已中断' if observation_verified else '进程状态待确认')
                    started=epoch(s.get('start_utc'))
                    if not started and running:
                        try:started=psutil.Process(s['pid']).create_time()
                        except psutil.Error:pass
                    elapsed=max(0,(now if running and not finished else stamp)-started) if started else None
                    remaining,source=self.estimate(str(statepath),s.get('start_utc') or s.get('pid'),done+failed,total,now if running and not finished else stamp,elapsed,running and not finished)
                    if running and not finished and age>120:status='进程运行中，进度暂未更新';remaining=None;source=None
                    rows.append(dict(id=str(statepath),name=name,kind='batch',status=status,completed=done,total=total,failed=failed,
                                     percent=min(100,100*(done+failed)/max(1,total)),running=list(s.get('running',{}))[:12],
                                     observed_running=bool(matched),pid=s.get('pid'),elapsed_seconds=elapsed,remaining_seconds=remaining,estimate_source=source,source_age_seconds=age))
                except (OSError,ValueError,TypeError,KeyError):
                    matched=next((p for p in discovered if normal(p['key'])==normal(statepath)),None)
                    rows.append(dict(id=str(statepath),name=name,status='状态文件暂不可读',observed_running=bool(matched),pid=matched['pid'] if matched else None,remaining_seconds=None))
        # Registered process statuses give reliable identity even across user accounts.
        statuses=[]
        for rawpath in self.cfg.get('status_files',[]):
            try:
                v=read_json(rawpath)
                args=v.get('command',[])
                if '--environment' in args:
                    def value(flag,default):return args[args.index(flag)+1] if flag in args else default
                    v['_environment']=value('--environment',None)
                    v['_condition']=value('--condition','strong_bursts')
                    v['_duration']=float(value('--duration-ms','600'))
                statuses.append(v)
            except (OSError,ValueError,IndexError,TypeError):pass
        processes=[p for p in discovered if p['kind']=='network']
        seen=set()
        for root in network_roots:
            root=Path(root)
            directories=list((root/'environment_networks_fixed_active_v3').glob('H01_*'))
            if (root/'full_window_network_fixed_active_v3').is_dir():directories.append(root/'full_window_network_fixed_active_v3')
            for folder in directories[:100]:
                path=folder/'progress.json'
                if not path.exists():continue
                env='H01_L3_R04' if folder.name=='full_window_network_fixed_active_v3' else folder.name
                try:
                    s=read_json(path);stamp=path.stat().st_mtime;age=max(0,now-stamp)
                    done=float(s['time_ms']);total=float(s['duration_ms']);condition=s.get('input_condition','unknown')
                    output=Path(s.get('output',''));resultpath=folder/('result_'+output.stem.removeprefix('pilot_')+'.json')
                    result=None
                    if resultpath.exists():
                        candidate=read_json(resultpath)
                        if candidate.get('duration_ms')==total and candidate.get('input_condition')==condition and candidate.get('finite') is True and done>=total-1e-6 and resultpath.stat().st_mtime>=stamp:result=candidate
                    proc=next((p for p in processes if normal(p['key'])==normal(path) and p['condition']==condition and p['duration']==total),None)
                    seq=next((v for v in statuses if (v.get('_environment')==env and v.get('_condition')==condition and v.get('_duration')==total) or (env in str(v.get('log',''))+str(v.get('partial_backup','')) and v.get('condition')==condition)),None)
                    has_queue=bool(seq and isinstance(seq.get('completed'),list) and 'condition' in seq)
                    pid=proc['pid'] if proc else seq.get('pid') if seq else None
                    active=bool(proc) or (not observation_verified and alive(pid,stamp))
                    elapsed=float(s.get('elapsed_s',0));start=proc['start'] if proc else (psutil.Process(pid).create_time() if active else stamp-elapsed)
                    key=str(path);seen.add((normal(path),condition,total))
                    if proc and stamp+2<start:
                        rows.append(dict(id=key,name=env+' · '+condition,kind='network',status='初始化中',observed_running=True,pid=pid,completed=0,total=total,unit='ms',percent=0,elapsed_seconds=max(0,now-start),remaining_seconds=None));continue
                    if result and active and resultpath.stat().st_mtime < start:result=None
                    if result:done=total
                    remaining,source=self.estimate(key,str(pid or '')+':'+str(round(start)),done,total,stamp,elapsed,active and not result and age<=120)
                    status='计算完成' if result else '运行中' if active else '已中断' if observation_verified else '进程状态待确认'
                    if active and not result and done>=total-1e-6:status='计算步已完成，结果尚未确认';remaining=None;source=None
                    elif active and not result and age>120:status='进程运行中，进度暂未更新'
                    rows.append(dict(id=key,name=env+' · '+condition,kind='network',status=status,observed_running=bool(proc),pid=pid,completed=done,total=total,unit='ms',percent=min(100,done/max(1,total)*100),elapsed_seconds=elapsed,remaining_seconds=remaining,estimate_source=source,source_age_seconds=age,
                                     queue_completed=len(seq['completed']) if has_queue else None,queue_total=3 if has_queue else None))
                except (OSError,ValueError,KeyError,TypeError,psutil.Error):
                    matches=[p for p in processes if normal(p['key'])==normal(path)]
                    for p in matches:seen.add((normal(path),p['condition'],p['duration']))
                    rows.append(dict(id=str(path),name=env,status='状态文件暂不可读',observed_running=bool(matches),pid=matches[0]['pid'] if matches else None,remaining_seconds=None))
        for p in processes:
            identity=(normal(p['key']),p['condition'],p['duration'])
            if identity in seen:continue
            seen.add(identity)
            rows.append(dict(id=p['key'],name=p['name'],kind='network',status='初始化中',observed_running=True,pid=p['pid'],completed=0,total=p['duration'],unit='ms',percent=0,elapsed_seconds=max(0,now-p['start']),remaining_seconds=None))
        for key,reportpath in self.cfg.get('terminal_reports',{}).items():
            row=next((r for r in rows if normal(r['id'])==normal(key)),None)
            if not row or row.get('observed_running') or row['status'] in ('已完成','计算完成'):continue
            try:
                report=read_json(reportpath);at=epoch(report.get('updated_at'))
                if not at or not Path(row['id']).stat().st_mtime<=at<=now+5:continue
                if report.get('phase')=='v4_terminated_to_preserve_existing_long_run':
                    free=report.get('memory',{}).get('free_physical_gib')
                    detail=f'可用物理内存降至 {free:.1f} GiB，触发内存保护，计算已终止；已有输出保留。' if isinstance(free,(int,float)) else '触发内存保护，计算已终止；已有输出保留。'
                    row.update(status='已中断',failure_code='memory_pressure',failure_detail=detail,remaining_seconds=None)
                elif report.get('failure_code') in ('missing_input','invalid_input','model_error','memory_pressure','resource_limit'):
                    row.update(status='已中断',failure_code=report['failure_code'],failure_detail=str(report.get('detail','任务终态报告确认失败'))[:2000],remaining_seconds=None)
            except (OSError,ValueError,TypeError):pass
        for row in rows:
            status=row['status']
            row['progress_state']='stale' if status=='进程运行中，进度暂未更新' else 'unreadable' if status=='状态文件暂不可读' else 'initializing' if status=='初始化中' else 'finalizing' if status=='计算步已完成，结果尚未确认' else 'fresh'
            row['process_state']='running' if row.get('observed_running') else 'stopped' if observation_verified and status=='已中断' else 'unknown'
            if status=='已中断':row.setdefault('failure_detail','计算进程已退出，任务未完成；退出原因未记录，已有输出保留。')
        rows.sort(key=lambda r:(r['status'] in ('已完成','计算完成','已中断'),r['name']))
        self.rows=rows[:60];self.last=now
        try:atomic_json(self.cache,self.history)
        except OSError:pass
        return self.rows
