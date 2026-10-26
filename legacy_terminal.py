"""Read registered legacy guard logs; never control or replay their jobs."""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re


def normal(value):
    return str(value).replace('\\', '/').casefold()


def guard_evidence(entry, now):
    if entry.get('execution_receipt_dir'):
        return execution_evidence(entry,now)
    progress=Path(entry['progress']); log=Path(entry['events'])
    if progress.stat().st_size>4096:raise ValueError('progress exceeds 4 KiB')
    raw=progress.read_bytes(); state=json.loads(raw.decode('utf-8-sig'))
    stamp=progress.stat().st_mtime
    done=float(state['time_ms']); total=float(state['duration_ms'])
    if not all(math.isfinite(x) for x in (done,total,stamp)) or not 0<=done<total:return None
    with log.open('rb') as stream:
        stream.seek(max(0,log.stat().st_size-262144)); lines=stream.read().decode('utf-8-sig',errors='replace').splitlines()
    for line in reversed(lines):
        try:
            event=json.loads(line.lstrip('\ufeff'))
            at=dt.datetime.fromisoformat(event['time']).timestamp()
            if not stamp<=at<=min(stamp+180,now+5):continue
            if not re.fullmatch(r'stop_one_exact_H01_Win[1-4]_network_science_process_to_preserve_host',event.get('action','')):continue
            command=normal(event.get('command',''))
            # Exact registered script and option boundaries, not substring names.
            if normal(entry['script']) not in command:continue
            def option(flag,default=None):
                match=re.search(r'(?:^|\s)'+re.escape(flag)+r'\s+([^\s"]+)',command)
                return match.group(1) if match else default
            if option('--environment')!=entry['environment'].casefold():continue
            if option('--condition','strong_bursts')!=state['input_condition'].casefold():continue
            if float(option('--duration-ms','600'))!=total:continue
            free=float(event['freeVirtualGiBBefore']); private=float(event['privatePageGiB']); pid=int(event['pid'])
            if not all(math.isfinite(x) and x>=0 for x in (free,private)) or pid<=0:continue
            return dict(id=str(progress),name=entry['environment']+' · '+state['input_condition'],
                completed=done,total=total,source_age_seconds=now-stamp,
                failure_code='memory_pressure',
                failure_detail=f'旧内存守护器在提交余量 {free:.3f} GiB 时主动终止进程 {pid}（该进程提交约 {private:.3f} GiB）；已有输出保留。',
                failure_evidence=dict(event=event,event_log=str(log),progress_sha256=hashlib.sha256(raw).hexdigest(),
                    progress_mtime=stamp,event_time=at,process_creation_time_recorded=False,
                    basis='registered_guard_command_condition_duration_and_progress_time'))
        except (ValueError,TypeError,KeyError):continue
    return None


def execution_evidence(entry, now):
    """Bind a bounded launcher receipt to this exact output generation."""
    progress=Path(entry['progress'])
    if not progress.exists():return None  # Initialization has no scientific step yet.
    if progress.stat().st_size>4096:raise ValueError('progress exceeds 4 KiB')
    raw=progress.read_bytes();state=json.loads(raw.decode('utf-8-sig'));stamp=progress.stat().st_mtime
    done=float(state['time_ms']);total=float(state['duration_ms'])
    if not all(math.isfinite(x) for x in (done,total,stamp)) or not 0<=done<total:return None
    for path in sorted(Path(entry['execution_receipt_dir']).glob('center_*_execution.json'),reverse=True):
        if path.stat().st_size>65536:continue
        receipt=json.loads(path.read_text(encoding='utf-8-sig'))
        if receipt.get('status')!='failed' or receipt.get('remaining_processes'):continue
        if not receipt.get('root_created') or not receipt.get('root_pid'):continue
        if not receipt.get('started',now+1)-1<=stamp<=receipt.get('finished',0)<=now+5:continue
        args=receipt.get('command',[])
        if not isinstance(args,list) or normal(entry['script']) not in [normal(x) for x in args]:continue
        def option(flag,default=None):
            return args[args.index(flag)+1] if flag in args and args.index(flag)+1<len(args) else default
        if option('--environment')!=entry['environment'] or option('--condition','strong_bursts')!=state['input_condition']:continue
        if float(option('--duration-ms','600'))!=total:continue
        code=receipt.get('failure_code') or 'unknown'
        if code=='resource_limit':detail=f"中心执行守护器检测到进程树提交内存超过 {receipt['private_limit_gb']:g} GiB 上限，已停止该执行并保留部分结果。"
        elif code=='time_limit':detail='中心执行守护器确认运行超过时限，已停止该执行并保留部分结果。'
        else:detail=f"原执行退出码 {receipt.get('exit_code')}，进程树已清理；中心保留退出证据与部分结果。"
        return dict(id=str(progress),name=entry['environment']+' · '+state['input_condition'],
            completed=done,total=total,source_age_seconds=now-stamp,failure_code=code,failure_detail=detail,
            failure_evidence=dict(receipt=receipt,receipt_path=str(path),progress_sha256=hashlib.sha256(raw).hexdigest(),
                process_creation_time_recorded=True,basis='registered_execution_receipt_command_generation'))
    return None


