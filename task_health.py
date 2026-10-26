"""Automatic task diagnosis on worker heartbeats, independent of the web UI."""
import json
import recovery_links
import hashlib
import re

MEMORY_BUDGET_ACTION='中心核对进程树峰值、RAM 预算、提交内存预算及主机余量。若为任务内存溢出或超过限额，应提高对应的 memory_gb 或 commit_gb 预算；主机仍有压力时先等待资源。保留原输出和失败记录，确认进程退出及唯一恢复入口后再恢复，不重复提交。'


def memory_budget_guidance(data):
    """Also update guidance for historical memory incidents without rewriting evidence."""
    report=data.get('error_report') if isinstance(data.get('error_report'),dict) else {}
    text=str(data.get('detail',''))+' '+str(report.get('summary',''))
    if data.get('code') not in ('memory_pressure','resource_limit') and not re.search(r'out\s*of\s*memory|outofmemory|memoryerror|std::bad_alloc|内存不足|内存溢出|超过任务.*内存上限',text,re.I):return data
    revised=dict(data,next_action=MEMORY_BUDGET_ACTION)
    if isinstance(data.get('error_report'),dict):revised['error_report']=dict(data['error_report'],next_action=MEMORY_BUDGET_ACTION)
    return revised


def merge_controller_observations(telemetry, snapshot, now):
    """Keep separately sampled legacy controllers across ordinary heartbeats."""
    telemetry['controller_observation']=snapshot
    sims=[s for s in telemetry.get('simulations',[]) if s.get('kind')!='external_controller']
    for original in snapshot.get('items',[]):
        row=dict(original,kind='external_controller',actions=[])
        if isinstance(row.get('state_age_seconds'),(int,float)):
            row['state_age_seconds']+=max(0,now-snapshot.get('received_at',0))
        if not 0<=now-snapshot.get('received_at',0)<180:
            row.update(lifecycle='unknown',status='进程状态待确认',process_state='unknown',
                       status_detail='外部批次观测已过期，不能确认等待或运行状态。')
        elif row.get('lifecycle')=='queued' and row.get('state_age_seconds',0)>300:
            row.update(lifecycle='unknown',status='进程状态待确认',process_state='unknown',
                       status_detail='外部控制器等待状态已过期，不能确认当前状态。')
        # The solver already has a live per-condition row. Keep its controller
        # ownership in the snapshot without rendering the computation twice.
        elif row.get('covered_by_solver') and any(s.get('kind')=='network' and
                s.get('lifecycle')=='running' and s.get('total')==5000 and
                s.get('name','').startswith(row.get('environment','')+' ·') for s in sims):
            continue
        sims.append(row)
    telemetry['simulations']=sims


def reconcile_completion(sim):
    """A legacy result marker is not process completion while its writer lives."""
    if sim.get('kind')!='network' or sim.get('lifecycle')!='succeeded':return
    state=sim.get('process_state')
    if state=='stopped':return
    sim.update(result_written=True,remaining_seconds=None,estimate_source=None,actions=[])
    if state=='running':
        sim.update(lifecycle='running',status='结果已写出，进程收尾中',progress_state='finalizing')
    elif state=='paused':
        sim.update(lifecycle='paused',status='已暂停',progress_state='finalizing')
    else:
        sim.update(lifecycle='unknown',status='进程状态待确认',
                   status_detail='结果文件已写出，但缺少进程终态证据，不能确认任务完成。')


