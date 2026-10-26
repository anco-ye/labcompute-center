"""FIFO resource placement with reservations and bounded backfill.

Plans are recomputed inside the controller's SQLite write transaction. No task
moves after leasing. Runtime estimates guide placement, never scientific gates.
"""
import json
import math
import time
from platform_contract import resolve as platform_request,version_ready

ACTIVE = ('leased','preparing','running','uploading','cancel_requested','recovery_needed','pause_requested','paused','resume_requested')

def spec(job):
    value=job['spec']
    return json.loads(value) if isinstance(value,str) else value

def queue_key(job):
    # Priority is trusted controller metadata, never a task-package instruction.
    return (-job.get('priority',0),job.get('queue_created',job['created']),job['created'],job['id'])


def inherit_dependency_priorities(jobs):
    """An urgent dependency chain must not wait behind ordinary new claims."""
    latest={}
    for job in sorted(jobs,key=lambda j:(j['created'],j['id'])):
        if spec(job).get('task_key'):latest[(job.get('batch_id'),spec(job)['task_key'])]=job
    changed=True
    while changed:
        changed=False
        for job in jobs:
            if job['status']!='queued':continue
            for key in spec(job).get('depends_on',[]):
                prior=latest.get((job.get('batch_id'),key))
                if prior and prior['status']=='queued' and prior.get('priority',0)<job.get('priority',0):
                    prior['priority']=job['priority'];changed=True
    return jobs

def telemetry(node):
    value=node.get('telemetry',{})
    return json.loads(value) if isinstance(value,str) else value

def pressure_reason(t,now):
    hold=t.get('shared_admission_hold',{})
    if hold.get('active'):return hold.get('reason') or '等待旧任务资源故障后的共享准入恢复'
    policy=t.get('scheduling_policy',{})
    sample=t.get('resource_observation')
    if not sample and not policy.get('require_observation'):return None
    if not sample or not 0<=now-sample.get('at',0)<20 or not sample.get('verified'):
        return '等待资源状态稳定采样'
    if sample.get('state')=='pressured':return '持续内存或换页压力，恢复稳定后自动接单'
    if sample.get('state')!='normal':return '等待资源状态核验'
    return None


def commit_budget(request):
    value=request.get('commit_gb',request['memory_gb'])
    return max(value,2)+.25 if request.get('kernel_memory_required') else value


def kernel_guard_verified(job,node,now):
    if spec(job).get('kernel_memory_required'):return True
    import hashlib
    t=telemetry(node);group=t.get('memory_guard_observation',{});proof=group.get('items',{}).get(job['id'],{})
    storage=t.get('storage_observation',{})
    expected=commit_budget(dict(spec(job),kernel_memory_required=True))*2**30
    return bool(job.get('lease') and proof.get('verified') is True
        and 0<=now-group.get('received_at',0)<15 and 0<=now-storage.get('at',0)<15
        and proof.get('limit_bytes')==int(expected)
        and proof.get('lease_digest')==hashlib.sha256(job['lease'].encode()).hexdigest()
        and any(p.get('job')==job['id'] and p.get('pid')==proof.get('pid') and p.get('created')==proof.get('created') for p in storage.get('managed',[])))


def reservation(job, node, now):
    """Ephemeral controller evidence never changes the job's execution spec."""
    original=spec(job);learned=job.get('admission_reservation');t=telemetry(node)
    observation=t.get('resource_observation',{});storage=t.get('storage_observation',{})
    if (not learned or not kernel_guard_verified(job,node,now) or original.get('resource_mode')!='estimated' or original.get('restart_policy')!='from_start' or original.get('resource_retries',0) or job.get('status')!='running' or job.get('execution_stopped')
            or learned.get('method') not in ('completed_profile_reservation','verified_workspace_reservation')
            or not 0<=now-learned.get('at',0)<15 or pressure_reason(t,now) is not None
            or not observation.get('verified') or observation.get('state')!='normal'
            or not 0<=now-observation.get('at',0)<20
            or t.get('external_busy') or t.get('memory_commit_verified') is not True
            or not 0<=now-storage.get('at',0)<15
            or not any(p.get('job')==job['id'] for p in storage.get('managed',[]))):return original
    values=[learned.get(k) for k in ('memory_gb','commit_gb')]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in values):return original
    if not values[0]<=values[1] or values[0]>original['memory_gb'] or values[1]>original.get('commit_gb',original['memory_gb']):return original
    return dict(original,memory_gb=values[0],commit_gb=values[1],kernel_memory_required=True)


