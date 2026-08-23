param(
  [switch]$NoDashboard
)

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

function Get-LanIPv4 {
  $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Sort-Object -Property @{Expression = { $_.InterfaceAlias -eq 'Wi-Fi' }; Descending = $true } |
    Select-Object -First 1
  return $candidate.IPAddress
}

function Confirm-PhoneReachability {
  param([string]$LanIP)

  if (-not $LanIP) {
    Write-Warning 'Could not detect a LAN IP address. Phone access may not work.'
    return
  }

  $phoneUrl = "http://${LanIP}:8000/dashboard"
  $urlFile = Join-Path $projectRoot 'phone_url.txt'
  Set-Content -Path $urlFile -Value $phoneUrl -Encoding ascii

  $firewallOk = [bool](Get-NetFirewallRule -DisplayName 'FUTURE Server (8000)' -ErrorAction SilentlyContinue)
  $wifiProfile = Get-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -ErrorAction SilentlyContinue

  try {
    $lanCheck = Invoke-WebRequest -Uri "http://${LanIP}:8000/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    $lanReachable = ($lanCheck.StatusCode -eq 200)
  }
  catch {
    $lanReachable = $false
  }

  Write-Output "Phone URL: $phoneUrl"

  if (-not $firewallOk -or ($wifiProfile -and $wifiProfile.NetworkCategory -eq 'Public') -or -not $lanReachable) {
    Write-Warning 'Your phone may NOT be able to reach FUTURE right now.'
    if (-not $firewallOk) {
      Write-Warning '  - No firewall rule found for port 8000.'
    }
    if ($wifiProfile -and $wifiProfile.NetworkCategory -eq 'Public') {
      Write-Warning '  - Wi-Fi network profile is set to Public, which blocks inbound connections.'
    }
    Write-Warning '  Run setup_future_firewall.ps1 once as Administrator to fix this permanently.'
  } else {
    Write-Output 'LAN check passed: phone should be able to reach FUTURE at the URL above.'
  }
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
    try {
      $authCheck = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/auth/status' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
      if ($authCheck.StatusCode -eq 200) {
        if (-not $NoDashboard) {
          Open-DashboardWindow -Url $dashboardUrl
        }
        Write-Output "FUTURE server already running on port 8000 (PID $ownerPid)."
        Confirm-PhoneReachability -LanIP (Get-LanIPv4)
        exit 0
      }
    }
    catch {
      # Replace stale Future processes that predate the current API contract.
    }

    Write-Output "Restarting stale FUTURE server (PID $ownerPid)..."
    Stop-Process -Id $ownerPid -Force
  }
  else {
    Write-Output "Port 8000 is in use by PID $ownerPid. Not replacing non-FUTURE process."
    exit 1
  }
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pythonExe
$psi.Arguments = '-m uvicorn api_server:app --host 0.0.0.0 --port 8000'
$psi.WorkingDirectory = $projectRoot
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

[void][System.Diagnostics.Process]::Start($psi)
$ready = Wait-ForServer -HealthUrl $healthUrl
if (-not $ready) {
  Write-Error 'FUTURE server did not become ready on http://127.0.0.1:8000/health.'
  exit 1
}

if (-not $NoDashboard) {
  Open-DashboardWindow -Url $dashboardUrl
  Write-Output 'FUTURE server started and desktop window opened.'
} else {
  Write-Output 'FUTURE server started in background mode for mobile access.'
}

Confirm-PhoneReachability -LanIP (Get-LanIPv4)
