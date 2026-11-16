# Sets up the Bambuddy computer on Windows: Docker Desktop, Bambuddy (print-farm manager) and its slicer
# service (Bambu Studio + OrcaSlicer sidecars), all in Docker, reachable by every SnapSTL computer on the
# network. Safe to run again: it updates to the versions this SnapSTL was tested with and keeps all data.
#
# In PowerShell opened with "Run as administrator":
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/bambuddy-server.ps1 | iex"
#
# What it does:
#   1. WSL 2 (Docker's engine) and Docker Desktop, if missing - may ask for one restart, then run it again
#   2. C:\ProgramData\SnapSTL-Bambuddy\docker-compose.yml (+ .env with the pinned versions)
#   3. docker compose pull / up: Bambuddy on port 8000, slicer sidecars inside Docker
#   4. turns on Bambuddy's "Use Slicer API" (Bambu Studio) while its login is off
#   5. opens port 8000 in Windows Firewall for the local network only
#   6. prints this computer's address for SnapSTL: Settings -> Connections -> Bambuddy address
# Never touches a Bambuddy installed another way: if one already answers on port 8000, only the slicer
# service is set up, and you point that Bambuddy at it.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar

function Invoke-BambuddyServerSetup {
  $Raw = "https://raw.githubusercontent.com/arihantjain124/snapstl/main"
  $Dir = Join-Path $env:ProgramData "SnapSTL-Bambuddy"
  $Port = if ($env:BAMBUDDY_PORT) { [int]$env:BAMBUDDY_PORT } else { 8000 }

  # ---- administrator (Docker install, firewall) ----
  $Admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $Admin) {
    throw "Open PowerShell with 'Run as administrator' (right-click PowerShell in the Start menu), then run the same command again."
  }

  function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  }
  function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    $ErrorActionPreference = "Continue"
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
  }
  function Get-Url($Url, $TimeoutSec = 5) {
    try { return Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec } catch { return $null }
  }

  # ---- 1. WSL 2 + Docker Desktop ----
  $DockerExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  Refresh-Path
  if (-not (Test-Path $DockerExe) -and -not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $ErrorActionPreference = "Continue"
    wsl.exe --status *> $null
    $WslOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    if (-not $WslOk) {
      Write-Host "Installing WSL 2 (the Linux engine Docker runs on)..."
      wsl.exe --install --no-distribution
      Write-Host ""
      Write-Host "Restart this computer, then run the same command again (as administrator) to continue." -ForegroundColor Yellow
      return
    }
    Write-Host "Downloading Docker Desktop (about 600 MB)..."
    $Installer = Join-Path $env:TEMP "DockerDesktopInstaller.exe"
    Invoke-WebRequest -UseBasicParsing -Uri "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" -OutFile $Installer
    Write-Host "Installing Docker Desktop (a few minutes)..."
    $p = Start-Process -FilePath $Installer -ArgumentList "install", "--quiet", "--accept-license", "--backend=wsl-2" -Wait -PassThru
    Remove-Item $Installer -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "Docker Desktop didn't install (code $($p.ExitCode)). Install it from https://www.docker.com/products/docker-desktop/ and run this again." }
    try { Add-LocalGroupMember -Group "docker-users" -Member "$env:USERDOMAIN\$env:USERNAME" -ErrorAction Stop } catch {}
    Refresh-Path
  }
  # Docker Desktop starts at sign-in (the containers restart with it)
  if (Test-Path $DockerExe) {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "Docker Desktop" -Value "`"$DockerExe`""
  }
  if (-not (Test-Docker)) {
    if (Test-Path $DockerExe) {
      Write-Host "Starting Docker Desktop (first start can take a few minutes)..."
      Start-Process -FilePath $DockerExe
    }
    $Deadline = (Get-Date).AddMinutes(6)
    while (-not (Test-Docker) -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 5; Refresh-Path }
    if (-not (Test-Docker)) {
      Write-Host ""
      Write-Host "Docker isn't running yet. Open Docker Desktop, finish its first-start screens (you can skip signing in)," -ForegroundColor Yellow
      Write-Host "wait until it says 'Engine running' - or sign out and in again once - then run the same command again." -ForegroundColor Yellow
      return
    }
  }
  Write-Host "Docker is running."

  # ---- 2. compose file + pinned versions ----
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  $Here = if ($PSCommandPath) { Split-Path -Parent (Split-Path -Parent $PSCommandPath) } else { $null }
  $LocalCompose = if ($Here) { Join-Path $Here "bambuddy-docker\docker-compose.yml" } else { $null }
  $LocalVersion = if ($Here) { Join-Path $Here "bambuddy-version.txt" } else { $null }
  if ($LocalCompose -and (Test-Path $LocalCompose)) {
    $Compose = Get-Content $LocalCompose -Raw
    $Version = (Get-Content $LocalVersion -Raw).Trim()
  } else {
    $Compose = (Invoke-WebRequest -UseBasicParsing "$Raw/scripts/bambuddy-docker/docker-compose.yml").Content
    $Version = ((Invoke-WebRequest -UseBasicParsing "$Raw/scripts/bambuddy-version.txt").Content).Trim()
  }
  if ($env:BAMBUDDY_VERSION) { $Version = $env:BAMBUDDY_VERSION }
  $Version = $Version.TrimStart("v")
  $Tz = "UTC"
  try {
    $WinTz = (Get-TimeZone).Id
    $Iana = $null
    if ([System.TimeZoneInfo].GetMethod("TryConvertWindowsIdToIanaId", [type[]]@([string], [string].MakeByRefType()))) {
      if ([System.TimeZoneInfo]::TryConvertWindowsIdToIanaId($WinTz, [ref]$Iana)) { $Tz = $Iana }
    } elseif ($WinTz -eq "India Standard Time") { $Tz = "Asia/Kolkata" }
  } catch {}

  # Something else already answers on the port (a Bambuddy installed another way, or another program)?
  $Ours = $false
  $ErrorActionPreference = "Continue"
  $Names = docker ps -a --format "{{.Names}}" 2>$null
  $ErrorActionPreference = "Stop"
  if ($Names -contains "snapstl-bambuddy") { $Ours = $true }
  $Busy = (-not $Ours) -and [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
  $Services = "bambuddy", "bambu-studio-api", "orca-slicer-api"
  if ($Busy) {
    Write-Host "Something already answers on port $Port (a Bambuddy installed another way?) - it's left alone; only the slicer service is set up." -ForegroundColor Yellow
    $Services = "bambu-studio-api", "orca-slicer-api"
  }

  [IO.File]::WriteAllText((Join-Path $Dir "docker-compose.yml"), $Compose.Replace("`r`n", "`n"))
  $Env = "BAMBUDDY_TAG=$Version`nSIDECAR_TAG=bambuddy-$Version`nBAMBUDDY_PORT=$Port`nTZ=$Tz`n"
  [IO.File]::WriteAllText((Join-Path $Dir ".env"), $Env)

  # ---- 3. pull + start ----
  Write-Host "Downloading Bambuddy $Version and its slicer service (first time: about 1 GB)..."
  Push-Location $Dir
  try {
    $ErrorActionPreference = "Continue"
    docker compose pull -q @Services
    if ($LASTEXITCODE -ne 0) { throw "Downloading the Docker images failed - check the internet connection and run this again." }
    docker compose up -d --remove-orphans @Services
    if ($LASTEXITCODE -ne 0) { throw "Starting the containers failed - see: docker compose -f `"$Dir\docker-compose.yml`" logs" }
  } finally { Pop-Location; $ErrorActionPreference = "Stop" }

  Write-Host "Waiting for the slicer service..."
  $Deadline = (Get-Date).AddMinutes(3)
  while (-not (Get-Url "http://127.0.0.1:3001/health") -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 3 }
  $SlicerOk = [bool](Get-Url "http://127.0.0.1:3001/health")

  # ---- 4. Bambuddy: use the slicer service ----
  $BbOk = $false; $Configured = $false
  if (-not $Busy) {
    Write-Host "Waiting for Bambuddy..."
    $Deadline = (Get-Date).AddMinutes(3)
    while (-not (Get-Url "http://127.0.0.1:$Port/api/v1/auth/status") -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 3 }
    $r = Get-Url "http://127.0.0.1:$Port/api/v1/auth/status"
    if ($r) {
      $BbOk = $true
      $Auth = ($r.Content | ConvertFrom-Json).auth_enabled
      if (-not $Auth) {
        try {
          $Body = @{ use_slicer_api = $true; preferred_slicer = "bambu_studio" } | ConvertTo-Json
          Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:$Port/api/v1/settings" -ContentType "application/json" -Body $Body -TimeoutSec 20 | Out-Null
          $Configured = $true
        } catch { Write-Host "Couldn't switch on Bambuddy's slicer service by itself: $($_.Exception.Message)" }
      }
    }
  }

  # ---- 5. firewall: port 8000 for this network only ----
  $Rule = "SnapSTL Bambuddy (TCP $Port)"
  if (-not $Busy -and -not (Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
      -RemoteAddress LocalSubnet -Profile Any | Out-Null
  }

  # ---- 6. this computer's address ----
  $Route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
  $Ips = @()
  if ($Route) { $Ips += (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $Route.InterfaceIndex -ErrorAction SilentlyContinue).IPAddress }
  $Ips += (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
      $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notmatch "vEthernet|WSL|Docker|Loopback" }).IPAddress
  $Ips = @($Ips | Where-Object { $_ } | Select-Object -Unique)

  Write-Host ""
  Write-Host "=== Bambuddy computer ready ===" -ForegroundColor Green
  Write-Host "Folder:          $Dir  (docker-compose.yml, .env)"
  Write-Host "Slicer service:  $(if ($SlicerOk) { 'running (Bambu Studio + OrcaSlicer)' } else { 'not answering yet - see: docker compose logs bambu-studio-api' })"
  if ($Busy) {
    Write-Host "Your own Bambuddy: Settings -> Slicer -> Use Slicer API on, Preferred Slicer Bambu Studio,"
    Write-Host "                   sidecar URL http://localhost:3001 (OrcaSlicer: http://localhost:3003)."
  } else {
    Write-Host "Bambuddy:        $(if ($BbOk) { "running, version $Version" } else { 'not answering yet - see: docker compose logs bambuddy' })"
    if ($Configured) { Write-Host "Slicing:         Use Slicer API is on (Bambu Studio)." }
    elseif ($BbOk) { Write-Host "Slicing:         in Bambuddy: Settings -> Slicer -> Use Slicer API on, Preferred Slicer Bambu Studio (URL fields empty)." }
    Write-Host ""
    Write-Host "In SnapSTL on every computer: Settings -> Connections -> Bambuddy address:" -ForegroundColor Green
    foreach ($ip in $Ips) { Write-Host "    http://$($ip):$Port" -ForegroundColor Green }
    Write-Host "Bambuddy itself: open that address in a browser, add printers by IP (Printers), import your slicing"
    Write-Host "profiles (Profiles -> Local Profiles -> Import Profiles)."
    Write-Host "Keep this computer on and signed in: Docker Desktop (and with it Bambuddy) runs while you're signed in."
  }
  Write-Host "Update later: run the same command again."
}

Invoke-BambuddyServerSetup