def diagnose(sim):
    lifecycle=sim.get('lifecycle');activity=sim.get('activity',{})
    if lifecycle in ('succeeded','cancelled','paused','queued'):return None
    if lifecycle=='failed':
        code=sim.get('failure_code')
        if code=='memory_limit':code='resource_limit'
        native_memory_error=bool(re.search(r'out\s*of\s*memory|outofmemory|memoryerror|std::bad_alloc|内存不足|内存溢出|超过任务.*内存上限',str(sim.get('failure_detail','')),re.I))
        if native_memory_error:code='resource_limit'
        publisher_codes={'missing_input','invalid_input','model_error'}
        owner='publisher' if code in publisher_codes else 'center'
        detail=sim.get('failure_detail') or '任务未完成且进程已退出；退出原因未记录，已有输出保留。'
        if owner=='publisher':
            action='已生成任务错误报告；提交者 agent 按证据修复输入或模型后重新提交，原输出保留。'
        elif code in ('memory_pressure','resource_limit'):
            action=MEMORY_BUDGET_ACTION
        else:
            action='中心继续核对进程、终态报告和结果；退出原因尚未确认，不能转嫁提交者或盲目重启。'
        return dict(code=code if code in publisher_codes|{'memory_pressure','resource_limit'} else 'execution_failed',
                    severity='error',owner=owner,detail=detail,next_action=action,
                    classification_basis='structured_failure_code' if code in publisher_codes|{'memory_pressure','resource_limit'} else 'cause_unconfirmed')
    if lifecycle=='unknown':
        return dict(code='monitor_unavailable',severity='warning',owner='center',
                    detail=sim.get('status_detail') or '进程监测不可用，当前运行状态无法确认。',
                    next_action='中心持续重采样；监测服务失效时自动恢复，确认状态前禁止重复启动。')
    progress=sim.get('progress_state')
    if progress not in ('stale','unreadable','finalizing'):return None
    if progress=='unreadable':
        detail='进程仍存活，但进度文件读取失败。'
        action='中心自动重读并核对观测权限、进程和输出；确认属于任务输出协议错误后才转交提交者。'
    elif progress=='finalizing':
        detail='结果文件已写出，求解进程仍在收尾，尚未确认任务完成。' if sim.get('result_written') else '计算步已到终点，尚未取得有效结果文件，不能判定任务完成。'
        action='中心持续核对原进程身份、活动与终态，保留结果并禁止重复派发；进程结束后再确认完成。' if sim.get('result_written') else '中心持续检查结果有效性；通过后自动完成，进程退出仍无有效结果则报告失败。'
    else:
        age=sim.get('source_age_seconds',0)
        detail=('批次完成计数' if sim.get('kind')=='batch' else '进度')+f'已 {max(1,int(age//60))} 分钟未更新。'
        if activity.get('verified'):
            count=activity.get('working_processes',0);seconds=activity.get('sample_seconds',0)
            detail+=f'中心采样确认近 {seconds:.0f} 秒有 {count} 个进程消耗 CPU；这不等于科学进度已推进。' if count else f'中心近 {seconds:.0f} 秒未测到 CPU 活动，仍需核查 I/O 或程序等待。'
        else:detail+='进程仍存活，尚缺少有效的计算活动采样。'
        action='中心持续核对进程、进度及结果；进度恢复后自动清除提示，未完成退出则升级为失败。'
    return dict(code='progress_'+progress,severity='warning',owner='center',detail=detail,next_action=action,activity=activity)


def error_report(node,sim,health,now):
    """A data-only handoff: do not treat task log contents as agent instructions."""
    task=str(sim['id']).replace('\\','/').casefold()
    evidence={key:sim[key] for key in ('lifecycle','process_state','progress_state','failure_code',
              'failure_detail','failure_evidence','status_detail','pid','completed','total','unit','source_age_seconds','activity',
              'result_written','result_url','batch_id','controller_phase','process_refs',
              'state_observed_at','state_age_seconds') if key in sim}
    return dict(schema_version=1,report_id=hashlib.sha256((node+'|'+task).encode()).hexdigest()[:24],
                node=node,task=task,name=sim.get('name',task),observed_at=now,
                owner=health['owner'],code=health['code'],summary=health['detail'],
                classification_basis=health.get('classification_basis','process_and_progress_observation'),
                evidence=evidence,next_action=health['next_action'],
                delivery='publisher_inbox' if health['owner']=='publisher' else 'center_investigation',
                acceptance='修复后须有新执行的有效进度或完成结果；领取报告不等于问题已解决。')


