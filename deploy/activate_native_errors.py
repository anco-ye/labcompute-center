"""Activate Web independently; only an active legacy upload writer blocks it."""
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import time
import urllib.request
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import release_identity
from artifact_spool import idle_reader,BusyFile


def blockers(root):
    root=Path(root)
    for path in (root/'data/results').glob('*.upload'):
        if path.name.endswith('.resumable.upload'):continue
        try:
            with idle_reader(path):pass
        except FileNotFoundError:continue
        except (BusyFile,PermissionError):return ['legacy result upload writer active']
    # Compute and bridges are independent owners; resumable transfers retain
    # their durable offset and retry an interrupted bounded request.
    return []


def main():
    import msvcrt
    import psutil
    root=Path('C:/LabComputeCenter');work=root/'maintenance/native-errors-20261023'
    with (work/'activation.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return
        receipt=work/'activation.json'
        target=release_identity.snapshot(root/'app','web')['digest']
        if receipt.exists():
            previous=json.loads(receipt.read_text())
            if previous.get('web_digest')==target:
                subprocess.run(['powershell','-NoProfile','-Command',"Unregister-ScheduledTask -TaskName LabCompute-NativeErrorActivation -Confirm:$false"],capture_output=True)
                return
        reasons=blockers(root)
        if reasons:
            (work/'activation-pending.json').write_text(json.dumps(dict(at=time.time(),blockers=reasons)))
            return
        def roles():
            out=[]
            for proc in psutil.process_iter(['cmdline','create_time']):
                try:
                    for arg in proc.info['cmdline'] or []:
                        role=str(arg).replace('\\','/').lower()
                        if role in ('c:/labcomputecenter/app/server.py','c:/labcomputecenter/app/external/ssh_bridge.py','c:/labcomputecenter/app/agent.py','c:/labcomputecenter/app/execution_broker.py'):
                            out.append(dict(pid=proc.pid,created=proc.create_time(),role=role));break
                except psutil.Error:pass
            return out
        before=roles();web=[psutil.Process(p['pid']) for p in before if p['role'].endswith('/server.py')]
        # Only the Web is replaced. All node workers and bridge lease owners survive.
        for proc in web:
            try:proc.terminate()
            except psutil.NoSuchProcess:pass
        _,alive=psutil.wait_procs(web,timeout=8)
        if alive:raise RuntimeError('Old Web still active; no duplicate listener started')
        command="Start-ScheduledTask -TaskName LabCompute-Center"
        for attempt in range(3):
            time.sleep(1)
            subprocess.run(['powershell','-NoProfile','-Command',command],check=True,capture_output=True)
            ctx=ssl.create_default_context(cafile=str(root/'tls/ca.crt'))
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx))
            for _ in range(10):
                try:
                    with opener.open('https://127.0.0.1:8443/health',timeout=2) as response:health=json.loads(response.read())
                    if health.get('loaded_release',{}).get('digest')==target:break
                except Exception:pass
                time.sleep(.5)
            else:continue
            break
        else:raise RuntimeError('Web activation not verified; preserve backup and maintenance logs')
        after=roles()
        assert all(item in after for item in before if not item['role'].endswith('/server.py'))
        receipt.write_text(json.dumps(dict(at=time.time(),web_digest=target,health=health,before=before,after=after,worker_and_bridge_identities_preserved=True),indent=2))
        subprocess.run(['powershell','-NoProfile','-Command',"if(Get-ScheduledTask -TaskName LabCompute-NativeErrorActivation -ErrorAction SilentlyContinue){Unregister-ScheduledTask -TaskName LabCompute-NativeErrorActivation -Confirm:$false}"],check=True,capture_output=True)


if __name__=='__main__':main()
