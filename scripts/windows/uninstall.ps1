# Removes the SnapSTL background task and shortcuts. Leaves the folder, your data
# (%LOCALAPPDATA%\SnapSTL, %USERPROFILE%\SnapSTL) as they are.
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "common.ps1")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Stop-SnapSTL
foreach ($Dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
  Remove-Item (Join-Path $Dir "SnapSTL.lnk") -ErrorAction SilentlyContinue
}
Unregister-ScheduledTask -TaskName $BambuddyTask -Confirm:$false -ErrorAction SilentlyContinue
Stop-Bambuddy
Write-Host "SnapSTL's and Bambuddy's tasks and shortcuts are removed (their folders and data stay). Remove the helper in chrome://extensions."
