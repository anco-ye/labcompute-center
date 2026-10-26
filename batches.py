"""User tasks are batches; worker jobs are internal execution units and attempts."""
import hashlib
import json
import compute_only

TERMINAL = {'succeeded', 'failed', 'cancelled'}


def batch_id(job):
    # Preserve the two original experimental batches in the combined migration ZIP.
    key = [job['batch_id'], job['spec'].get('science_batch', '')]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]


def matches_id(job, requested):
    return batch_id(job)==requested or (not job['spec'].get('science_batch') and job['batch_id']==requested)


def unit_id(job):
    s = job['spec']
    return s.get('science_identity') or s.get('unit_id') or s.get('retry_of') or job['id']


def current_results(attempts):
    """Match the displayed current units; historical attempts stay downloadable separately."""
    latest={}
    for job in sorted(attempts,key=lambda j:(j['created'],j['id'])):
        latest[(batch_id(job),unit_id(job))]=job
    # Select latest first: a queued replacement must not fall back to old output.
    return [j for j in latest.values() if j.get('result') and not j['spec'].get('result_transfer_error')]


def historical_stage(job):
    return bool(job.get('non_compute_stage') or compute_only.declared_review(job['spec'])
                or (job['status']=='cancelled' and job.get('detail')=='复核/汇总移至提交者Mac，移出中心计算图'))


def summarize(jobs):
    groups = {}
    for job in jobs:
        groups.setdefault(batch_id(job), []).append(job)
    out = []
    for bid, attempts in groups.items():
        attempts.sort(key=lambda j: (j['created'], j['id']))
        latest = {}
        for j in attempts:
            latest[unit_id(j)] = j
        historical = [j for j in latest.values() if historical_stage(j)]
        units = [j for j in latest.values() if not historical_stage(j)]
        counts = {s: sum(j['status'] == s for j in units) for s in {j['status'] for j in units}}
        running = sum(v for s, v in counts.items() if s not in TERMINAL | {'queued', 'held'})
        queued = counts.get('queued', 0) + counts.get('held', 0)
        group = 'running' if running else 'queued' if queued else 'cancelled' if counts.get('cancelled') == len(units) else 'completed'
        actions = []
        if any(j['status'] in {'queued', 'leased', 'preparing', 'running', 'resume_requested'} for j in units):
            actions.append('pause')
        if any(j['status'] in {'held', 'paused', 'pause_requested'} for j in units):
            actions.append('resume')
        if any(j['status'] not in TERMINAL | {'cancel_requested'} for j in units):
            actions.append('cancel')
        if counts.get('failed') or counts.get('cancelled'):
            actions.append('retry')
        spec = attempts[0]['spec']
        phases={s:v for s,v in counts.items() if s not in TERMINAL}
        installing=sum(j['status']=='running' and j.get('stage')=='installing' for j in units)
        if installing:
            phases['installing']=installing;phases['running']-=installing
            if not phases['running']:phases.pop('running')
        offline=sum(j.get('node_online') is False and j['status'] not in TERMINAL | {'queued','held'} for j in units)
        phase=next((s for s in ('cancel_requested','recovery_needed','pause_requested','resume_requested','running','preparing','uploading','leased','paused','queued','held') if counts.get(s)), 'failed' if counts.get('failed') else 'cancelled' if counts.get('cancelled') else 'succeeded')
        if phase=='running' and installing==counts['running']:phase='installing'
        name = spec.get('batch_name') or spec.get('science_batch') or (spec['name'] if len(units) == 1 else '批量任务')
        if spec.get('science_batch') and not spec.get('batch_name'):
            name = 'H01 L2 R04 补全实验 · ' + spec['science_batch'].rsplit('_', 1)[-1]
        out.append(dict(id=bid, name=name, group=group, created=attempts[0]['created'],
                        total=len(units), completed=counts.get('succeeded', 0), failed=counts.get('failed', 0),
                        cancelled=counts.get('cancelled', 0), running=running, queued=queued,
                        held=counts.get('held', 0), paused=counts.get('paused', 0),
                        nodes=sorted({j['node'] for j in units if j.get('node') and j['status'] not in TERMINAL}),
                        actions=actions, units=units, attempts=attempts))
        out[-1].update(phase=phase,phases=phases,offline=offline, historical_stage_count=len(historical), historical_stage_only=not units)
        pending=[j for j in units if j['status'] in ('queued','held')]
        queue_priority=max((j.get('priority',0) for j in pending),default=0)
        out[-1].update(submitted_by=attempts[0].get('submitted_by',''),submitted_at=attempts[0].get('submitted_at',attempts[0]['created']),
                       priority=queue_priority,
                       priorities=sorted({j.get('priority',0) for j in pending}),
                       queue_created=min((j.get('queue_created',j['created']) for j in pending if j.get('priority',0)==queue_priority),default=attempts[0]['created']),
                       can_set_priority=any(j.get('can_set_priority',False) for j in pending))
        out[-1]['completed_units_percent']=counts.get('succeeded',0)/len(units)*100 if units else 0
        out[-1]['progress_percent']=(100 if units[0]['status']=='succeeded' else units[0].get('progress_percent')) if len(units)==1 else None
    return sorted(out, key=lambda b: (b['created'], b['id']))