def disk_capacities(node, active, now=None):
    now=time.time() if now is None else now
    t=telemetry(node);default=t.get('default_work_volume')
    volumes=t.get('work_volumes') or [dict(id=default,free_gb=t.get('disk_free_gb',100000))]
    observation=t.get('storage_observation',{})
    fresh=0<=now-observation.get('at',0)<30
    usage=observation.get('disk_usage',{}) if fresh else {}
    def remaining(job):
        request=spec(job);total=request.get('disk_gb',4)+request.get('runtime_disk_gb',0)
        row=usage.get(job['id'],{});used=row.get('allocated_gb',0)
        # Only trusted full workspace scans can cancel already occupied bytes.
        if not row.get('verified') or not 0<=now-row.get('at',0)<60 or not isinstance(used,(float,int)) or isinstance(used,bool) or not math.isfinite(used) or used<0:used=0
        return max(0,total-used)
    return [(v['id'],v['free_gb']-v.get('reserve_gb',0)-sum(remaining(j)
                for j in active if not j.get('execution_stopped') and spec(j).get('work_volume',default)==v['id'])) for v in volumes]


def budget(node,active=(),now=None,minimum_reserve=0):
    """One accounting path used by dispatch and the diagnostic API."""
    now=time.time() if now is None else now
    t=telemetry(node);active=[j for j in active if not j.get('execution_stopped')];holds={j['id']:reservation(j,node,now) for j in active};reserved=list(holds.values())
    policy=t.get('scheduling_policy',{});observation=t.get('resource_observation',{})
    shared=t.get('external_busy',False) and policy.get('external_mode')=='shared'
    pool=list(t.get('cpu_pool') or range(max(1,int(t.get('cpu_threads',1))-1)))
    used={i for r in reserved for i in r.get('cpu_ids',range(r['cores']))}
    blocked=[] if shared else t.get('external_cpu_ids',[])
    available=[i for i in pool if i not in used and i not in blocked]
    if shared:
        # A soft share with legacy workloads. Central jobs retain disjoint IDs
        # among themselves and run below normal priority, never pin legacy jobs.
        external=math.ceil(observation.get('external_cpu_cores',len(pool)))
        headroom=max(1,math.ceil(len(pool)*.1))
        slots=min(math.floor(len(pool)*policy.get('shared_cpu_fraction',.5)),len(pool)-external-headroom)
        free=available[:max(0,slots-sum(r['cores'] for r in reserved))]
    else:
        external=max(0,int(math.ceil(t.get('external_reserved_cores',0)))-len(blocked))
        free=available[:max(0,len(available)-external)]
    reserve=max(t.get('reserve_gb',4),minimum_reserve,*(r.get('min_free_memory_gb',0) for r in reserved))
    if shared:reserve=max(reserve,policy.get('shared_reserve_gb',0))
    declared=sum(r['memory_gb'] for r in reserved)
    # Stale or absent usage receives no reclaimed resident credit.
    usages=t.get('usages',{})
    fresh=lambda u:isinstance(u.get('at'),(int,float)) and not isinstance(u['at'],bool) and 0<=now-u['at']<20
    resident=sum(min(holds[j['id']]['memory_gb'],max(0,usages.get(j['id'],{}).get('rss_gb',0))) for j in active if fresh(usages.get(j['id'],{})))
    physical=min(t.get('memory_total_gb',0),t.get('memory_available_gb',0)+resident)-declared-reserve
    # RAM alone may be lightly oversubscribed. CPU IDs and OS commit stay strict.
    observation=t.get('resource_observation',{})
    elastic=policy.get('elastic_memory',{})
    eligible=0
    if (elastic.get('enabled') and not shared and not t.get('external_busy')
            and pressure_reason(t,now) is None and observation.get('verified')
            and observation.get('state')=='normal' and 0<=now-observation.get('at',0)<20
            and t.get('memory_commit_verified') is True
            and t.get('memory_commit_limit_gb',0)>0 and t.get('memory_committed_gb') is not None):
        for j in active:
            r=holds[j['id']];u=usages.get(j['id'],{})
            if (j['status']=='running' and r.get('resource_mode')=='estimated'
                    and r.get('restart_policy')=='from_start' and not r.get('resource_retries',0)
                    and u.get('phase')=='running' and 0<=now-u.get('at',0)<20
                    and u.get('elapsed_seconds',0)>=60):eligible+=r['memory_gb']
    fraction=min(.1,max(0,float(elastic.get('fraction',.1))))
    credit=min(eligible*fraction,t.get('memory_total_gb',0)*fraction)
    physical=min(physical+credit,t.get('memory_available_gb',0)-reserve)

    # Commit is constrained by the OS commit limit, NOT installed RAM.
    commit=None
    if t.get('memory_committed_gb') is not None and t.get('memory_commit_limit_gb') is not None:
        declared_commit=sum(commit_budget(r) for r in reserved)
        private=sum(min(holds[j['id']].get('commit_gb',holds[j['id']]['memory_gb']),max(0,usages.get(j['id'],{}).get('private_gb',usages.get(j['id'],{}).get('rss_gb',0)))) for j in active if fresh(usages.get(j['id'],{})))
        commit_reserve=max(2,t['memory_commit_limit_gb']*.03,policy.get('shared_commit_reserve_gb',0) if shared else 0)
        commit=t['memory_commit_limit_gb']-t['memory_committed_gb']+private-declared_commit-commit_reserve
    if shared and policy.get('shared_memory_mode') != 'available':
        # Bound exposure while legacy jobs have no declared future peak contract.
        shared_limit=min(policy.get('shared_memory_gb',min(8,t.get('memory_total_gb',0)*.1)),t.get('memory_total_gb',0)*min(.5,max(0,policy.get('shared_memory_fraction',.25))))
        physical=min(physical,shared_limit-declared)
    gpu_allocations=[]
    for index,gpu in enumerate(t.get('gpus',[])):
        gid=gpu.get('index',index)
        owners=[r for r in reserved if gid in r.get('gpu_ids',[])]
        gpu_allocations.append(dict(index=gid,allocated=bool(owners),reserved_memory_gb=sum(r.get('gpu_memory_gb',0) for r in owners)))
    return dict(allocated_cpu_threads=len(set(pool)&used),total_cpu_threads=len(pool),gpu_allocations=gpu_allocations,cpu_ids=free,memory_gb=max(0,physical),elastic_memory_credit_gb=credit,reserved_memory_gb=declared,unresident_reserved_memory_gb=max(0,declared-resident),system_reserve_gb=reserve,commit_gb=None if commit is None else max(0,commit),
                execution_limit_memory_gb=sum(spec(j)['memory_gb'] for j in active),learned_reservation_jobs=sum(holds[j['id']]!=spec(j) for j in active),shared=shared,pressure_reason=pressure_reason(t,now),external_reserved_cores=external)


