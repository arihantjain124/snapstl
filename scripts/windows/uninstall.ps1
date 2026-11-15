# Removes the SnapSTL background task and shortcuts. Leaves the folder, your data
# (%LOCALAPPDATA%\SnapSTL, %USERPROFILE%\SnapSTL) as they are.
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Stop-SnapSTL
foreach ($Dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  Remove-Item (Join-Path $Dir "SnapSTL.lnk") -ErrorAction SilentlyContinue
}
Write-Host "SnapSTL's task and shortcuts are removed. Remove the helper in chrome://extensions."
