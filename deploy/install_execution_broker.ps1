$ErrorActionPreference='Stop'
$r='C:/LabComputeCenter'
foreach($dir in @('broker','broker/requests','broker/receipts')) { New-Item -ItemType Directory -Force "$r/$dir" | Out-Null }
# The unprivileged agent may request a job UUID, but cannot forge broker receipts.
& icacls "$r/broker" /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' 'LabCompute:(OI)(CI)RX' | Out-Null
if($LASTEXITCODE -ne 0){throw 'Broker ACL failed'}
& icacls "$r/broker/requests" /grant 'LabCompute:(OI)(CI)M' | Out-Null
if($LASTEXITCODE -ne 0){throw 'Request ACL failed'}
$action=New-ScheduledTaskAction -Execute "$r/venv/Scripts/python.exe" -Argument ('"'+$r+'/app/execution_broker.py" --root "'+$r+'"')
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger=New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName LabCompute-ExecutionBroker -Action $action -Settings $settings -Trigger $trigger -User SYSTEM -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName LabCompute-ExecutionBroker