def merge_terminal_evidence(telemetry,snapshot,now):
    telemetry['terminal_evidence']=snapshot
    if not 0<=now-snapshot.get('received_at',0)<180:return
    by_id={normal(e['id']):e for e in snapshot.get('items',[])}
    for row in telemetry.get('simulations',[]):
        evidence=by_id.get(normal(row.get('id','')))
        if not evidence or row.get('lifecycle')!='failed' or row.get('process_state')!='stopped':continue
        if any(row.get(k)!=evidence.get(k) for k in ('name','completed','total')):continue
        # Ages are translated through each sample's receipt time, so node clock
        # skew does not attach a terminal event to a newer execution generation.
        observed_age=row.get('source_age_seconds',-1000000)+max(0,now-telemetry.get('sampled_at',now))
        evidence_age=evidence['source_age_seconds']+now-snapshot['received_at']
        if abs(observed_age-evidence_age)>15:continue
        row.update({k:evidence[k] for k in ('failure_code','failure_detail','failure_evidence')})


def reconcile_shared_admission_hold(telemetry, previous=None):
    """A confirmed guard kill is not new safe capacity for the surviving cohort."""
    old=(previous or telemetry).get('shared_admission_hold',{})
    seen=set(old.get('seen_events',[]))
    rows={normal(r.get('id','')):r for r in telemetry.get('simulations',[])}
    cohort=dict(old.get('cohort',{}))
    for key,generation in list(cohort.items()):
        row=rows.get(key)
        # Missing/unknown evidence cannot prove that the original workload ended.
        if row and row.get('process_state')!='unknown':
            if row.get('lifecycle') in ('succeeded','failed','cancelled') and row.get('process_state')=='stopped':
                cohort.pop(key)
            elif row.get('generation') and row['generation']!=generation:
                cohort.pop(key)
    for row in rows.values():
        evidence=row.get('failure_evidence',{})
        if row.get('failure_code')!='memory_pressure' or row.get('lifecycle')!='failed' or row.get('process_state')!='stopped':continue
        if evidence.get('basis')!='registered_guard_command_condition_duration_and_progress_time':continue
        event=evidence.get('event',{})
        token=hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
        if token in seen:continue
        seen.add(token)
        for key,live in rows.items():
            if live.get('kind')=='network' and live.get('generation') and live.get('lifecycle') in ('running','paused','unknown'):
                # Relative ages avoid clock skew and exclude jobs clearly begun
                # after an old incident first reaches this center version.
                age=row.get('source_age_seconds')
                elapsed=live.get('elapsed_seconds')
                offset=evidence.get('event_time',0)-evidence.get('progress_mtime',0)
                if (live.get('lifecycle')=='running' and isinstance(age,(int,float))
                    and isinstance(elapsed,(int,float)) and 0<=offset<=180
                    and elapsed+180<age-offset):continue
                cohort[key]=live['generation']
    if seen or cohort:
        telemetry['shared_admission_hold']=dict(active=bool(cohort),cohort=cohort,seen_events=sorted(seen),
            reason='同机旧任务已触发提交内存守护；保留现有计算，等待当时存活作业结束后恢复新增共享任务' if cohort else None)
