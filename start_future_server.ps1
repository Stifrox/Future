$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$projectRoot = $PSScriptRoot

$runtimeVenv = Join-Path $projectRoot '.future_runtime'
$runtimePython = Join-Path $runtimeVenv 'Scripts\python.exe'

function Ensure-RuntimePython {
  if (Test-Path $runtimePython) {
    return
  }

  Write-Output 'Creating FUTURE runtime environment...'
  & py -3.14 -m venv $runtimeVenv
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $runtimePython)) {
    Write-Error 'Failed to create runtime environment with py -3.14.'
    exit 1
  }
}

function Ensure-ServerDependencies {
  $checkCmd = @'
import importlib.util
mods = ['fastapi', 'uvicorn', 'requests', 'pydantic', 'dotenv', 'openai', 'psutil']
missing = [m for m in mods if importlib.util.find_spec(m) is None]
print('|'.join(missing))
'@
  $missingModules = [string](& $runtimePython -c $checkCmd)
  $missingModules = $missingModules.Trim()
  if ([string]::IsNullOrWhiteSpace($missingModules)) {
    return
  }

  Write-Output "Installing FUTURE server dependencies (missing: $missingModules)..."
  & $runtimePython -m pip install --upgrade pip *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Error 'Failed to upgrade pip in FUTURE runtime environment.'
    exit 1
  }

  & $runtimePython -m pip install --disable-pip-version-check --no-input --progress-bar off requests openai python-dotenv fastapi uvicorn psutil
  if ($LASTEXITCODE -ne 0) {
    Write-Error 'Failed to install FUTURE server dependencies.'
    exit 1
  }
}

function Open-DashboardWindow {
  param([string]$Url)

  $edgeCandidates = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
  )
  foreach ($edge in $edgeCandidates) {
    if (Test-Path $edge) {
      Start-Process -FilePath $edge -ArgumentList "--app=$Url", '--new-window' | Out-Null
      return
    }
  }

  $chromeCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
  )
  foreach ($chrome in $chromeCandidates) {
    if (Test-Path $chrome) {
      Start-Process -FilePath $chrome -ArgumentList "--app=$Url", '--new-window' | Out-Null
      return
    }
  }

  Start-Process $Url | Out-Null
}

function Wait-ForServer {
  param([string]$HealthUrl, [int]$TimeoutSeconds = 25)

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
      if ($response.StatusCode -eq 200) {
        return $true
      }
    }
    catch {
      # Keep waiting until timeout.
    }
    Start-Sleep -Milliseconds 600
  }
  return $false
}

Ensure-RuntimePython
Ensure-ServerDependencies

$pythonExe = $runtimePython

$dashboardUrl = 'http://127.0.0.1:8000/dashboard'
$healthUrl = 'http://127.0.0.1:8000/health'
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue

if ($listener) {
  $ownerPid = $listener[0].OwningProcess
  $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction SilentlyContinue
  $cmd = [string]($owner.CommandLine)

  if ($cmd -match 'uvicorn\s+api_server:app') {
    Open-DashboardWindow -Url $dashboardUrl
    Write-Output "FUTURE server already running on port 8000 (PID $ownerPid)."
    exit 0
  }

  Write-Output "Port 8000 is in use by PID $ownerPid. Not replacing non-FUTURE process."
  exit 1
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pythonExe
$psi.Arguments = '-m uvicorn api_server:app --host 127.0.0.1 --port 8000'
$psi.WorkingDirectory = $projectRoot
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

[void][System.Diagnostics.Process]::Start($psi)
$ready = Wait-ForServer -HealthUrl $healthUrl
if (-not $ready) {
  Write-Error 'FUTURE server did not become ready on http://127.0.0.1:8000/health.'
  exit 1
}

Open-DashboardWindow -Url $dashboardUrl
Write-Output 'FUTURE server started and desktop window opened.'