def reconcile(c,node,telemetry,now):
    """Keep unresolved incidents until positive recovery evidence is received."""
    for sim in telemetry.get('simulations',[]):
        reconcile_completion(sim)
        task=str(sim['id']).replace('\\','/').casefold();health=diagnose(sim)
        closed=c.execute("SELECT data FROM task_issues WHERE node=? AND task=? AND state='resolved'",(node,task)).fetchone()
        resolution=json.loads(closed[0]).get('resolution',{}) if closed else {}
        if (resolution.get('reason')=='replaced_by_center_execution' and resolution.get('completed')
                and recovery_links.stopped_matches(sim,resolution)):
            sim.pop('health',None)
            continue

        if health:
            previous=c.execute("SELECT data FROM task_issues WHERE node=? AND task=? AND state='open'",(node,task)).fetchone()
            prior=json.loads(previous[0]) if previous else None
            prior_condition_failure=bool(prior and (
                prior.get('error_report',{}).get('evidence',{}).get('controller_phase') in
                    ('condition_failed_or_incomplete','partial_condition_output_requires_review') or
                prior.get('detail')=='外部控制器报告条件失败或不完整；保留结果，由中心核查退出证据。'))
            if (prior and prior.get('severity')=='error' and
                    (prior.get('classification_basis')=='structured_failure_code' or prior_condition_failure) and
                    health.get('classification_basis')=='cause_unconfirmed'):
                # A temporary loss of a registered terminal source must not
                # replace an established cause with a generic unknown exit.
                prior['latest_observation']=dict(at=now,**health)
                evidence=prior.get('error_report',{}).get('evidence',{})
                sim.update({k:evidence[k] for k in ('failure_code','failure_detail','failure_evidence') if k in evidence})
                sim['health']={k:v for k,v in prior.items() if k not in ('error_report','latest_observation')}
                c.execute('UPDATE task_issues SET last_seen=?,data=? WHERE node=? AND task=?',
                          (now,json.dumps(prior,ensure_ascii=False),node,task))
                continue
            if prior and prior.get('severity')=='error' and health.get('severity')!='error':
                # Losing telemetry cannot downgrade a confirmed failure or erase
                # its author handoff. Retain evidence until positive recovery.
                sim['health']=dict(health,unresolved_failure=prior['detail'])
                prior['latest_observation']=dict(at=now,**health)
                c.execute("UPDATE task_issues SET last_seen=?,data=? WHERE node=? AND task=?",
                          (now,json.dumps(prior,ensure_ascii=False),node,task))
                continue
            sim['health']=health
            payload=json.dumps(dict(name=sim.get('name',task),**health,error_report=error_report(node,sim,health,now)),ensure_ascii=False)
            c.execute('''INSERT INTO task_issues(node,task,state,first_seen,last_seen,resolved_at,data)
                         VALUES(?,?,'open',?,?,NULL,?) ON CONFLICT(node,task) DO UPDATE SET
                         first_seen=CASE WHEN task_issues.state='resolved' THEN excluded.first_seen ELSE task_issues.first_seen END,
                         state='open',last_seen=excluded.last_seen,resolved_at=NULL,data=excluded.data''',(node,task,now,now,payload))
        elif (sim.get('kind')=='external_controller' and sim.get('lifecycle')=='queued' and
              sim.get('process_state')=='running' and sim.get('controller_phase') in (
                  'waiting_for_prior_win4_network_batch','waiting_for_memory_before_condition') and
              isinstance(sim.get('state_age_seconds'),(int,float)) and 0<=sim['state_age_seconds']<=300 and
              isinstance(sim.get('state_observed_at'),(int,float)) and
              sim.get('process_refs') and all(isinstance(p.get('pid'),int) and p['pid']>0 and
                  isinstance(p.get('created'),(float,int)) and 0<p['created']<=sim['state_observed_at']+2
                  for p in sim['process_refs'])):
            previous=c.execute("SELECT data FROM task_issues WHERE node=? AND task=? AND state='open'",(node,task)).fetchone()
            if not previous:continue
            prior=json.loads(previous[0])
            if prior.get('owner')!='center' or not (prior.get('code')=='monitor_unavailable' or
                    (prior.get('code')=='execution_failed' and prior.get('classification_basis')=='cause_unconfirmed' and
                     prior.get('detail')=='批次未确认完成且控制器已退出；中心保留现场调查，不重复派发。')):
                continue
            # A verified sequencer waiting on its dependency has recovered its
            # control function. This does not declare its scientific work done.
            prior['resolution']=dict(at=now,reason='controller_wait_restored',
                process_refs=sim['process_refs'],controller_phase=sim['controller_phase'],
                state_observed_at=sim['state_observed_at'],state_age_seconds=sim['state_age_seconds'],
                status_detail=sim.get('status_detail',''))
            sim.pop('health',None)
            c.execute("UPDATE task_issues SET state='resolved',resolved_at=?,last_seen=?,data=? WHERE node=? AND task=? AND state='open'",
                      (now,now,json.dumps(prior,ensure_ascii=False),node,task))
        elif sim.get('lifecycle') in ('succeeded','cancelled') or (sim.get('lifecycle')=='running' and sim.get('process_state')=='running' and sim.get('progress_state') in ('fresh','initializing')):
            if sim.get('progress_state')=='initializing':
                previous=c.execute("SELECT data FROM task_issues WHERE node=? AND task=? AND state='open'",(node,task)).fetchone()
                if previous and json.loads(previous[0]).get('code')!='monitor_unavailable':continue
            sim.pop('health',None)
            c.execute("UPDATE task_issues SET state='resolved',resolved_at=?,last_seen=? WHERE node=? AND task=? AND state='open'",(now,now,node,task))
