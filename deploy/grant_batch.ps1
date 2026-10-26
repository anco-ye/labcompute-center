$ErrorActionPreference='Stop'
Add-Type @'
using System;using System.Runtime.InteropServices;using System.Security.Principal;
public class BatchRight{
[StructLayout(LayoutKind.Sequential)]public struct OA{public uint Length;public IntPtr RootDirectory,ObjectName;public uint Attributes;public IntPtr SecurityDescriptor,SecurityQualityOfService;}
[StructLayout(LayoutKind.Sequential)]public struct US{public ushort Length,MaximumLength;public IntPtr Buffer;}
[DllImport("advapi32.dll")]static extern uint LsaOpenPolicy(IntPtr system,ref OA attrs,uint access,out IntPtr handle);
[DllImport("advapi32.dll")]static extern uint LsaAddAccountRights(IntPtr handle,byte[] sid,US[] rights,uint count);
[DllImport("advapi32.dll")]static extern uint LsaClose(IntPtr handle);
[DllImport("advapi32.dll")]static extern uint LsaNtStatusToWinError(uint status);
public static void Grant(string name){
var sid=(SecurityIdentifier)new NTAccount(name).Translate(typeof(SecurityIdentifier));byte[] bytes=new byte[sid.BinaryLength];sid.GetBinaryForm(bytes,0);
OA oa=new OA();oa.Length=(uint)Marshal.SizeOf(oa);IntPtr h;uint s=LsaOpenPolicy(IntPtr.Zero,ref oa,0x810,out h);
if(s!=0)throw new Exception("LsaOpenPolicy "+LsaNtStatusToWinError(s));
string right="SeBatchLogonRight";US u=new US();u.Buffer=Marshal.StringToHGlobalUni(right);u.Length=(ushort)(right.Length*2);u.MaximumLength=(ushort)(u.Length+2);
try{s=LsaAddAccountRights(h,bytes,new US[]{u},1);if(s!=0)throw new Exception("LsaAddAccountRights "+LsaNtStatusToWinError(s));}
finally{Marshal.FreeHGlobal(u.Buffer);LsaClose(h);}
}}
'@
[BatchRight]::Grant("$env:COMPUTERNAME\LabCompute")
Get-ScheduledTask -TaskName LabCompute* | Start-ScheduledTask
'BATCH_RIGHT_GRANTED'
