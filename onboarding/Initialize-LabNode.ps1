#Requires -RunAsAdministrator
param(
 [Parameter(Mandatory=$true)][ValidateSet('win3','win4')][string]$NodeId,
 [Parameter(Mandatory=$true)][string]$PublicKey,
 [Parameter(Mandatory=$true)][string[]]$AllowedSourceAddresses,
 [string]$Win1LanAddress='192.0.2.30'
)
$ErrorActionPreference='Stop'
$ts=Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if(!(Test-Path $ts)){
 Start-Process 'https://tailscale.com/download/windows'
 throw 'Install Tailscale from the official page, log into the same tailnet as Mac/Win1/Win2, then run this script again.'
}
# Tailscale emits UTF-8; Windows PowerShell 5.1 may otherwise decode it with a legacy code page.
$previousOutputEncoding=[Console]::OutputEncoding
try {
 [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
 $tsStatus=@(& $ts status --json)
 $tsStatusExitCode=$LASTEXITCODE
} finally {
 [Console]::OutputEncoding=$previousOutputEncoding
}
if($tsStatusExitCode -ne 0){throw 'Tailscale service is not ready. Open Tailscale and finish signing in.'}
try {
 $tsState=($tsStatus -join "`n") | ConvertFrom-Json -ErrorAction Stop
} catch {
 throw 'Cannot parse Tailscale status as UTF-8 JSON. Send this error and your PowerShell version; do not paste the full status output.'
}
if($tsState.BackendState -ne 'Running'){
 & $ts up --timeout=60s
 if($LASTEXITCODE -ne 0){throw 'Finish the browser login, then rerun this script.'}
}
& $ts set "--hostname=$NodeId" --unattended=true
if($LASTEXITCODE -ne 0){throw 'Tailscale settings failed. Do not continue until the error is resolved.'}
$tsIp=(& $ts ip -4 | Select-Object -First 1).Trim()
if($LASTEXITCODE -ne 0 -or $tsIp -notmatch '^100\.') {throw 'No Tailscale IPv4 address; check login and device approval.'}

$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$reportDir=Join-Path ([Environment]::GetFolderPath('Desktop')) ("LabNode-$NodeId-$stamp")
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$cap=Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if($cap.State -ne 'Installed'){
 $installed=Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
 if($installed.RestartNeeded){throw 'OpenSSH requires a restart. Save your work, restart Windows, then rerun this script.'}
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
$key=$PublicKey
$authorized=Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
if(Test-Path $authorized){Copy-Item $authorized (Join-Path $reportDir 'administrators_authorized_keys.before')}
$existing=if(Test-Path $authorized){@(Get-Content $authorized)}else{@()}
if($existing -notcontains $key){Add-Content -Path $authorized -Value $key -Encoding ascii}
& icacls $authorized /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if($LASTEXITCODE -ne 0){throw 'Cannot apply SSH key permissions.'}
$sshd=Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
& $sshd -t
if($LASTEXITCODE -ne 0){throw 'Existing sshd_config is invalid. Keep the diagnostic message for troubleshooting.'}
$effective=& $sshd -T -C "user=$env:USERNAME,host=localhost,addr=100.79.47.77"
if($LASTEXITCODE -ne 0 -or !($effective -match 'authorizedkeysfile.*administrators_authorized_keys')){
 throw 'This account or SSH configuration uses a nonstandard authorized_keys path. Send the diagnostic message; do not overwrite sshd_config.'
}
# Narrow the standard OpenSSH rule to the existing Mac and Win1 Tailscale addresses.
$allowed=$AllowedSourceAddresses
$rule=Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if($rule){
 $rule | Get-NetFirewallAddressFilter | Select-Object RemoteAddress | ConvertTo-Json | Set-Content (Join-Path $reportDir 'ssh-firewall.before.json') -Encoding UTF8
 Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Action Allow -Profile Any -RemoteAddress $allowed
}else{
 New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'Lab SSH from Mac and Win1 via Tailscale' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -RemoteAddress $allowed -Profile Any | Out-Null
}
Restart-Service sshd
# AC power only: keep compute tasks running; display and battery settings stay as configured.
& powercfg /query SCHEME_CURRENT SUB_SLEEP | Set-Content (Join-Path $reportDir 'sleep.before.txt')
& powercfg /change standby-timeout-ac 0
if($LASTEXITCODE -ne 0){throw 'Cannot set AC sleep timeout.'}
& powercfg /change hibernate-timeout-ac 0
if($LASTEXITCODE -ne 0){throw 'Cannot set AC hibernate timeout.'}

function Test-LabPort([string]$Address,[int]$Port){
 $client=New-Object System.Net.Sockets.TcpClient
 try{
  $pending=$client.BeginConnect($Address,$Port,$null,$null)
  if(!$pending.AsyncWaitHandle.WaitOne(5000)){return $false}
  $client.EndConnect($pending);return $true
 }catch{return $false}finally{$client.Dispose()}
}
$ips=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*' -and $_.InterfaceAlias -notlike '*Tailscale*'} | Select-Object InterfaceAlias,IPAddress,PrefixLength)
$cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors)
$memory=[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)
$disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID,@{n='TotalGiB';e={[math]::Round($_.Size/1GB,1)}},@{n='FreeGiB';e={[math]::Round($_.FreeSpace/1GB,1)}})
$hostPub=Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key.pub'
$fingerprint=& (Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe') -lf $hostPub
$summary=[ordered]@{
 Node=$NodeId; Computer=$env:COMPUTERNAME; SSHUser=$env:USERNAME; Account=(& whoami);
 TailscaleIPv4=$tsIp; LAN=$ips; SSHService=(Get-Service sshd).Status.ToString();
 Win1Tailscale8443=(Test-LabPort '100.100.0.6' 8443);
 Win1LANAddress=$Win1LanAddress; Win1LAN8443=(Test-LabPort $Win1LanAddress 8443);
 CPU=$cpu; MemoryGiB=$memory; Disks=$disks; SSHHostKeyFingerprint=$fingerprint
}
$summary | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $reportDir 'node-info.json') -Encoding UTF8
$summary | ConvertTo-Json -Depth 5
Write-Host "Saved: $reportDir\node-info.json"
Write-Host 'Send node-info.json or its text to Codex. No password or private key is required.'
Write-Host 'This completes network and SSH setup. Platform worker enrollment is a separate step.'
