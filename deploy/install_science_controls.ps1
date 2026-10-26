param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference='Stop'
throw 'Retired: external scientific controllers and automatic scientific restarts are prohibited. Submit work through the center queue.'
$root='C:\LabComputeCenter'
$cfg=Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$account="$env:COMPUTERNAME\LabCompute"
New-Item -ItemType Directory -Force $cfg.spool,"$($cfg.spool)\requests","$($cfg.spool)\responses","$($cfg.spool)\restart-status" | Out-Null
if($cfg.backup_root){New-Item -ItemType Directory -Force $cfg.backup_root | Out-Null}
Copy-Item $ConfigPath "$root\config\science-controls.json" -Force
& icacls "$($cfg.spool)\requests" /grant "${account}:(OI)(CI)M" | Out-Null
if($LASTEXITCODE -ne 0){throw 'Cannot grant control request access'}
& icacls "$($cfg.spool)\responses" /grant "${account}:(OI)(CI)M" | Out-Null
if($LASTEXITCODE -ne 0){throw 'Cannot grant control response access'}
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
foreach($property in $cfg.recipes.PSObject.Properties){
 $recipe=$property.Value
 if(!$recipe.restart_task){continue}
 if($recipe.restart_task -notmatch '^LabCompute-Science-[a-zA-Z0-9_-]+$'){throw 'Invalid registered task name'}
 $owner=$recipe.owner
 & icacls $root /grant "${owner}:(OI)(CI)RX" | Out-Null
 & icacls "$($cfg.spool)\restart-status" /grant "${owner}:(OI)(CI)M" | Out-Null
 & icacls $cfg.backup_root /grant "${owner}:(OI)(CI)M" | Out-Null
 # Restricted ordinary token; scientific code never runs as SYSTEM.
 $principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType S4U -RunLevel Limited
 $arguments='"'+$root+'\app\science_restart.py" --config "'+$root+'\config\science-controls.json" --key "'+$property.Name+'"'
 $action=New-ScheduledTaskAction -Execute "$root\venv\Scripts\python.exe" -Argument $arguments -WorkingDirectory $root
 Register-ScheduledTask -TaskName $recipe.restart_task -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}
$launcher="$root\app\start-science-controls.ps1"
$line='$env:PYTHONUTF8="1"'+"`r`n"+'& "C:\LabComputeCenter\venv\Scripts\python.exe" "C:\LabComputeCenter\app\science_control.py" --config "C:\LabComputeCenter\config\science-controls.json" >> "C:\LabComputeCenter\logs\science-controls.log" 2>&1'
Set-Content $launcher $line -Encoding UTF8
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "'+$launcher+'"')
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$trigger=New-ScheduledTaskTrigger -AtStartup
$serviceSettings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'LabCompute-ScienceControl' -Action $action -Principal $principal -Trigger $trigger -Settings $serviceSettings -Force | Out-Null
Start-ScheduledTask -TaskName 'LabCompute-ScienceControl'
& "$root\app\deploy\install_health_watchdog.ps1"
'SCIENCE_CONTROL_INSTALLED'
