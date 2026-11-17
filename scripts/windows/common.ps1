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

# The "SnapSTL" background task: starts at log-on, runs run-service.ps1 with NO window. conhost --headless
# is needed because on Windows 11 the console opens in Windows Terminal, which ignores -WindowStyle Hidden:
# the window stayed open, and closing it stopped SnapSTL.
function Register-SnapSTLTask($Root) {
  $Run = Join-Path $Root "scripts\windows\run-service.ps1"
  $Action = New-ScheduledTaskAction -Execute "conhost.exe" -WorkingDirectory $Root `
    -Argument "--headless powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Run`""
  $Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force `
    -Description "SnapSTL background service: web portal, MakerWorld downloads, plate builds" | Out-Null
}

# Older installs ran the task in a visible window: switch them to the headless one (takes effect at the next start).
function Repair-SnapSTLTask($Root) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t -and ($t.Actions | Select-Object -First 1).Execute -ne "conhost.exe") {
    try { Register-SnapSTLTask $Root } catch {}
  }
}

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

# Bambuddy, by what you asked for; never fails the SnapSTL install:
#   SNAPSTL_BAMBUDDY=server  this PC is the Bambuddy computer: Bambuddy as a Windows service + its slicer
#                            service in Docker (bambuddy-server.ps1, as administrator)
#   SNAPSTL_BAMBUDDY=native  the older install next to SnapSTL (bambuddy.ps1, no slicer service)
#   otherwise                nothing: Bambuddy runs on its own computer (bambuddy-server.ps1 there); a
#                            Bambuddy installed here by an older SnapSTL keeps being updated
#   SNAPSTL_NO_BAMBUDDY=1    nothing at all
function Install-Bambuddy {
  if ($env:SNAPSTL_NO_BAMBUDDY -eq "1") { return }
  if ($env:SNAPSTL_BAMBUDDY -eq "server") {
    $Server = Join-Path $PSScriptRoot "bambuddy-server.ps1"
    Write-Host "Setting up Bambuddy and its slicer service (a window asks for administrator rights)..."
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$Server`""
    return
  }
  if ($env:SNAPSTL_BAMBUDDY -eq "native" -or (Test-Path (Join-Path $BambuddyDir ".git"))) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "bambuddy.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Host "Bambuddy couldn't be installed - SnapSTL works without it. Try again: scripts\windows\bambuddy.ps1" }
    return
  }
  Write-Host "Bambuddy isn't installed on this PC: it runs on your Bambuddy computer (scripts\windows\bambuddy-server.ps1)."
}

# SNAPSTL_BAMBUDDY_URL=http://<Bambuddy computer>:8000 -> connect this SnapSTL to it (once SnapSTL runs).
function Connect-Bambuddy {
  if (-not $env:SNAPSTL_BAMBUDDY_URL) { return }
  try {
    $Body = @{ url = $env:SNAPSTL_BAMBUDDY_URL } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/farm/connect" -ContentType "application/json" -Body $Body -TimeoutSec 30 | Out-Null
    Write-Host "Connected to Bambuddy at $($env:SNAPSTL_BAMBUDDY_URL)."
  } catch {
    Write-Host "Couldn't connect to Bambuddy at $($env:SNAPSTL_BAMBUDDY_URL) yet - set it in SnapSTL: Settings -> Connections. ($($_.Exception.Message))"
  }
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