def readiness(node, request, now):
    t=telemetry(node)
    if not node.get('enabled',False) or not t.get('worker',True):return '服务器未开放接单'
    if node.get('last_seen',0)<now-30:return '服务器离线或状态过期'
    if request.get('target','auto') not in ('auto',node['id']):return '任务指定了其他服务器'
    request=platform_request(request,t.get('os','windows'))
    if request is None:return '任务包未提供该操作系统的兼容入口'
    if request.get('os','windows')!=t.get('os','windows'):return '任务与服务器操作系统不兼容'
    if request.get('requires_isolation') and not t.get('isolation_verified'):return '中心正在准备用户隔离环境'
    runtime=t.get('runtimes',{}).get(request['runtime'])
    if not runtime or runtime.get('status')=='not_installed':return '缺少任务所需应用'
    if runtime.get('status','ready')!='ready':return '应用尚未通过后台运行验证'
    if runtime.get('kind')!=request['kind']:return '应用类型不兼容'
    if not version_ready(request,runtime):return '应用版本未满足任务最低版本或版本尚未核实'
    if request.get('gpus',0) and runtime.get('gpu_supported') is False:return '运行环境未开放 GPU'
    limits=t.get('execution_limits',{})
    # Transport limits are admission constraints, not shared-host inventory.
    for field,cap,label in [('cores','max_cpu_threads','CPU'),('memory_gb','max_memory_gb','内存'),('commit_gb','max_commit_gb','提交预算'),('disk_gb','max_disk_gb','磁盘')]:
        if limits.get(cap) is not None and request.get(field,0)>limits[cap]:return '超过当前后端'+label+'限额'
    if request['cores']>len(t.get('cpu_pool') or range(max(1,int(t.get('cpu_threads',1))-1))):return '所需 CPU 超过单机可分配容量'
    if request['memory_gb']>t.get('memory_total_gb',0)-max(t.get('reserve_gb',4),request.get('min_free_memory_gb',0)):return '所需内存超过单机可分配容量'
    if request.get('gpus',0)>len(t.get('gpus',[])):return '缺少所需 GPU'
    if t.get('memory_commit_verified') is False:return '等待内存承诺状态核验'
    # A wait which cannot be resolved by draining this host must not reserve it.
    if t.get('memory_commit_limit_gb') is not None:
        limit=t['memory_commit_limit_gb']
        if commit_budget(request)>limit-max(2,limit*.03):return '所需提交额度超过单机可分配容量'
    if request.get('gpus',0):
        capable=sum(g.get('memory_total_gb',float('inf'))>=request.get('gpu_memory_gb',0)+.5 for g in t.get('gpus',[]))
        if capable<request['gpus']:return '所需显存超过单机 GPU 容量'
    if t.get('external_busy') and 'external_reserved_cores' not in t:return '等待现有计算的资源占用核验'
    pressure=pressure_reason(t,now)
    if pressure:return pressure
    return None


