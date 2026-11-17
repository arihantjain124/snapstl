# Run by the "SnapSTL" scheduled task (hidden): keeps SnapSTL running. `uv run` installs any update
# first, so after Settings -> Updates -> Update now, SnapSTL restarts here with the new version.
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")
New-Item -ItemType Directory -Force $DataDir | Out-Null
$env:SNAPSTL_SERVICE = "1"
Repair-SnapSTLTask $Root   # older installs: no visible window from the next start on
Set-Location $Root
while ($true) {
  $Uv = Find-Uv
  if ($Uv) {
    "$(Get-Date -Format s) starting SnapSTL" | Out-File -Append -Encoding utf8 $LogFile
    & $Uv run --no-dev --quiet --project $Root snapstl-serve *>> $LogFile
  }
  Start-Sleep -Seconds 5
}
