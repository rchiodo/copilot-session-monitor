# Stops the collector (self-observing this machine) and, as a safety net,
# any watcher still running from before self-observation existed. A no-op
# watcher stop is harmless if none is running.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Stop-Watcher.ps1')
& (Join-Path $PSScriptRoot 'Stop-Collector.ps1')
