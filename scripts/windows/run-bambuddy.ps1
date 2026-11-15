# Run by the "Bambuddy" scheduled task (hidden): keeps Bambuddy running on http://127.0.0.1:8000.
. (Join-Path $PSScriptRoot "common.ps1")
$Logs = Join-Path $BambuddyData "logs"
New-Item -ItemType Directory -Force $Logs | Out-Null
$env:DATA_DIR = $BambuddyData
$env:LOG_DIR = $Logs
Set-Location $BambuddyDir
$Uvicorn = Join-Path $BambuddyDir "venv\Scripts\uvicorn.exe"
while ($true) {
  if (Test-Path $Uvicorn) {
    "$(Get-Date -Format s) starting Bambuddy" | Out-File -Append -Encoding utf8 (Join-Path $Logs "bambuddy.out.log")
    & $Uvicorn backend.app.main:app --host 127.0.0.1 --port $BambuddyPort --loop asyncio --timeout-graceful-shutdown 5 *>> (Join-Path $Logs "bambuddy.out.log")
  }
  Start-Sleep -Seconds 5
}
