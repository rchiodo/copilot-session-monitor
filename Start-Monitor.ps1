param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'src\configuration.mjs') local
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the local watcher/collector pairing.' }
& (Join-Path $PSScriptRoot 'Start-Collector.ps1') -NoBrowser:$NoBrowser
& (Join-Path $PSScriptRoot 'Start-Watcher.ps1')
