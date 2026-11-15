# Updates SnapSTL from its git repository and restarts it. Same as Settings -> Updates -> Update now.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\update.ps1
# Orders, batches, settings and downloaded models live outside this folder and are kept.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $Root
if (git status --porcelain --untracked-files=no) {
  Write-Host "Files in $Root were changed by hand:"; git status --short --untracked-files=no
  Write-Host "Undo them with:  git -C `"$Root`" checkout -- .   then run this again."; exit 1
}
$Before = git rev-parse HEAD
git pull --ff-only --quiet
if ($LASTEXITCODE -ne 0) { throw "git pull failed" }
if ($Before -eq (git rev-parse HEAD)) { Write-Host "SnapSTL is already up to date ($(git log -1 --format='%h %s'))."; exit 0 }
Write-Host "Updated:"; git log --format="  - %s" "$Before..HEAD"
Stop-SnapSTL                                   # the background task starts it again, with the update
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (Wait-SnapSTL 180) { Write-Host "SnapSTL restarted." } else { Write-Host "Still starting - log: $LogFile" }
Write-Host "The SnapSTL Helper reloads itself in Chrome within a minute."
