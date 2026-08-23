$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'start_future_server.ps1'
$startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$startupName = 'Future'

if (-not (Test-Path $scriptPath)) {
    throw "Missing server script: $scriptPath"
}

$command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"$scriptPath\" -NoDashboard"

try {
    Remove-ItemProperty -Path $startupKey -Name $startupName -ErrorAction SilentlyContinue
} catch {}

Set-ItemProperty -Path $startupKey -Name $startupName -Value $command -Force

Write-Output "Registered FUTURE to launch at Windows logon via the current-user startup key."
Write-Output "Startup command: $command"
