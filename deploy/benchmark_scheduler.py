"""Deterministic resource-packing and event-replay benchmark; never submits jobs.

Optional --baseline /path/to/scheduler.py compares a saved release. Reported
utilization is reserved compute capacity in a model, not host CPU telemetry.
"""
import argparse,importlib.util,json,random,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scheduler

def nodes(now):
    out=[]
    for name,cpu,ram,gpu in [('a',8,24,2),('b',8,32,0),('c',16,96,1),('d',16,48,0)]:
        out.append(dict(id=name,enabled=True,last_seen=now,telemetry=dict(cpu_threads=cpu,cpu_pool=list(range(cpu)),memory_total_gb=ram,memory_available_gb=ram,reserve_gb=0,memory_commit_limit_gb=ram*2,memory_committed_gb=0,max_parallel=cpu,disk_free_gb=10000,runtimes={'python':dict(kind='python',status='ready')},gpus=[dict(index=i,available=True,memory_free_gb=24 if i==0 else 8) for i in range(gpu)])))
    return out

def jobs(seed,count=30):
    rng=random.Random(seed);out=[]
    for i in range(count):
        cores=rng.choice([1,1,2,4,8]);ram=rng.choice([1,2,4,8,16,24]);gpu=int(rng.random()<.2);vram=rng.choice([2,6,16])
        duration=60+(i*47+seed*31)%300
        out.append(dict(id=str(i),created=1+i,status='queued',model_seconds=duration,
                        spec=dict(cores=cores,memory_gb=ram,commit_gb=ram,disk_gb=1,runtime='python',kind='python',gpus=gpu,gpu_memory_gb=vram,wall_minutes=duration/60)))
    return out

def packing(mod,seed):
    started=time.perf_counter();p,_=mod.plan(nodes(10000),jobs(seed),10000)
    return dict(cpu=sum(v['spec']['cores'] for v in p.values()),gpu=sum(v['spec'].get('gpus',0) for v in p.values()),jobs=len(p),milliseconds=(time.perf_counter()-started)*1000)

def replay(mod,seed):
    now=10000;start=now;ns=nodes(now);js=jobs(seed);wait=[];cpu_seconds=gpu_seconds=0;calls=0
    while any(j['status']!='succeeded' for j in js):
        for n in ns:
            n['last_seen']=now;t=n['telemetry'];active=[j for j in js if j.get('node')==n['id'] and j['status']=='running']
            t['memory_available_gb']=t['memory_total_gb']-sum(j['spec']['memory_gb'] for j in active)
            t['memory_committed_gb']=sum(mod.commit_budget(j['spec']) for j in active)
            t['usages']={j['id']:dict(at=now,rss_gb=j['spec']['memory_gb'],private_gb=j['spec']['commit_gb']) for j in active}
        p,_=mod.plan(ns,js,now);calls+=1
        for j in js:
            if j['id'] in p:
                j.update(status='running',node=p[j['id']]['node'],spec=p[j['id']]['spec'],compute_started=now,ends=now+j['model_seconds']);wait.append(now-start)
        active=[j for j in js if j['status']=='running']
        if not active:raise AssertionError('runnable workload stranded: '+str(seed))
        end=min(j['ends'] for j in active);dt=end-now
        cpu_seconds+=dt*sum(j['spec']['cores'] for j in active);gpu_seconds+=dt*sum(j['spec'].get('gpus',0) for j in active)
        now=end
        for j in active:
            if j['ends']<=now:j['status']='succeeded'
        if calls>100:raise AssertionError('replay did not converge')
    return dict(makespan=now-start,cpu_utilization=cpu_seconds/((now-start)*48),gpu_utilization=gpu_seconds/((now-start)*3),mean_wait=sum(wait)/len(wait))

def run(mod,seeds,replay_seeds):
    rows=[dict(seed=s,**packing(mod,s)) for s in seeds]
    events=[dict(seed=s,**replay(mod,s)) for s in replay_seeds]
    return dict(packing=rows,replay=events,summary=dict(cpus=sum(r['cpu'] for r in rows),gpus=sum(r['gpu'] for r in rows),jobs=sum(r['jobs'] for r in rows),packing_mean_ms=sum(r['milliseconds'] for r in rows)/len(rows),replay_mean_cpu_utilization=sum(r['cpu_utilization'] for r in events)/len(events),replay_mean_gpu_utilization=sum(r['gpu_utilization'] for r in events)/len(events),replay_mean_makespan=sum(r['makespan'] for r in events)/len(events)))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--baseline');ap.add_argument('--output',required=True);args=ap.parse_args()
    seeds=list(range(100))+list(range(1000,1100));event_seeds=list(range(2000,2020))
    result=dict(description='Four heterogeneous hosts; CPU/RAM/commit/GPU/VRAM constraints. Capacity-model utilization, not measured hardware CPU.',candidate=run(scheduler,seeds,event_seeds))
    if args.baseline:
        load=importlib.util.spec_from_file_location('baseline_scheduler',args.baseline);baseline=importlib.util.module_from_spec(load);load.loader.exec_module(baseline)
        result['baseline']=run(baseline,seeds,event_seeds)
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v['summary'] for k,v in result.items() if isinstance(v,dict)},indent=2))
