param([ValidateSet('collector','watcher')][string]$Role, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$data = if ($env:MONITOR_DATA_DIR) { $env:MONITOR_DATA_DIR } else { Join-Path $root '.local' }
$node = (Get-Command node -ErrorAction Stop).Source
if ([int]((& $node --version) -replace '^v(\d+).*$', '$1') -lt 24) { throw 'Node.js 24 or newer is required. No npm install is needed.' }
$runtimeFile = if ($Role -eq 'collector') { 'runtime.json' } else { 'watcher-runtime.json' }
$endpoint = if ($Role -eq 'collector') { '/api/status' } else { '/status' }
$runtimePath = Join-Path $data $runtimeFile
function Get-LiveRole {
    if (Test-Path $runtimePath) {
        $runtime = Get-Content $runtimePath -Raw | ConvertFrom-Json
        if ($runtime.url -notmatch '^http://127\.0\.0\.1:\d+$') { throw 'Invalid local role URL.' }
        try {
            $status = Invoke-RestMethod "$($runtime.url)$endpoint" -TimeoutSec 2
            if ($status.instanceId -eq $runtime.instanceId) { return $runtime }
        } catch { Write-Verbose 'Previous role is not responsive.' }
    }
    return $null
}
$runtime = Get-LiveRole
if (-not $runtime) {
    [void](New-Item -ItemType Directory -Path $data -Force)
    $entry = if ($Role -eq 'collector') { 'server.mjs' } else { 'watcher.mjs' }
    $process = Start-Process -FilePath $node -ArgumentList @('--disable-warning=ExperimentalWarning', "`"$(Join-Path $root "src\$entry")`"") `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $data "$Role.log") -RedirectStandardError (Join-Path $data "$Role-error.log")
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        $runtime = Get-LiveRole
        if ($runtime) { break }
        if ($process.HasExited) { throw "$Role exited. See .local\$Role-error.log." }
    }
    if (-not $runtime) { throw "$Role did not respond. See .local\$Role-error.log." }
}
for ($i = 0; $i -lt 60; $i++) {
    $status = Invoke-RestMethod "$($runtime.url)$endpoint" -TimeoutSec 3
    if ($status.healthy) { break }
    if ($Role -eq 'watcher' -and $status.paired -eq $false) { break }
    Start-Sleep -Milliseconds 500
}
$stopScript = if ($Role -eq 'collector') { 'Stop-Host.ps1' } else { 'Stop-Client.ps1' }
if ($Role -eq 'watcher' -and $status.paired -eq $false) {
    Write-Host "watcher running, not yet paired. Use the tray icon's 'Connect to host...' menu to pair with a host. Stop with .\$stopScript."
    return
}
if (-not $status.healthy) { throw "$Role is running but not healthy. Check .local\$Role-error.log and collector source coverage." }
Write-Host "$Role running. Stop with .\$stopScript."
if ($Role -eq 'collector') {
    Write-Host "Dashboard: $($runtime.url)"
    if (-not $NoBrowser) { Start-Process $runtime.url }
}