def admission_capacity(node,request,active,now):
    capacity=budget(node,active,now,request.get('min_free_memory_gb',0));t=telemetry(node)
    # A guarded estimated startup can share a small part of its own padding.
    # This bootstraps the existing 10% overlap without waiting 60 seconds for a
    # task that otherwise cannot start. Every existing hold must have complete
    # source/profile evidence; raw estimates and strict commit remain covered.
    estimate=request.get('resource_estimate',{}).get('memory_gb')
    elastic=t.get('scheduling_policy',{}).get('elastic_memory',{})
    if (active and capacity['learned_reservation_jobs']==len(active)
            and capacity['elastic_memory_credit_gb']>0 and elastic.get('enabled')
            and request.get('kernel_memory_required') and request.get('resource_mode')=='estimated'
            and request.get('restart_policy')=='from_start' and not request.get('resource_retries',0)
            and not request.get('resource_calibration') and not isinstance(estimate,bool)
            and isinstance(estimate,(float,int)) and math.isfinite(estimate) and estimate>0):
        fraction=min(.1,max(0,float(elastic.get('fraction',.1))))
        cap=min(t.get('memory_total_gb',0)*fraction,
                (capacity['reserved_memory_gb']+request['memory_gb'])*fraction)
        extra=max(0,min(cap-capacity['elastic_memory_credit_gb'],request['memory_gb']-estimate))
        capacity['memory_gb']=min(capacity['memory_gb']+extra,t.get('memory_available_gb',0)-capacity['system_reserve_gb'])
        capacity['startup_memory_credit_gb']=extra
    return capacity

def required_disk(request, runtime_disk=0, retained_allocated_gb=0):
    total=request.get('disk_gb',4)+runtime_disk
    if not math.isfinite(retained_allocated_gb) or retained_allocated_gb<0:
        raise ValueError('Invalid retained disk allocation')
    return max(0,total-min(total,retained_allocated_gb))+2


