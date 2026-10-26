"""Publish verified runtime templates without modifying research environments."""
import json,platform,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import atomic_json
root=Path('C:/LabComputeCenter');path=root/'config/agent.json'
cfg=json.loads(path.read_text(encoding='utf-8-sig'))
lock=(root/'runtimes/science-v1-lock.txt').read_text(encoding='utf-16').splitlines()
packages=[line.strip() for line in lock if '==' in line]
assert len(packages)>=7
runtimes=dict(cfg.get('runtimes',{}))
runtimes['python']=dict(kind='python',label='Python 基础',version=platform.python_version(),revision='python311-v1',status='ready',executable=str(root/'runtimes/python311-v1/Scripts/python.exe'),packages=[])
runtimes['python-science']=dict(kind='python',label='Python 科学计算',version=platform.python_version(),revision='science-v1',status='ready',executable=str(root/'runtimes/python311-science-v1/Scripts/python.exe'),packages=packages)
for kind,label in [('matlab','MATLAB'),('comsol','COMSOL')]:
 r=runtimes.get(kind,{})
 exists=Path(r.get('executable','__missing__')).is_file()
 r.update(kind=kind,label=label,status=('ready' if r.get('status')=='ready' else 'unverified') if exists else 'not_installed',max_parallel=1,
          detail='已检测安装，等待批处理与许可验证' if exists else '尚未安装并发布运行环境')
 runtimes[kind]=r
cfg.update(runtimes=runtimes,worker=True,max_parallel=max(1,(os.cpu_count() or 1)-1))
if cfg['id']=='win1':cfg.update(archive_dir='E:/LabComputeArchive',disk_media={'C':'SSD','D':'SSD','E':'HDD'})
elif cfg['id']=='win2':cfg.update(archive_dir='D:/LabComputeArchive',disk_media={'C':'SSD','D':'HDD'})
atomic_json(path,cfg)
print('Published',cfg['id'],list(runtimes))
