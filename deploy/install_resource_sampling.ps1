param([string]$Root='C:\LabComputeCenter')
$ErrorActionPreference='Stop'
if (!(Test-Path "$Root\app\resource_sampling.py")) { throw 'resource_sampling.py is not deployed' }
if (!(Test-Path "$Root\config\agent.json")) { throw 'Agent configuration is missing' }
$action=New-ScheduledTaskAction -Execute "$Root\venv\Scripts\python.exe" -Argument "-u `"$Root\app\resource_sampling.py`" --config `"$Root\config\agent.json`""
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'LabCompute-ResourceSampler' -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName 'LabCompute-ResourceSampler'
# This observer has no task claim/launch/stop action and owns only its own lock.