def allocate(node, request, active=(), now=None):
    now=time.time() if now is None else now
    reason=readiness(node,request,now)
    if reason:return None,reason
    t=telemetry(node);request=platform_request(request,t.get('os','windows'));active=[j for j in active if not j.get('execution_stopped')];reserved=[spec(j) for j in active]
    if any(j['status']=='recovery_needed' for j in active):return None,'等待异常任务恢复，避免重复占用'
    if len(active)>=t.get('max_parallel',1):return None,'等待执行席位'
    capacity=admission_capacity(node,request,active,now);free=capacity['cpu_ids']
    if len(free)<request['cores']:return None,'等待 CPU 容量释放'
    if capacity['memory_gb']<request['memory_gb']:return None,'等待物理内存或共享额度释放（保留运行任务峰值余量）'
    if capacity['commit_gb'] is not None and capacity['commit_gb']<commit_budget(request):
        return None,'等待系统提交额度释放'
    runtime=t.get('runtimes',{}).get(request['runtime'],{})
    runtime_disk=max(0,runtime.get('disk_overhead_gb',0))
    disks=disk_capacities(node,active,now)
    disks=[v for v in disks if now-request.get('storage_failures',{}).get(node['id']+':'+str(v[0]),0)>=60]
    needed_disk=required_disk(request,runtime_disk)
    # Administrator order prefers the established fast work disk; overflow
    # uses another configured drive without moving any active directory.
    volume,free_disk=next((v for v in disks if v[1]>=needed_disk),max(disks,key=lambda v:v[1]) if disks else (None,0))
    if free_disk<needed_disk:return None,'可用工作盘空间不足'
    seats=t.get('scheduling_policy',{}).get('runtime_max_parallel',{}).get(request['runtime'],runtime.get('max_parallel'))
    if seats and sum(r['runtime']==request['runtime'] for r in reserved)>=seats:return None,'等待应用许可或并发席位'
    gpu_used={i for r in reserved for i in r.get('gpu_ids',[])}
    # Best fit preserves the larger-VRAM devices for jobs that need them.
    gpu_ids=[gid for _,gid in sorted((g.get('memory_free_gb',0),g.get('index',i)) for i,g in enumerate(t.get('gpus',[])) if g.get('index',i) not in gpu_used and g.get('available',False) and g.get('memory_free_gb',0)>=request.get('gpu_memory_gb',0)+.5)]
    if len(gpu_ids)<request.get('gpus',0):return None,'等待 GPU 或显存释放'
    result=dict(request,cpu_ids=free[:request['cores']],shared_node=capacity['shared'],runtime_disk_gb=runtime_disk)
    if capacity.get('startup_memory_credit_gb'):
        normal=budget(node,active,now,request.get('min_free_memory_gb',0))
        borrowed=max(0,request['memory_gb']-normal['memory_gb'])
        if borrowed:result['startup_memory_credit_gb']=borrowed
    result.pop('work_volume',None)
    result.pop('work_layout_ready',None)
    if volume is not None:result['work_volume']=volume
    if request.get('gpus'):result['gpu_ids']=gpu_ids[:request['gpus']]
    return result,None

def finish_bound(job, now):
    # Wall-time declarations are historical metadata, never enforced releases.
    # Preserve resource admission without promising a long task will be killed.
    return None


def reservation_release(node,request,active,now):
    # Find the first enforced release that actually makes the head fit. Unknown
    # duration jobs may remain if their reserved resources do not block it.
    limits={j['id']:finish_bound(j,now) for j in active}
    for at in sorted({x for x in limits.values() if x is not None}):
        finished=[j for j in active if limits[j['id']] is not None and limits[j['id']]<=at]
        # Current learned holds are revocable observations, not a future release
        # guarantee. Project remaining executions at their unchanged hard limits.
        remaining=[dict(j,admission_reservation=None) for j in active if j not in finished]
        t=dict(telemetry(node));usages=t.get('usages',{})
        policy=dict(t.get('scheduling_policy',{}))
        policy['elastic_memory']=dict(enabled=False)
        t['scheduling_policy']=policy
        t['memory_available_gb']=min(t.get('memory_total_gb',0),t.get('memory_available_gb',0)+sum(usages.get(j['id'],{}).get('rss_gb',0) for j in finished))
        if t.get('memory_committed_gb') is not None:
            t['memory_committed_gb']=max(0,t['memory_committed_gb']-sum(usages.get(j['id'],{}).get('private_gb',usages.get(j['id'],{}).get('rss_gb',0)) for j in finished))
        # A center-owned GPU becomes admissible when its owning lease ends.
        # Keep the current free-memory reading: it is a conservative lower bound.
        released_gpu={gpu for j in finished for gpu in spec(j).get('gpu_ids',[])}
        still_used={gpu for j in remaining for gpu in spec(j).get('gpu_ids',[])}
        t['gpus']=[dict(g,available=True) if g.get('index',i) in released_gpu-still_used else dict(g) for i,g in enumerate(t.get('gpus',[]))]
        projected=dict(node,telemetry=t)
        if allocate(projected,request,remaining,now)[0] is not None:return at
    return None


