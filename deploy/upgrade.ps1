$ErrorActionPreference='Stop'
throw 'Retired: legacy upgrade may interrupt active center transfers. Stage files and use the guarded Web/worker activation workflow.'
$root='C:\LabComputeCenter'
function Check-Idle {
 foreach($request in Get-ChildItem "$root\science-control\requests\*.json" -ErrorAction SilentlyContinue){
  if(!(Test-Path "$root\science-control\responses\$($request.Name)")){throw 'Scientific control operation pending; defer update'}
 }
 if(Test-Path "$root\jobs\active.json"){throw 'Center task active; defer update'}
 if(Test-Path "$root\jobs\active_jobs.json"){
  $active=Get-Content "$root\jobs\active_jobs.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  if(@($active.PSObject.Properties).Count -gt 0){throw 'Center tasks active; defer update'}
 }
}
Check-Idle
if(!(Test-Path "$root\runtimes\science-v1-lock.txt")){throw 'Runtime setup unfinished'}
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$backup="$root\backups\$stamp"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Compress-Archive -Path "$root\app\*" -DestinationPath "$backup\app.zip"
Copy-Item "$root\config\agent.json" "$backup\agent.json"
if(Test-Path "$root\config\science-controls.json"){Copy-Item "$root\config\science-controls.json" "$backup\science-controls.json"}
if(Test-Path "$root\config\server.json"){
 & "$root\venv\Scripts\python.exe" -c "import sqlite3; a=sqlite3.connect('C:/LabComputeCenter/data/center.sqlite3'); b=sqlite3.connect(r'$backup\center.sqlite3'); a.backup(b); b.close(); a.close()"
 if($LASTEXITCODE -ne 0){throw 'Database backup failed'}
}
Stop-ScheduledTask -TaskName LabCompute-Agent
try {Check-Idle} catch {Start-ScheduledTask -TaskName LabCompute-Agent;throw}
$controller=Test-Path "$root\config\server.json"
$science=Get-ScheduledTask -TaskName LabCompute-ScienceControl -ErrorAction SilentlyContinue
if($science){Stop-ScheduledTask -TaskName LabCompute-ScienceControl}
if($controller){Stop-ScheduledTask -TaskName LabCompute-Center}
try {
 $performance=Get-LocalGroup -SID 'S-1-5-32-558'
 if(-not (Get-LocalGroupMember -Group $performance | Where-Object {$_.Name -eq "$env:COMPUTERNAME\LabCompute"})){
  Add-LocalGroupMember -Group $performance -Member LabCompute
 }
 Expand-Archive -Path "$env:USERPROFILE\lab-center-bootstrap\center-update.zip" -DestinationPath "$root\app" -Force
 & "$root\venv\Scripts\python.exe" "$root\app\deploy\publish_runtimes.py"
 if($LASTEXITCODE -ne 0){throw 'Publish failed'}
 $cfg=Get-Content "$root\config\agent.json" -Raw -Encoding UTF8 | ConvertFrom-Json
 if($cfg.archive_dir){
  New-Item -ItemType Directory -Path $cfg.archive_dir -Force | Out-Null
  $account="$env:COMPUTERNAME\LabCompute"
  & icacls $cfg.archive_dir /grant "${account}:(OI)(CI)M" | Out-Null
 }
 & "$root\venv\Scripts\python.exe" -m py_compile "$root\app\scheduler.py" "$root\app\server.py" "$root\app\agent.py" "$root\app\runner.py" "$root\app\python_entry.py" "$root\app\science_control.py" "$root\app\science_restart.py"
 if($LASTEXITCODE -ne 0){throw 'Compile failed'}
 & "$root\venv\Scripts\python.exe" "$root\app\deploy\apply_release.py"
 if($LASTEXITCODE -ne 0){throw 'Release configuration failed'}
 if(Test-Path "$root\config\science-controls.json"){& "$root\app\deploy\install_health_watchdog.ps1"}
} catch {
 Expand-Archive -Path "$backup\app.zip" -DestinationPath "$root\app" -Force
 Copy-Item "$backup\agent.json" "$root\config\agent.json" -Force
 throw
} finally {
 if($controller){Start-ScheduledTask -TaskName LabCompute-Center}
 Start-ScheduledTask -TaskName LabCompute-Agent
 if($science){Start-ScheduledTask -TaskName LabCompute-ScienceControl}
}
"UPDATED backup=$backup"
