"""SYSTEM broker: one unprivileged Windows account and task per execution.

Requests contain only a UUID; runtime/runner paths come from administrator config.
No publisher command line is evaluated with broker privileges.
"""
import argparse,json,re,subprocess,time,uuid,tempfile
import psutil
from pathlib import Path
from common import atomic_json
import release_identity

LOADED_RELEASE=release_identity.snapshot(Path(__file__).parent,'broker')


def bounded_command(argv,timeout=90,log_dir=None):
    """Wait for the command, never for EOF held by an orphaned descendant."""
    directory=Path(log_dir or Path(tempfile.gettempdir())/'labcompute-broker-commands')
    directory.mkdir(parents=True,exist_ok=True);key=uuid.uuid4().hex
    stdout=directory/(key+'.out');stderr=directory/(key+'.err')
    with stdout.open('wb') as out,stderr.open('wb') as err:
        process=subprocess.Popen(argv,stdout=out,stderr=err)
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:descendants=psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:descendants=[]
            for child in reversed(descendants):
                try:child.kill()
                except psutil.NoSuchProcess:pass
            process.kill();process.wait(timeout=5)
            _,alive=psutil.wait_procs(descendants,timeout=5)
            raise TimeoutError('Platform command exceeded deadline; logs retained: '+key+
                               ('; descendants still exiting' if alive else ''))
    with stdout.open('rb')as stream:output=stream.read(8*1024**2).decode('utf-8',errors='replace')
    with stderr.open('rb')as stream:error=stream.read(8*1024**2).decode('utf-8',errors='replace')
    if process.returncode:raise RuntimeError(error[-1000:])
    for path in (stdout,stderr):
        try:path.unlink()
        except PermissionError:pass
    return output


