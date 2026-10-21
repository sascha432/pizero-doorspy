# Share the PC's internet connection with the Pi's USB gadget link (Windows ICS).
# ICS runs a DHCP server on 192.168.137.0/24, which keeps the Pi's usb0 link from
# dropping (NetworkManager gives up and downs it when no DHCP answers) and gives it internet.
# Run elevated:  powershell -ExecutionPolicy Bypass -File enable-ics.ps1 [-Public Wi-Fi] [-Private "Ethernet 3"]
# The private adapter is the "Remote NDIS Compatible Device" (see Get-NetAdapter).
param(
  [string]$Public = 'Wi-Fi',
  [string]$Private = 'Ethernet 3'
)
$log = Join-Path $PSScriptRoot 'enable-ics.log'
Start-Transcript -Path $log -Force | Out-Null
try {
  $hn = New-Object -ComObject HNetCfg.HNetShare
  # Windows allows only one shared pair; clear any other.
  foreach ($c in $hn.EnumEveryConnection) {
    $p = $hn.NetConnectionProps.Invoke($c)
    $cfg = $hn.INetSharingConfigurationForINetConnection.Invoke($c)
    if ($cfg.SharingEnabled -and $p.Name -notin $Public, $Private) { $cfg.DisableSharing(); "Disabled sharing on $($p.Name)" }
  }
  foreach ($c in $hn.EnumEveryConnection) {
    $p = $hn.NetConnectionProps.Invoke($c)
    $cfg = $hn.INetSharingConfigurationForINetConnection.Invoke($c)
    if ($p.Name -eq $Public)  { if ($cfg.SharingEnabled) { $cfg.DisableSharing() }; $cfg.EnableSharing(0); "Public: $Public" }
    if ($p.Name -eq $Private) { if ($cfg.SharingEnabled) { $cfg.DisableSharing() }; $cfg.EnableSharing(1); "Private: $Private" }
  }
  'RESULT: OK'
} catch { "RESULT: FAIL $($_.Exception.Message)" }
finally { Stop-Transcript | Out-Null }
