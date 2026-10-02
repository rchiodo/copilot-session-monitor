$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot '.local\runtime.json'
if (-not (Test-Path $runtimePath)) {
    Write-Host 'No monitor runtime file; the monitor is not running.'
    exit 0
}
$runtime = Get-Content $runtimePath -Raw | ConvertFrom-Json
if ($runtime.url -notmatch '^http://127\.0\.0\.1:\d+$') { throw 'Invalid monitor runtime URL.' }
$status = Invoke-RestMethod "$($runtime.url)/api/status" -TimeoutSec 3
if ($status.instanceId -ne $runtime.instanceId) { throw 'Instance mismatch; refusing to stop a different service.' }
$null = Invoke-RestMethod "$($runtime.url)/api/stop" -Method Post `
    -Headers @{ Authorization = "Bearer $($runtime.token)" } -TimeoutSec 3
Write-Host 'Monitor stopped. Copilot sessions were not changed.'
