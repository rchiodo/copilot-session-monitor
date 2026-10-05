param([ValidateSet('collector','watcher')][string]$Role)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$data = if ($env:MONITOR_DATA_DIR) { $env:MONITOR_DATA_DIR } else { Join-Path $root '.local' }
$file = if ($Role -eq 'collector') { 'runtime.json' } else { 'watcher-runtime.json' }
$statusPath = if ($Role -eq 'collector') { '/api/status' } else { '/status' }
$stopPath = if ($Role -eq 'collector') { '/api/stop' } else { '/stop' }
$runtimePath = Join-Path $data $file
if (-not (Test-Path $runtimePath)) { Write-Host "No $Role runtime file."; return }
$runtime = Get-Content $runtimePath -Raw | ConvertFrom-Json
if ($runtime.url -notmatch '^http://127\.0\.0\.1:\d+$') { throw 'Invalid local role URL.' }
$status = Invoke-RestMethod "$($runtime.url)$statusPath" -TimeoutSec 3
if ($status.instanceId -ne $runtime.instanceId) { throw 'Instance mismatch; refusing to stop another process.' }
$null = Invoke-RestMethod "$($runtime.url)$stopPath" -Method Post `
    -Headers @{ Authorization = "Bearer $($runtime.token)" } -TimeoutSec 3
for ($i = 0; $i -lt 60 -and (Test-Path $runtimePath); $i++) { Start-Sleep -Milliseconds 250 }
if (Test-Path $runtimePath) { throw "$Role did not finish stopping." }
Write-Host "$Role stopped. Copilot sessions were not changed."
