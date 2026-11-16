# Shared by the SnapSTL Windows scripts.
$TaskName = "SnapSTL"
$Port = 8317
$DataDir = Join-Path $env:LOCALAPPDATA "SnapSTL"
$LogFile = Join-Path $DataDir "SnapSTL.log"
# Bambuddy (print farm), installed next to SnapSTL by bambuddy.ps1
$BambuddyTask = "Bambuddy"
$BambuddyPort = if ($env:BAMBUDDY_PORT) { [int]$env:BAMBUDDY_PORT } else { 8000 }
$BambuddyDir = Join-Path $HOME "SnapSTL-Bambuddy"
$BambuddyData = Join-Path $env:LOCALAPPDATA "Bambuddy"

# Stops only the Bambuddy SnapSTL installed (its python runs from $BambuddyDir) - never another Bambuddy or
# whatever else listens on the port.
function Stop-Bambuddy {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$BambuddyDir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# A Bambuddy installed some other way (its own Windows installer: a service + C:\ProgramData\Bambuddy).
function Get-OtherBambuddy {
  $svc = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*bambuddy*" -or $_.DisplayName -like "*Bambuddy*" }
  $data = Join-Path $env:ProgramData "Bambuddy"
  if ($svc -or (Test-Path $data)) { return @{ Service = $svc; Data = $data } }
  return $null
}

# OrcaSlicer: Make It slices plates with its command line. Never fails the SnapSTL install.
function Install-Orca {
  if ($env:SNAPSTL_NO_ORCA -eq "1") { return }
  $Exe = Join-Path $env:ProgramFiles "OrcaSlicer\orca-slicer.exe"
  if (Test-Path $Exe) { return }
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "Installing OrcaSlicer (the slicer SnapSTL uses)..."
    winget install --id SoftFever.OrcaSlicer -e --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Host
  } else {
    Write-Host "Install OrcaSlicer from https://github.com/OrcaSlicer/OrcaSlicer/releases (SnapSTL slices with it)."
  }
}

# Install / update Bambuddy unless SNAPSTL_NO_BAMBUDDY=1; never fails the SnapSTL install.
function Install-Bambuddy {
  if ($env:SNAPSTL_NO_BAMBUDDY -eq "1") { return }
  powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "bambuddy.ps1")
  if ($LASTEXITCODE -ne 0) { Write-Host "Bambuddy couldn't be installed - SnapSTL works without it. Try again: scripts\windows\bambuddy.ps1" }
}

function Find-Uv {
  $c = Get-Command uv -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

# Stop the running SnapSTL server (the background task's loop starts it again).
function Stop-SnapSTL {
  Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}

function Wait-SnapSTL([int]$Seconds) {
  $end = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $end) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/api/auth-check"
      if ($r.StatusCode -eq 200) { return $true }
    } catch { }
    Start-Sleep -Seconds 2
  }
  return $false
}
