param([Parameter(Mandatory=$true)][string]$Account)
$ErrorActionPreference='Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class CenterBatchLogon {
 [StructLayout(LayoutKind.Sequential)] struct OA { public int Length; public IntPtr Root,Name; public uint Attributes; public IntPtr Security,Qos; }
 [StructLayout(LayoutKind.Sequential)] struct US { public ushort Length,MaximumLength; public IntPtr Buffer; }
 [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system,ref OA attrs,uint access,out IntPtr handle);
 [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr policy,IntPtr sid,US[] rights,uint count);
 [DllImport("advapi32.dll")] static extern uint LsaNtStatusToWinError(uint status);
 [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr handle);
 public static void Grant(string account) {
  var sid=(SecurityIdentifier)new NTAccount(account).Translate(typeof(SecurityIdentifier));
  var bytes=new byte[sid.BinaryLength];sid.GetBinaryForm(bytes,0);
  IntPtr ptr=Marshal.AllocHGlobal(bytes.Length),text=Marshal.StringToHGlobalUni("SeBatchLogonRight"),policy=IntPtr.Zero;
  try {
   Marshal.Copy(bytes,0,ptr,bytes.Length);var oa=new OA();oa.Length=Marshal.SizeOf(typeof(OA));
   uint status=LsaOpenPolicy(IntPtr.Zero,ref oa,0x810,out policy);
   if(status!=0)throw new System.ComponentModel.Win32Exception((int)LsaNtStatusToWinError(status));
   var rights=new US[]{new US{Length=34,MaximumLength=36,Buffer=text}};
   status=LsaAddAccountRights(policy,ptr,rights,1);
   if(status!=0)throw new System.ComponentModel.Win32Exception((int)LsaNtStatusToWinError(status));
  } finally {if(policy!=IntPtr.Zero)LsaClose(policy);Marshal.FreeHGlobal(ptr);Marshal.FreeHGlobal(text);}
 }
}
'@
[CenterBatchLogon]::Grant($Account)
# The broker loads this hook before it starts a new execution. Both the NTFS
# junction and its physical destination need this execution account's ACL.
if($Account -match '\\LCJ([a-f0-9]{17})$'){
 $prefix=$Matches[1]
 $sid=([System.Security.Principal.NTAccount]$Account).Translate([System.Security.Principal.SecurityIdentifier]).Value
 $app=Split-Path $PSScriptRoot -Parent
 $root=Split-Path $app -Parent
 & "$root/venv/Scripts/python.exe" "$app/provision_job_storage.py" --root $root --prefix $prefix --sid $sid
 if($LASTEXITCODE -ne 0){throw 'Physical work directory ACL setup failed'}
}
