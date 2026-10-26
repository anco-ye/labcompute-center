"""Install directory provisioning without stopping the worker or any solver."""
import argparse,json,shutil,subprocess,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
from common import atomic_json
from execution_broker import powershell


def install(root,drives):
    root=Path(root);path=root/'config/agent.json';cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    # PowerShell never parses or prints the credential-bearing configuration.
    sid=powershell("$u=(Get-ScheduledTask -TaskName 'LabCompute-Agent').Principal.UserId;if($u -like 'S-1-*'){$u}else{(New-Object System.Security.Principal.NTAccount($u)).Translate([System.Security.Principal.SecurityIdentifier]).Value}").strip()
    if not sid.startswith('S-1-'):raise ValueError('Worker principal SID unavailable')
    roots=[]
    for drive in drives:
        if len(drive)!=1 or not 'D'<=drive<='Z':raise ValueError('Expected fixed drive letter')
        volume=Path(drive+':/')
        if not volume.is_dir():continue
        usage=psutil.disk_usage(str(volume))
        if usage.total<100*2**30 or usage.free<20*2**30:continue
        base=volume/'LabComputeWork';jobs=base/'jobs';marker=base/'.labcompute-root.json'
        if base.exists() and not marker.exists() and any(base.iterdir()):raise ValueError('Refusing unrelated existing directory')
        jobs.mkdir(parents=True,exist_ok=True)
        for p in (base,jobs):
            subprocess.run(['icacls',str(p),'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(RX)','*'+sid+':(OI)(CI)F'],check=True,capture_output=True)
        atomic_json(marker,dict(owner='LabComputeCenter',worker_sid=sid));roots.append(str(jobs))
    backup=root/'backups'/('storage-config-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir();shutil.copy2(path,backup/'agent.json')
    cfg['work_roots']=roots;atomic_json(path,cfg)
    powershell(r'''$a=New-ScheduledTaskAction -Execute 'C:/LabComputeCenter/venv/Scripts/python.exe' -Argument '"C:/LabComputeCenter/app/storage_daemon.py" --config "C:/LabComputeCenter/config/agent.json"';$t=New-ScheduledTaskTrigger -AtStartup;$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);Register-ScheduledTask -TaskName 'LabCompute-StorageDirectory' -Action $a -Trigger $t -Settings $s -User SYSTEM -RunLevel Highest -Force | Out-Null;Start-ScheduledTask -TaskName 'LabCompute-StorageDirectory' ''')
    return dict(roots=roots,backup=str(backup))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default='C:/LabComputeCenter');p.add_argument('--drives',nargs='+',default=['D','E']);a=p.parse_args();print(json.dumps(install(a.root,a.drives)))
