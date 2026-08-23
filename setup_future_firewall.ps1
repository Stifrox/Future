# One-time setup: opens port 8000 for LAN access so phones can reach the FUTURE server.
# Self-elevates if not already running as Administrator.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Output 'Requesting Administrator privileges (a UAC prompt will appear)...'
  Start-Process -FilePath 'powershell.exe' -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
  exit 0
}

$ruleName = 'FUTURE Server (8000)'

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
  Write-Output "Firewall rule already exists: $ruleName"
} else {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Any | Out-Null
  Write-Output "Created firewall rule: $ruleName (TCP 8000 inbound, all profiles)"
}

$wifiProfile = Get-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -ErrorAction SilentlyContinue
if ($wifiProfile -and $wifiProfile.NetworkCategory -eq 'Public') {
  Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Private
  Write-Output "Changed Wi-Fi network profile from Public to Private."
} else {
  Write-Output "Wi-Fi network profile is already Private (or not found)."
}

Write-Output ''
Write-Output 'Setup complete. Your phone should now be able to reach the FUTURE server over your home Wi-Fi.'
Read-Host 'Press Enter to close'
