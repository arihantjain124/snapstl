# Restarts SnapSTL (e.g. after editing .env). The background task starts it again within seconds.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1
. (Join-Path $PSScriptRoot "common.ps1")
Stop-SnapSTL
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (Wait-SnapSTL 120) { Write-Host "SnapSTL restarted." } else { Write-Host "Still starting - log: $LogFile" }
