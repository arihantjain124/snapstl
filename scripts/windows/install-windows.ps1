# Installs (or updates) SnapSTL on this Windows PC in one go. In PowerShell:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/install-windows.ps1 | iex"
# Installs Git if it's missing (with winget), puts SnapSTL in %USERPROFILE%\SnapSTL-app (a copy of the
# public repository, so the app can update itself), sets up the background task and shortcuts, then
# opens the app. Your orders, settings and downloads live elsewhere and are never touched.
$ErrorActionPreference = "Stop"
$Repo = "https://github.com/arihantjain124/snapstl.git"
$Dir = Join-Path $HOME "SnapSTL-app"

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Git is needed: install it from https://git-scm.com/download/win (default options), then run this again."
  }
  Write-Host "Installing Git (needed to download and update SnapSTL)..."
  winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Host
  Refresh-Path
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git didn't install. Install it from https://git-scm.com/download/win, then run this again."
  }
}

if (Test-Path (Join-Path $Dir ".git")) {
  Write-Host "Updating SnapSTL in $Dir..."
  git -C $Dir pull --ff-only --quiet
} elseif (Test-Path $Dir) {
  Write-Host "Turning the existing $Dir into an updatable copy..."
  git -C $Dir init -q -b main
  git -C $Dir remote add origin $Repo 2>$null
  if ($LASTEXITCODE -ne 0) { git -C $Dir remote set-url origin $Repo }
  git -C $Dir fetch -q origin
  git -C $Dir reset -q --hard origin/main
  git -C $Dir branch -q --set-upstream-to=origin/main main
} else {
  Write-Host "Downloading SnapSTL into $Dir..."
  git clone -q $Repo $Dir
}
if ($LASTEXITCODE -ne 0) { throw "Downloading SnapSTL failed (check the internet connection)." }

powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "scripts\windows\install.ps1")
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "SnapSTL.lnk"
if (Test-Path $Shortcut) { Start-Process $Shortcut }
Write-Host ""
Write-Host "SnapSTL is installed. Next, in the app: Settings -> Setup (add the helper to Chrome, enter the license password)."
