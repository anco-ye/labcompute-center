"""Activate a registered worker release and expose every blocked condition."""
import json,ssl,subprocess,time,urllib.request
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import worker_release,atomic_json

def decision(expected,actual,active,local_jobs):
    if not expected:return 'no_update'
    if expected!=actual:return 'target_mismatch'
    if active or local_jobs:return 'draining'
    return 'ready'

def main():
    root=Path('C:/LabComputeCenter')
    cfg=json.loads((root/'config/agent.json').read_text(encoding='utf-8-sig'))
    context=ssl.create_default_context(cafile=cfg['ca_file'])
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    req=urllib.request.Request(cfg['controller'].rstrip('/')+'/agent/'+cfg['id']+'/update-status',headers={'Authorization':'Bearer '+cfg['token']})
    status=json.loads(opener.open(req,timeout=20).read())
    state=Path(cfg['work_dir'])/'active_jobs.json'
    jobs=json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
    actual=worker_release(root/'app')
    action=decision(status['release'],actual,status['active'],jobs)
    atomic_json(root/'maintenance/worker-update-status.json',dict(at=time.time(),
        state=action,requested_release=status['release'],disk_release=actual,
        center_active=status['active'],local_active=len(jobs)))
    if action=='no_update':
        subprocess.run(['schtasks','/Change','/TN','LabCompute-FinishUpdate','/Disable'],check=True,capture_output=True)
    elif action=='ready':
        subprocess.run(['powershell','-NoProfile','-Command','Stop-ScheduledTask -TaskName LabCompute-Agent; Start-ScheduledTask -TaskName LabCompute-Agent'],check=True,capture_output=True)

if __name__=='__main__':main()
