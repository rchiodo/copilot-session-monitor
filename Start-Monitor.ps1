# The collector now observes this machine's own Copilot sessions in-process
# (self-observation), so a separate local watcher is no longer needed or
# started here. This script is kept only as a backward-compatible alias for
# Start-Collector.ps1. Use Start-Watcher.ps1 only to monitor a different
# (remote) machine.
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Start-Collector.ps1') -NoBrowser:$NoBrowser
