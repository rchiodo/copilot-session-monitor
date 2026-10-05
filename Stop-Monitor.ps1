$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Stop-Watcher.ps1')
& (Join-Path $PSScriptRoot 'Stop-Collector.ps1')
