"""Local, allowlisted process control for pre-existing scientific schedulers.

No user supplied command lines are executed. Restart uses an administrator
registered scheduled task running as the original ordinary Windows user.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

import psutil
from common import atomic_json
from runner import kill_tree
from research_discovery import task_from_args


def normal(value):return str(value).replace('\\','/').casefold()


class ProcessInventory(list):
    """A list-compatible sample that distinguishes missing from unreadable PIDs."""
    def __init__(self, *args):
        super().__init__(*args)
        self.verified = True


def mark_unverified(inventory):
    if hasattr(inventory, 'verified'):inventory.verified = False


def process_inventory():
    rows=ProcessInventory()
    for p in psutil.process_iter(['pid','name','create_time']):
        candidate=False
        try:
            name=(p.info['name'] or '').lower()
            candidate=any(x in name for x in ('python','matlab','comsol','nrniv')) or name in ('cmd.exe','powershell.exe','pwsh.exe')
            if candidate:
                rows.append((p,p.cmdline()))
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied:
            if candidate:mark_unverified(rows)
    return rows


def affinity_snapshot(inventory=None):
    """Read cross-user affinity as the local service, without granting worker elevation."""
    rows={}
    for p,args in process_inventory() if inventory is None else inventory:
        try:
            if any(x in (p.info['name'] or '').lower() for x in ('python','matlab','comsol','nrniv')):
                rows[str(p.pid)]={'created':p.create_time(),'cpu_ids':p.cpu_affinity(),
                                  'managed':any(normal(root) in normal(' '.join(args)) for root in (Path(__file__).parent,Path(__file__).parent.parent/'jobs')) or (p.username().split('\\')[-1].startswith('LCJ') and len(p.username().split('\\')[-1])==20)}
        except (psutil.NoSuchProcess,psutil.AccessDenied,AttributeError):pass
    return rows


def research_processes(roots, inventory=None):
    inventory=process_inventory() if inventory is None else inventory
    rows=[]
    for p,args in inventory:
        try:
            if 'python' not in (p.info.get('name') or '').lower():continue
            if any(str(a).endswith('.py') and any(normal(a).startswith(normal(r).rstrip('/')+'/') for r in roots) for a in args):
                rows.append(dict(pid=p.pid,args=args,start=p.create_time(),ppid=p.ppid()))
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied:mark_unverified(inventory)
    return rows


def identity(p):return {'pid':p.pid,'created':p.create_time()}


def process(ref):
    try:
        p=psutil.Process(ref['pid'])
        return p if abs(p.create_time()-ref['created'])<.01 and p.is_running() and p.status()!=psutil.STATUS_ZOMBIE else None
    except psutil.NoSuchProcess:return None


def matched(recipe,exclude=(),inventory=None):
    own_inventory=inventory is None
    inventory=process_inventory() if own_inventory else inventory
    found=[]
    expected={Path(a).name.casefold() for match in recipe.get('matches',[]) for a in match if a.lower().endswith(('.py','.cmd','.ps1'))}
    for p,command in inventory:
        if p.pid in exclude:continue
        try:
            name=(p.info['name'] or '').lower()
            if not (name.startswith('python') or name in ('cmd.exe','powershell.exe','pwsh.exe')):continue
            args=[normal(a) for a in command]
            relative=[a for a in command if Path(a).name.casefold() in expected and not Path(a).is_absolute()]
            if relative:args += [normal(Path(p.cwd())/a) for a in relative]
            if any(all(normal(a) in args for a in match) for match in recipe.get('matches',[])) and p.is_running():
                found.append(p)
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied:mark_unverified(inventory)
    if own_inventory and not getattr(inventory,'verified',True):
        raise RuntimeError('进程监测不可用，无法确认完整进程状态；未执行操作，请恢复监测后重试。')
    return found


def generation(processes):
    refs=[]
    for p in processes:
        try:refs.append((p.pid,p.create_time()))
        except psutil.NoSuchProcess:pass
    refs.sort()
    return hashlib.sha256(json.dumps(refs).encode()).hexdigest()[:24]


class ScienceControl:
    def __init__(self,config):
        self.config=Path(config)
        self.cfg=json.loads(self.config.read_text(encoding='utf-8-sig'))
        self.root=Path(self.cfg['spool']);self.root.mkdir(parents=True,exist_ok=True)
        self.statefile=self.root/'state.json'
        try:self.state=json.loads(self.statefile.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={}
        self.discoveryfile=self.root/'discovered-recipes.json'
        try:self.discovered=json.loads(self.discoveryfile.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.discovered={}
        self.activity_samples={}

    def activity(self,key,current,verified=True,now=None):
        """CPU activity is evidence of execution, never proof of scientific progress."""
        now=time.monotonic() if now is None else now
        complete=verified
        candidates=list(current)
        child_pids=set()
        for p in current:
            try:
                children=p.children(recursive=True)
                candidates.extend(children)
                child_pids.update(p.pid for p in children)
            except psutil.Error:complete=False
        counters={}
        seen=set()
        for p in candidates:
            try:
                ref=(p.pid,p.create_time())
                if ref in seen:continue
                seen.add(ref)
                times=p.cpu_times()
                counters[ref]=times.user+times.system
            except psutil.Error:complete=False
        previous=self.activity_samples.get(key)
        self.activity_samples[key]=dict(at=now,counters=counters,complete=complete)
        seconds=now-previous['at'] if previous else None
        # A new PID, exit, unavailable tree or long sampling gap invalidates the
        # interval rather than turning unreadable CPU time into a false zero.
        usable=bool(complete and previous and previous['complete'] and
                    0<seconds<=15 and counters.keys()==previous['counters'].keys())
        deltas=[value-previous['counters'][ref] for ref,value in counters.items()] if usable else []
        usable=usable and all(value>=0 for value in deltas)
        return dict(sample_seconds=seconds,cpu_seconds=sum(deltas) if usable else None,
                    working_processes=sum(value>0 for value in deltas) if usable else None,
                    process_count=len(counters),child_processes=sum(pid in child_pids for pid,_ in counters),
                    verified=usable)

    def discover(self, observations):
        roots=self.cfg.get('discovery_roots',[]);by_pid={p['pid']:p for p in observations}
        for p in observations:
            task=task_from_args(p['args'],roots)
            if not task:continue
            registered_key=next((k for k in self.cfg['recipes'] if normal(k)==normal(task['key'])),None)
            if registered_key:
                task['key']=registered_key
                registered=self.cfg['recipes'][registered_key]
                if any(all(normal(a) in [normal(x) for x in p['args']] for a in match) for match in registered.get('matches',[])):
                    continue
            args=p['args'];start=args.index(next(a for a in args if normal(a)==normal(task['script'])))
            matches=[args[start:]]
            parent=by_pid.get(p.get('ppid'))
            if parent and task['kind']=='network':
                # Only a dedicated single-environment launcher, never a shared sequencer.
                token=task['environment'].removeprefix('H01_').replace('_','').lower()
                for i,a in enumerate(parent['args']):
                    if str(a).endswith('.py') and token in Path(a).stem.lower() and 'run_' in Path(a).stem.lower():
                        matches.append(parent['args'][i:]);break
            previous=self.discovered.get(task['key'],{})
            combined=previous.get('matches',[])+matches
            self.discovered[task['key']]=dict(name=task['name'],matches=list({tuple(m):m for m in combined}.values()),discovered=True)
        atomic_json(self.discoveryfile,self.discovered)

    def perform(self,recipe,record,request):
        current=matched(recipe)
        token=generation(current)
        if token!=request.get('generation'):raise ValueError('进程已变化，请刷新后重新操作')
        action=request['action']
        saved=[p for ref in record.get('suspended',[]) if (p:=process(ref)) is not None]
        if action=='pause':
            if not current:raise ValueError('当前没有可暂停的进程')
            frozen={p.pid:p for p in saved}
            try:
                # Suspend all schedulers before enumerating their workers.
                for p in current:
                    if p.pid not in frozen:
                        if p.status()!=psutil.STATUS_STOPPED:p.suspend()
                        frozen[p.pid]=p
                for p in current:
                    for child in p.children(recursive=True):
                        if child.pid not in frozen:
                            if child.status()!=psutil.STATUS_STOPPED:child.suspend()
                            frozen[child.pid]=child
            except Exception:
                for p in reversed(list(frozen.values())):
                    try:p.resume()
                    except psutil.NoSuchProcess:pass
                raise
            record.update(status='paused',suspended=[identity(p) for p in frozen.values()])
        elif action=='resume':
            if not saved:
                saved=[p for parent in current for p in [parent]+parent.children(recursive=True) if p.status()==psutil.STATUS_STOPPED]
            if not saved:raise ValueError('没有可继续的暂停进程；请使用重新排队')
            for p in reversed(saved):
                if p.status()==psutil.STATUS_STOPPED:p.resume()
            record.update(status='running',suspended=[])
        elif action=='cancel':
            # Stop schedulers first. Killing only workers would make schedulers
            # replace them or move to the next experiment.
            all_processes={p.pid:p for p in saved}
            for p in current:
                try:
                    if p.status()!=psutil.STATUS_STOPPED:p.suspend()
                    all_processes[p.pid]=p
                    for child in p.children(recursive=True):all_processes[child.pid]=child
                except psutil.NoSuchProcess:pass
            try:
                for task in recipe.get('original_tasks',[])+([recipe['restart_task']] if recipe.get('restart_task') else []):
                    if task in recipe.get('original_tasks',[]):
                        subprocess.run(['schtasks.exe','/Change','/TN',task,'/Disable'],check=True,timeout=30,capture_output=True)
                    subprocess.run(['schtasks.exe','/End','/TN',task],timeout=30,capture_output=True)
                for p in matched(recipe):
                    all_processes[p.pid]=p
                    for child in p.children(recursive=True):all_processes[child.pid]=child
            finally:
                for p in reversed(list(all_processes.values())):
                    try:kill_tree(p)
                    except psutil.NoSuchProcess:pass
            record.update(status='cancelled',suspended=[])
        elif action=='start':
            if current:raise ValueError('原进程仍在运行，不能重复启动')
            task=recipe.get('restart_task')
            if not task or not task.startswith('LabCompute-Science-'):raise ValueError('尚未注册可保留结果的续跑入口')
            subprocess.run(['schtasks.exe','/Run','/TN',task],check=True,timeout=30,capture_output=True)
            record.update(status='starting',suspended=[],start_requested=time.time())
        else:raise ValueError('不支持的操作')
        record['detail']={'pause':'已暂停，仍保留内存与已生成结果','resume':'已继续原进程','cancel':'已停止对应调度链；结果文件保留','start':'正在备份已有输出并启动原调度脚本'}[action]

    def once(self):
        inventory=process_inventory()
        observations=research_processes(self.cfg.get('discovery_roots',[]),inventory)
        self.discover(observations)
        recipes={**self.cfg['recipes'],**self.discovered}
        for path in sorted((self.root/'requests').glob('*.json')):
            if (self.root/'responses'/path.name).exists():continue
            try:
                request=json.loads(path.read_text(encoding='utf-8'))
                if not getattr(inventory,'verified',True):
                    raise RuntimeError('进程监测不可用，无法确认完整进程状态；未执行操作，请恢复监测后重试。')
                recipe=recipes[request['simulation']]
                record=self.state.setdefault(request['simulation'],{})
                if record.get('last_command')!=path.stem:
                    self.perform(recipe,record,request)
                    record['last_command']=path.stem
                response=dict(id=path.stem,status='succeeded',detail=record['detail'])
            except Exception as exc:
                response=dict(id=path.stem,status='failed',detail=str(exc)[:1500])
            atomic_json(self.statefile,self.state)
            atomic_json(self.root/'responses'/path.name,response)
        rows={}
        for key,recipe in recipes.items():
            record=self.state.setdefault(key,{})
            current=matched(recipe,inventory=inventory);status=record.get('status','unmanaged')
            if current:
                paused=any(p.status()==psutil.STATUS_STOPPED for p in current)
                status='paused' if paused else 'running'
                actions=['resume','cancel'] if paused else ['pause','cancel']
            else:
                if status in ('running','paused','unmanaged','unknown'):status='stopped'
                if status=='starting' and time.time()-record.get('start_requested',0)>120:status='stopped'
                actions=['start','cancel'] if recipe.get('restart_task') and status!='starting' else ['cancel'] if status=='starting' else []
            if recipe.get('restart_task') and record.get('start_requested'):
                try:
                    report=json.loads((self.root/'restart-status'/(recipe['restart_task']+'.json')).read_text(encoding='utf-8'))
                    if report['at']>=record['start_requested'] and record.get('status')!='cancelled':
                        record['detail']=report['detail']
                        if report['status']=='preparing':
                            status='starting' if current else 'stopped'
                            actions=['cancel'] if current else ['start','cancel']
                        elif report['status']=='failed' and not current:status='stopped';actions=['start','cancel']
                except (OSError,ValueError,KeyError):pass
            record['status']=status
            rows[key]=dict(status=status,actions=actions,generation=generation(current),pids=[p.pid for p in current],detail=record.get('detail',''),name=recipe.get('name',key),
                           activity=self.activity(key,current,getattr(inventory,'verified',True)))
        verified=getattr(inventory,'verified',True)
        if not verified:
            for key,row in rows.items():
                row.update(status='unknown',actions=[],detail='进程监测不可用，无法确认任务是否仍在运行。')
                row['activity'].update(verified=False,cpu_seconds=None,working_processes=None)
                self.state[key]['status']='unknown'
        atomic_json(self.statefile,self.state)
        affinities=affinity_snapshot(inventory)
        atomic_json(self.root/'snapshot.json',dict(at=time.time(),observation_verified=verified,items=rows,affinities=affinities,research_processes=observations))

    def run(self):
        while True:
            try:
                self.once()
                (self.root/'observer-error.json').unlink(missing_ok=True)
            except Exception as exc:
                import traceback
                atomic_json(self.root/'observer-error.json',dict(at=time.time(),error=type(exc).__name__,detail=str(exc)[:300],traceback=traceback.format_exc()[-2500:]))
            time.sleep(2)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    ScienceControl(args.config).run()