def powershell(script):
    import base64
    command='[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference="Stop"; '+script
    if __import__('os').name=='nt':
        atomic_json(Path('C:/LabComputeCenter/broker/health.json'),dict(at=time.time(),
            status='busy',phase='platform_command',loaded_release=release_identity.public(LOADED_RELEASE)))
    return bounded_command(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',base64.b64encode(command.encode('utf-16le')).decode()])


def cleanup_profiles(root):
    """Retired per-job Windows profiles are disposable; keep outputs in jobs/."""
    spool=Path(root)/'broker';state=spool/'profile-cleanup.json'
    if state.exists():
        previous=json.loads(state.read_text(encoding='utf-8-sig'))
        if time.time()-previous.get('at',0)<60:return
    names=[]
    for path in (spool/'receipts').glob('*.json'):
        if re.fullmatch('[a-f0-9]{32}',path.stem) and json.loads(path.read_text(encoding='utf-8-sig')).get('status')=='retired':names.append('LCJ'+path.stem[:17])
    if not names:return
    quoted=','.join("'"+name+"'" for name in names)
    script="$names=@("+quoted+"); $removed=@(); $pending=@(); foreach($p in @(Get-CimInstance Win32_UserProfile)){if((Split-Path $p.LocalPath -Leaf) -notin $names){continue}; try {if($p.Loaded){$ErrorActionPreference='Continue'; & reg.exe unload ('HKU/'+$p.SID+'_Classes') 2>$null | Out-Null; & reg.exe unload ('HKU/'+$p.SID) 2>$null | Out-Null; $ErrorActionPreference='Stop'}; $fresh=Get-CimInstance Win32_UserProfile | Where-Object {$_.SID -eq $p.SID}; if($fresh){Remove-CimInstance -InputObject $fresh -ErrorAction Stop};$removed+=$p.LocalPath} catch {$pending+=@{path=$p.LocalPath;reason=$_.Exception.Message}}}; @{removed=$removed;pending=$pending} | ConvertTo-Json -Compress"
    # Registry paths use Windows separators, never publisher-controlled text.
    script=script.replace('HKU/','HKU\\')
    try:result=json.loads(powershell(script));atomic_json(state,dict(at=time.time(),**result))
    except Exception as e:atomic_json(state,dict(at=time.time(),error=str(e)[:1000]))


def once(root):
    root=Path(root);spool=root/'broker';cfg=json.loads((root/'config/agent.json').read_text(encoding='utf-8-sig'))
    for request in sorted((spool/'requests').glob('*.json')):
        jid=request.stem
        if not re.fullmatch('[a-f0-9]{32}',jid):continue
        receipt=spool/'receipts'/(jid+'.json');folder=root/'jobs'/jid
        try:
            prior=json.loads(receipt.read_text(encoding='utf-8-sig')) if receipt.exists() else {}
            if prior.get('status') in ('launched','retired','error'):continue
            launch=json.loads((folder/'launch.json').read_text(encoding='utf-8-sig'))
            if not launch['spec'].get('requires_isolation'):raise ValueError('Isolation required')
            if (folder/'cancel').exists():
                atomic_json(folder/'exit.json',dict(status='cancelled',exit_code=-1,detail='用户取消',cleanup_verified=True));atomic_json(receipt,dict(status='retired'));continue
            name='LCJ'+jid[:17];task='LabCompute-Job-'+jid
            # Use the center interpreter, never a publisher-selected executable.
            script=r'''$r='C:/LabComputeCenter'; $jid='__ID__'; $name='__NAME__'; $task='__TASK__';
if(Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue){throw 'Existing execution task requires reconciliation'}
if(Get-LocalUser -Name $name -ErrorAction SilentlyContinue){throw 'Existing execution account requires reconciliation'}
$pwd=[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(36))+'aA1!';
$secure=ConvertTo-SecureString $pwd -AsPlainText -Force;
New-LocalUser -Name $name -Password $secure -AccountNeverExpires -UserMayNotChangePassword | Out-Null;
Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $name;
$who="$env:COMPUTERNAME\$name";
& "$r/app/deploy/grant_batch_logon.ps1" -Account $who;
& icacls $r /grant "${who}:(RX)" | Out-Null;
foreach($dir in @('app','venv','runtimes','wheels')){if(!(Test-Path "$r/$dir")){continue}; & icacls "$r/$dir" /grant "${who}:(OI)(CI)RX" /C | Out-Null; if($LASTEXITCODE -ne 0){throw "Runtime ACL failed: $dir"}}
& icacls "$r/jobs" /grant "${who}:(RX)" | Out-Null;
& icacls "$r/jobs/$jid" /grant "${who}:(OI)(CI)M" | Out-Null;if($LASTEXITCODE -ne 0){throw 'Job ACL failed'}
$base=Get-Content "$r/venv/pyvenv.cfg" | Where-Object {$_ -match '^home\s*='};$base=($base -split '=',2)[1].Trim();
& icacls $base /grant "${who}:(OI)(CI)RX" /C | Out-Null;if($LASTEXITCODE -ne 0){throw 'Base Python ACL failed'}
$action=New-ScheduledTaskAction -Execute "$r/venv/Scripts/python.exe" -Argument ('"'+$r+'/app/runner.py" "'+$r+'/jobs/'+$jid+'"');
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;
Register-ScheduledTask -TaskName $task -Action $action -Settings $settings -User $who -Password $pwd -RunLevel Limited | Out-Null;
Start-ScheduledTask -TaskName $task;
'''.replace('__ID__',jid).replace('__NAME__',name).replace('__TASK__',task)
            # Windows PowerShell/.NET Framework has no static GetBytes(count).
            script=script.replace('[Security.Cryptography.RandomNumberGenerator]::GetBytes(36)',"$( $bytes=New-Object byte[] 36; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); $bytes )")
            powershell(script)
            atomic_json(receipt,dict(status='launched',account=name,task=task,at=time.time()))
        except Exception as e:
            # A timed-out launch may already have created its scheduled task.
            # The ordinary reconciliation below must establish process cleanup.
            atomic_json(receipt,dict(status='error',detail=str(e)[:1000],at=time.time()))
    # The privileged broker confirms task exit and clears only this job account.
    for receipt in (spool/'receipts').glob('*.json'):
        d=json.loads(receipt.read_text(encoding='utf-8-sig'));jid=receipt.stem
        if d.get('status') not in ('launched','error'):continue
        name='LCJ'+jid[:17];task='LabCompute-Job-'+jid
        state=powershell("$t=Get-ScheduledTask -TaskName '"+task+"' -ErrorAction SilentlyContinue; if($t){$t.State}else{'Missing'}").strip()
        folder=root/'jobs'/jid;exitfile=folder/'exit.json'
        request=json.loads((spool/'requests'/(jid+'.json')).read_text(encoding='utf-8-sig'))
        if request.get('cancel') is True:
            (folder/'cancel').touch()
            if state=='Running':
                powershell("Stop-ScheduledTask -TaskName '"+task+"'")
                continue
        elif state=='Running':continue
        if not exitfile.exists() and time.time()-d.get('at',0)<120 and request.get('cancel') is not True:continue
        # Job Object kills children on supervisor exit; independently verify the
        # disposable account has no surviving processes before acknowledging.
        owned=[]
        for process in psutil.process_iter():
            try:
                if process.username().split('\\')[-1].lower()==name.lower():owned.append(process)
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        for process in owned:
            try:process.kill()
            except psutil.NoSuchProcess:pass
        _,alive=psutil.wait_procs(owned,timeout=5)
        if alive:continue
        if exitfile.exists():result=json.loads(exitfile.read_text(encoding='utf-8-sig'))
        else:result=dict(status='cancelled' if (folder/'cancel').exists() else 'failed',exit_code=-1,detail='用户取消' if (folder/'cancel').exists() else '执行意外中断，中心保留已有输出' if (folder/'execution.json').exists() else '独立执行环境未能启动，中心保留任务与诊断',failure_code='runner_interrupted' if (folder/'execution.json').exists() else 'isolation_setup')
        if result['status']=='cancelled':result.pop('failure_code',None)
        result['cleanup_verified']=True;atomic_json(exitfile,result)
        powershell("$r='C:/LabComputeCenter'; $t=Get-ScheduledTask -TaskName '"+task+"' -ErrorAction SilentlyContinue; if($t){Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false}; $base=Get-Content \"$r/venv/pyvenv.cfg\" | Where-Object {$_ -match '^home\\s*='};$base=($base -split '=',2)[1].Trim(); foreach($dir in @($r,$base,\"$r/app\",\"$r/venv\",\"$r/runtimes\",\"$r/wheels\",\"$r/jobs\",\"$r/jobs/"+jid+"\")){if(Test-Path $dir){& icacls $dir /remove:g '"+name+"' /C | Out-Null}}; Remove-LocalUser -Name '"+name+"' -ErrorAction SilentlyContinue")
        atomic_json(receipt,dict(d,status='retired',retired_at=time.time()))
    cleanup_profiles(root)
    atomic_json(spool/'health.json',dict(at=time.time(),status='ready',loaded_release=release_identity.public(LOADED_RELEASE)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default='C:/LabComputeCenter');a=p.parse_args()
    while True:
        try:once(a.root)
        except Exception as e:
            atomic_json(Path(a.root)/'broker/health.json',dict(at=time.time(),status='error',detail=str(e)[:1500],loaded_release=release_identity.public(LOADED_RELEASE)))
        time.sleep(3)
