# Sets up the Bambuddy computer on Windows, for every SnapSTL computer on the network:
#   * Bambuddy (print-farm manager) from its own Windows installer: a Windows service that starts with
#     Windows and finds the printers on the network by itself
#   * Bambuddy's slicer service in Docker (Bambu Studio + OrcaSlicer sidecars), used by Bambuddy
#
# In PowerShell opened with "Run as administrator":
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/bambuddy-server.ps1 | iex"
#
# Run it again any time: it only applies settings (Bambuddy's slicer settings, firewall) and installs or
# downloads nothing that's already there. Choices are remembered in C:\ProgramData\SnapSTL-Bambuddy\setup.json.
#   $env:BAMBUDDY_SLICER="bambu_studio" | "orcaslicer"   the slicer Bambuddy uses (default bambu_studio)
#   $env:BAMBUDDY_UPGRADE="1"   also move Bambuddy + the slicer service to the version this SnapSTL was
#                               tested with (downloads; printers, profiles and history are kept)
# Clear one again with:  Remove-Item Env:BAMBUDDY_UPGRADE
# A Bambuddy you installed yourself is used as it is: never reinstalled, upgraded or stopped.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar

function Invoke-BambuddyServerSetup {
  $Raw = "https://raw.githubusercontent.com/arihantjain124/snapstl/main"
  $Dir = Join-Path $env:ProgramData "SnapSTL-Bambuddy"
  $SetupFile = Join-Path $Dir "setup.json"
  $Port = 8000   # Bambuddy's Windows installer always uses 8000

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
  function Get-Bambuddy {
    # Bambuddy's Windows installer: a service "Bambuddy" + an uninstall entry with its version
    $svc = Get-Service -Name "Bambuddy" -ErrorAction SilentlyContinue
    $reg = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
      ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
      Where-Object { $_.DisplayName -like "Bambuddy*" } | Select-Object -First 1
    if (-not $svc -and -not $reg) { return $null }
    $v = if ($reg) { [string]$reg.DisplayVersion } else { "" }
    return @{ Service = $svc; Version = $v }
  }

  # ---- settings: what you set now > what was set last time > defaults ----
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  $Saved = $null
  if (Test-Path $SetupFile) { try { $Saved = Get-Content $SetupFile -Raw | ConvertFrom-Json } catch {} }
  $Slicer = "bambu_studio"
  if ($env:BAMBUDDY_SLICER) { $Slicer = $env:BAMBUDDY_SLICER.ToLower() } elseif ($Saved -and $Saved.slicer) { $Slicer = [string]$Saved.slicer }
  if ($Slicer -notin @("bambu_studio", "orcaslicer")) { throw "BAMBUDDY_SLICER is bambu_studio or orcaslicer" }
  $Upgrade = ($env:BAMBUDDY_UPGRADE -eq "1")
  $Ours = [bool]($Saved -and $Saved.native_installed_by_snapstl)

  # version: the tested one on first setup or upgrade, else the one already set up
  $Here = if ($PSCommandPath) { Split-Path -Parent (Split-Path -Parent $PSCommandPath) } else { $null }
  $LocalCompose = if ($Here) { Join-Path $Here "bambuddy-docker\docker-compose.yml" } else { $null }
  $Version = $null
  if ($Saved -and $Saved.version -and -not $Upgrade) { $Version = [string]$Saved.version }
  if (-not $Version) {
    if ($Here -and (Test-Path (Join-Path $Here "bambuddy-version.txt"))) {
      $Version = (Get-Content (Join-Path $Here "bambuddy-version.txt") -Raw).Trim().TrimStart("v")
    } else {
      $Version = ((Invoke-WebRequest -UseBasicParsing "$Raw/scripts/bambuddy-version.txt").Content).Trim().TrimStart("v")
    }
  }
  $Note = if ($Upgrade) { " (upgrade)" } else { "" }
  Write-Host "Bambuddy $Version (Windows service) + slicer service in Docker, slicer $Slicer$Note"

  # ---- 1. Docker for the slicer service (installed only if missing) ----
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
  if (Test-Path $DockerExe) {   # Docker Desktop starts at sign-in (the slicer containers restart with it)
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

  # ---- 2. Bambuddy in Docker from an earlier version of this script: removed (frees port 8000) ----
  $ErrorActionPreference = "Continue"
  $Containers = @(docker ps -a --format "{{.Names}}" 2>$null)
  if ($Containers -contains "snapstl-bambuddy") {
    Write-Host "Removing the Docker Bambuddy an earlier version of this script made (its data stays in Docker volume"
    Write-Host "snapstl-bambuddy_bambuddy_data; printers are added again in the Windows Bambuddy)..."
    docker rm -f snapstl-bambuddy *> $null
    $Deadline = (Get-Date).AddSeconds(30)   # Docker lets go of port 8000 a moment later
    while ((Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 2 }
  }
  $ErrorActionPreference = "Stop"

  # ---- 3. Bambuddy as a Windows service (installed only if missing, or on upgrade if this script installed it) ----
  $Bb = Get-Bambuddy
  $NeedInstall = (-not $Bb) -or ($Upgrade -and $Ours -and $Bb.Version -and ($Bb.Version -ne $Version))
  if ($Bb -and -not $Ours) { Write-Host "Using the Bambuddy already installed on this PC ($($Bb.Version)) - it isn't reinstalled or upgraded." }
  elseif (-not $Bb -and [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    throw "Port $Port is taken by another program - free it (or stop that program) and run this again."
  }
  if ($NeedInstall) {
    $Exe = Join-Path $env:TEMP "bambuddy-$Version-windows-x64-setup.exe"
    Write-Host "Downloading Bambuddy $Version for Windows..."
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/maziggy/bambuddy/releases/download/v$Version/bambuddy-$Version-windows-x64-setup.exe" -OutFile $Exe
    Write-Host "Installing Bambuddy as a Windows service (data in C:\ProgramData\Bambuddy)..."
    $p = Start-Process -FilePath $Exe -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MERGETASKS=!firewallrule,!desktopicon" -Wait -PassThru
    Remove-Item $Exe -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "Bambuddy's installer failed (code $($p.ExitCode))." }
    $Ours = $true
    $Bb = Get-Bambuddy
  }
  if ($Bb -and $Bb.Service -and $Bb.Service.Status -ne "Running") { Start-Service -Name "Bambuddy" -ErrorAction SilentlyContinue }

  # ---- 4. slicer service: compose file + .env; images downloaded only when missing ----
  if ($LocalCompose -and (Test-Path $LocalCompose)) { $Compose = Get-Content $LocalCompose -Raw }
  else { $Compose = (Invoke-WebRequest -UseBasicParsing "$Raw/scripts/bambuddy-docker/docker-compose.yml").Content }
  [IO.File]::WriteAllText((Join-Path $Dir "docker-compose.yml"), $Compose.Replace("`r`n", "`n"))
  [IO.File]::WriteAllText((Join-Path $Dir ".env"), "SIDECAR_TAG=bambuddy-$Version`n")
  Push-Location $Dir
  try {
    $ErrorActionPreference = "Continue"
    $Need = @()
    foreach ($s in @("bambu-studio-api", "orca-slicer-api")) {
      docker image inspect "ghcr.io/maziggy/$($s):bambuddy-$Version" *> $null
      if ($LASTEXITCODE -ne 0) { $Need += $s }
    }
    if ($Need.Count -gt 0) {
      Write-Host "Downloading the slicer service: $($Need -join ', ') ($Version, about 330 MB the first time)..."
      docker compose pull -q @Need
      if ($LASTEXITCODE -ne 0) { throw "Downloading the slicer images failed - check the internet connection and run this again." }
    }
    docker compose up -d --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw "Starting the slicer service failed - see: docker compose -f `"$Dir\docker-compose.yml`" logs" }
  } finally { Pop-Location; $ErrorActionPreference = "Stop" }

  $Deadline = (Get-Date).AddMinutes(3)
  while (-not (Get-Url "http://127.0.0.1:3001/health") -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 3 }
  $SlicerOk = [bool](Get-Url "http://127.0.0.1:3001/health")

  # ---- 5. Bambuddy uses the slicer service (set while its login is off) ----
  $BbOk = $false; $Configured = $false
  $Deadline = (Get-Date).AddMinutes(3)
  while (-not (Get-Url "http://127.0.0.1:$Port/api/v1/auth/status") -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 3 }
  $r = Get-Url "http://127.0.0.1:$Port/api/v1/auth/status"
  if ($r) {
    $BbOk = $true
    if (-not ($r.Content | ConvertFrom-Json).auth_enabled) {
      $Body = @{ use_slicer_api = $true; preferred_slicer = $Slicer
                 bambu_studio_api_url = "http://localhost:3001"; orcaslicer_api_url = "http://localhost:3003" } | ConvertTo-Json
      try {
        Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:$Port/api/v1/settings" -ContentType "application/json" -Body $Body -TimeoutSec 20 | Out-Null
        $Configured = $true
      } catch { Write-Host "Couldn't set Bambuddy's slicer settings by itself: $($_.Exception.Message)" }
    }
  }

  # ---- 6. firewall, this network only: Bambuddy's page/API (TCP 8000) and printer discovery (UDP 2021) ----
  Get-NetFirewallRule -DisplayName "SnapSTL Bambuddy*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName "SnapSTL Bambuddy (TCP $Port)" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
    -RemoteAddress LocalSubnet -Profile Any | Out-Null
  New-NetFirewallRule -DisplayName "SnapSTL Bambuddy printer discovery (UDP 2021)" -Direction Inbound -Protocol UDP -LocalPort 2021 `
    -Action Allow -RemoteAddress LocalSubnet -Profile Any | Out-Null

  # ---- remember the choices ----
  @{ slicer = $Slicer; version = $Version; native_installed_by_snapstl = $Ours } | ConvertTo-Json |
    Set-Content -Path $SetupFile -Encoding ASCII

  # ---- 7. summary ----
  $Route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
  $Ips = @()
  if ($Route) { $Ips += (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $Route.InterfaceIndex -ErrorAction SilentlyContinue).IPAddress }
  $Ips = @($Ips | Where-Object { $_ } | Select-Object -Unique)
  $SlicerName = if ($Slicer -eq "orcaslicer") { "OrcaSlicer" } else { "Bambu Studio" }
  $SlicerState = if ($SlicerOk) { "running in Docker (Bambu Studio + OrcaSlicer)" } else { "not answering yet - see: docker compose -f `"$Dir\docker-compose.yml`" logs" }
  $BbVersion = if ($Bb -and $Bb.Version) { $Bb.Version } else { $Version }
  $BbState = if ($BbOk) { "running as a Windows service, version $BbVersion" } else { "not answering yet - Services: Bambuddy; logs in C:\ProgramData\Bambuddy\logs" }

  Write-Host ""
  Write-Host "=== Bambuddy computer ready ===" -ForegroundColor Green
  Write-Host "Bambuddy:        $BbState"
  Write-Host "Slicer service:  $SlicerState"
  if ($Configured) { Write-Host "Slicing:         Bambuddy uses the slicer service ($SlicerName)." }
  elseif ($BbOk) {
    Write-Host "Slicing:         Bambuddy's login is on - in Bambuddy: Settings -> Slicer -> Use Slicer API on,"
    Write-Host "                 Preferred Slicer $SlicerName, sidecar URL http://localhost:3001 (OrcaSlicer: http://localhost:3003)."
  }
  Write-Host "Settings:        $SetupFile - run the same command again to apply changes (nothing is reinstalled)."
  Write-Host ""
  Write-Host "In SnapSTL on every computer: Settings -> Connections -> Bambuddy address:" -ForegroundColor Green
  foreach ($ip in $Ips) { Write-Host "    http://$($ip):$Port" -ForegroundColor Green }
  Write-Host ""
  Write-Host "Printers: open that address -> Printers -> Add printer; Bambuddy finds them on the network."
  Write-Host "Bambuddy starts with Windows. The slicer service runs while someone is signed in (Docker Desktop)."
}

Invoke-BambuddyServerSetup
