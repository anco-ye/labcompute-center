"""Trusted original-volume accounting for an explicitly authorized stopped task."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

RELEASE = '2026-10-24-retained-storage-v1'


def _snapshot(folder):
    started=time.monotonic(); rows={}; allocated=0
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs+files:
            p=Path(base)/name
            if p.is_symlink() or getattr(p, 'is_junction', lambda:False)():
                raise ValueError('Links in retained workspace require separate dependency handling')
        for name in files:
            p=Path(base)/name; st=p.stat()
            rows[str(p.relative_to(folder))]=(st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino)
            # Shared cache/hardlink bytes cannot credit several continuations.
            if st.st_nlink==1:
                if os.name=='nt':
                    import ctypes
                    size=ctypes.windll.kernel32.GetCompressedFileSizeW
                    size.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
                    size.restype=ctypes.c_uint32
                    high=ctypes.c_uint32();ctypes.windll.kernel32.SetLastError(0)
                    low=size(str(p),ctypes.byref(high))
                    if low==0xffffffff and ctypes.windll.kernel32.GetLastError():
                        raise OSError('Cannot observe allocated file size')
                    allocated+=(high.value<<32)|low
                else: allocated+=st.st_blocks*512
            if len(rows)>200000 or time.monotonic()-started>15:
                raise TimeoutError('Retained observation budget exceeded')
    return rows,allocated


def observe(folder, *, job_id, lease_digest, volume, node, credit=True):
    """Call under the native task lock; inspect allocation, never file contents."""
    folder=Path(folder)
    if folder.is_symlink() or not folder.is_dir():raise ValueError('Unsafe retained workspace')
    stat=folder.stat()
    # A stopped environment may contain normal interpreter symlinks. Without
    # allocation credit, reserve its full budget and never traverse those links.
    rows,allocated=_snapshot(folder) if credit else ({},0)
    if (folder.stat().st_dev,folder.stat().st_ino)!=(stat.st_dev,stat.st_ino):
        raise ValueError('Retained workspace changed')
    return dict(release=RELEASE,verified=True,at=time.time(),job_id=job_id,
                lease_digest=lease_digest,volume=volume,node=node,allocated_bytes=allocated,
                device=stat.st_dev,inode=stat.st_ino,files=len(rows),
                accounting_mode='exclusive_allocated' if credit else 'full_budget_no_credit')


def relocate_copy(source, destination, *, stopped, commit_location):
    """Locked caller copies an inactive workspace, preserving the entire source.

    No file hashing or source deletion. The callback changes only the original
    placement record after metadata and continued inactivity are verified.
    """
    import shutil
    source,destination=Path(source),Path(destination)
    if not stopped():raise ValueError('Active task cannot relocate')
    if source.is_symlink() or destination.exists() or source==destination:
        raise ValueError('Unsafe or existing relocation destination')
    before,_=_snapshot(source)
    stage=destination.with_name(destination.name+'.retained-copy')
    if stage.exists():raise ValueError('Interrupted copy exists; retain and inspect before retry')
    shutil.copytree(source,stage,copy_function=shutil.copy2)
    after,_=_snapshot(source);copied,_=_snapshot(stage)
    if before!=after or {k:v[:2]for k,v in before.items()}!={k:v[:2]for k,v in copied.items()}:
        raise ValueError('Workspace changed or incomplete copy; originals retained')
    if not stopped():raise ValueError('Task became active; copy retained without location change')
    stage.replace(destination)
    # A failed callback leaves both copies; never infer a completed handoff.
    commit_location()
    return dict(release=RELEASE,source=str(source),destination=str(destination),
                files=len(before),bytes=sum(v[0]for v in before.values()),
                source_preserved=True,placement_committed=True)


def received_observation(evidence, received_at=None):
    """Fresh authenticated RPC response uses the controller clock, not node NTP."""
    stamp=time.time() if received_at is None else received_at
    if not isinstance(stamp,(float,int)) or not math.isfinite(stamp):
        raise ValueError('Invalid receipt time')
    return dict(evidence,remote_at=evidence['at'],at=stamp)


def admit(node, active, original, selected, evidence, now=None):
    """Reuse only proven existing allocation; retain the full execution disk limit.

    This runs after ordinary scheduler.plan has admitted CPU/RAM/GPU, licenses,
    queue priority and the enabled node. It only binds that placement back to the
    original volume and checks its additional growth using the same disk reserve.
    Evidence is administrator/native data, never a task manifest field.
    """
    import scheduler
    now = time.time() if now is None else now
    spec = scheduler.spec(original)
    if (not node.get('enabled') or original.get('control') != 'run'
            or original.get('status') != 'failed' or not original.get('execution_stopped')
            or original.get('node') != node['id']):
        raise ValueError('Original task is not eligible for continuation')
    if original['id'] in {j['id'] for j in active if not j.get('execution_stopped')}:
        raise ValueError('Task already active')
    volume = spec.get('work_volume')
    digest = hashlib.sha256(original['lease'].encode()).hexdigest()
    if (evidence.get('release') != RELEASE or evidence.get('verified') is not True
            or not 0 <= now-evidence.get('at', 0) < 30
            or not 0 <= now-node.get('last_seen', 0) < 30
            or evidence.get('job_id') != original['id'] or evidence.get('lease_digest') != digest
            or evidence.get('node') != node['id'] or evidence.get('volume') != volume):
        raise ValueError('Retained storage identity/freshness mismatch')
    for key in ('cores', 'memory_gb', 'commit_gb', 'disk_gb', 'gpus', 'gpu_memory_gb'):
        if selected.get(key) != spec.get(key):
            raise ValueError('Continuation resource budget changed')
    size = evidence.get('allocated_bytes')
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError('Invalid retained allocation')
    budget = selected.get('disk_gb', 4)+selected.get('runtime_disk_gb', 0)
    used = min(budget, size/2**30)
    capacities = dict(scheduler.disk_capacities(node, active, now))
    required = scheduler.required_disk(selected, selected.get('runtime_disk_gb', 0), used)
    if volume not in capacities or capacities[volume] < required:
        raise ValueError('Original volume lacks remaining growth budget: available=%.3f required=%.3f GiB' %
                         (capacities.get(volume, 0), required))
    return dict(selected, work_volume=volume), dict(volume=volume, budget_gb=budget,
                    retained_allocated_gb=used, growth_and_margin_gb=required,
                    available_gb=capacities[volume], execution_budget_unchanged=True)
