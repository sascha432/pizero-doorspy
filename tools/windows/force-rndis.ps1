# Bind Windows' inbox RNDIS network driver to the Pi's USB gadget (USB\VID_2E8A&PID_0013).
# Windows otherwise matches its CDC class to usbser and shows a useless "USB Serial Device (COMx)".
# This is what Device Manager > Update driver > Network adapters > Microsoft > Remote NDIS
# Compatible Device does. Run elevated with the Pi plugged into its USB (data) port:
#   powershell -ExecutionPolicy Bypass -File force-rndis.ps1
# The binding is per USB port; re-run after plugging into a different port.
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'force-rndis.log'
Start-Transcript -Path $log -Force | Out-Null
try {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class Rn {
  [StructLayout(LayoutKind.Sequential)]
  public struct DEVINFO { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct INSTPARAMS { public int cbSize; public int Flags; public int FlagsEx; public IntPtr hwndParent; public IntPtr MsgHandler; public IntPtr MsgCtx; public IntPtr FileQueue; public IntPtr ClassRes; public int Reserved;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string DriverPath; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct DRVINFO { public int cbSize; public int DriverType; public IntPtr Reserved;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string Description;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string MfgName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string ProviderName;
    public long DriverDate; public long DriverVersion; }
  [DllImport("setupapi.dll", SetLastError=true)] public static extern IntPtr SetupDiCreateDeviceInfoList(IntPtr g, IntPtr h);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiOpenDeviceInfoW(IntPtr s, string id, IntPtr h, int f, ref DEVINFO d);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr s, ref DEVINFO d, int p, byte[] b, int n);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiGetDeviceInstallParamsW(IntPtr s, ref DEVINFO d, ref INSTPARAMS p);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiSetDeviceInstallParamsW(IntPtr s, ref DEVINFO d, ref INSTPARAMS p);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiBuildDriverInfoList(IntPtr s, ref DEVINFO d, int t);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiEnumDriverInfoW(IntPtr s, ref DEVINFO d, int t, int i, ref DRVINFO r);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
  [DllImport("newdev.dll", SetLastError=true)] public static extern bool DiInstallDevice(IntPtr h, IntPtr s, ref DEVINFO d, ref DRVINFO r, int f, out bool reboot);
  static void Chk(bool ok, string what) { if (!ok) throw new Exception(what + " failed, Win32 error " + Marshal.GetLastWin32Error()); }
  static IntPtr Open(string id, out DEVINFO d) {
    IntPtr s = SetupDiCreateDeviceInfoList(IntPtr.Zero, IntPtr.Zero);
    d = new DEVINFO(); d.cbSize = Marshal.SizeOf(typeof(DEVINFO));
    Chk(SetupDiOpenDeviceInfoW(s, id, IntPtr.Zero, 0, ref d), "OpenDeviceInfo");
    return s;
  }
  public static void SetClass(string id, string guid) {
    DEVINFO d; IntPtr s = Open(id, out d);
    byte[] b = System.Text.Encoding.Unicode.GetBytes(guid + "\0");
    Chk(SetupDiSetDeviceRegistryPropertyW(s, ref d, 8, b, b.Length), "SetClassGuid");
    SetupDiDestroyDeviceInfoList(s);
  }
  public static string Install(string id, string inf, string match) {
    DEVINFO d; IntPtr s = Open(id, out d);
    INSTPARAMS p = new INSTPARAMS(); p.cbSize = Marshal.SizeOf(typeof(INSTPARAMS));
    Chk(SetupDiGetDeviceInstallParamsW(s, ref d, ref p), "GetInstallParams");
    p.Flags |= 0x00010000;      // DI_ENUMSINGLEINF
    p.FlagsEx |= 0x00000800;    // DI_FLAGSEX_ALLOWEXCLUDEDDRVS
    p.DriverPath = inf;
    Chk(SetupDiSetDeviceInstallParamsW(s, ref d, ref p), "SetInstallParams");
    Chk(SetupDiBuildDriverInfoList(s, ref d, 1), "BuildDriverInfoList");
    DRVINFO r = new DRVINFO(); r.cbSize = Marshal.SizeOf(typeof(DRVINFO));
    for (int i = 0; SetupDiEnumDriverInfoW(s, ref d, 1, i, ref r); i++) {
      if (r.Description.IndexOf(match, StringComparison.OrdinalIgnoreCase) >= 0) {
        bool reboot;
        Chk(DiInstallDevice(IntPtr.Zero, s, ref d, ref r, 0, out reboot), "DiInstallDevice");
        SetupDiDestroyDeviceInfoList(s);
        return "Installed '" + r.Description + "' reboot=" + reboot;
      }
    }
    throw new Exception("No driver matching '" + match + "' in " + inf);
  }
}
'@
$dev = Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_2E8A&PID_0013' | Select-Object -First 1
if (-not $dev) { throw 'Pi USB gadget not found - is it plugged in?' }
"Device: $($dev.InstanceId) class=$($dev.Class)"
$net   = '{4d36e972-e325-11ce-bfc1-08002be10318}'
$ports = '{4d36e978-e325-11ce-bfc1-08002be10318}'
[Rn]::SetClass($dev.InstanceId, $net)
try {
  [Rn]::Install($dev.InstanceId, "$env:windir\INF\rndiscmp.inf", 'Remote NDIS')
} catch {
  [Rn]::SetClass($dev.InstanceId, $ports)
  throw
}
'RESULT: OK'
} catch {
  "RESULT: FAIL $($_.Exception.Message)"
} finally { Stop-Transcript | Out-Null }
