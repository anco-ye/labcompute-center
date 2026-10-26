"""Bound a registered legacy execution tree and persist its actual exit evidence.

Used by orchestration launchers, without changing solver code or inputs. A bound
is an execution limit, never a claim that partial output is a checkpoint.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import psutil


def save(path, value):
    path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2),encoding='utf-8');os.replace(tmp,path)


def run(command, receipt, private_gb, wall_minutes=1440, interval=2):
    started=time.time();p=subprocess.Popen(command);root=psutil.Process(p.pid)
    known={(root.pid,root.create_time()):root};reason=None;peak=0
    evidence=dict(command=command,started=started,root_pid=p.pid,root_created=root.create_time(),
                  private_limit_gb=private_gb,wall_minutes=wall_minutes,status='running')
    save(receipt,evidence)
    try:
        while p.poll() is None:
            try:
                for child in root.children(recursive=True):known[(child.pid,child.create_time())]=child
            except psutil.NoSuchProcess:pass
            private=0;identities=[]
            for (pid,created),child in known.items():
                try:
                    if not child.is_running() or child.status()==psutil.STATUS_ZOMBIE:continue
                    m=child.memory_info();private+=getattr(m,'private',m.rss)
                    identities.append(dict(pid=pid,created=created))
                except psutil.NoSuchProcess:pass
            peak=max(peak,private)
            evidence.update(at=time.time(),private_bytes=private,peak_private_bytes=peak,processes=identities)
            if private>private_gb*2**30:reason='resource_limit'
            elif time.time()-started>wall_minutes*60:reason='time_limit'
            save(receipt,evidence)
            if reason:break
            time.sleep(interval)
    finally:
        live_children=[]
        for child in known.values():
            try:
                if child.pid!=p.pid and child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:live_children.append(child)
            except psutil.NoSuchProcess:pass
        if p.poll() is not None and live_children and not reason:reason='orphaned_children'
        if p.poll() is None or reason:
            for child in reversed(list(known.values())):
                try:
                    if child.is_running():child.terminate()
                except psutil.NoSuchProcess:pass
            _,alive=psutil.wait_procs(list(known.values()),timeout=5)
            for child in alive:
                try:child.kill()
                except psutil.NoSuchProcess:pass
            psutil.wait_procs(alive,timeout=5)
        try:code=p.wait(timeout=10)
        except psutil.TimeoutExpired:code=None
        except subprocess.TimeoutExpired:code=None
        remaining=[]
        for child in known.values():
            try:
                if child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:remaining.append(child.pid)
            except psutil.NoSuchProcess:pass
        evidence.update(finished=time.time(),exit_code=code,failure_code=reason,
                        remaining_processes=remaining,status='termination_pending' if remaining else 'succeeded' if code==0 and not reason else 'failed')
        save(receipt,evidence)
    return 0 if evidence['status']=='succeeded' else (code or 3)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--receipt',required=True);parser.add_argument('--private-gb',type=float,required=True)
    parser.add_argument('--wall-minutes',type=float,default=1440);parser.add_argument('command',nargs=argparse.REMAINDER);a=parser.parse_args()
    command=a.command[1:] if a.command[:1]==['--'] else a.command
    if not command or a.private_gb<=0 or a.wall_minutes<=0:parser.error('Command and positive resource limits required')
    raise SystemExit(run(command,a.receipt,a.private_gb,a.wall_minutes))
