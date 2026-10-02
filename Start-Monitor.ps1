param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$node = (Get-Command node -ErrorAction Stop).Source
$major = [int]((& $node --version) -replace '^v(\d+).*$', '$1')
if ($major -lt 24) { throw 'Node.js 24 or newer is required. No npm install is needed.' }
$runtimePath = Join-Path $PSScriptRoot '.local\runtime.json'
function Get-LiveMonitor {
    if (Test-Path $runtimePath) {
        $runtime = Get-Content $runtimePath -Raw | ConvertFrom-Json
        if ($runtime.url -notmatch '^http://127\.0\.0\.1:\d+$') { throw 'Invalid monitor runtime URL.' }
        try {
            $status = Invoke-RestMethod "$($runtime.url)/api/status" -TimeoutSec 2
            if ($status.instanceId -eq $runtime.instanceId) { return $runtime }
        } catch {
            Write-Verbose 'Previous monitor is not responsive; starting a new instance.'
        }
    }
    return $null
}
$runtime = Get-LiveMonitor
if (-not $runtime) {
    $dataDir = Join-Path $PSScriptRoot '.local'
    [void](New-Item -ItemType Directory -Path $dataDir -Force)
    $scriptPath = Join-Path $PSScriptRoot 'src\server.mjs'
    $process = Start-Process -FilePath $node -ArgumentList @('--disable-warning=ExperimentalWarning', "`"$scriptPath`"") `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDir 'monitor.log') -RedirectStandardError (Join-Path $dataDir 'monitor-error.log')
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        $runtime = Get-LiveMonitor
        if ($runtime) { break }
        if ($process.HasExited) { throw 'Monitor exited. See .local\monitor-error.log.' }
    }
    if (-not $runtime) { throw 'Monitor did not become responsive. See .local\monitor-error.log.' }
}
Write-Host "Monitor running: $($runtime.url)"
for ($i = 0; $i -lt 60; $i++) {
    $status = Invoke-RestMethod "$($runtime.url)/api/status" -TimeoutSec 3
    if ($status.healthy) { break }
    if ($status.notification.state -eq 'failed') { throw $status.notification.message }
    Start-Sleep -Milliseconds 500
}
if (-not $status.healthy) { throw "Monitor is responsive but observation is unavailable: $($status.issues -join '; '). See the local UI for details." }
Write-Host 'This machine only. Stop with .\Stop-Monitor.ps1 or the tray menu.'
if (-not $NoBrowser) { Start-Process $runtime.url }
