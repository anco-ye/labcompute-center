$ErrorActionPreference='Stop'
$root='C:\LabComputeCenter'
$action=New-ScheduledTaskAction -Execute "$root\venv\Scripts\python.exe" -Argument ('"'+$root+'\app\health_watchdog.py"') -WorkingDirectory $root
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$boot=New-ScheduledTaskTrigger -AtStartup
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'LabCompute-HealthWatchdog' -Action $action -Principal $principal -Settings $settings -Trigger @($boot,$repeat) -Force | Out-Null
Start-ScheduledTask -TaskName 'LabCompute-HealthWatchdog'
