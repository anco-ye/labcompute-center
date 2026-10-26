"""Read-only inventory for external SSH targets, separate from pull workers.

Registration does not grant leases. Explicit owner policy determines capacity;
a Linux target is never converted into a Windows worker.
"""
import json
import math
from pathlib import Path


def summary(target):
    limits=target.get('limits',{})
    valid=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0
    missing=[]
    if not target.get('backend_ready'):missing.append('外部 SSH 唯一派发、断线对账与结果回收后端待实现，禁止直接领取现有任务')
    if not all(valid(limits.get(k)) for k in ('cpu_threads','memory_gb','disk_gb')):missing.append('缺少有界资源配置')
    if not target.get('credential_ref'):missing.append('中心尚无可用的受保护 SSH 凭据')
    if not target.get('host_key_verified'):missing.append('SSH 主机密钥仅已观测，尚未确认可信来源')
    if not target.get('workspace_verified'):missing.append('专用工作目录及空间额度尚未确认')
    if not target.get('limits_enforced'):missing.append('尚未确认进程树 CPU 和内存限额执行能力')
    if not target.get('runtimes'):missing.append('尚无中心验证的 Linux 运行时')
    return dict(id=target['id'],label=target.get('label',target['id']),transport='ssh',os='linux',
                enabled=bool(target.get('enabled') and not missing),dispatch_ready=not missing,phase='可接收 Linux CPU/GPU 任务' if target.get('gpu_count',0) and not missing else '可接收 Linux CPU 任务' if not missing else '接入准备中',limits=limits,gpus=target.get('gpu_count',0) if not missing else 0,
                runtimes=target.get('runtimes',{}),supported_kinds=sorted({r.get('kind','python') for r in target.get('runtimes',{}).values()}),resource_scope=target.get('resource_scope','bounded_shared_cpu_pool'),
                candidate_runtimes=target.get('candidate_runtimes',[]),blockers=missing)


def inventory(config_path):
    path=Path(config_path)
    if not path.exists():return []
    document=json.loads(path.read_text(encoding='utf-8-sig'))
    return [summary(t) for t in document.get('targets',[])]


def launch(config_path, app_dir):
    """External configuration/connection failures must never stop the Web app."""
    import logging,subprocess,sys
    path=Path(config_path)
    if not path.exists():return []
    try:targets=json.loads(path.read_text(encoding='utf-8-sig')).get('targets',[])
    except (OSError,ValueError):
        logging.getLogger(__name__).exception('External target registry unavailable; Windows workers unaffected')
        return []
    started=[]
    for target in targets:
        try:
            if not summary(target)['enabled']:continue
            config=Path(target['bridge_config'])
            # The bridge owns leases independently of the Web. A Web restart
            # must reuse its owner rather than spawn duplicate lock failures.
            if bridge_running(config.parent/'bridge.lock'):continue
            with open(config.parent/'bridge.log','ab',buffering=0) as log:
                child=subprocess.Popen([sys.executable,'-u',str(Path(app_dir)/'external/ssh_bridge.py'),'--config',str(config)],stdout=log,stderr=subprocess.STDOUT)
            started.append(child.pid)
        except (OSError,KeyError,TypeError):
            logging.getLogger(__name__).exception('External bridge not started; Windows workers unaffected')
    return started


def bridge_running(path):
    """Probe the exact OS lock used by Bridge.run, never guess from a PID."""
    import os
    path=Path(path)
    if not path.exists():return False
    with path.open('r+b') as stream:
        if os.name=='nt':
            import msvcrt
            stream.seek(0)
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return True
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    return False