def reservation_deficits(node, request, active, now):
    """Resource cuts for an older head, even without a known finish time.

    Backfill may consume surplus in a dimension, never enlarge an existing
    deficit or create one. This protects CPU/RAM/commit/licenses independently
    rather than making a GPU wait reserve an entire host.
    """
    t=telemetry(node);request=platform_request(request,t.get('os','windows'));active=[j for j in active if not j.get('execution_stopped')]
    reserved=[spec(j) for j in active]
    c=admission_capacity(node,request,active,now)
    used_gpu={g for r in reserved for g in r.get('gpu_ids',[])}
    gpu_count=sum(g.get('index',i) not in used_gpu and g.get('available',False)
                  and g.get('memory_free_gb',0)>=request.get('gpu_memory_gb',0)+.5
                  for i,g in enumerate(t.get('gpus',[])))
    runtime=t.get('runtimes',{}).get(request['runtime'],{})
    seats=t.get('scheduling_policy',{}).get('runtime_max_parallel',{}).get(request['runtime'],runtime.get('max_parallel'))
    cuts=[request['cores']-len(c['cpu_ids']),request['memory_gb']-c['memory_gb'],
          1-(t.get('max_parallel',1)-len(active)),request.get('gpus',0)-gpu_count,
          request.get('disk_gb',4)+max(0,runtime.get('disk_overhead_gb',0))+2
          -max((free for _,free in disk_capacities(node,active,now)),default=0)]
    if c['commit_gb'] is not None:cuts.append(commit_budget(request)-c['commit_gb'])
    if seats:cuts.append(1-(seats-sum(r['runtime']==request['runtime'] for r in reserved)))
    return [max(0,x) for x in cuts]


def preserves_reservation(node, head, active, candidate, now, release):
    after=active+[candidate]
    before_cuts=reservation_deficits(node,head,active,now)
    after_cuts=reservation_deficits(node,head,after,now)
    if all(b<=a+1e-9 for a,b in zip(before_cuts,after_cuts)):return True
    if release is None:return False
    preserved=reservation_release(node,head,after,now)
    return preserved is not None and preserved<=release


def dependency_jobs(jobs):
    """Latest attempts control readiness; leased inputs stay frozen in spec."""
    latest={};oldest={}
    for job in sorted(jobs,key=lambda j:(j['created'],j['id'])):
        key=spec(job).get('task_key')
        if key:
            group=(job.get('batch_id'),key);latest[group]=job;oldest[group]=min(oldest.get(group,job['created']),job['created'])
    out=[]
    for job in jobs:
        s=dict(spec(job));job=dict(job,spec=s)
        if isinstance(s.get('queue_origin_created'),(float,int)) and not isinstance(s['queue_origin_created'],bool) and math.isfinite(s['queue_origin_created']) and s['queue_origin_created']<=job['created']:job['queue_created']=s['queue_origin_created']
        elif s.get('retry_origin')=='manual':job['queue_created']=job['created']
        elif s.get('execution_retries',0) or s.get('resource_retries',0):job['queue_created']=oldest.get((job.get('batch_id'),s.get('task_key')),job['created'])
        if job['status']=='queued' and s.get('depends_on'):
            s.pop('dependency_wait',None);s['dependency_jobs']={}
            for key in s['depends_on']:
                prior=latest.get((job.get('batch_id'),key))
                if not prior or prior['status']!='succeeded':
                    status=prior['status'] if prior else 'missing'
                    s['dependency_wait']=('前序任务失败或取消，请重试前序任务：' if status in ('failed','cancelled') else '等待前序任务完成：')+key
                    break
                s['dependency_jobs'][key]=prior['id']
        out.append(job)
    return out


