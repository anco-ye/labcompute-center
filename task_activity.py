"""Display-only activity metadata from existing per-job observations."""
import math


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0


def summarize(job, usage, now, online):
    at=usage.get('at');age=now-at if finite(at) and at<=now else None
    cpu=usage.get('cpu_cores');cpu=cpu if finite(cpu) else None
    fresh=bool(online and age is not None and age<35)
    progress=job.get('progress') or {};stage=str(progress.get('stage') or '').strip()
    # COMSOL also emits blank or memory-only progress labels. Never present
    # those diagnostic counters as the name of a computational stage.
    if stage.lower().startswith('memory:'):stage=''
    updated=job.get('progress_updated_at')
    return dict(cpu_cores=cpu,allocated_cpu_threads=job.get('spec',{}).get('cores'),
                observed_at=at if finite(at) else None,observation_age_seconds=age,cpu_fresh=fresh,
                progress_updated_at=updated if finite(updated) else None,
                stage_name=stage or None,
                stage_label=stage or ('求解器计算中（未报告具体阶段）' if job.get('spec',{}).get('kind')=='comsol' else '计算中（未报告具体阶段）'),
                state='cpu_active' if fresh and cpu is not None and cpu>.05 else 'observed' if fresh else 'stale')
