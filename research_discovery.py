"""Recognize scientific job identities inside administrator-approved roots."""
import re
from pathlib import PurePosixPath


def normal(value):return str(value).replace('\\','/').casefold()


def task_from_args(args, roots):
    def value(flag,default=None):
        return args[args.index(flag)+1] if flag in args and args.index(flag)+1<len(args) else default
    for raw in args:
        path=str(raw).replace('\\','/');name=PurePosixPath(path).name
        if not any(normal(path).startswith(normal(r).rstrip('/')+'/') for r in roots):continue
        if re.fullmatch(r'full_window_network_fixed_active_v\d+(?:_[a-zA-Z0-9_]+)?\.py',name):
            env=value('--environment','H01_L3_R04');root=str(PurePosixPath(path).parent.parent)
            folder=root+('/full_window_network_fixed_active_v3' if env=='H01_L3_R04' else '/environment_networks_fixed_active_v3/'+env)
            try:duration=float(value('--duration-ms','600'))
            except ValueError:return None
            return dict(kind='network',script=path,root=root,key=folder+'/progress.json',name=env+' · '+value('--condition','strong_bursts'),environment=env,condition=value('--condition','strong_bursts'),duration=duration)
        if name in ('windows_observed_batch_runner.py','windows_inferred_batch_runner.py') and value('--batch-name'):
            batch=value('--batch-name')
            if '/' in batch or '\\' in batch or batch in ('.','..'):return None
            folder=str(PurePosixPath(path).parent)
            return dict(kind='batch',script=path,root=folder,key=folder+'/'+batch+'_runner_state.json',name=batch)
    return None