def future_shapes(queued, start, limit=32):
    """A batch of identical small jobs must not hide a later scarce request."""
    seen=set();out=[]
    for job in queued[start:]:
        need=spec(job)
        if need.get('dependency_wait'):continue
        shape=tuple(need.get(k) for k in ('runtime','kind','target','cores','memory_gb','commit_gb','gpus','gpu_memory_gb','disk_gb','min_free_memory_gb','kernel_memory_required'))+(json.dumps(need.get('platforms'),sort_keys=True),)
        if shape in seen:continue
        seen.add(shape);out.append(need)
        if len(out)>=limit:break
    return out


def demand_weight(request, nodes):
    """Dominant resource fraction: no fixed CPU-to-GiB/GPU exchange rate."""
    ts=[telemetry(n) for n in nodes]
    cpu=sum(len(t.get('cpu_pool') or range(max(1,int(t.get('cpu_threads',1))-1))) for t in ts)
    ram=sum(max(0,t.get('memory_total_gb',0)-t.get('reserve_gb',4)) for t in ts)
    commit=sum(max(0,t.get('memory_commit_limit_gb',0)) for t in ts)
    gpu=sum(len(t.get('gpus',[])) for t in ts)
    return max(request['cores']/max(1,cpu),request['memory_gb']/max(1,ram),
               commit_budget(request)/max(1,commit) if commit else 0,
               request.get('gpus',0)/max(1,gpu))


def repair_placements(nodes, queued, active, assignments, reasons, reservations, heads, head_ids, now):
    """Bounded augmenting placement before any lease has been committed.

    Move at most one newly planned job to admit another. Never move a live
    lease, drop an admitted job, change a budget, or bypass an older reservation.
    All resource validation goes through the same allocator as normal dispatch.
    """
    remaining=512
    def allowed(node, job, placement, base):
        nid=node['id']
        if nid not in reservations or head_ids[nid]==job['id']:return True
        candidate=dict(job,node=nid,spec=placement,status='leased',updated=now)
        return preserves_reservation(node,heads[nid],base,candidate,now,reservations[nid])
    def admit(node,job,placement):
        nid=node['id'];assignments[job['id']]=dict(node=nid,spec=placement)
        active[nid].append(dict(job,node=nid,spec=placement,status='leased',updated=now))
        reasons.pop(job['id'],None)
        if head_ids.get(nid)==job['id']:
            reservations.pop(nid,None);heads.pop(nid,None);head_ids.pop(nid,None)
    for job in queued:
        if job['id'] in assignments or spec(job).get('dependency_wait'):continue
        for node in nodes:
            if readiness(node,spec(job),now):continue
            nid=node['id'];placement,_=allocate(node,spec(job),active[nid],now)
            if placement and allowed(node,job,placement,active[nid]):
                admit(node,job,placement);break
            repaired=False
            for victim in list(active[nid]):
                if victim['id'] not in assignments:continue
                remaining-=1
                if remaining<0:return
                base=[j for j in active[nid] if j['id']!=victim['id']]
                placement,_=allocate(node,spec(job),base,now)
                if not placement or not allowed(node,job,placement,base):continue
                for other in nodes:
                    if other['id']==nid:continue
                    moved,_=allocate(other,spec(victim),active[other['id']],now)
                    if not moved or not allowed(other,victim,moved,active[other['id']]):continue
                    active[nid]=base
                    admit(other,victim,moved);admit(node,job,placement)
                    repaired=True;break
                if repaired:break
            if repaired:break


def node_preference(node):
    # Placement preference applies only after compatibility and capacity checks.
    return {'dell-r940':0,'a800-ssh':1}.get(node['id'],2)


