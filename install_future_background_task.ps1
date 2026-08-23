param(
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'start_future_server.ps1'
$startupFolder = [Environment]::GetFolderPath('Startup')
$linkPath = Join-Path $startupFolder 'FUTURE Mobile.lnk'

if (-not (Test-Path $scriptPath)) {
  throw "Missing launcher script: $scriptPath"
}

if ($Uninstall) {
  if (Test-Path $linkPath) {
    Remove-Item $linkPath -Force
    Write-Output "Removed startup shortcut: $linkPath"
  } else {
    Write-Output "Startup shortcut not found."
  }
  exit 0
}

$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($linkPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -NoDashboard"
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.IconLocation = 'powershell.exe,0'
$shortcut.Save()

Write-Output "Installed FUTURE startup shortcut in: $startupFolder"
Write-Output "This will launch FUTURE in the background every time you sign in to Windows."
$lanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
  Select-Object -ExpandProperty IPAddress -First 1
if ($lanIp) {
  Write-Output "Phone URL: http://${lanIp}:8000/dashboard"
}

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $scriptPath -NoDashboard
