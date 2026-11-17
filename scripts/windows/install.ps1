# Installs SnapSTL on this Windows PC (run again after moving the folder; updates don't need it):
#   * uv (Python tool manager) if missing, and SnapSTL's Python parts
#   * a background task "SnapSTL" that starts at log-on and restarts SnapSTL if it stops
#     (web portal http://127.0.0.1:8317, downloads, plate builds)
#   * "SnapSTL" shortcuts in the Start menu and on the Desktop (the app window)
#   * Bambuddy: SNAPSTL_BAMBUDDY_URL=http://<Bambuddy computer>:8000 connects to yours; SNAPSTL_BAMBUDDY=server
#     sets it up on this PC (Windows service + slicer service in Docker) - see Install-Bambuddy in common.ps1
# Run:  powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")

$Uv = Find-Uv
if (-not $Uv) {
  Write-Host "Installing uv (Python tool manager)..."
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  $Uv = Find-Uv
  if (-not $Uv) { throw "uv didn't install - see https://docs.astral.sh/uv/" }
}
Write-Host "Installing SnapSTL's Python parts (first time: a few minutes)..."
& $Uv sync --no-dev --quiet --project $Root
if ($LASTEXITCODE -ne 0) { throw "Installing SnapSTL's Python parts failed" }

Write-Host "Setting up the SnapSTL background task..."
Register-SnapSTLTask $Root
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Stop-SnapSTL
Start-ScheduledTask -TaskName $TaskName

Write-Host "Adding SnapSTL to the Start menu and Desktop..."
$Pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Shell = New-Object -ComObject WScript.Shell
foreach ($Dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  $L = $Shell.CreateShortcut((Join-Path $Dir "SnapSTL.lnk"))
  $L.TargetPath = $Pyw
  $L.Arguments = "-m snapstl"
  $L.WorkingDirectory = $Root
  $L.Description = "SnapSTL - name organizer plates"
  $L.Save()
}

Install-Bambuddy

if (Wait-SnapSTL 90) {
  Connect-Bambuddy
  Write-Host "SnapSTL is running: open it from the Start menu (or http://127.0.0.1:8317)."
} else {
  Write-Host "SnapSTL didn't answer yet. Log: $LogFile"
}