def plan(nodes, jobs, now=None, estimate=None):
    now=time.time() if now is None else now
    nodes=sorted((dict(n) for n in nodes),key=node_preference);jobs=inherit_dependency_priorities(dependency_jobs([dict(j) for j in jobs]))
    active={n['id']:[j for j in jobs if j.get('node')==n['id'] and j['status'] in ACTIVE and not j.get('execution_stopped')] for n in nodes}
    assignments={};reasons={};reservations={};reservation_heads={};reservation_ids={}
    queued=sorted((j for j in jobs if j['status']=='queued'),key=queue_key)
    top_priority=max((j.get('priority',0) for j in queued),default=0)
    urgent=[j for j in queued if top_priority>0 and j.get('priority',0)==top_priority]
    for index,job in enumerate(queued):
        if urgent and job.get('priority',0)<top_priority:
            reasons[job['id']]='等待最高优先级任务实际领取资源；现有任务完成后优先安排，暂不补位'
            continue
        request=spec(job);fits=[];failures=[];compatible=[]
        if request.get('dependency_wait'):
            reasons[job['id']]=request['dependency_wait'];continue
        for node in nodes:
            nid=node['id'];why=readiness(node,request,now)
            if why:failures.append(node.get('label',nid)+'：'+why);continue
            compatible.append(node)
            assignment,why=allocate(node,request,active[nid],now)
            if why:failures.append(node.get('label',nid)+'：'+why);continue
            if nid in reservations:
                release=reservations[nid]
                candidate=dict(job,node=nid,spec=assignment,status='leased',updated=now)
                if not preserves_reservation(node,reservation_heads[nid],active[nid],candidate,now,release):
                    failures.append(node.get('label',nid)+'：为更早排队的任务保留资源');continue
            t=telemetry(node);duration=estimate(assignment,nid) if estimate else None
            occupied=sum(spec(j)['cores'] for j in active[nid])+t.get('external_reserved_cores',0)
            # Known same-task timings outrank load heuristics. With no history,
            # spread load and preserve nodes with scarce application capabilities.
            scarcity=len({r.get('kind') for r in t.get('runtimes',{}).values() if r.get('status','ready')=='ready'})
            unused_gpu=0 if request.get('gpus') else len(t.get('gpus',[]))
            score=(node_preference(node),0 if duration else 1,duration or 0,unused_gpu,occupied/max(1,len(t.get('cpu_pool') or range(t.get('cpu_threads',1)))),scarcity,len(t.get('cpu_pool') or range(t.get('cpu_threads',1)))-occupied-request['cores'],nid)
            fits.append((score,node,assignment))
        if fits:
            # Keep FIFO start priority, but place this job where it destroys
            # the fewest remaining placement options. A bounded ready-task
                # lookahead prevents CPU-only jobs taking the only GPU / large-RAM
            # node while an otherwise usable smaller node sits idle.
            if len(fits)>1:
                future=[]
                for need in future_shapes(queued,index+1):
                    options={n['id'] for n in nodes if allocate(n,need,active[n['id']],now)[0] is not None}
                    if options:future.append((need,options,demand_weight(need,nodes)))
                ranked=[]
                for score,node,assignment in fits:
                    nid=node['id'];after=active[nid]+[dict(job,node=nid,spec=assignment,status='leased',updated=now)]
                    lost=sum(weight/len(options) for need,options,weight in future if nid in options and allocate(node,need,after,now)[0] is None)
                    ranked.append(((score[0],lost,*score[1:]),node,assignment))
                fits=ranked
            _,node,assignment=min(fits,key=lambda f:f[0]);nid=node['id']
            assignments[job['id']]=dict(node=nid,spec=assignment)
            active[nid].append(dict(job,node=nid,spec=assignment,status='leased',updated=now))
            continue
        reasons[job['id']]='；'.join(failures) or '等待兼容的服务器'
        # Prevent a stream of small later jobs from starving an earlier large job.
        # Reserve one eligible node; other nodes remain available for later work.
        # A queue of similar large jobs must not reserve every compatible host.
        # One protected host in an overlapping pool preserves FIFO progress;
        # the others can backfill with independent CPU / small-memory jobs.
        candidates=[] if any(n['id'] in reservations for n in compatible) else [n for n in compatible if n['id'] not in reservations]
        if candidates:
            def reservation(node):return reservation_release(node,request,active[node['id']],now)
            # Prefer a known earlier release over an unbounded external hold.
            candidate=min(candidates,key=lambda n:(reservation(n) is None,reservation(n) or float('inf'),node_preference(n),sum(spec(j)['cores'] for j in active[n['id']]),n['id']))
            reservations[candidate['id']]=reservation(candidate)
            reservation_heads[candidate['id']]=request
            reservation_ids[candidate['id']]=job['id']
    repair_queue=queued
    if urgent:
        repair_queue=[j for j in queued if j.get('priority',0)>=top_priority]
    repair_placements(nodes,repair_queue,active,assignments,reasons,reservations,reservation_heads,reservation_ids,now)
    return assignments,reasons
