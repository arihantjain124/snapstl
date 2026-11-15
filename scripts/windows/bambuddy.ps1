# Installs (or updates) Bambuddy on this PC, next to SnapSTL: the open-source print-farm manager
# (AGPL-3.0, https://github.com/maziggy/bambuddy) that SnapSTL's "Push to queue" sends plates to.
#   * the version in scripts\bambuddy-version.txt (tested with this SnapSTL), in %USERPROFILE%\SnapSTL-Bambuddy
#   * a background task "Bambuddy" (starts at log-on, restarts if it stops): http://127.0.0.1:8000
#     (this computer only; SnapSTL connects to it by itself)
#   * its data in %LOCALAPPDATA%\Bambuddy
# Run by install.ps1 and update.ps1; safe to run again. Skip Bambuddy: set SNAPSTL_NO_BAMBUDDY=1.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")
$Version = if ($env:BAMBUDDY_VERSION) { $env:BAMBUDDY_VERSION } else { (Get-Content (Join-Path $Root "scripts\bambuddy-version.txt") -Raw).Trim() }
$Repo = "https://github.com/maziggy/bambuddy.git"
$Changed = $false

if (Test-Path (Join-Path $BambuddyDir ".git")) {
  $Current = git -C $BambuddyDir describe --tags --exact-match 2>$null
  if ($Current -ne $Version) {
    Write-Host "Updating Bambuddy $Current -> $Version..."
    Stop-Bambuddy   # Windows can't replace files of a running Python
    git -C $BambuddyDir fetch -q --depth 1 origin tag $Version
    git -C $BambuddyDir -c advice.detachedHead=false checkout -q $Version
    if ($LASTEXITCODE -ne 0) { throw "Updating Bambuddy failed (git)" }
    $Changed = $true
  }
} else {
  Write-Host "Downloading Bambuddy $Version into $BambuddyDir..."
  git -c advice.detachedHead=false clone -q --depth 1 --branch $Version $Repo $BambuddyDir
  if ($LASTEXITCODE -ne 0) { throw "Downloading Bambuddy failed (check the internet connection)" }
  $Changed = $true
}

$Uvicorn = Join-Path $BambuddyDir "venv\Scripts\uvicorn.exe"
if ($Changed -or -not (Test-Path $Uvicorn)) {
  $Uv = Find-Uv
  if (-not $Uv) { throw "uv is missing - run scripts\windows\install.ps1 first" }
  Write-Host "Installing Bambuddy's Python parts (first time: a few minutes)..."
  Stop-Bambuddy
  & $Uv venv -q --allow-existing --python 3.12 (Join-Path $BambuddyDir "venv")
  & $Uv pip install -q --python (Join-Path $BambuddyDir "venv\Scripts\python.exe") -r (Join-Path $BambuddyDir "requirements.lock")
  if ($LASTEXITCODE -ne 0) { throw "Installing Bambuddy's Python parts failed" }
  $Changed = $true
}

$Run = Join-Path $PSScriptRoot "run-bambuddy.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $BambuddyDir `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Run`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $BambuddyTask -Action $Action -Trigger $Trigger -Settings $Settings -Force `
  -Description "Bambuddy print-farm manager for SnapSTL (http://127.0.0.1:$BambuddyPort)" | Out-Null
if ($Changed) { Stop-ScheduledTask -TaskName $BambuddyTask -ErrorAction SilentlyContinue; Stop-Bambuddy }
Start-ScheduledTask -TaskName $BambuddyTask

$End = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $End) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$BambuddyPort/api/v1/auth/status"
    if ($r.StatusCode -eq 200) { Write-Host "Bambuddy $Version is running: http://127.0.0.1:$BambuddyPort (SnapSTL -> Printers to add your printers)"; exit 0 }
  } catch { }
  Start-Sleep -Seconds 2
}
Write-Host "Bambuddy didn't answer yet - log: $BambuddyData\logs\bambuddy.out.log"
