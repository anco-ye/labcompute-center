param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference='Stop'
$c=Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$root='C:\LabComputeCenter'
if(Get-LocalUser -Name LabCompute -ErrorAction SilentlyContinue){throw 'LabCompute already exists; refusing to reset its password'}
New-Item -ItemType Directory -Force $root,"$root\app","$root\config","$root\data","$root\jobs","$root\logs","$root\tls" | Out-Null
Expand-Archive -Path "$PSScriptRoot\app.zip" -DestinationPath "$root\app" -Force
Copy-Item "$PSScriptRoot\ca.crt" "$root\tls\ca.crt"
Copy-Item "$PSScriptRoot\agent.json" "$root\config\agent.json"
& $c.python -m venv "$root\venv"
if($LASTEXITCODE -ne 0){throw 'venv failed'}
& "$root\venv\Scripts\python.exe" -m pip install --no-index --find-links "$PSScriptRoot\wheels" -r "$root\app\requirements.txt"
if($LASTEXITCODE -ne 0){throw 'dependency install failed'}
$secure=ConvertTo-SecureString $c.account_password -AsPlainText -Force
New-LocalUser -Name LabCompute -Password $secure -Description 'Lab compute center background tasks' -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
$users=(Get-LocalGroup -SID 'S-1-5-32-545').Name
Add-LocalGroupMember -Group $users -Member LabCompute
$performance=(Get-LocalGroup -SID 'S-1-5-32-558').Name
Add-LocalGroupMember -Group $performance -Member LabCompute
$account="$env:COMPUTERNAME\LabCompute"
& icacls (Split-Path $c.python) /grant "${account}:(OI)(CI)RX" | Out-Null
& icacls $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${account}:(OI)(CI)RX" | Out-Null
foreach($dir in @('data','jobs','logs')){& icacls "$root\$dir" /grant "${account}:(OI)(CI)M" | Out-Null}
if($c.controller){
 Copy-Item "$PSScriptRoot\server.json" "$root\config\server.json"
 Copy-Item "$PSScriptRoot\server.crt","$PSScriptRoot\server.key" "$root\tls"
}
function Install-Task($name,$role){
 $launcher="$root\app\start-$role.ps1"
 $line='$env:PYTHONUTF8="1"'+"`r`n"+'& "C:\LabComputeCenter\venv\Scripts\python.exe" "C:\LabComputeCenter\app\'+$role+'.py" --config "C:\LabComputeCenter\config\'+$role+'.json" >> "C:\LabComputeCenter\logs\'+$role+'.log" 2>&1'+"`r`n"+'exit $LASTEXITCODE'
 Set-Content $launcher $line -Encoding UTF8
 $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$launcher+'"')
 $trigger=New-ScheduledTaskTrigger -AtStartup
 $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
 Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -User $account -Password $c.account_password -RunLevel Limited -Force | Out-Null
 Start-ScheduledTask -TaskName $name
}
if($c.controller){Install-Task 'LabCompute-Center' 'server'
 New-NetFirewallRule -Name 'LabCompute-HTTPS-LAN' -DisplayName 'Lab compute HTTPS LAN' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8443 -RemoteAddress LocalSubnet -Profile Any | Out-Null
}
& "$PSScriptRoot\prepare_runtimes.ps1"
& "$root\venv\Scripts\python.exe" "$root\app\deploy\publish_runtimes.py"
if($LASTEXITCODE -ne 0){throw 'runtime publish failed'}
& "$PSScriptRoot\grant_batch.ps1"
& "$PSScriptRoot\install_execution_broker.ps1"
Install-Task 'LabCompute-Agent' 'agent'
Remove-Item $ConfigPath
'INSTALL_OK'
