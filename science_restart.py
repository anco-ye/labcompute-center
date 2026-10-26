"""Fixed restart recipe, run by a scheduled task under the original user."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import psutil

from common import atomic_json
from science_control import matched


def restart(config,key):
    cfg=json.loads(Path(config).read_text(encoding='utf-8-sig'));recipe=cfg['recipes'][key]
    status=Path(cfg['spool'])/'restart-status'/(recipe['restart_task']+'.json')
    try:
        own_chain=[os.getpid()]+[p.pid for p in psutil.Process().parents()]
        if matched(recipe,own_chain):raise RuntimeError('原调度进程仍在运行，拒绝重复启动')
        stamp=time.strftime('%Y%m%d-%H%M%S')
        backup=Path(cfg['backup_root'])/recipe['restart_task']/stamp
        backup.mkdir(parents=True,exist_ok=False)
        sources=[Path(p) for p in recipe.get('preserve_paths',[]) if Path(p).exists()]
        size=sum(p.stat().st_size for source in sources for p in ([source] if source.is_file() else source.rglob('*')) if p.is_file() and not p.is_symlink())
        if shutil.disk_usage(backup).free<size*1.1+2*1024**3:raise RuntimeError('保留旧结果所需磁盘空间不足；尚未启动新计算')
        atomic_json(status,dict(at=time.time(),status='preparing',detail='备份已有结果',backup=str(backup)))
        for i,source in enumerate(sources):
            target=backup/(str(i)+'_'+source.name)
            if source.is_dir():shutil.copytree(source,target,symlinks=True)
            else:shutil.copy2(source,target)
        atomic_json(backup/'sources.json',dict(paths=[str(p) for p in sources],argv=recipe['argv'],cwd=recipe['cwd']))
        # The registered task is configured IgnoreNew; recheck after copying.
        if matched(recipe,own_chain):raise RuntimeError('备份期间出现原任务进程，已保留备份，未重复启动')
        env=os.environ.copy();env.update(recipe.get('env',{}));env['PYTHONUTF8']='1'
        argv=list(recipe['argv'])
        if recipe.get('replace_literals'):
            source=Path(argv[1]);text=source.read_text(encoding='utf-8-sig')
            for old,new in recipe['replace_literals'].items():
                if text.count(old)!=1:raise RuntimeError('续跑入口已变化，需要重新核验备份文件命名')
                text=text.replace(old,new.replace('{stamp}',stamp))
            copy=backup/'resume_entry.py'
            copy.write_text('import sys\nsys.path.insert(0,'+repr(str(source.parent))+')\n'+text,encoding='utf-8')
            argv[1]=str(copy)
        with (backup/'restart.log').open('ab',buffering=0) as log:
            proc=subprocess.Popen(argv,cwd=recipe['cwd'],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            atomic_json(status,dict(at=time.time(),status='running',detail='原调度脚本已启动',pid=proc.pid,backup=str(backup)))
            code=proc.wait()
        atomic_json(status,dict(at=time.time(),status='finished' if code==0 else 'failed',detail=f'原调度脚本退出码 {code}；日志和旧结果位于 {backup}',backup=str(backup)))
    except Exception as exc:
        atomic_json(status,dict(at=time.time(),status='failed',detail=str(exc)[:1500]))
        raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--key',required=True);a=p.parse_args()
    restart(a.config,a.key)
